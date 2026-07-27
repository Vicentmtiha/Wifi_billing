from sqlalchemy import Column, Integer, String, DateTime
from database import Base
from datetime import datetime


# ================= VOUCHER =================
class Voucher(Base):
    __tablename__ = "vouchers"

    id = Column(Integer, primary_key=True, index=True)

    code = Column(String, unique=True, index=True)

    profile = Column(String)
    price = Column(Integer)
    uptime = Column(String)
    data_limit = Column(String)

    status = Column(String, default="unused")
    used_by = Column(String, default="")

    created_at = Column(DateTime, default=datetime.now)

    # 🔥 IMPORTANT: expiry field (THIS FIXES YOUR ERROR)
    expires_at = Column(DateTime, nullable=True)


# ================= PACKAGE =================
class Package(Base):
    __tablename__ = "packages"

    id = Column(Integer, primary_key=True, index=True)

    name = Column(String)
    price = Column(Integer)
    uptime = Column(String)
    data_limit = Column(String)


# ================= USER =================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    username = Column(String, unique=True)
    password = Column(String)

    role = Column(String, default="staff")
    status = Column(String, default="active")
