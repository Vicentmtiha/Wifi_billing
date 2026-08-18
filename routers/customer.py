import os
import random
import string
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from bson import ObjectId
import routeros_api

logger = logging.getLogger("app")

router = APIRouter(tags=["customer"])
templates = Jinja2Templates(directory="templates")

# ==================== HELPER FUNCTIONS ====================
def _to_object_id(id_str: str):
    """Kubadili String ID kwenda ObjectId ya MongoDB kwa usalama"""
    try:
        return ObjectId(id_str)
    except Exception:
        return None

def _get_context():
    """Kupata Database na Mikrotik Config kutoka main.py"""
    from main import db, mikrotik_config
    return db, mikrotik_config

def _get_current_user(request: Request):
    return request.session.get("user")

def _username(request: Request) -> str:
    user = _get_current_user(request)
    if isinstance(user, dict):
        return user.get("username", "Admin")
    return user or "Admin"

def _customer_guard(request: Request):
    user = _get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return None

def _sync_voucher_to_mikrotik(router_doc: dict, code: str, profile_name: str = "default"):
    """Inatuma voucher kule kwenye Mikrotik Router via RouterOS API"""
    try:
        host = router_doc.get("host")
        username = router_doc.get("username", "admin")
        password = router_doc.get("password", "")
        port = int(router_doc.get("port", 8728))

        connection = routeros_api.RouterOsApiPool(
            host, 
            username=username, 
            password=password, 
            port=port,
            plaintext_login=True
        )
        api = connection.get_api()
        hotspot_user = api.get_resource('/ip/hotspot/user')

        # Kuongeza User kwenye Hotspot
        hotspot_user.add(
            name=code,
            password=code,
            profile=profile_name,
            comment=f"Generated via CORE-WISP at {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        connection.disconnect()
        return True
    except Exception as e:
        logger.error(f"Imeshindikana kusukuma voucher {code} kwenda Mikrotik ({router_doc.get('name')}): {e}")
        return False


# ==================== DASHBOARD ROUTE ====================
@router.get("/dashboard", response_class=HTMLResponse)
def customer_dashboard(request: Request):
    guard = _customer_guard(request)
    if guard:
        return guard

    db, _ = _get_context()
    username = _username(request)

    user_info = db.users.find_one({"username": username}) or {}
    user_email = user_info.get("email", "mteja@gmail.com")

    vouchers = list(db.vouchers.find({"owner_username": username}).sort("created_at", -1))
    routers = list(db.routers.find({"owner_username": username}).sort("created_at", -1))
    packages = list(db.packages.find({"owner_username": username}).sort("created_at", -1))
    active_clients_list = list(db.active_clients.find({"owner_username": username}))
    sales_reports = list(db.sales.find({"owner_username": username}).sort("created_at", -1))

    total_vouchers = len(vouchers)
    active_vouchers = sum(1 for v in vouchers if v.get("status") == "active")
    unused_vouchers = sum(1 for v in vouchers if v.get("status") == "unused")
    used_vouchers = sum(1 for v in vouchers if v.get("status") == "used")

    total_revenue = sum(sale.get("price", 0) for sale in sales_reports)
    avg_price = (total_revenue / used_vouchers) if used_vouchers > 0 else 0

    return templates.TemplateResponse(
        request=request,
        name="customer_dashboard.html",
        context={
            "user": username,
            "user_email": user_email,
            "account_status": user_info.get("status", "Active / Verified"),
            "vouchers": vouchers,
            "routers": routers,
            "packages": packages,
            "active_clients_list": active_clients_list,
            "sales_reports": sales_reports,
            "total_vouchers": total_vouchers,
            "active_vouchers": active_vouchers,
            "unused_vouchers": unused_vouchers,
            "used_vouchers": used_vouchers,
            "total_routers": len(routers),
            "total_packages": len(packages),
            "active_clients": len(active_clients_list),
            "total_revenue": total_revenue,
            "avg_voucher_price": int(avg_price),
            "live_rx_rate": "12.45",
            "live_tx_rate": "3.80",
            "connected_router_name": routers[0].get("name") if routers else "No Router Connected"
        }
    )


# ==================== VOUCHERS ROUTES ====================
@router.post("/vouchers/generate")
def generate_vouchers(
    request: Request,
    price: float = Form(1000.0),
    validity: str = Form("24 Hours"),
    data_limit: str = Form("1GB"),
    quantity: int = Form(10),
    prefix: str = Form("VC-"),
    code_length: int = Form(6)
):
    guard = _customer_guard(request)
    if guard: return guard

    db, _ = _get_context()
    username = _username(request)

    # Kupata router ya mteja ili kuisukuma huko
    active_router = db.routers.find_one({"owner_username": username, "status": "online"}) or db.routers.find_one({"owner_username": username})

    new_vouchers = []
    for _ in range(quantity):
        random_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=code_length))
        clean_prefix = prefix.strip().upper() if prefix else ""
        code = f"{clean_prefix}{random_code}" if clean_prefix else random_code

        synced = False
        if active_router:
            synced = _sync_voucher_to_mikrotik(active_router, code, profile_name="default")

        new_vouchers.append({
            "code": code,
            "package_name": f"Batch {validity}",
            "price": int(price),
            "validity": validity,
            "data_limit": data_limit,
            "status": "unused",
            "synced_to_router": synced,
            "owner_username": username,
            "created_at": datetime.utcnow()
        })

    if new_vouchers:
        db.vouchers.insert_many(new_vouchers)

    return RedirectResponse("/customer/dashboard#vouchers", status_code=303)


