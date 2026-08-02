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
from inspect import iscoroutinefunction as asyncio_iscoroutinefunction

import requests
from librouteros import connect
from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
from pymongo import MongoClient
from urllib.parse import quote_plus
from bson import ObjectId

# ================= SETUP =================
load_dotenv()

# MongoDB Connection
username = quote_plus("vicentmtiha4_db_user")
password = quote_plus("sKJFIrbFy4RvjUpZ")
MONGO_URL = f"mongodb+srv://{username}:{password}@cluster0.jqljfd3.mongodb.net/?retryWrites=true&w=majority"

client = MongoClient(MONGO_URL)
db = client["core_wisp_db"]

app = FastAPI(title="CORE-WISP WiFi Billing System")
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


# ================= AZAMPAY CONFIG & HELPERS =================
AZAMPAY_BASE_URL = os.getenv("AZAMPAY_BASE_URL", "https://sandbox.azampay.co.tz")

def detect_provider(phone: str) -> str:
    """Inatambua mtandao wa simu nchini Tanzania kulingana na namba"""
    clean = phone.strip().replace("+", "")
    if clean.startswith("255"):
        clean = "0" + clean[3:]
    
    prefix = clean[:3]
    
    if prefix in ["074", "075", "076", "079"]:
        return "Mpesa"
    elif prefix in ["065", "067", "071"]:
        return "Tigo"
    elif prefix in ["068", "069", "078"]:
        return "Airtel"
    elif prefix in ["062", "061"]:
        return "Halopesa"
    elif prefix in ["073"]:
        return "Azampesa"
    
    return "Airtel"  # Fallback


def get_access_token():
    env_token = os.getenv("AZAMPAY_TOKEN", "").strip().strip('"').strip("'")
    if env_token:
        return env_token

    url = f"{AZAMPAY_BASE_URL}/app/outer/v1/token"
    payload = {
        "appName": os.getenv("AZAMPAY_APP_NAME"),
        "clientId": os.getenv("AZAMPAY_CLIENT_ID"),
        "clientSecret": os.getenv("AZAMPAY_CLIENT_SECRET")
    }
    
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data.get("data"), dict):
                return data["data"].get("accessToken")
            elif isinstance(data.get("data"), str):
                return data["data"]
            return data.get("accessToken")
        logger.error(f"AzamPay Token Error: {response.text}")
        return None
    except Exception as e:
        logger.error(f"AzamPay Token Exception: {e}")
        return None


@app.api_route("/lipa", methods=["GET", "POST"])
async def lipa_internet(amount: int = 1000, phone: str = "0712345678"):
    token = get_access_token()
    if not token:
        return {
            "status": "error", 
            "message": "Imeshindwa kupata Token. Hakikisha AZAMPAY credentials ziko sahihi kwenye .env"
        }

    url = f"{AZAMPAY_BASE_URL}/azampay/mno/checkout"
    
    clean_phone = phone.strip().replace("+", "")
    if clean_phone.startswith("0"):
        clean_phone = "255" + clean_phone[1:]

    provider = detect_provider(phone)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    payload = {
        "accountNumber": clean_phone,
        "amount": str(amount),
        "currency": "TZS",
        "externalId": f"order_{random.randint(100000, 999999)}",
        "provider": provider
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20)
        logger.info(f"Checkout Response Status: {response.status_code}")
        
        if response.status_code == 200:
            try:
                return response.json()
            except Exception:
                return {"status": "success", "raw": response.text}
        else:
            return {
                "status": "error",
                "http_code": response.status_code,
                "message": response.text
            }
            
    except requests.exceptions.ConnectionError:
        return {
            "status": "error",
            "message": "AzamPay server imefunga connection. Jaribu tena au kagua kama Sandbox ya AzamPay iko hewani."
        }
    except Exception as e:
        logger.error(f"Checkout Error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/lipa-page", response_class=HTMLResponse)
