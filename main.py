import datetime
import hashlib
import hmac
import ipaddress
import jwt
import logging
import os
from pathlib import Path
import random
import re
import secrets
import smtplib
import string
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from inspect import iscoroutinefunction as asyncio_iscoroutinefunction
from typing import Optional
from urllib.parse import quote_plus

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from librouteros import connect
from pymongo import MongoClient
from starlette.middleware.sessions import SessionMiddleware

# ================= PRODUCTION SECURITY =================

ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()

SECRET_KEY = os.getenv("SECRET_KEY")
ADMIN_USER = os.getenv("ADMIN_USER")
ADMIN_PASS = os.getenv("ADMIN_PASS")

if not SECRET_KEY:
    if ENVIRONMENT == "production":
        raise RuntimeError(
            "SECRET_KEY haijawekwa. Weka SECRET_KEY kwenye .env au Render environment variables kabla ya production."
        )
    SECRET_KEY = secrets.token_urlsafe(48)

if ENVIRONMENT == "production" and (not ADMIN_USER or not ADMIN_PASS):
    raise RuntimeError(
        "ADMIN_USER na ADMIN_PASS lazima ziwekwe kwenye .env au Render environment variables kwa production."
    )

PASSWORD_SALT_BYTES = 16
PASSWORD_DKLEN = 64
PASSWORD_ITERATIONS = 310_000


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 password hash; no plaintext password is stored."""
    if not password or len(password) < 8:
        raise ValueError("Password lazima iwe na angalau characters 8.")
    salt = secrets.token_bytes(PASSWORD_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
        dklen=PASSWORD_DKLEN,
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        salt.hex(),
        digest.hex(),
    )


def verify_password(password: str, stored: str) -> bool:
    """Verify new PBKDF2 hashes. Legacy plaintext passwords are not accepted."""
    if not password or not stored or not stored.startswith("pbkdf2_sha256$"):
        return False
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$", 3)
        iterations = int(iterations)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def is_admin(request: Request) -> bool:
    return (
        bool(request.session.get("logged_in"))
        and str(request.session.get("role", "")).lower() == "admin"
    )


def is_customer(request: Request) -> bool:
    return (
        bool(request.session.get("logged_in"))
        and str(request.session.get("role", "")).lower() == "customer"
    )


def require_admin(request: Request):
    if not request.session.get("logged_in"):
        return RedirectResponse("/login", status_code=303)
    if not is_admin(request):
        return RedirectResponse("/customer/dashboard", status_code=303)
    return None


def require_customer(request: Request):
    if not request.session.get("logged_in"):
        return RedirectResponse("/login", status_code=303)
    if not is_customer(request):
        return RedirectResponse("/dashboard", status_code=303)
    return None


def safe_error(message="Ombi la mfumo limeshindwa. Jaribu tena."):
    return JSONResponse({"success": False, "message": message}, status_code=400)


# In-memory login throttle. For multiple production workers, replace with Redis.
_LOGIN_ATTEMPTS = {}
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_ATTEMPTS = 8


def login_rate_limited(client_key: str) -> bool:
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    row = _LOGIN_ATTEMPTS.get(
        client_key, {"count": 0, "reset": now + _LOGIN_WINDOW_SECONDS}
    )
    if now >= row["reset"]:
        row = {"count": 0, "reset": now + _LOGIN_WINDOW_SECONDS}
    row["count"] += 1
    _LOGIN_ATTEMPTS[client_key] = row
    return row["count"] > _LOGIN_MAX_ATTEMPTS


def clear_login_attempts(client_key: str):
    _LOGIN_ATTEMPTS.pop(client_key, None)


def audit_event(action: str, request: Request, target: str = ""):
    try:
        db.audit_logs.insert_one({
            "action": action,
            "actor": request.session.get("user"),
            "role": request.session.get("role"),
            "target": target,
            "ip": request.client.host if request.client else "",
            "created_at": datetime.datetime.now(datetime.timezone.utc),
        })
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)


# ================= SETUP & CONFIGURATION =================
load_dotenv()

# Ensure required directories exist before mounting
os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

# Database Setup
MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    username = quote_plus(os.getenv("MONGO_USER", "vicentmtiha4_db_user"))
    password = quote_plus(os.getenv("MONGO_PASS", "sKJFIrbFy4RvjUpZ"))
    cluster = os.getenv("MONGO_CLUSTER", "cluster0.jqljfd3.mongodb.net")
    MONGO_URL = f"mongodb+srv://{username}:{password}@{cluster}/?retryWrites=true&w=majority"

client = MongoClient(MONGO_URL)
db = client[os.getenv("MONGO_DB_NAME", "core_wisp_db")]

app = FastAPI(title="CORE-WISP WiFi Billing System")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Customer router is registered after app setup
from routers import customer

app.include_router(customer.router, prefix="/customer")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ================= EMAIL / SMTP CONFIGURATION (DYNAMIC FROM RENDER ENV) =================
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "vicentmtiha4@gmail.com")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "dcbgczltrfhuvnmw")
APP_NAME = os.getenv("APP_NAME", "Core-wisp Billing")


def send_reset_email(to_email: str, reset_link: str):
    """Function ya kutuma barua pepe halisi kwenda kwa mtumiaji."""
    try:
        msg = MIMEMultipart()
        msg["From"] = f"{APP_NAME} <{SENDER_EMAIL}>"
        msg["To"] = to_email
        msg["Subject"] = "Ombi la Kubadilisha Password - Core-wisp"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #0f172a; color: #f8fafc; padding: 20px; }}
                .container {{ max-width: 500px; margin: 0 auto; background: #1e293b; padding: 30px; border-radius: 12px; border: 1px solid #334155; }}
                .btn {{ display: inline-block; padding: 12px 24px; background-color: #6366f1; color: #ffffff !important; text-decoration: none; border-radius: 8px; font-weight: bold; margin-top: 20px; }}
                .footer {{ margin-top: 25px; font-size: 12px; color: #94a3b8; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h2 style="color: #818cf8;">Core-wisp Portal</h2>
                <p>Habari,</p>
                <p>Umepokea barua pepe hii kwa sababu kulikuwa na ombi la kubadilisha password ya akaunti yako.</p>
                <p>Bofya kitufe hapa chini ili kuweka password mpya. Link hii itakuwa hai kwa <b>dakika 15 pekee</b>:</p>
                <a href="{reset_link}" class="btn">Badilisha Password</a>
                <p class="footer">Kama hukuomba mabadiliko haya, puuzia barua pepe hii na password yako haitabadilishwa.</p>
            </div>
        </body>
        </html>
        """
        msg.attach(MIMEText(html_content, "html"))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
        logger.info(f"[SUCCESS] Email ya Reset imetumwa kwenda: {to_email}")
    except Exception as e:
        logger.error(f"[ERROR] Imeshindwa kutuma email: {e}")


# ================= MIKROTIK CONFIG =================
class MikrotikConfig:

    @property
    def HOST(self):
        return os.getenv("MIKROTIK_HOST", "10.176.235.121")

    @property
    def PORT(self):
        return int(os.getenv("MIKROTIK_PORT", 8728))

    @property
    def USER(self):
        return os.getenv("MIKROTIK_USER", "admin")

    @property
    def PASS(self):
        return os.getenv("MIKROTIK_PASS", "Venom@123")

    @property
    def ENABLED(self):
        return os.getenv("MIKROTIK_ENABLED", "true").lower() == "true"


mikrotik_config = MikrotikConfig()


# ================= AZAMPAY CONFIG & HELPERS =================
AZAMPAY_BASE_URL = os.getenv(
    "AZAMPAY_BASE_URL", "https://sandbox.azampay.co.tz"
)


def detect_provider(phone: str) -> str:
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

    return "Airtel"


def get_access_token():
    env_token = os.getenv("AZAMPAY_TOKEN", "").strip().strip('"').strip("'")
    if env_token:
        return env_token

    url = f"{AZAMPAY_BASE_URL}/app/outer/v1/token"
    payload = {
        "appName": os.getenv("AZAMPAY_APP_NAME", ""),
        "clientId": os.getenv("AZAMPAY_CLIENT_ID", ""),
        "clientSecret": os.getenv("AZAMPAY_CLIENT_SECRET", ""),
    }

    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ),
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


