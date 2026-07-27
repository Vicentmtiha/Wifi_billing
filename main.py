import os
import random
import string
import logging
import threading
import time
import re
from datetime import datetime, timedelta
from typing import Optional
from functools import wraps

import requests
from librouteros import connect
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

from database import engine, Base, SessionLocal
from models import User, Voucher, Package

# ================= SETUP =================
load_dotenv()
Base.metadata.create_all(bind=engine)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "wifi_billing_secret_key_123"))
templates = Jinja2Templates(directory="templates")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ================= MIKROTIK CONFIG =================
class MikrotikConfig:
    HOST = os.getenv("MIKROTIK_HOST", "192.168.122.209")
    PORT = int(os.getenv("MIKROTIK_PORT", 8728))
    USER = os.getenv("MIKROTIK_USER", "admin")
    PASS = os.getenv("MIKROTIK_PASS", "Venom@123")
    ENABLED = os.getenv("MIKROTIK_ENABLED", "true").lower() == "true"


# ================= AZAMPAY =================
def get_access_token():
    url = "https://authenticator-sandbox.azampay.co.tz/AppRegistration/GenerateToken"
    payload = {
        "appName": os.getenv("AZAMPAY_APP_NAME"),
        "clientId": os.getenv("AZAMPAY_CLIENT_ID"),
        "clientSecret": os.getenv("AZAMPAY_CLIENT_SECRET")
    }
    try:
        response = requests.post(url, json=payload)
        data = response.json()
        if response.status_code == 200 and data.get("success"):
            return data["data"]["accessToken"]
        logger.error(f"AzamPay token error: {data}")
        return None
    except Exception as e:
        logger.error(f"AzamPay token exception: {e}")
        return None