async def lipa_page(request: Request):
    return """
    <!DOCTYPE html>
    <html lang="sw">
    <head>
        <meta charset="UTF-8">
        <title>CORE-WISP - Lipa</title>
        <style>
            body { font-family: Arial; background: #f4f6f9; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
            .card { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); width: 350px; text-align: center; }
            input, select { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ccc; border-radius: 5px; box-sizing: border-box; }
            button { width: 100%; background: #28a745; color: white; border: none; padding: 10px; border-radius: 5px; font-weight: bold; cursor: pointer; }
        </style>
    </head>
    <body>
        <div class="card">
            <h2>Lipa Intaneti</h2>
            <form action="/lipa" method="GET">
                <input type="text" name="phone" placeholder="Namba ya Simu (Mf: 0712345678)" required>
                <select name="amount">
                    <option value="1000">TZS 1,000 - Siku 1</option>
                    <option value="5000">TZS 5,000 - Wiki 1</option>
                </select>
                <button type="submit">LIPA SASA</button>
            </form>
        </div>
    </body>
    </html>
    """

# ================= VOUCHER SERVICE (MongoDB) =================
class VoucherService:

    @staticmethod
    def generate_code(prefix: str = ""):
        code_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if prefix:
            return f"{prefix}-{code_part}"
        return code_part

    @staticmethod
    def convert_to_mikrotik_time(time_str: str) -> str:
        time_str = str(time_str).lower().strip()
        match = re.search(r'\d+', time_str)
        num = int(match.group()) if match else 1
        if 'day' in time_str or 'd' in time_str:
            return f"{num * 24:02}:00:00"
        elif 'h' in time_str:
            return f"{num:02}:00:00"
        elif 'm' in time_str:
            return f"00:{num:02}:00"
        return "01:00:00"

    @staticmethod
    def create_voucher(price: int, uptime: str, data_limit: str, profile_name: str = "default", prefix: str = "") -> Optional[str]:
        try:
            code = VoucherService.generate_code(prefix)
            existing = db.vouchers.find_one({"code": code})
            if existing:
                return VoucherService.create_voucher(price, uptime, data_limit, profile_name, prefix)

            expiry_date = datetime.now() + timedelta(days=30)
            voucher_doc = {
                "id": random.randint(10000, 99999),
                "code": code,
                "profile": profile_name,
                "price": price,
                "uptime": uptime,
                "data_limit": data_limit,
                "status": "unused",
                "created_at": datetime.now(),
                "expires_at": expiry_date,
                "used_by": ""
            }
            db.vouchers.insert_one(voucher_doc)
            logger.info(f"Voucher created: {code}")

            if MikrotikConfig.ENABLED:
                threading.Thread(
                    target=MikrotikService.sync_voucher_to_mikrotik,
                    args=(code, uptime),
                    daemon=True
                ).start()

            return code
        except Exception as e:
            logger.error(f"Error creating voucher: {e}")
            return None

    @staticmethod
    def get_voucher(voucher_id: int):
        try:
            return db.vouchers.find_one({"id": voucher_id})
        except Exception:
            return None

    @staticmethod
    def get_voucher_by_code(code: str):
        try:
            return db.vouchers.find_one({"code": code})
        except Exception:
            return None

    @staticmethod
    def mark_used(voucher_id: int, client_mac: str = "") -> bool:
        try:
            result = db.vouchers.update_one(
                {"id": voucher_id},
                {"$set": {"status": "used", "used_by": client_mac, "used_at": datetime.now()}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error marking voucher as used: {e}")
            return False

    @staticmethod
    def mark_expired(voucher_id: int) -> bool:
        try:
            v = db.vouchers.find_one({"id": voucher_id})
            if not v:
                return False
            db.vouchers.update_one({"id": voucher_id}, {"$set": {"status": "expired"}})
            if MikrotikConfig.ENABLED:
                threading.Thread(
                    target=MikrotikService.remove_user_from_mikrotik,
                    args=(v["code"],),
                    daemon=True
                ).start()
            return True
        except Exception as e:
            logger.error(f"Error marking voucher as expired: {e}")
            return False

    @staticmethod
    def delete_voucher(voucher_id: int) -> bool:
        try:
            v = db.vouchers.find_one({"id": voucher_id})
            if not v:
                return False
            code = v["code"]
            db.vouchers.delete_one({"id": voucher_id})
            if MikrotikConfig.ENABLED:
                threading.Thread(
                    target=MikrotikService.remove_user_from_mikrotik,
                    args=(code,),
                    daemon=True
                ).start()
            return True
        except Exception as e:
            logger.error(f"Error deleting voucher: {e}")
            return False

    @staticmethod
    def get_all_vouchers():
        try:
            return list(db.vouchers.find())
        except Exception:
            return []

    @staticmethod
    def check_and_expire_vouchers() -> int:
        expired_count = 0
        try:
            now = datetime.now()
            vouchers = list(db.vouchers.find({"status": "unused", "expires_at": {"$lt": now}}))
            for v in vouchers:
                db.vouchers.update_one({"_id": v["_id"]}, {"$set": {"status": "expired"}})
                expired_count += 1
                if MikrotikConfig.ENABLED:
                    threading.Thread(
                        target=MikrotikService.remove_user_from_mikrotik,
                        args=(v["code"],),
                        daemon=True
                    ).start()
        except Exception as e:
            logger.error(f"Error checking expiry: {e}")
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
            try:
                api.close()
            except Exception:
                pass
            return True
        return False

    @staticmethod
    def user_exists_on_mikrotik(api, voucher_code: str) -> bool:
        try:
            hotspot_users = api.path("ip", "hotspot", "user")
            users = list(hotspot_users)
            return any(u.get("name") == voucher_code for u in users)
        except Exception as e:
            logger.error(f"Error checking user existence: {e}")
            return False

    @staticmethod
    def sync_voucher_to_mikrotik(voucher_code: str, uptime: str):
        api = None
        try:
            api = MikrotikService.get_api()
            if not api:
                return False

            if MikrotikService.user_exists_on_mikrotik(api, voucher_code):
                return True

            mikrotik_uptime = VoucherService.convert_to_mikrotik_time(uptime)
            hotspot_users = api.path("ip", "hotspot", "user")
            hotspot_users.add(
                name=voucher_code,
                password=voucher_code,
                **{"limit-uptime": mikrotik_uptime, "comment": "Auto Voucher"}
            )
            return True
        except Exception as e:
            logger.error(f"Error syncing {voucher_code}: {e}")
            return False
        finally:
            if api:
                try:
                    api.close()
                except Exception:
                    pass

    @staticmethod
    def remove_user_from_mikrotik(voucher_code: str):
        api = None
        try:
            api = MikrotikService.get_api()
            if not api:
                return False
            hotspot_users = api.path("ip", "hotspot", "user")
            users = list(hotspot_users)
            target_user = next((u for u in users if u.get("name") == voucher_code), None)
            if target_user:
                hotspot_users.remove(target_user[".id"])
            return True
        except Exception as e:
            logger.error(f"Error removing {voucher_code}: {e}")
            return False
        finally:
            if api:
                try:
                    api.close()
                except Exception:
                    pass

    @staticmethod
    def lock_voucher_to_mac(voucher_code: str, mac: str):
        api = None
        try:
            api = MikrotikService.get_api()
            if not api:
                return False

            hotspot_users = api.path("ip", "hotspot", "user")
            users = list(hotspot_users)
            target_user = next((u for u in users if u.get("name") == voucher_code), None)

            if target_user:
                user_id = target_user[".id"]
                list(hotspot_users.update(**{".id": user_id, "mac-address": mac}))
            return True
        except Exception as e:
            logger.error(f"Error locking MAC for {voucher_code}: {e}")
            return False
        finally:
            if api:
                try:
                    api.close()
                except Exception:
                    pass

    @staticmethod
    def get_active_users():
        """Inavuta orodha ya wateja wote waliopo hewani muda huu kutoka MikroTik"""
        api = None
        try:
            api = MikrotikService.get_api()
            if not api:
                return []
            
            active_path = api.path("ip", "hotspot", "active")
            active_list = list(active_path)
            
            formatted_users = []
            for u in active_list:
                formatted_users.append({
                    "id": u.get(".id"),
                    "user": u.get("user", "Unknown"),
                    "address": u.get("address", "-"),
                    "mac": u.get("mac-address", "-"),
                    "uptime": u.get("uptime", "0s"),
                    "bytes_in": u.get("bytes-in", "0"),
                    "bytes_out": u.get("bytes-out", "0"),
                    "login_by": u.get("login-by", "-")
                })
            return formatted_users
        except Exception as e:
            logger.error(f"Error fetching active users from Mikrotik: {e}")
            return []
        finally:
            if api:
                try:
                    api.close()
                except Exception:
                    pass

    @staticmethod
    def disconnect_user(active_id: str) -> bool:
        """Inamtoa (Kick) mteja aliyepo hewani kwa kutumia Active ID yake"""
        api = None
        try:
            api = MikrotikService.get_api()
            if not api:
                return False
            
            active_path = api.path("ip", "hotspot", "active")
            active_path.remove(active_id)
            logger.info(f"User with active ID {active_id} disconnected from MikroTik.")
            return True
        except Exception as e:
            logger.error(f"Error disconnecting user {active_id}: {e}")
            return False
        finally:
            if api:
                try:
                    api.close()
                except Exception:
                    pass


# ================= USER SERVICE (MongoDB) =================
class UserService:

    @staticmethod
    def create_user(username: str, password: str, role: str = "Staff", status: str = "Active") -> bool:
        try:
            existing = db.users.find_one({"username": username})
            if existing:
                return False
            user_doc = {
                "id": random.randint(10000, 99999),
                "username": username,
                "password": password,
                "role": role,
                "status": status
            }
            db.users.insert_one(user_doc)
            return True
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            return False

    @staticmethod
    def get_user(user_id: int):
        try:
            return db.users.find_one({"id": user_id})
        except Exception:
            return None

    @staticmethod
    def authenticate(username: str, password: str):
        try:
            return db.users.find_one({"username": username, "password": password})
        except Exception:
            return None

    @staticmethod
    def update_user(user_id: int, username: str, password: str, role: str, status: str) -> bool:
        try:
            result = db.users.update_one(
                {"id": user_id},
                {"$set": {"username": username, "password": password, "role": role, "status": status}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating user: {e}")
            return False

    @staticmethod
    def delete_user(user_id: int) -> bool:
        try:
            result = db.users.delete_one({"id": user_id})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Error deleting user: {e}")
            return False

    @staticmethod
    def get_all_users():
        try:
            return list(db.users.find())
        except Exception:
            return []


# ================= PACKAGE SERVICE (MongoDB) =================
class PackageService:

    @staticmethod
    def create_package(name: str, price: int, validity: str = "1 Day", data_limit: str = "Unlimited") -> bool:
        try:
            pkg_doc = {
                "id": random.randint(10000, 99999),
                "name": name,
                "price": price,
                "validity": validity,
                "data_limit": data_limit
            }
            db.packages.insert_one(pkg_doc)
            return True
        except Exception as e:
            logger.error(f"Error creating package: {e}")
            return False

    @staticmethod
    def get_package(pkg_id: str):
        try:
            if str(pkg_id).isdigit():
                return db.packages.find_one({"id": int(pkg_id)})
            elif ObjectId.is_valid(pkg_id):
                return db.packages.find_one({"_id": ObjectId(pkg_id)})
            return db.packages.find_one({"id": pkg_id})
        except Exception as e:
            logger.error(f"Error fetching package {pkg_id}: {e}")
            return None

    @staticmethod
    def update_package(pkg_id: int, name: str, price: int) -> bool:
        try:
            result = db.packages.update_one(
                {"id": pkg_id},
                {"$set": {"name": name, "price": price}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating package: {e}")
            return False

    @staticmethod
    def delete_package(pkg_id: int) -> bool:
        try:
            result = db.packages.delete_one({"id": pkg_id})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Error deleting package: {e}")
            return False

    @staticmethod
    def get_all_packages():
        try:
            return list(db.packages.find())
        except Exception:
            return []


# ================= HELPERS & DECORATOR =================
def login_required(f):
    @wraps(f)
    async def decorated_function(request: Request, *args, **kwargs):
        if not request.session.get("logged_in"):
            return RedirectResponse("/login", status_code=303)
        return await f(request, *args, **kwargs) if asyncio_iscoroutinefunction(f) else f(request, *args, **kwargs)
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
    admin_user = os.getenv("ADMIN_USER", "admin")
    admin_pass = os.getenv("ADMIN_PASS", "admin123")

    if username == admin_user and password == admin_pass:
        request.session.update({"logged_in": True, "user": "SuperAdmin", "role": "admin"})
        logger.info("SuperAdmin logged in")
        return RedirectResponse("/dashboard", status_code=303)

    user = UserService.authenticate(username, password)
    if user:
        request.session.update({"logged_in": True, "user": user["username"], "role": user["role"]})
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


# ================= HOTSPOT ROUTES =================
@app.get("/hotspot", response_class=HTMLResponse)
@app.get("/hotspot-login", response_class=HTMLResponse)
def get_hotspot_page(request: Request, mac: str = ""):
    """Inaonyesha Ukurasa wa Hotspot Landing Page (hotspot.html)"""
    return templates.TemplateResponse(
        request=request,
        name="hotspot.html",
        context={"request": request, "mac": mac}
    )

@app.post("/hotspot-login")
def hotspot_login(voucher: str = Form(...), mac: str = Form("")):
    """Inashughulikia uwekaji wa vocha kutoka ukurasa wa Hotspot"""
    voucher_obj = VoucherService.get_voucher_by_code(voucher.strip().upper())

    if not voucher_obj:
        return {"status": "error", "message": "Voucher haipo au ni makosa!"}

    if voucher_obj["status"] != "unused":
        return {"status": "error", "message": "Voucher tayari imetumika!"}

    if voucher_obj["expires_at"] < datetime.now():
        return {"status": "error", "message": "Voucher muda wake umekwisha!"}

    success = VoucherService.mark_used(voucher_obj["id"], mac)
    if not success:
        return {"status": "error", "message": "Imeshindwa kusasisha voucher kwenye database"}

    if MikrotikConfig.ENABLED:
        threading.Thread(
            target=MikrotikService.lock_voucher_to_mac,
            args=(voucher_obj["code"], mac),
            daemon=True
        ).start()

    return RedirectResponse("https://www.google.com", status_code=303)


# ================= ACTIVE HOTSPOT USERS =================
@app.get("/active-users")
@login_required
def active_users(request: Request):
    """Inaonyesha ukurasa wa wateja waliopo hewani"""
    active_list = MikrotikService.get_active_users()
    return templates.TemplateResponse(
        request=request,
        name="active_users.html",
        context={
            "request": request,
            "active_users": active_list,
            "total_active": len(active_list),
            "user": get_current_user(request)
        }
    )

@app.get("/kick-user/{active_id:path}")
@login_required
def kick_user(request: Request, active_id: str):
    """Inam-disconnect mteja aliyepo hewani"""
    MikrotikService.disconnect_user(active_id)
    return RedirectResponse("/active-users", status_code=303)


# ================= DASHBOARD =================
@app.get("/dashboard")
@login_required
def dashboard(request: Request):
    all_vouchers = VoucherService.get_all_vouchers()
    expired_vouchers = [v for v in all_vouchers if v.get('status') == 'expired']
    active_list = MikrotikService.get_active_users()
    all_packages = PackageService.get_all_packages()

    context = {
        "request": request,
        "total": len(all_vouchers),
        "used": len([v for v in all_vouchers if v.get('status') == 'used']),
        "unused": len([v for v in all_vouchers if v.get('status') == 'unused']),
        "expired": len(expired_vouchers),
        "expired_count": len(expired_vouchers),
        "total_active": len(active_list),
        "active_users": active_list,
        "packages": all_packages,
        "router_status": "Connected" if MikrotikService.check_connection() else "Disconnected"
    }
    return templates.TemplateResponse(request=request, name="dashboard.html", context=context)


# ================= QUICK VOUCHER GENERATOR AJAX ROUTE (Updated) =================
@app.post("/generate-vouchers-fast")
@login_required
async def generate_vouchers_fast(
    request: Request,
    generation_type: str = Form("package"),  # "package" au "custom"
    package_id: Optional[str] = Form(None),
    custom_price: Optional[int] = Form(1000),
    custom_duration: Optional[str] = Form("1 Day"),
    custom_data_limit: Optional[str] = Form("Unlimited"),
    quantity: int = Form(1),
    prefix: str = Form("")
):
    """Inapokea maombi ya kutengeneza vocha kwa kuchagua kifurushi au kuweka bei, GB/Data na Muda moja kwa moja kiotomatiki"""
    try:
        if generation_type == "package" and package_id:
            pkg = PackageService.get_package(package_id)
            price = pkg.get("price", 1000) if pkg else 1000
            uptime = pkg.get("validity", "1 Day") if pkg else "1 Day"
            data_limit = pkg.get("data_limit", "Unlimited") if pkg else "Unlimited"
            profile_name = pkg.get("name", "default") if pkg else "default"
        else:
            price = custom_price if custom_price is not None else 1000
            uptime = custom_duration if custom_duration else "1 Day"
            data_limit = custom_data_limit if custom_data_limit else "Unlimited"
            profile_name = f"{data_limit}_{uptime}"

        generated_codes = []
        clean_prefix = prefix.strip().upper()

        for _ in range(quantity):
            code = VoucherService.create_voucher(
                price=price,
                uptime=uptime,
                data_limit=data_limit,
                profile_name=profile_name,
                prefix=clean_prefix
            )
            if code:
                generated_codes.append(code)

        if generated_codes:
            return JSONResponse({
                "success": True,
                "message": f"Vocha {len(generated_codes)} zimetengenezwa kikamilifu!",
                "count": len(generated_codes),
                "vouchers": generated_codes
            }, status_code=200)

        return JSONResponse({"success": False, "message": "Imeshindwa kutengeneza vocha!"}, status_code=400)

    except Exception as e:
        logger.error(f"Error in fast voucher generation: {e}")
        return JSONResponse({"success": False, "message": f"Kosa la Server: {str(e)}"}, status_code=500)


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

@app.get("/clear-expired-vouchers")
@login_required
def clear_expired_vouchers(request: Request):
    """Inafuta kabisa vocha zote zilizokwisha muda (expired) kutoka kwenye database na MikroTik kwa pamoja"""
    try:
        expired_vouchers = list(db.vouchers.find({"status": "expired"}))
        count = len(expired_vouchers)
        
        for v in expired_vouchers:
            code = v.get("code")
            if MikrotikConfig.ENABLED and code:
                threading.Thread(
                    target=MikrotikService.remove_user_from_mikrotik,
                    args=(code,),
                    daemon=True
                ).start()
        
        db.vouchers.delete_many({"status": "expired"})
        logger.info(f"Imefuta vocha {count} zilizokwisha muda kwa pamoja.")
        
        return RedirectResponse("/vouchers?msg=expired_cleaned", status_code=303)
    except Exception as e:
        logger.error(f"Kosa wakati wa kufuta expired vouchers: {e}")
        return RedirectResponse("/vouchers?msg=error", status_code=303)

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
    used_vouchers = [v for v in all_vouchers if v.get("status") == "used"]
    context = {
        "request": request,
        "user": get_current_user(request),
        "vouchers": all_vouchers,
        "total_sales": sum(v.get("price", 0) for v in used_vouchers),
        "total_issued": len(all_vouchers),
        "total_used": len(used_vouchers),
        "total_unused": len([v for v in all_vouchers if v.get("status") == "unused"]),
        "total_expired": len([v for v in all_vouchers if v.get("status") == "expired"])
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
def save_settings(request: Request):
    return RedirectResponse("/settings", status_code=303)