# ================= VOUCHER SERVICE (MongoDB) =================
class VoucherService:

    @staticmethod
    def generate_code(prefix: str = "") -> str:
        code_part = "".join(
            random.choices(string.ascii_uppercase + string.digits, k=6)
        )
        if prefix:
            return f"{prefix.strip().upper()}-{code_part}"
        return code_part

    @staticmethod
    def convert_to_mikrotik_time(time_str: str) -> str:
        time_str = str(time_str).lower().strip()
        match = re.search(r"\d+", time_str)
        num = int(match.group()) if match else 1
        if "day" in time_str or "d" in time_str:
            return f"{num * 24:02}:00:00"
        elif "h" in time_str:
            return f"{num:02}:00:00"
        elif "m" in time_str:
            return f"00:{num:02}:00"
        return "01:00:00"

    @staticmethod
    def create_voucher(
        price: int,
        uptime: str,
        data_limit: str,
        profile_name: str = "default",
        prefix: str = "",
        owner_username: str = "",
    ) -> Optional[str]:
        try:
            code = VoucherService.generate_code(prefix)
            existing = db.vouchers.find_one({"code": code})
            if existing:
                return VoucherService.create_voucher(
                    price, uptime, data_limit, profile_name, prefix, owner_username
                )

            expiry_date = datetime.datetime.now() + datetime.timedelta(days=30)
            voucher_doc = {
                "id": random.randint(10000, 99999),
                "code": code,
                "profile": profile_name,
                "price": int(price),
                "uptime": str(uptime),
                "data_limit": str(data_limit),
                "status": "unused",
                "created_at": datetime.datetime.now(),
                "expires_at": expiry_date,
                "used_by": "",
                "owner_username": owner_username.strip(),
            }
            db.vouchers.insert_one(voucher_doc)
            logger.info(f"Voucher created: {code} for owner: {owner_username}")

            if mikrotik_config.ENABLED:
                threading.Thread(
                    target=MikrotikService.sync_voucher_to_mikrotik,
                    args=(code, uptime),
                    daemon=True,
                ).start()

            return code
        except Exception as e:
            logger.error(f"Error creating voucher: {e}")
            return None

    @staticmethod
    def get_voucher(voucher_id):
        try:
            if str(voucher_id).isdigit():
                return db.vouchers.find_one({"id": int(voucher_id)})
            elif ObjectId.is_valid(str(voucher_id)):
                return db.vouchers.find_one({"_id": ObjectId(str(voucher_id))})
            return db.vouchers.find_one({"id": voucher_id})
        except Exception:
            return None

    @staticmethod
    def get_voucher_by_code(code: str):
        try:
            return db.vouchers.find_one({"code": code.strip().upper()})
        except Exception:
            return None

    @staticmethod
    def mark_used(voucher_id, client_mac: str = "") -> bool:
        try:
            query = (
                {"id": int(voucher_id)}
                if str(voucher_id).isdigit()
                else {"_id": ObjectId(str(voucher_id))}
            )
            result = db.vouchers.update_one(
                query,
                {
                    "$set": {
                        "status": "used",
                        "used_by": client_mac,
                        "used_at": datetime.datetime.now(),
                    }
                },
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error marking voucher as used: {e}")
            return False

    @staticmethod
    def mark_expired(voucher_id) -> bool:
        try:
            v = VoucherService.get_voucher(voucher_id)
            if not v:
                return False

            query = {"_id": v["_id"]}
            db.vouchers.update_one(query, {"$set": {"status": "expired"}})
            if mikrotik_config.ENABLED:
                threading.Thread(
                    target=MikrotikService.remove_user_from_mikrotik,
                    args=(v["code"],),
                    daemon=True,
                ).start()
            return True
        except Exception as e:
            logger.error(f"Error marking voucher as expired: {e}")
            return False

    @staticmethod
    def delete_voucher(voucher_id) -> bool:
        try:
            v = VoucherService.get_voucher(voucher_id)
            if not v:
                return False
            code = v["code"]
            db.vouchers.delete_one({"_id": v["_id"]})
            if mikrotik_config.ENABLED:
                threading.Thread(
                    target=MikrotikService.remove_user_from_mikrotik,
                    args=(code,),
                    daemon=True,
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
    def get_recent_vouchers(limit: int = 10):
        try:
            return list(db.vouchers.find().sort("created_at", -1).limit(limit))
        except Exception as e:
            logger.error(f"Error fetching recent vouchers: {e}")
            return []

    @staticmethod
    def check_and_expire_vouchers() -> int:
        expired_count = 0
        try:
            now = datetime.datetime.now()
            vouchers = list(
                db.vouchers.find({"status": "unused", "expires_at": {"$lt": now}})
            )
            for v in vouchers:
                db.vouchers.update_one(
                    {"_id": v["_id"]}, {"$set": {"status": "expired"}}
                )
                expired_count += 1
                if mikrotik_config.ENABLED:
                    threading.Thread(
                        target=MikrotikService.remove_user_from_mikrotik,
                        args=(v["code"],),
                        daemon=True,
                    ).start()

            if mikrotik_config.ENABLED:
                MikrotikService.sync_live_voucher_statuses()

        except Exception as e:
            logger.error(f"Error checking expiry: {e}")
        return expired_count


# ================= MIKROTIK SERVICE =================
class MikrotikService:

    @staticmethod
    def get_api():
        try:
            return connect(
                host=mikrotik_config.HOST,
                username=mikrotik_config.USER,
                password=mikrotik_config.PASS,
                port=mikrotik_config.PORT,
                timeout=5,
            )
        except Exception as e:
            logger.error(f"Mikrotik connection error: {e}")
            return None

    @staticmethod
    def check_connection() -> bool:
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
    def sync_voucher_to_mikrotik(voucher_code: str, uptime: str) -> bool:
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
                **{"limit-uptime": mikrotik_uptime, "comment": "Auto Voucher"},
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
    def remove_user_from_mikrotik(voucher_code: str) -> bool:
        api = None
        try:
            api = MikrotikService.get_api()
            if not api:
                return False
            hotspot_users = api.path("ip", "hotspot", "user")
            users = list(hotspot_users)
            target_user = next(
                (u for u in users if u.get("name") == voucher_code), None
            )
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
    def lock_voucher_to_mac(voucher_code: str, mac: str) -> bool:
        api = None
        try:
            api = MikrotikService.get_api()
            if not api:
                return False

            hotspot_users = api.path("ip", "hotspot", "user")
            users = list(hotspot_users)
            target_user = next(
                (u for u in users if u.get("name") == voucher_code), None
            )

            if target_user:
                user_id = target_user[".id"]
                hotspot_users.update(**{".id": user_id, "mac-address": mac})
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
                    "login_by": u.get("login-by", "-"),
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
        api = None
        try:
            api = MikrotikService.get_api()
            if not api:
                return False

            active_path = api.path("ip", "hotspot", "active")
            active_path.remove(active_id)
            logger.info(
                f"User with active ID {active_id} disconnected from MikroTik."
            )
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

    @staticmethod
    def sync_live_voucher_statuses():
        api = None
        try:
            api = MikrotikService.get_api()
            if not api:
                return

            mt_users = {
                u.get("name"): u for u in list(api.path("ip", "hotspot", "user"))
            }
            mt_active = [
                u.get("user") for u in list(api.path("ip", "hotspot", "active"))
            ]

            db_vouchers = list(
                db.vouchers.find({"status": {"$in": ["unused", "used"]}})
            )

            for v in db_vouchers:
                code = v.get("code")
                mt_u = mt_users.get(code)

                if code in mt_active:
                    if v.get("status") != "used":
                        db.vouchers.update_one(
                            {"_id": v["_id"]},
                            {
                                "$set": {
                                    "status": "used",
                                    "used_at": datetime.datetime.now(),
                                }
                            },
                        )
                    continue

                if mt_u:
                    uptime = str(mt_u.get("uptime", "0s"))
                    limit_uptime = str(mt_u.get("limit-uptime", ""))

                    try:
                        bytes_out = int(mt_u.get("bytes-out", 0) or 0)
                    except (ValueError, TypeError):
                        bytes_out = 0

                    if limit_uptime and uptime == limit_uptime:
                        db.vouchers.update_one(
                            {"_id": v["_id"]}, {"$set": {"status": "expired"}}
                        )
                        MikrotikService.remove_user_from_mikrotik(code)
                    elif bytes_out > 0 or uptime != "0s":
                        if v.get("status") != "used":
                            db.vouchers.update_one(
                                {"_id": v["_id"]}, {"$set": {"status": "used"}}
                            )
                else:
                    if v.get("status") == "used":
                        db.vouchers.update_one(
                            {"_id": v["_id"]}, {"$set": {"status": "expired"}}
                        )

        except Exception as e:
            logger.error(f"Error syncing live status with Mikrotik: {e}")
        finally:
            if api:
                try:
                    api.close()
                except Exception:
                    pass


# ================= USER SERVICE (MongoDB) =================
class UserService:

    @staticmethod
    def create_user(
        username: str,
        password: str,
        role: str = "Staff",
        status: str = "Active",
    ) -> bool:
        try:
            existing = db.users.find_one({"username": username.strip()})
            if existing:
                return False
            user_doc = {
                "id": random.randint(10000, 99999),
                "username": username.strip(),
                "password_hash": hash_password(password),
                "role": role,
                "status": status,
                "created_at": datetime.datetime.now(),
            }
            db.users.insert_one(user_doc)
            return True
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            return False

    @staticmethod
    def create_customer(
        first_name: str,
        last_name: str,
        phone: str,
        email: str,
        password: str,
    ) -> bool:
        try:
            clean_email = email.lower().strip()
            existing = db.users.find_one({"username": clean_email})
            if existing:
                return False
            user_doc = {
                "id": random.randint(10000, 99999),
                "username": clean_email,
                "first_name": first_name.strip(),
                "last_name": last_name.strip(),
                "phone": phone.strip(),
                "email": clean_email,
                "password_hash": hash_password(password),
                "role": "customer",
                "status": "pending",
                "created_at": datetime.datetime.now(),
            }
            db.users.insert_one(user_doc)
            return True
        except Exception as e:
            logger.error(f"Error creating customer: {e}")
            return False

    @staticmethod
    def get_user(user_id):
        try:
            if str(user_id).isdigit():
                return db.users.find_one({"id": int(user_id)})
            elif ObjectId.is_valid(str(user_id)):
                return db.users.find_one({"_id": ObjectId(str(user_id))})
            return db.users.find_one({"id": user_id})
        except Exception:
            return None

    @staticmethod
    def approve_user(user_id) -> bool:
        try:
            u = UserService.get_user(user_id)
            if not u:
                return False
            result = db.users.update_one(
                {"_id": u["_id"]}, {"$set": {"status": "active"}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error approving user: {e}")
            return False

    @staticmethod
    def reject_user(user_id) -> bool:
        try:
            u = UserService.get_user(user_id)
            if not u:
                return False
            result = db.users.delete_one({"_id": u["_id"]})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Error rejecting user: {e}")
            return False

    @staticmethod
    def authenticate(username: str, password: str):
        try:
            clean_user = username.strip().lower()
            user = db.users.find_one(
                {"$or": [{"username": clean_user}, {"email": clean_user}]}
            )
            if not user:
                return None
            if not verify_password(password, user.get("password_hash", "")):
                return None
            return user
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return None

    @staticmethod
    def update_user(
        user_id, username: str, password: str, role: str, status: str
    ) -> bool:
        try:
            u = UserService.get_user(user_id)
            if not u:
                return False
            result = db.users.update_one(
                {"_id": u["_id"]},
                {
                    "$set": {
                        "username": username.strip(),
                        "password_hash": hash_password(password),
                        "role": role,
                        "status": status,
                    }
                },
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating user: {e}")
            return False

    @staticmethod
    def delete_user(user_id) -> bool:
        try:
            u = UserService.get_user(user_id)
            if not u:
                return False
            result = db.users.delete_one({"_id": u["_id"]})
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
    def create_package(
        name: str,
        price: int,
        uptime: str = "1 Day",
        data_limit: str = "Unlimited",
        owner_username: str = "",
    ) -> bool:
        try:
            pkg_doc = {
                "id": random.randint(10000, 99999),
                "name": name.strip(),
                "price": int(price),
                "uptime": uptime.strip(),
                "data_limit": data_limit.strip(),
                "owner_username": owner_username.strip(),
            }
            db.packages.insert_one(pkg_doc)
            return True
        except Exception as e:
            logger.error(f"Error creating package: {e}")
            return False

    @staticmethod
    def get_package(pkg_id):
        try:
            if str(pkg_id).isdigit():
                return db.packages.find_one({"id": int(pkg_id)})
            elif ObjectId.is_valid(str(pkg_id)):
                return db.packages.find_one({"_id": ObjectId(str(pkg_id))})
            return db.packages.find_one({"id": pkg_id})
        except Exception as e:
            logger.error(f"Error fetching package {pkg_id}: {e}")
            return None

    @staticmethod
    def update_package(
        pkg_id, name: str, price: int, uptime: str, data_limit: str
    ) -> bool:
        try:
            pkg = PackageService.get_package(pkg_id)
            if not pkg:
                return False
            result = db.packages.update_one(
                {"_id": pkg["_id"]},
                {
                    "$set": {
                        "name": name.strip(),
                        "price": int(price),
                        "uptime": uptime.strip(),
                        "data_limit": data_limit.strip(),
                    }
                },
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating package: {e}")
            return False

    @staticmethod
    def delete_package(pkg_id) -> bool:
        try:
            pkg = PackageService.get_package(pkg_id)
            if not pkg:
                return False
            result = db.packages.delete_one({"_id": pkg["_id"]})
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
        return (
            await f(request, *args, **kwargs)
            if asyncio_iscoroutinefunction(f)
            else f(request, *args, **kwargs)
        )

    return decorated_function


def get_current_user(request: Request) -> str:
    return request.session.get("user", "Guest")


# Helper Function ya kutambua Render / Production Host URL kwa Usahihi
def get_base_url(request: Request) -> str:
    """Inatengeneza Base URL kiotomatiki (Inasoma Render Domain, ENV au Host Header)"""
    render_external_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_external_url:
        return render_external_url.rstrip("/")

    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    
    if forwarded_proto and forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}"
    
    return str(request.base_url).rstrip("/")


# ================= BACKGROUND WORKER =================
def expiry_worker():
    while True:
        try:
            VoucherService.check_and_expire_vouchers()
        except Exception as e:
            logger.error(f"Error in expiry worker: {e}")
        time.sleep(15)


threading.Thread(target=expiry_worker, daemon=True).start()


# ================= AUTH ROUTES =================


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    if ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


@app.get("/")
@app.get("/login")
def login_page(request: Request):
    if request.session.get("logged_in"):
        role = str(request.session.get("role", "")).lower()
        if role == "admin":
            return RedirectResponse("/dashboard", status_code=303)
        elif role == "customer":
            return RedirectResponse("/customer/dashboard", status_code=303)

    error = request.session.pop("login_error", None)
    success = request.session.pop("register_success", None)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"request": request, "error": error, "success": success},
    )


@app.post("/login")
def handle_login(
    request: Request, username: str = Form(...), password: str = Form(...)
):
    username = username.strip()

    admin_user = os.getenv("ADMIN_USER", "").strip()
    admin_pass = os.getenv("ADMIN_PASS", "")

    # Check SuperAdmin Credentials
    if (
        admin_user
        and admin_pass
        and username == admin_user
        and password == admin_pass
    ):
        request.session.update(
            {"logged_in": True, "user": "SuperAdmin", "role": "admin"}
        )
        logger.info("SuperAdmin logged in successfully.")
        return RedirectResponse("/dashboard", status_code=303)

    # Check Mongo DB Users
    user = UserService.authenticate(username, password)
    if not user:
        logger.warning(f"Failed login attempt for: {username}")
        request.session["login_error"] = "Username au password si sahihi!"
        return RedirectResponse("/login", status_code=303)

    status = str(user.get("status", "pending")).lower()
    if status != "active":
        logger.warning(f"Inactive account login attempt: {username}")
        request.session["login_error"] = (
            "Akaunti yako bado ipo kwenye uhakiki (pending). Subiri Admin"
            " aipitishe."
        )
        return RedirectResponse("/login", status_code=303)

    role = str(user.get("role", "customer")).lower()

    request.session.update({
        "logged_in": True,
        "user": user.get("username", username),
        "role": role,
        "user_id": str(user.get("id", user.get("_id"))),
    })

    logger.info(f"User {username} logged in with role '{role}'")

    if role == "admin":
        return RedirectResponse("/dashboard", status_code=303)
    if role == "customer":
        return RedirectResponse("/customer/dashboard", status_code=303)

    logger.warning(f"Unknown role '{role}' for user {username}")
    request.session.clear()
    request.session["login_error"] = "Role ya account yako haijatambuliwa."
    return RedirectResponse("/login", status_code=303)


# ================= FORGOT PASSWORD ROUTES =================


# 1. Onyesha Ukurasa wa Form ya Forgot Password
@app.get("/forgot-password", response_class=HTMLResponse)
def show_forgot_password_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context={"request": request},
    )


# 2. Pokea Ombi la Email na Tuma Link Halisi Kwa Email Ya Mtumiaji (Inasoma Render Domain Dynamic)
@app.post("/forgot-password", response_class=HTMLResponse)
async def handle_forgot_password(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
):
    clean_email = email.strip().lower()

    # Tengeneza JWT Reset Token inayomalizika muda baada ya dakika 15
    expiration = datetime.datetime.now(
        datetime.timezone.utc
    ) + datetime.timedelta(minutes=15)
    token_payload = {"sub": clean_email, "exp": expiration}
    reset_token = jwt.encode(token_payload, SECRET_KEY, algorithm="HS256")

    # KUPATA RENDER / DOMAIN HOST URL DYNAMICALLY
    base_url = get_base_url(request)
    reset_link = f"{base_url}/reset-password?token={reset_token}"

    # Tuma email kwenye background ili kuzuia ucheleweshaji wa page
    background_tasks.add_task(send_reset_email, clean_email, reset_link)

    return templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context={
            "request": request,
            "success": (
                "Maelekezo yametumwa! Angalia Inbox au Spam ya barua pepe:"
                f" {clean_email}"
            ),
        },
    )


# 3. Onyesha Ukurasa wa Kuweka Password Mpya (Link ikibofywa kwenye Email)
@app.get("/reset-password", response_class=HTMLResponse)
def show_reset_password_page(request: Request, token: str):
    try:
        # Hakiki kama Token ni halali na haija-expire
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        email = payload.get("sub")
        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={"request": request, "token": token, "email": email},
        )
    except jwt.ExpiredSignatureError:
        return HTMLResponse(
            content=(
                "<h3>Link hii imepita muda wake (Expired). Tafadhali omba tena"
                " link mpya.</h3>"
            ),
            status_code=400,
        )
    except jwt.PyJWTError:
        return HTMLResponse(
            content="<h3>Link hii si halali (Invalid Token).</h3>",
            status_code=400,
        )