# ================= VOUCHER SERVICE =================
class VoucherService:

    @staticmethod
    def generate_code():
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

    @staticmethod
    def convert_to_mikrotik_time(time_str: str) -> str:
        time_str = time_str.lower().strip()
        match = re.search(r'\d+', time_str)
        num = int(match.group()) if match else 1
        if 'day' in time_str:
            return f"{num * 24:02}:00:00"
        elif 'h' in time_str:
            return f"{num:02}:00:00"
        elif 'm' in time_str:
            return f"00:{num:02}:00"
        return "01:00:00"

    @staticmethod
    def create_voucher(price: int, uptime: str, data_limit: str, profile_name: str = "default") -> Optional[str]:
        db = SessionLocal()
        try:
            code = VoucherService.generate_code()
            existing = db.query(Voucher).filter(Voucher.code == code).first()
            if existing:
                return VoucherService.create_voucher(price, uptime, data_limit, profile_name)

            expiry_date = datetime.now() + timedelta(days=30)
            voucher = Voucher(
                code=code,
                profile=profile_name,
                price=price,
                uptime=uptime,
                data_limit=data_limit,
                status="unused",
                created_at=datetime.now(),
                expires_at=expiry_date,
                used_by=""
            )
            db.add(voucher)
            db.commit()
            logger.info(f"Voucher created: {code}")

            # Sync to Mikrotik - only once here, NOT again during hotspot login
            if MikrotikConfig.ENABLED:
                threading.Thread(
                    target=MikrotikService.sync_voucher_to_mikrotik,
                    args=(code, uptime),
                    daemon=True
                ).start()

            return code
        except Exception as e:
            logger.error(f"Error creating voucher: {e}")
            db.rollback()
            return None
        finally:
            db.close()

    @staticmethod
    def get_voucher(voucher_id: int) -> Optional[Voucher]:
        db = SessionLocal()
        try:
            return db.query(Voucher).filter(Voucher.id == voucher_id).first()
        finally:
            db.close()

    @staticmethod
    def get_voucher_by_code(code: str) -> Optional[Voucher]:
        db = SessionLocal()
        try:
            return db.query(Voucher).filter(Voucher.code == code).first()
        finally:
            db.close()

    @staticmethod
    def mark_used(voucher_id: int, client_mac: str = "") -> bool:
        db = SessionLocal()
        try:
            voucher = db.query(Voucher).filter(Voucher.id == voucher_id).first()
            if not voucher:
                return False
            voucher.status = "used"
            voucher.used_by = client_mac
            voucher.used_at = datetime.now()
            db.commit()
            logger.info(f"Voucher {voucher.code} marked as used")
            return True
        except Exception as e:
            logger.error(f"Error marking voucher as used: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    @staticmethod
    def mark_expired(voucher_id: int) -> bool:
        db = SessionLocal()
        try:
            voucher = db.query(Voucher).filter(Voucher.id == voucher_id).first()
            if not voucher:
                return False
            voucher.status = "expired"
            db.commit()
            if MikrotikConfig.ENABLED:
                threading.Thread(
                    target=MikrotikService.remove_user_from_mikrotik,
                    args=(voucher.code,),
                    daemon=True
                ).start()
            logger.info(f"Voucher {voucher.code} marked as expired")
            return True
        except Exception as e:
            logger.error(f"Error marking voucher as expired: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    @staticmethod
    def delete_voucher(voucher_id: int) -> bool:
        db = SessionLocal()
        try:
            voucher = db.query(Voucher).filter(Voucher.id == voucher_id).first()
            if not voucher:
                return False
            code = voucher.code
            db.delete(voucher)
            db.commit()
            if MikrotikConfig.ENABLED:
                threading.Thread(
                    target=MikrotikService.remove_user_from_mikrotik,
                    args=(code,),
                    daemon=True
                ).start()
            logger.info(f"Voucher {code} deleted")
            return True
        except Exception as e:
            logger.error(f"Error deleting voucher: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    @staticmethod
    def get_all_vouchers():
        db = SessionLocal()
        try:
            return db.query(Voucher).all()
        finally:
            db.close()

    @staticmethod
    def check_and_expire_vouchers() -> int:
        db = SessionLocal()
        expired_count = 0
        try:
            now = datetime.now()
            vouchers = db.query(Voucher).filter(
                Voucher.status == "unused",
                Voucher.expires_at < now
            ).all()
            for v in vouchers:
                v.status = "expired"
                expired_count += 1
                if MikrotikConfig.ENABLED:
                    threading.Thread(
                        target=MikrotikService.remove_user_from_mikrotik,
                        args=(v.code,),
                        daemon=True
                    ).start()
            if expired_count > 0:
                db.commit()
                logger.info(f"Expired {expired_count} vouchers")
        except Exception as e:
            logger.error(f"Error checking expiry: {e}")
            db.rollback()
        finally:
            db.close()
        return expired_count


# ================= MIKROTIK SERVICE =================
class MikrotikService:

    @staticmethod
    def get_api():
        try:
            return connect(
                host=MikrotikConfig.HOST,
                username=MikrotikConfig.USER,
                password=MikrotikConfig.PASS,
                port=MikrotikConfig.PORT
            )
        except Exception as e:
            logger.error(f"Mikrotik connection error: {e}")
            return None

    @staticmethod
    def check_connection():
        api = MikrotikService.get_api()
        if api:
            api.close()
            return True
        return False

    @staticmethod
    def user_exists_on_mikrotik(api, voucher_code: str) -> bool:
        """Angalia kama user tayari yupo Mikrotik"""
        try:
            hotspot_users = api.path("ip", "hotspot", "user")
            users = list(hotspot_users)
            return any(u.get("name") == voucher_code for u in users)
        except Exception as e:
            logger.error(f"Error checking user existence: {e}")
            return False

    @staticmethod
    def sync_voucher_to_mikrotik(voucher_code: str, uptime: str):
        """Ongeza voucher Mikrotik - angalia kwanza kama haipo"""
        try:
            api = MikrotikService.get_api()
            if not api:
                return False

            # Kama tayari yupo, usimwongeze tena (hii ndiyo fix ya "already have user")
            if MikrotikService.user_exists_on_mikrotik(api, voucher_code):
                logger.info(f"User {voucher_code} tayari yupo Mikrotik, kuruka sync")
                api.close()
                return True

            mikrotik_uptime = VoucherService.convert_to_mikrotik_time(uptime)
            hotspot_users = api.path("ip", "hotspot", "user")
            hotspot_users.add(
                name=voucher_code,
                password=voucher_code,
                **{"limit-uptime": mikrotik_uptime, "comment": "Auto Voucher"}
            )
            api.close()
            logger.info(f"Voucher {voucher_code} synced to Mikrotik")
            return True
        except Exception as e:
            logger.error(f"Error syncing {voucher_code}: {e}")
            return False

    @staticmethod
    def remove_user_from_mikrotik(voucher_code: str):
        try:
            api = MikrotikService.get_api()
            if not api:
                return False
            hotspot_users = api.path("ip", "hotspot", "user")
            users = list(hotspot_users)
            target_user = next((u for u in users if u.get("name") == voucher_code), None)
            if target_user:
                hotspot_users.remove(target_user[".id"])
                logger.info(f"User {voucher_code} removed from Mikrotik")
            api.close()
            return True
        except Exception as e:
            logger.error(f"Error removing {voucher_code}: {e}")
            return False

    @staticmethod
    def lock_voucher_to_mac(voucher_code: str, mac: str):
        """
        Lock voucher kwa MAC address.
        FIX: librouteros inahitaji .id kama string na kutumia update() vizuri.
        """
        try:
            api = MikrotikService.get_api()
            if not api:
                return False

            hotspot_users = api.path("ip", "hotspot", "user")
            users = list(hotspot_users)
            target_user = next((u for u in users if u.get("name") == voucher_code), None)

            if target_user:
                user_id = target_user[".id"]
                # FIX: Tumia syntax sahihi ya librouteros kwa update
                list(hotspot_users.update(**{".id": user_id, "mac-address": mac}))
                logger.info(f"Voucher {voucher_code} locked to MAC {mac}")

            api.close()
            return True
        except Exception as e:
            logger.error(f"Error locking MAC for {voucher_code}: {e}")
            return False


# ================= USER SERVICE =================
class UserService:

    @staticmethod
    def create_user(username: str, password: str, role: str = "Staff", status: str = "Active") -> bool:
        db = SessionLocal()
        try:
            existing = db.query(User).filter(User.username == username).first()
            if existing:
                logger.warning(f"User {username} already exists")
                return False
            user = User(username=username, password=password, role=role, status=status)
            db.add(user)
            db.commit()
            logger.info(f"User {username} created")
            return True
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    @staticmethod
    def get_user(user_id: int) -> Optional[User]:
        db = SessionLocal()
        try:
            return db.query(User).filter(User.id == user_id).first()
        finally:
            db.close()

    @staticmethod
    def authenticate(username: str, password: str) -> Optional[User]:
        db = SessionLocal()
        try:
            return db.query(User).filter(
                User.username == username,
                User.password == password
            ).first()
        finally:
            db.close()

    @staticmethod
    def update_user(user_id: int, username: str, password: str, role: str, status: str) -> bool:
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return False
            user.username = username
            user.password = password
            user.role = role
            user.status = status
            db.commit()
            logger.info(f"User {user_id} updated")
            return True
        except Exception as e:
            logger.error(f"Error updating user: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    @staticmethod
    def delete_user(user_id: int) -> bool:
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return False
            db.delete(user)
            db.commit()
            logger.info(f"User {user_id} deleted")
            return True
        except Exception as e:
            logger.error(f"Error deleting user: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    @staticmethod
    def get_all_users():
        db = SessionLocal()
        try:
            return db.query(User).all()
        finally:
            db.close()


# ================= PACKAGE SERVICE =================
class PackageService:

    @staticmethod
    def create_package(name: str, price: int) -> bool:
        db = SessionLocal()
        try:
            package = Package(name=name, price=price)
            db.add(package)
            db.commit()
            logger.info(f"Package {name} created")
            return True
        except Exception as e:
            logger.error(f"Error creating package: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    @staticmethod
    def get_package(pkg_id: int) -> Optional[Package]:
        db = SessionLocal()
        try:
            return db.query(Package).filter(Package.id == pkg_id).first()
        finally:
            db.close()

    @staticmethod
    def update_package(pkg_id: int, name: str, price: int) -> bool:
        db = SessionLocal()
        try:
            pkg = db.query(Package).filter(Package.id == pkg_id).first()
            if not pkg:
                return False
            pkg.name = name
            pkg.price = price
            db.commit()
            logger.info(f"Package {pkg_id} updated")
            return True
        except Exception as e:
            logger.error(f"Error updating package: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    @staticmethod
    def delete_package(pkg_id: int) -> bool:
        db = SessionLocal()
        try:
            pkg = db.query(Package).filter(Package.id == pkg_id).first()
            if not pkg:
                return False
            db.delete(pkg)
            db.commit()
            logger.info(f"Package {pkg_id} deleted")
            return True
        except Exception as e:
            logger.error(f"Error deleting package: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    @staticmethod
    def get_all_packages():
        db = SessionLocal()
        try:
            return db.query(Package).all()
        finally:
            db.close()


# ================= HELPERS =================
def login_required(f):
    @wraps(f)
    def decorated_function(request: Request, *args, **kwargs):
        if not request.session.get("logged_in"):
            return RedirectResponse("/login", status_code=303)
        return f(request, *args, **kwargs)
    return decorated_function

def get_current_user(request: Request) -> str:
    return request.session.get("user", "Guest")


# ================= BACKGROUND WORKER =================
def expiry_worker():
    while True:
        try:
            VoucherService.check_and_expire_vouchers()
        except Exception as e:
            logger.error(f"Error in expiry worker: {e}")
        time.sleep(300)

threading.Thread(target=expiry_worker, daemon=True).start()


# ================= AUTH ROUTES =================
@app.get("/")
@app.get("/login")
def login_page(request: Request):
    if request.session.get("logged_in"):
        return RedirectResponse("/dashboard", status_code=303)
    error = request.session.pop("login_error", None)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"request": request, "error": error}
    )

@app.post("/login")
def handle_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == "admin" and password == "admin123":
        request.session.update({"logged_in": True, "user": "SuperAdmin", "role": "admin"})
        logger.info("SuperAdmin logged in")
        return RedirectResponse("/dashboard", status_code=303)

    user = UserService.authenticate(username, password)
    if user:
        request.session.update({"logged_in": True, "user": user.username, "role": user.role})
        logger.info(f"User {username} logged in")
        return RedirectResponse("/dashboard", status_code=303)

    logger.warning(f"Failed login attempt: {username}")
    request.session["login_error"] = "Taarifa si sahihi!"
    return RedirectResponse("/login", status_code=303)

@app.get("/logout")
def logout(request: Request):
    logger.info(f"User {get_current_user(request)} logged out")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ================= AZAMPAY ROUTE =================
@app.post("/lipa")
async def lipa_internet(amount: int, phone: str):
    token = get_access_token()
    if not token:
        return {"status": "error", "message": "Imeshindwa kupata Token"}

    url = "https://checkout.azampay.co.tz/api/v1/MnoCheckout/POST"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "amount": amount,
        "phoneNumber": phone,
        "externalId": f"order_{random.randint(1000, 9999)}",
        "callbackURL": "https://yako-ngrok-url.com/payment-callback"
    }
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code != 200:
        logger.error(f"AzamPay checkout error: {response.text}")
        return {"status": "error", "message": response.text}
    return response.json()


# ================= HOTSPOT ROUTES =================
@app.get("/hotspot-login")
def get_hotspot_login(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="hotspot.html",
        context={"request": request}
    )

@app.post("/hotspot-login")
def hotspot_login(voucher: str = Form(...), mac: str = Form(...)):
    voucher_obj = VoucherService.get_voucher_by_code(voucher.strip())

    if not voucher_obj:
        return {"status": "error", "message": "Voucher haipo"}

    if voucher_obj.status != "unused":
        return {"status": "error", "message": "Voucher tayari imetumika"}

    if voucher_obj.expires_at < datetime.now():
        return {"status": "error", "message": "Voucher muda wake umekwisha"}

    success = VoucherService.mark_used(voucher_obj.id, mac)
    if not success:
        return {"status": "error", "message": "Imeshindwa kusasisha voucher kwenye database"}

    # MUHIMU: sync_voucher_to_mikrotik tayari ilifanyika wakati wa create_voucher.
    # Hapa tunafanya lock tu (MAC binding) - SIFANYI sync tena ili kuepuka "already have user".
    if MikrotikConfig.ENABLED:
        threading.Thread(
            target=MikrotikService.lock_voucher_to_mac,
            args=(voucher_obj.code, mac),
            daemon=True
        ).start()

    return RedirectResponse("https://www.google.com", status_code=303)


# ================= DASHBOARD =================
@app.get("/dashboard")
@login_required
def dashboard(request: Request):
    all_vouchers = VoucherService.get_all_vouchers()
    expired_vouchers = [v for v in all_vouchers if v.status == 'expired']
    context = {
        "request": request,
        "total": len(all_vouchers),
        "used": len([v for v in all_vouchers if v.status == 'used']),
        "unused": len([v for v in all_vouchers if v.status == 'unused']),
        "expired": len(expired_vouchers),
        "expired_count": len(expired_vouchers),
        "router_status": "Connected" if MikrotikService.check_connection() else "Disconnected"
    }
    return templates.TemplateResponse(request=request, name="dashboard.html", context=context)


# ================= USERS MANAGEMENT =================
@app.get("/users")
@login_required
def users(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={"request": request, "users": UserService.get_all_users(), "user": get_current_user(request)}
    )

@app.post("/add-user")
@login_required
def add_user(request: Request, username: str = Form(...), password: str = Form(...)):
    UserService.create_user(username, password)
    return RedirectResponse("/users", status_code=303)

@app.get("/edit-user/{user_id}")
@login_required
def edit_user_form(request: Request, user_id: int):
    user = UserService.get_user(user_id)
    if not user:
        return RedirectResponse("/users", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="edit_user.html",
        context={"request": request, "account": user}
    )

@app.post("/edit-user/{user_id}")
@login_required
def update_user(
    request: Request, user_id: int,
    username: str = Form(...), password: str = Form(...),
    role: str = Form(...), status: str = Form(...)
):
    UserService.update_user(user_id, username, password, role, status)
    return RedirectResponse("/users", status_code=303)

@app.get("/delete-user/{user_id}")
@login_required
def delete_user(request: Request, user_id: int):
    UserService.delete_user(user_id)
    return RedirectResponse("/users", status_code=303)


# ================= PACKAGES MANAGEMENT =================
@app.get("/packages")
@login_required
def packages(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="packages.html",
        context={"request": request, "packages": PackageService.get_all_packages(), "user": get_current_user(request)}
    )

@app.post("/add-package")
@login_required
def add_package(request: Request, name: str = Form(...), price: int = Form(...)):
    PackageService.create_package(name, price)
    return RedirectResponse("/packages", status_code=303)

@app.get("/edit-package/{pkg_id}")
@login_required
def edit_package_form(request: Request, pkg_id: int):
    pkg = PackageService.get_package(pkg_id)
    if not pkg:
        return RedirectResponse("/packages", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="edit_package.html",
        context={"request": request, "package": pkg}
    )

@app.post("/edit-package/{pkg_id}")
@login_required
def update_package(request: Request, pkg_id: int, name: str = Form(...), price: int = Form(...)):
    PackageService.update_package(pkg_id, name, price)
    return RedirectResponse("/packages", status_code=303)

@app.get("/delete-package/{pkg_id}")
@login_required
def delete_package(request: Request, pkg_id: int):
    PackageService.delete_package(pkg_id)
    return RedirectResponse("/packages", status_code=303)


# ================= VOUCHERS MANAGEMENT =================
@app.get("/vouchers")
@login_required
def vouchers(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="vouchers.html",
        context={"request": request, "vouchers": VoucherService.get_all_vouchers(), "user": get_current_user(request)}
    )

@app.post("/generate-voucher")
@login_required
def generate_voucher(
    request: Request,
    price: int = Form(...),
    duration: str = Form(...),
    data_limit: str = Form(...)
):
    code = VoucherService.create_voucher(price, duration, data_limit)
    if not code:
        logger.error("Failed to generate voucher")
    return RedirectResponse("/vouchers", status_code=303)

@app.get("/activate-voucher/{voucher_id}")
@login_required
def activate_voucher(request: Request, voucher_id: int):
    VoucherService.mark_used(voucher_id)
    return RedirectResponse("/vouchers", status_code=303)

@app.get("/expire-voucher/{voucher_id}")
@login_required
def expire_voucher(request: Request, voucher_id: int):
    VoucherService.mark_expired(voucher_id)
    return RedirectResponse("/vouchers", status_code=303)

@app.get("/delete-voucher/{voucher_id}")
@login_required
def delete_voucher(request: Request, voucher_id: int):
    VoucherService.delete_voucher(voucher_id)
    return RedirectResponse("/vouchers", status_code=303)

@app.get("/print-vouchers")
@login_required
def print_vouchers(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="print_vouchers.html",
        context={"request": request, "vouchers": VoucherService.get_all_vouchers()}
    )


# ================= REPORTS =================
@app.get("/reports")
@login_required
def reports(request: Request):
    all_vouchers = VoucherService.get_all_vouchers()
    used_vouchers = [v for v in all_vouchers if v.status == "used"]
    context = {
        "request": request,
        "user": get_current_user(request),
        "vouchers": all_vouchers,
        "total_sales": sum(v.price for v in used_vouchers),
        "total_issued": len(all_vouchers),
        "total_used": len(used_vouchers),
        "total_unused": len([v for v in all_vouchers if v.status == "unused"]),
        "total_expired": len([v for v in all_vouchers if v.status == "expired"])
    }
    return templates.TemplateResponse(request=request, name="reports.html", context=context)


# ================= SETTINGS =================
@app.get("/settings")
@login_required
def settings(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "request": request,
            "user": get_current_user(request),
            "mikrotik_status": MikrotikService.check_connection(),
            "mikrotik_host": MikrotikConfig.HOST
        }
    )

@app.post("/save-settings")
@login_required
def save_settings(request: Request, system_name: str = Form(...), tax_rate: int = Form(...)):
    logger.info(f"Settings saved by {get_current_user(request)}")
    return RedirectResponse("/settings", status_code=303)


# ================= HEALTH CHECK =================
@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "mikrotik": MikrotikService.check_connection(),
        "database": "ok"
    }


# ================= RUN =================
if __name__ == "__main__":
    import uvicorn
    logger.info("Starting CORE-WISP WiFi Billing System")
    uvicorn.run(app, host="0.0.0.0", port=8002)