@router.post("/vouchers/edit")
def edit_voucher(
    request: Request,
    voucher_id: str = Form(...),
    code: str = Form(...),
    price: float = Form(...),
    status: str = Form(...),
    validity: str = Form(...),
    data_limit: str = Form(...)
):
    guard = _customer_guard(request)
    if guard: return guard

    db, _ = _get_context()
    username = _username(request)
    obj_id = _to_object_id(voucher_id)

    if obj_id:
        db.vouchers.update_one(
            {"_id": obj_id, "owner_username": username},
            {"$set": {
                "code": code.strip(),
                "price": int(price),
                "status": status,
                "validity": validity,
                "data_limit": data_limit,
                "updated_at": datetime.utcnow()
            }}
        )

    return RedirectResponse("/customer/dashboard#vouchers", status_code=303)


@router.post("/vouchers/delete")
def delete_voucher(request: Request, item_id: str = Form(...)):
    guard = _customer_guard(request)
    if guard: return guard

    db, _ = _get_context()
    username = _username(request)
    obj_id = _to_object_id(item_id)

    if obj_id:
        # Pata taarifa za voucher kabla ya kuifuta database
        voucher = db.vouchers.find_one({"_id": obj_id, "owner_username": username})
        
        # Ukipenda pia kuifuta Mikrotik routeros direct:
        if voucher:
            active_router = db.routers.find_one({"owner_username": username, "status": "online"})
            if active_router:
                try:
                    conn = routeros_api.RouterOsApiPool(
                        active_router.get("host"),
                        username=active_router.get("username", "admin"),
                        password=active_router.get("password", ""),
                        port=int(active_router.get("port", 8728)),
                        plaintext_login=True
                    )
                    api = conn.get_api()
                    users = api.get_resource('/ip/hotspot/user').get(name=voucher.get("code"))
                    for u in users:
                        api.get_resource('/ip/hotspot/user').remove(id=u['id'])
                    conn.disconnect()
                except Exception as e:
                    logger.error(f"Kufuta Mikrotik imefail: {e}")

        db.vouchers.delete_one({"_id": obj_id, "owner_username": username})

    return RedirectResponse("/customer/dashboard#vouchers", status_code=303)


# ==================== ROUTERS ROUTES ====================
@router.post("/routers/add")
def add_router(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(8728),
    username_router: str = Form("admin", alias="username"),
    password: Optional[str] = Form("")
):
    guard = _customer_guard(request)
    if guard: return guard

    db, _ = _get_context()
    username = _username(request)

    db.routers.insert_one({
        "name": name,
        "host": host,
        "port": port,
        "username": username_router,
        "password": password,
        "status": "online",
        "owner_username": username,
        "created_at": datetime.utcnow()
    })
    return RedirectResponse("/customer/dashboard#routers", status_code=303)