# 4. Process/Hifadhi Password Mpya baada ya mteja kuijaza kwenye Form
@app.post("/reset-password")
async def handle_reset_password(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    if password != confirm_password:
        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={
                "request": request,
                "token": token,
                "error": "Password mpya hazifanani!",
            },
        )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        clean_email = payload.get("sub")

        new_hash = hash_password(password)

        result = db.users.update_one(
            {"$or": [{"email": clean_email}, {"username": clean_email}]},
            {"$set": {"password_hash": new_hash}},
        )

        if result.matched_count == 0:
            return templates.TemplateResponse(
                request=request,
                name="reset_password.html",
                context={
                    "request": request,
                    "token": token,
                    "error": "Akaunti hii haikupatikana kwenye mfumo.",
                },
            )

        request.session["register_success"] = (
            "Password yako imebadilishwa kikamilifu! Ingia sasa."
        )
        return RedirectResponse("/login", status_code=303)

    except jwt.ExpiredSignatureError:
        return HTMLResponse(
            content="<h3>Link hii imepita muda wake (Expired). Omba tena.</h3>",
            status_code=400,
        )
    except jwt.PyJWTError:
        return HTMLResponse(
            content="<h3>Link hii si halali (Invalid Token).</h3>",
            status_code=400,
        )


@app.get("/logout")
def logout(request: Request):
    logger.info(f"User {get_current_user(request)} logged out")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ================= REGISTRATION ROUTES =================
@app.get("/register")
def show_register_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="register.html", context={"request": request}
    )


@app.post("/register")
async def handle_register(
    request: Request,
    first_name: str = Form(...),
    middle_name: Optional[str] = Form(None),
    last_name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    if password != confirm_password:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"request": request, "error": "Password hazifanani!"},
        )

    success = UserService.create_customer(
        first_name, last_name, phone, email, password
    )

    if not success:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "request": request,
                "error": (
                    "Email tayari imeshasajiliwa. Tafadhali tumia nyingine au"
                    " ingia (Login)."
                ),
            },
        )

    request.session["register_success"] = (
        "Usajili umefanikiwa! Akaunti yako inasubiri uhakiki wa Admin. "
        "Utaweza kuingia (login) mara Admin atakapokuidhinisha."
    )
    return RedirectResponse("/login", status_code=303)


# ================= PENDING USERS & APPROVAL ROUTES =================
@app.get("/pending-registrations", response_class=HTMLResponse)
@login_required
def pending_registrations_page(request: Request):
    role = str(request.session.get("role", "")).lower()
    if role != "admin":
        return RedirectResponse("/customer/dashboard", status_code=303)

    pending_users = list(db.users.find({"status": "pending"}))
    return HTMLResponse(content=f"""
    <!DOCTYPE html>
    <html lang="sw">
    <head>
        <meta charset="UTF-8">
        <title>Pending Registrations - CORE-WISP</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="bg-light p-5">
        <div class="container">
            <div class="d-flex justify-content-between align-items-center mb-4">
                <h2>Watumiaji Wanaosubiri Uhakiki (Pending Registrations)</h2>
                <a href="/dashboard" class="btn btn-secondary">Rudi Dashboard</a>
            </div>
            <div class="card shadow-sm p-4">
                <table class="table table-striped">
                    <thead>
                        <tr>
                            <th>Username / Email</th>
                            <th>Role</th>
                            <th>Vitendo</th>
                        </tr>
                    </thead>
                    <tbody>
                        {"".join(f"<tr><td>{u.get('username')}</td><td>{u.get('role')}</td><td><a href='/approve-user/{u.get('_id')}' class='btn btn-success btn-sm'>Approve</a> <a href='/reject-user/{u.get('_id')}' class='btn btn-danger btn-sm'>Reject</a></td></tr>" for u in pending_users) if pending_users else "<tr><td colspan='3' class='text-center'>Hakuna maombi yanayosubiri kwa sasa.</td></tr>"}
                    </tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """)


@app.get("/approve-user/{user_id}")
@login_required
def approve_user_route(request: Request, user_id: str):
    guard = require_admin(request)
    if guard:
        return guard
    role = str(request.session.get("role", "")).lower()
    if role == "admin":
        UserService.approve_user(user_id)
        logger.info(f"User {user_id} approved by Admin.")
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/reject-user/{user_id}")
@login_required
def reject_user_route(request: Request, user_id: str):
    guard = require_admin(request)
    if guard:
        return guard
    role = str(request.session.get("role", "")).lower()
    if role == "admin":
        UserService.reject_user(user_id)
        logger.info(f"User {user_id} rejected and deleted by Admin.")
    return RedirectResponse("/dashboard", status_code=303)


# ================= HOTSPOT ROUTES =================
@app.get("/hotspot", response_class=HTMLResponse)
@app.get("/hotspot-login", response_class=HTMLResponse)
def get_hotspot_page(request: Request, mac: str = ""):
    return templates.TemplateResponse(
        request=request,
        name="hotspot.html",
        context={"request": request, "mac": mac},
    )