@router.post("/routers/edit")
def edit_router(
    request: Request,
    router_id: str = Form(...),
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(...),
    username_router: str = Form(..., alias="username"),
    password: Optional[str] = Form(None)
):
    guard = _customer_guard(request)
    if guard: return guard

    db, _ = _get_context()
    username = _username(request)
    obj_id = _to_object_id(router_id)

    if obj_id:
        update_data = {
            "name": name,
            "host": host,
            "port": port,
            "username": username_router,
            "updated_at": datetime.utcnow()
        }
        if password:
            update_data["password"] = password

        db.routers.update_one(
            {"_id": obj_id, "owner_username": username},
            {"$set": update_data}
        )
    return RedirectResponse("/customer/dashboard#routers", status_code=303)


@router.post("/routers/delete")
def delete_router(request: Request, item_id: str = Form(...)):
    guard = _customer_guard(request)
    if guard: return guard

    db, _ = _get_context()
    username = _username(request)
    obj_id = _to_object_id(item_id)

    if obj_id:
        db.routers.delete_one({"_id": obj_id, "owner_username": username})

    return RedirectResponse("/customer/dashboard#routers", status_code=303)


# ==================== PACKAGES ROUTES ====================
@router.post("/packages/add")
def add_package(
    request: Request,
    name: str = Form(...),
    price: float = Form(...),
    validity: str = Form(...),
    data_limit: str = Form(...),
    speed: str = Form("5M/10M")
):
    guard = _customer_guard(request)
    if guard: return guard

    db, _ = _get_context()
    username = _username(request)

    db.packages.insert_one({
        "name": name,
        "price": int(price),
        "validity": validity,
        "data_limit": data_limit,
        "speed": speed,
        "owner_username": username,
        "created_at": datetime.utcnow()
    })
    return RedirectResponse("/customer/dashboard#packages", status_code=303)


@router.post("/packages/edit")
def edit_package(
    request: Request,
    package_id: str = Form(...),
    name: str = Form(...),
    price: float = Form(...),
    validity: str = Form(...),
    data_limit: str = Form(...),
    speed: str = Form(...)
):
    guard = _customer_guard(request)
    if guard: return guard

    db, _ = _get_context()
    username = _username(request)
    obj_id = _to_object_id(package_id)

    if obj_id:
        db.packages.update_one(
            {"_id": obj_id, "owner_username": username},
            {"$set": {
                "name": name,
                "price": int(price),
                "validity": validity,
                "data_limit": data_limit,
                "speed": speed,
                "updated_at": datetime.utcnow()
            }}
        )
    return RedirectResponse("/customer/dashboard#packages", status_code=303)


@router.post("/packages/delete")
def delete_package(request: Request, item_id: str = Form(...)):
    guard = _customer_guard(request)
    if guard: return guard

    db, _ = _get_context()
    username = _username(request)
    obj_id = _to_object_id(item_id)

    if obj_id:
        db.packages.delete_one({"_id": obj_id, "owner_username": username})

    return RedirectResponse("/customer/dashboard#packages", status_code=303)


# ==================== ACTIVE CLIENTS / USERS ROUTES ====================
@router.post("/clients/disconnect")
def disconnect_client(request: Request, client_id: str = Form(...)):
    """Mbinu ya kumtoa / kumdisconnect active client kwenye dashboard"""
    guard = _customer_guard(request)
    if guard: return guard

    db, _ = _get_context()
    username = _username(request)
    obj_id = _to_object_id(client_id)

    if obj_id:
        db.active_clients.delete_one({"_id": obj_id, "owner_username": username})

    return RedirectResponse("/customer/dashboard#clients", status_code=303)


# ==================== LIVE TRAFFIC API ====================
@router.get("/traffic/live")
def get_live_traffic(request: Request):
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    return JSONResponse(content={
        "status": "success",
        "rx_rate": f"{round(random.uniform(5.0, 25.0), 2):.2f}",
        "tx_rate": f"{round(random.uniform(1.0, 10.0), 2):.2f}"
    })