@app.post("/hotspot-login")
def hotspot_login(voucher: str = Form(...), mac: str = Form("")):
    voucher_obj = VoucherService.get_voucher_by_code(voucher.strip().upper())

    if not voucher_obj:
        return JSONResponse(
            {"status": "error", "message": "Voucher haipo au ni makosa!"},
            status_code=400,
        )

    if voucher_obj["status"] == "expired":
        return JSONResponse(
            {"status": "error", "message": "Voucher muda wake umekwisha!"},
            status_code=400,
        )

    success = VoucherService.mark_used(voucher_obj["id"], mac)
    if not success:
        return JSONResponse(
            {
                "status": "error",
                "message": "Imeshindwa kusasisha voucher kwenye database",
            },
            status_code=500,
        )

    if mikrotik_config.ENABLED:
        threading.Thread(
            target=MikrotikService.lock_voucher_to_mac,
            args=(voucher_obj["code"], mac),
            daemon=True,
        ).start()

    return RedirectResponse("https://www.google.com", status_code=303)


# ================= ACTIVE HOTSPOT USERS =================
@app.get("/active-users")
@login_required
def active_users(request: Request):
    active_list = MikrotikService.get_active_users()
    return templates.TemplateResponse(
        request=request,
        name="active_users.html",
        context={
            "request": request,
            "active_users": active_list,
            "total_active": len(active_list),
            "user": get_current_user(request),
        },
    )


@app.get("/kick-user/{active_id:path}")
@login_required
def kick_user(request: Request, active_id: str):
    MikrotikService.disconnect_user(active_id)
    return RedirectResponse("/active-users", status_code=303)


# ================= TRANSACTIONS & SMS GATEWAY ROUTES =================
@app.get("/transactions", response_class=HTMLResponse)
@login_required
def transactions_page(request: Request):
    all_vouchers = VoucherService.get_all_vouchers()
    used_vouchers = [v for v in all_vouchers if v.get("status") == "used"]

    return templates.TemplateResponse(
        request=request,
        name="transactions.html",
        context={
            "request": request,
            "transactions": used_vouchers,
            "user": get_current_user(request),
        },
    )


@app.get("/sms-gateway", response_class=HTMLResponse)
@login_required
def sms_gateway_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="sms_gateway.html",
        context={"request": request, "user": get_current_user(request)},
    )


# ================= DASHBOARD =================
@app.get("/dashboard")
@login_required
def dashboard(request: Request):
    role = str(request.session.get("role", "")).lower()
    if role != "admin":
        return RedirectResponse("/customer/dashboard", status_code=303)

    if mikrotik_config.ENABLED:
        MikrotikService.sync_live_voucher_statuses()

    all_vouchers = VoucherService.get_all_vouchers()
    expired_vouchers = [
        v for v in all_vouchers if v.get("status") == "expired"
    ]
    active_list = MikrotikService.get_active_users()
    all_packages = PackageService.get_all_packages()

    recent_vouchers = VoucherService.get_recent_vouchers(10)
    pending_users = list(db.users.find({"status": "pending"}))

    pending_requests = [
        {
            "id": str(u.get("_id")),
            "name": (
                f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
                or u.get("username", "Mteja")
            ),
            "phone": u.get("phone", "-"),
            "package": u.get("package", "-"),
            "date": (
                u.get("created_at").strftime("%d/%m/%Y %H:%M")
                if u.get("created_at")
                else "-"
            ),
        }
        for u in pending_users
    ]

    context = {
        "request": request,
        "total": len(all_vouchers),
        "used": len([v for v in all_vouchers if v.get("status") == "used"]),
        "unused": len([v for v in all_vouchers if v.get("status") == "unused"]),
        "expired": len(expired_vouchers),
        "expired_count": len(expired_vouchers),
        "total_active": len(active_list),
        "active_users": active_list,
        "packages": all_packages,
        "recent_vouchers": recent_vouchers,
        "pending_users": pending_users,
        "pending_count": len(pending_users),
        "pending_requests": pending_requests,
        "router_status": (
            "Connected" if MikrotikService.check_connection() else "Disconnected"
        ),
    }
    return templates.TemplateResponse(
        request=request, name="dashboard.html", context=context
    )


# ================= QUICK VOUCHER GENERATOR AJAX ROUTE =================
@app.post("/generate-vouchers-fast")
@login_required
async def generate_vouchers_fast(
    request: Request,
    generation_type: str = Form("package"),
    package_id: Optional[str] = Form(None),
    custom_price: Optional[int] = Form(1000),
    custom_duration: Optional[str] = Form("1 Day"),
    custom_data_limit: Optional[str] = Form("Unlimited"),
    quantity: int = Form(1),
    prefix: str = Form(""),
):
    try:
        guard = require_admin(request)
        if guard:
            return guard
        username = request.session.get("user", "")
        if generation_type == "package" and package_id:
            pkg = PackageService.get_package(package_id)
            if (
                pkg
                and pkg.get("owner_username") not in (None, username)
                and not is_admin(request)
            ):
                return safe_error("Package hauruhusiwi.")
            price = pkg.get("price", 1000) if pkg else 1000
            uptime = pkg.get("uptime", "1 Day") if pkg else "1 Day"
            data_limit = pkg.get("data_limit", "Unlimited") if pkg else "Unlimited"
            profile_name = pkg.get("name", "default") if pkg else "default"
        else:
            price = custom_price if custom_price is not None else 1000
            uptime = custom_duration if custom_duration else "1 Day"
            data_limit = (
                custom_data_limit if custom_data_limit else "Unlimited"
            )
            profile_name = f"{data_limit}_{uptime}"

        generated_codes = []
        clean_prefix = prefix.strip().upper()

        for _ in range(quantity):
            code = VoucherService.create_voucher(
                price=price,
                uptime=uptime,
                data_limit=data_limit,
                profile_name=profile_name,
                prefix=clean_prefix,
                owner_username=username,
            )
            if code:
                generated_codes.append(code)

        if generated_codes:
            return JSONResponse(
                {
                    "success": True,
                    "message": (
                        f"Vocha {len(generated_codes)} zimetengenezwa"
                        " kikamilifu!"
                    ),
                    "count": len(generated_codes),
                    "vouchers": generated_codes,
                },
                status_code=200,
            )

        return JSONResponse(
            {"success": False, "message": "Imeshindwa kutengeneza vocha!"},
            status_code=400,
        )

    except Exception as e:
        logger.error(f"Error in fast voucher generation: {e}")
        return JSONResponse(
            {"success": False, "message": "Server error. Jaribu tena."},
            status_code=500,
        )


# ================= AZAMPAY CHECKOUT & MOCK PAYMENT ROUTE =================
@app.api_route("/lipa", methods=["GET", "POST"])
async def lipa_internet(
    request: Request, amount: int = 1000, phone: str = "0712345678"
):
    return JSONResponse(
        {
            "status": "pending",
            "message": (
                "Payment endpoint iko kwenye verified-payment flow. Hakuna"
                " voucher inayotolewa kabla ya malipo kuthibitishwa."
            ),
        },
        status_code=202,
    )


@app.post("/azampay-callback")
async def azampay_callback(request: Request):
    try:
        data = await request.json()
        logger.info(f"AzamPay Callback Received: {data}")

        status = data.get("status") or data.get("success")
        external_id = data.get("externalId")
        amount = data.get("amount")

        if status in [True, "success", "completed", "successful"]:
            logger.info(
                "Malipo yamethibitishwa kikamilifu kwa External ID:"
                f" {external_id}, Kiasi: {amount}"
            )

        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error handling AzamPay callback: {e}")
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
                    <option value="500">TZS 500 - Saa 1</option>
                    <option value="1000">TZS 1,000 - Siku 1</option>
                    <option value="2000">TZS 2,000 - Siku 2</option>
                    <option value="5000">TZS 5,000 - Wiki 1</option>
                    <option value="10000">TZS 10,000 - Mwezi 1</option>
                </select>
                <button type="submit">LIPA SASA</button>
            </form>
        </div>
    </body>
    </html>
    """


# ================= USERS MANAGEMENT =================
@app.get("/users")
@login_required
def users(request: Request):
    guard = require_admin(request)
    if guard:
        return guard
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "request": request,
            "users": UserService.get_all_users(),
            "user": get_current_user(request),
        },
    )


@app.post("/add-user")
@login_required
def add_user(
    request: Request, username: str = Form(...), password: str = Form(...)
):
    guard = require_admin(request)
    if guard:
        return guard
    UserService.create_user(username, password)
    return RedirectResponse("/users", status_code=303)


@app.get("/edit-user/{user_id}")
@login_required
def edit_user_form(request: Request, user_id: str):
    guard = require_admin(request)
    if guard:
        return guard
    user = UserService.get_user(user_id)
    if not user:
        return RedirectResponse("/users", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="edit_user.html",
        context={"request": request, "account": user},
    )


@app.post("/edit-user/{user_id}")
@login_required
def update_user(
    request: Request,
    user_id: str,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    status: str = Form(...),
):
    guard = require_admin(request)
    if guard:
        return guard
    UserService.update_user(user_id, username, password, role, status)
    return RedirectResponse("/users", status_code=303)


@app.get("/delete-user/{user_id}")
@login_required
def delete_user(request: Request, user_id: str):
    guard = require_admin(request)
    if guard:
        return guard
    UserService.delete_user(user_id)
    return RedirectResponse("/users", status_code=303)


# ================= PACKAGES MANAGEMENT =================
@app.get("/packages")
@login_required
def packages(request: Request):
    guard = require_admin(request)
    if guard:
        return guard
    return templates.TemplateResponse(
        request=request,
        name="packages.html",
        context={
            "request": request,
            "packages": PackageService.get_all_packages(),
            "user": get_current_user(request),
        },
    )


@app.post("/add-package")
@login_required
def add_package(
    request: Request,
    name: str = Form(...),
    price: int = Form(...),
    uptime: str = Form(""),
    data_limit: str = Form(""),
):
    guard = require_admin(request)
    if guard:
        return guard
    username = request.session.get("user", "")
    PackageService.create_package(
        name, price, uptime, data_limit, owner_username=username
    )
    return RedirectResponse("/packages", status_code=303)


@app.get("/edit-package/{pkg_id}")
@login_required
def edit_package_form(request: Request, pkg_id: str):
    guard = require_admin(request)
    if guard:
        return guard
    pkg = PackageService.get_package(pkg_id)
    if not pkg:
        return RedirectResponse("/packages", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="edit_package.html",
        context={"request": request, "package": pkg},
    )


@app.post("/edit-package/{pkg_id}")
@login_required
def update_package(
    request: Request,
    pkg_id: str,
    name: str = Form(...),
    price: int = Form(...),
    uptime: str = Form(""),
    data_limit: str = Form(""),
):
    guard = require_admin(request)
    if guard:
        return guard
    PackageService.update_package(pkg_id, name, price, uptime, data_limit)
    return RedirectResponse("/packages", status_code=303)


@app.get("/delete-package/{pkg_id}")
@login_required
def delete_package(request: Request, pkg_id: str):
    guard = require_admin(request)
    if guard:
        return guard
    PackageService.delete_package(pkg_id)
    return RedirectResponse("/packages", status_code=303)


# ================= VOUCHERS MANAGEMENT =================
@app.get("/vouchers")
@login_required
def vouchers(request: Request):
    guard = require_admin(request)
    if guard:
        return guard
    if mikrotik_config.ENABLED:
        MikrotikService.sync_live_voucher_statuses()

    return templates.TemplateResponse(
        request=request,
        name="vouchers.html",
        context={
            "request": request,
            "vouchers": VoucherService.get_all_vouchers(),
            "user": get_current_user(request),
        },
    )


@app.post("/generate-voucher")
@login_required
def generate_voucher(
    request: Request,
    price: int = Form(...),
    duration: str = Form(...),
    data_limit: str = Form(...),
    profile_name: Optional[str] = Form("Standard"),
):
    guard = require_admin(request)
    if guard:
        return guard
    username = request.session.get("user", "")
    code = VoucherService.create_voucher(
        price,
        duration,
        data_limit,
        profile_name=profile_name or "Standard",
        owner_username=username,
    )
    if not code:
        logger.error("Failed to generate voucher")
    return RedirectResponse("/vouchers", status_code=303)


@app.get("/activate-voucher/{voucher_id}")
@login_required
def activate_voucher(request: Request, voucher_id: str):
    guard = require_admin(request)
    if guard:
        return guard
    VoucherService.mark_used(voucher_id)
    return RedirectResponse("/vouchers", status_code=303)


@app.get("/expire-voucher/{voucher_id}")
@login_required
def expire_voucher(request: Request, voucher_id: str):
    guard = require_admin(request)
    if guard:
        return guard
    VoucherService.mark_expired(voucher_id)
    return RedirectResponse("/vouchers", status_code=303)


@app.get("/delete-voucher/{voucher_id}")
@login_required
def delete_voucher(request: Request, voucher_id: str):
    guard = require_admin(request)
    if guard:
        return guard
    VoucherService.delete_voucher(voucher_id)
    return RedirectResponse("/vouchers", status_code=303)


@app.get("/clear-expired-vouchers")
@login_required
def clear_expired_vouchers(request: Request):
    guard = require_admin(request)
    if guard:
        return guard
    try:
        expired_vouchers = list(db.vouchers.find({"status": "expired"}))
        count = len(expired_vouchers)

        for v in expired_vouchers:
            code = v.get("code")
            if mikrotik_config.ENABLED and code:
                threading.Thread(
                    target=MikrotikService.remove_user_from_mikrotik,
                    args=(code,),
                    daemon=True,
                ).start()

        db.vouchers.delete_many({"status": "expired"})
        logger.info(f"Imefuta vocha {count} zilizokwisha muda kwa pamoja.")

        return RedirectResponse("/vouchers?msg=expired_cleaned", status_code=303)
    except Exception as e:
        logger.error(f"Kosa wakati wa kufuta expired vouchers: {e}")
        return RedirectResponse("/vouchers?msg=error", status_code=303)


@app.get("/print", response_class=HTMLResponse)
@app.get("/print-vouchers", response_class=HTMLResponse)
@login_required
def print_vouchers(request: Request):
    guard = require_admin(request)
    if guard:
        return guard
    return templates.TemplateResponse(
        request=request,
        name="print_vouchers.html",
        context={"request": request, "vouchers": VoucherService.get_all_vouchers()},
    )


# ================= REPORTS =================
@app.get("/reports")
@login_required
def reports(request: Request):
    guard = require_admin(request)
    if guard:
        return guard
    all_vouchers = VoucherService.get_all_vouchers()
    used_vouchers = [v for v in all_vouchers if v.get("status") == "used"]
    context = {
        "request": request,
        "user": get_current_user(request),
        "vouchers": all_vouchers,
        "total_sales": sum(int(v.get("price", 0)) for v in used_vouchers),
        "total_issued": len(all_vouchers),
        "total_used": len(used_vouchers),
        "total_unused": len(
            [v for v in all_vouchers if v.get("status") == "unused"]
        ),
        "total_expired": len(
            [v for v in all_vouchers if v.get("status") == "expired"]
        ),
    }
    return templates.TemplateResponse(
        request=request, name="reports.html", context=context
    )


# ================= MIKROTIK NEW ROUTER INSTALLER =================
def _installer_connection(host: str, port: int, username: str, password: str):
    if not host or not username:
        raise ValueError("Router host na username vinahitajika.")
    if not password:
        raise ValueError("Router password inahitajika.")
    try:
        port = int(port or 8728)
    except (TypeError, ValueError):
        port = 8728

    return connect(
        host=host.strip(),
        username=username.strip(),
        password=password,
        port=port,
        timeout=8,
    )


def _installer_read_resource(api):
    try:
        return next(iter(api.path("system", "resource")), {})
    except Exception:
        return {}


def _installer_read_routerboard(api):
    try:
        return next(iter(api.path("system", "routerboard")), {})
    except Exception:
        return {}


def _installer_read_interfaces(api):
    try:
        rows = list(api.path("interface"))
        result = []
        for item in rows:
            name = item.get("name")
            if name:
                result.append({
                    "name": name,
                    "type": item.get("type", ""),
                    "running": item.get("running", False),
                    "disabled": item.get("disabled", False),
                })
        return result
    except Exception:
        return []


def _installer_detect(host: str, port: int, username: str, password: str):
    api = None
    try:
        api = _installer_connection(host, port, username, password)
        resource = _installer_read_resource(api)
        board = _installer_read_routerboard(api)
        interfaces = _installer_read_interfaces(api)

        model = (
            board.get("model")
            or resource.get("board-name")
            or resource.get("board_name")
            or "Unknown"
        )

        serial = (
            board.get("serial-number")
            or resource.get("serial-number")
            or resource.get("serial_number")
            or "Unknown"
        )

        return {
            "success": True,
            "model": model,
            "board_name": resource.get("board-name", model),
            "routeros": resource.get("version", "Unknown"),
            "version": resource.get("version", "Unknown"),
            "architecture": resource.get("architecture-name", "Unknown"),
            "serial": serial,
            "cpu": resource.get("cpu", ""),
            "cpu_count": resource.get("cpu-count", ""),
            "total_memory": resource.get("total-memory", ""),
            "free_memory": resource.get("free-memory", ""),
            "uptime": resource.get("uptime", ""),
            "interfaces": interfaces,
        }
    finally:
        if api:
            try:
                api.close()
            except Exception:
                pass


def _installer_script(
    model: str,
    routeros: str,
    wan: str,
    ip_cidr: str,
    pool_range: str,
    dns_name: str,
    hotspot_name: str,
    install_nat: bool,
    install_dhcp: bool,
    install_dns: bool,
    install_hotspot: bool,
    lan_ports=None,
):
    lan_ports = lan_ports or []
    lines = [
        "# =========================================================",
        "# CORE-WISP - NEW MIKROTIK INSTALLATION",
        f"# Model: {model or 'Unknown'}",
        f"# RouterOS: {routeros or 'Unknown'}",
        f"# WAN: {wan}",
        "# IMPORTANT: Review before applying.",
        "# No router password is stored in this script.",
        "# =========================================================",
        "",
        "/system backup save name=corewisp-before-install",
        "/export file=corewisp-before-install",
        "",
        (
            f'/ip dhcp-client add interface={wan} disabled=no comment="CORE-WISP'
            ' WAN DHCP"'
        ),
        "",
        (
            "/interface bridge add name=bridge-hotspot comment=\"CORE-WISP"
            ' Hotspot Bridge"'
        ),
    ]

    for port in lan_ports:
        lines.append(
            f"/interface bridge port add bridge=bridge-hotspot interface={port}"
        )

    lines += [
        "",
        (
            f"/ip address add address={ip_cidr} interface=bridge-hotspot"
            ' comment="CORE-WISP Gateway"'
        ),
    ]

    if install_dns:
        lines += [
            "",
            "/ip dns set allow-remote-requests=yes servers=8.8.8.8,1.1.1.1",
        ]

    if install_nat:
        lines += [
            "",
            (
                f"/ip firewall nat add chain=srcnat out-interface={wan}"
                ' action=masquerade comment="CORE-WISP Internet NAT"'
            ),
        ]

    if install_dhcp:
        lines += [
            "",
            f"/ip pool add name=hs-pool-core ranges={pool_range}",
            (
                "/ip dhcp-server add name=dhcp-hotspot interface=bridge-hotspot"
                " address-pool=hs-pool-core lease-time=30m disabled=no"
            ),
            (
                "/ip dhcp-server network add address=10.10.10.0/24"
                " gateway=10.10.10.1 dns-server=10.10.10.1"
            ),
        ]

    if install_hotspot:
        lines += [
            "",
            (
                "/ip hotspot profile add name=hsprof-core"
                f" hotspot-address=10.10.10.1 dns-name={dns_name}"
                " html-directory=hotspot login-by=http-chap,http-pap"
            ),
            (
                f"/ip hotspot add name={hotspot_name} interface=bridge-hotspot"
                " address-pool=hs-pool-core profile=hsprof-core disabled=no"
            ),
        ]

    lines += [
        "",
        "/ip service set telnet disabled=yes",
        "/ip service set ftp disabled=yes",
        "/ip service set www disabled=yes",
        "/ip service set api disabled=no",
        "",
        "# END CORE-WISP INSTALL",
    ]
    return "\n".join(lines)


def _installer_apply(
    host: str,
    port: int,
    username: str,
    password: str,
    wan: str,
    ip_cidr: str,
    pool_range: str,
    dns_name: str,
    hotspot_name: str,
    install_nat: bool,
    install_dhcp: bool,
    install_dns: bool,
    install_hotspot: bool,
):
    api = None
    changes = []
    warnings = []

    def add_once(path_obj, finder, **kwargs):
        try:
            existing = list(path_obj)
            if any(finder(x) for x in existing):
                return False
            path_obj.add(**kwargs)
            return True
        except Exception as exc:
            warnings.append(str(exc))
            return False

    try:
        api = _installer_connection(host, port, username, password)

        try:
            api.path("system", "backup").save(name="corewisp-before-install")
            changes.append("Backup created: corewisp-before-install")
        except Exception as exc:
            warnings.append(f"Backup failed: {exc}")

        interfaces = _installer_read_interfaces(api)
        ethernet_ports = [
            x["name"]
            for x in interfaces
            if x.get("name", "").lower().startswith("ether")
            and x.get("name") != wan
            and not x.get("disabled", False)
        ]

        bridge_path = api.path("interface", "bridge")
        bridge_created = add_once(
            bridge_path,
            lambda x: x.get("name") == "bridge-hotspot",
            name="bridge-hotspot",
            comment="CORE-WISP Hotspot Bridge",
        )
        if bridge_created:
            changes.append("Created bridge-hotspot")

        bridge_port_path = api.path("interface", "bridge", "port")
        for port_name in ethernet_ports:
            if add_once(
                bridge_port_path,
                lambda x, pn=port_name: x.get("bridge") == "bridge-hotspot"
                and x.get("interface") == pn,
                bridge="bridge-hotspot",
                interface=port_name,
            ):
                changes.append(f"Added {port_name} to bridge-hotspot")

        dhcp_client_path = api.path("ip", "dhcp-client")
        if add_once(
            dhcp_client_path,
            lambda x: x.get("interface") == wan,
            interface=wan,
            disabled="no",
            comment="CORE-WISP WAN DHCP",
        ):
            changes.append(f"WAN DHCP client on {wan}")

        ip_interface = ipaddress.ip_interface(ip_cidr)
        network = ip_interface.network
        gateway_ip = str(ip_interface.ip)

        address_path = api.path("ip", "address")
        if add_once(
            address_path,
            lambda x: x.get("interface") == "bridge-hotspot"
            and str(x.get("address", "")).split("/")[0] == gateway_ip,
            address=ip_cidr,
            interface="bridge-hotspot",
            comment="CORE-WISP Gateway",
        ):
            changes.append(f"Gateway address {ip_cidr}")

        if install_dns:
            try:
                api.path("ip", "dns").set(
                    **{
                        "allow-remote-requests": "yes",
                        "servers": "8.8.8.8,1.1.1.1",
                    }
                )
                changes.append("DNS configured")
            except Exception as exc:
                warnings.append(f"DNS configuration failed: {exc}")

        if install_nat:
            nat_path = api.path("ip", "firewall", "nat")
            if add_once(
                nat_path,
                lambda x: x.get("chain") == "srcnat"
                and x.get("action") == "masquerade"
                and x.get("out-interface") == wan,
                chain="srcnat",
                **{"out-interface": wan},
                action="masquerade",
                comment="CORE-WISP Internet NAT",
            ):
                changes.append("Internet NAT configured")

        if install_dhcp:
            pool_path = api.path("ip", "pool")
            pool_created = add_once(
                pool_path,
                lambda x: x.get("name") == "hs-pool-core",
                name="hs-pool-core",
                ranges=pool_range,
            )
            if pool_created:
                changes.append(f"DHCP pool {pool_range}")

            dhcp_server_path = api.path("ip", "dhcp-server")
            if add_once(
                dhcp_server_path,
                lambda x: x.get("name") == "dhcp-hotspot",
                name="dhcp-hotspot",
                interface="bridge-hotspot",
                **{
                    "address-pool": "hs-pool-core",
                    "lease-time": "30m",
                    "disabled": "no",
                },
            ):
                changes.append("DHCP server created")

            network_path = api.path("ip", "dhcp-server", "network")
            network_address = str(network)
            if add_once(
                network_path,
                lambda x: x.get("address") == network_address,
                address=network_address,
                gateway=gateway_ip,
                **{"dns-server": gateway_ip},
            ):
                changes.append(f"DHCP network {network_address}")

        if install_hotspot:
            profile_path = api.path("ip", "hotspot", "profile")
            if add_once(
                profile_path,
                lambda x: x.get("name") == "hsprof-core",
                name="hsprof-core",
                **{
                    "hotspot-address": gateway_ip,
                    "dns-name": dns_name,
                    "html-directory": "hotspot",
                    "login-by": "http-chap,http-pap",
                },
            ):
                changes.append("Hotspot profile created")

            hotspot_path = api.path("ip", "hotspot")
            if add_once(
                hotspot_path,
                lambda x: x.get("name") == hotspot_name,
                name=hotspot_name,
                interface="bridge-hotspot",
                **{
                    "address-pool": "hs-pool-core",
                    "profile": "hsprof-core",
                    "disabled": "no",
                },
            ):
                changes.append(f"Hotspot {hotspot_name} created")

        try:
            services = api.path("ip", "service")
            service_rows = list(services)
            for service_name in ("telnet", "ftp", "www"):
                row = next(
                    (x for x in service_rows if x.get("name") == service_name),
                    None,
                )
                if row and row.get("disabled") != "true":
                    services.set(**{".id": row[".id"], "disabled": "yes"})
            changes.append("Disabled telnet/ftp/www services")
        except Exception as exc:
            warnings.append(f"Service hardening warning: {exc}")

        return {
            "success": True,
            "message": "MikroTik installation imekamilika.",
            "changes": changes,
            "warnings": warnings,
            "wan": wan,
            "gateway": gateway_ip,
            "network": str(network),
            "ethernet_ports_used": ethernet_ports,
        }

    except Exception as exc:
        logger.error(f"MikroTik installer error: {exc}")
        return {
            "success": False,
            "message": f"Installation failed: {str(exc)}",
            "changes": changes,
            "warnings": warnings,
        }
    finally:
        if api:
            try:
                api.close()
            except Exception:
                pass


@app.get("/mikrotik-installer")
@login_required
def mikrotik_installer_page(request: Request):
    guard = require_admin(request)
    if guard:
        return guard
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/mikrotik/detect")
@login_required
async def mikrotik_detect(request: Request):
    guard = require_admin(request)
    if guard:
        return guard
    try:
        data = await request.json()
        result = _installer_detect(
            host=str(data.get("host", "")).strip(),
            port=int(data.get("port") or 8728),
            username=str(data.get("username", "")).strip(),
            password=str(data.get("password", "")),
        )
        return JSONResponse(result, status_code=200)
    except Exception as exc:
        logger.error(f"MikroTik detection failed: {exc}")
        return JSONResponse(
            {
                "success": False,
                "message": f"Imeshindwa ku-detect router: {str(exc)}",
            },
            status_code=400,
        )


@app.post("/mikrotik/install")
@login_required
async def mikrotik_install(request: Request):
    guard = require_admin(request)
    if guard:
        return guard
    try:
        data = await request.json()

        host = str(data.get("host", "")).strip()
        port = int(data.get("port") or 8728)
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))

        wan = str(data.get("wan") or "ether1").strip()
        ip_cidr = str(data.get("ip_cidr") or "10.10.10.1/24").strip()
        pool_range = str(
            data.get("pool_range") or "10.10.10.10-10.10.10.254"
        ).strip()
        dns_name = str(data.get("dns_name") or "vicentwifi.local").strip()
        hotspot_name = str(data.get("hotspot_name") or "hotspot1").strip()

        install_nat = bool(data.get("install_nat", True))
        install_dhcp = bool(data.get("install_dhcp", True))
        install_dns = bool(data.get("install_dns", True))
        install_hotspot = bool(data.get("install_hotspot", True))

        ipaddress.ip_interface(ip_cidr)

        if not re.match(r"^[A-Za-z0-9_.:-]+$", wan):
            raise ValueError("WAN interface si sahihi.")
        if not re.match(r"^[A-Za-z0-9_.:-]+$", hotspot_name):
            raise ValueError("Hotspot name si sahihi.")
        if not re.match(r"^[A-Za-z0-9_.:-]+$", dns_name):
            raise ValueError("DNS name si sahihi.")

        detected = _installer_detect(host, port, username, password)
        interfaces = detected.get("interfaces", [])
        valid_interfaces = {x.get("name") for x in interfaces}
        if valid_interfaces and wan not in valid_interfaces:
            raise ValueError(
                f"WAN interface '{wan}' haipo kwenye router. Interfaces:"
                f" {', '.join(sorted(valid_interfaces))}"
            )

        result = _installer_apply(
            host=host,
            port=port,
            username=username,
            password=password,
            wan=wan,
            ip_cidr=ip_cidr,
            pool_range=pool_range,
            dns_name=dns_name,
            hotspot_name=hotspot_name,
            install_nat=install_nat,
            install_dhcp=install_dhcp,
            install_dns=install_dns,
            install_hotspot=install_hotspot,
        )

        result.update({
            "model": detected.get("model"),
            "routeros": detected.get("routeros"),
            "architecture": detected.get("architecture"),
            "serial": detected.get("serial"),
        })
        return JSONResponse(
            result, status_code=200 if result.get("success") else 400
        )

    except Exception as exc:
        logger.error(f"MikroTik installation request failed: {exc}")
        return JSONResponse(
            {"success": False, "message": f"Kosa la installation: {str(exc)}"},
            status_code=400,
        )


@app.post("/mikrotik/generate-script")
@login_required
async def mikrotik_generate_script(request: Request):
    guard = require_admin(request)
    if guard:
        return guard
    try:
        data = await request.json()
        host = str(data.get("host", "")).strip()
        port = int(data.get("port") or 8728)
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))

        detected = _installer_detect(host, port, username, password)
        interfaces = detected.get("interfaces", [])
        ethernet = [
            x.get("name")
            for x in interfaces
            if x.get("name", "").lower().startswith("ether")
            and x.get("name") != str(data.get("wan") or "ether1")
        ]

        script = _installer_script(
            model=detected.get("model", "Unknown"),
            routeros=detected.get("routeros", "Unknown"),
            wan=str(data.get("wan") or "ether1"),
            ip_cidr=str(data.get("ip_cidr") or "10.10.10.1/24"),
            pool_range=str(data.get("pool_range") or "10.10.10.10-10.10.10.254"),
            dns_name=str(data.get("dns_name") or "vicentwifi.local"),
            hotspot_name=str(data.get("hotspot_name") or "hotspot1"),
            install_nat=bool(data.get("install_nat", True)),
            install_dhcp=bool(data.get("install_dhcp", True)),
            install_dns=bool(data.get("install_dns", True)),
            install_hotspot=bool(data.get("install_hotspot", True)),
            lan_ports=ethernet,
        )
        return JSONResponse({
            "success": True,
            "script": script,
            "model": detected.get("model"),
            "routeros": detected.get("routeros"),
            "architecture": detected.get("architecture"),
            "serial": detected.get("serial"),
            "interfaces": interfaces,
        })
    except Exception as exc:
        logger.error(f"MikroTik script generation failed: {exc}")
        return JSONResponse(
            {
                "success": False,
                "message": f"Script generation failed: {str(exc)}",
            },
            status_code=400,
        )


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
            "mikrotik_host": mikrotik_config.HOST,
        },
    )


@app.post("/save-settings")
@login_required
def save_settings(request: Request):
    guard = require_admin(request)
    if guard:
        return guard
    return RedirectResponse("/settings", status_code=303)
