"""
database.py — Rain Alert Bot
SQLAlchemy models: User, UserLocation, AlertLog
"""

from sqlalchemy import (
    create_engine, Column, String, Float, Boolean,
    DateTime, Integer, Text
)
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timezone
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./rain_alert.db")

# Railway/Render ส่ง postgres:// แต่ SQLAlchemy ต้องการ postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    line_user_id   = Column(String(64), primary_key=True)
    display_name   = Column(String(128), nullable=True)
    is_active      = Column(Boolean, default=True)
    alert_enabled  = Column(Boolean, default=True)
    alert_cooldown = Column(Integer, default=5)  # นาที (ลดจาก 30)
    alert_start_hour = Column(Integer, default=6)   # เริ่มแจ้งเตือนตั้งแต่เวลาไหน (0-23)
    alert_end_hour   = Column(Integer, default=22)  # หยุดแจ้งเตือนเวลาไหน (0-23)
    created_at     = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at     = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                            onupdate=lambda: datetime.now(timezone.utc))

    # ── Commute Alert ──────────────────────────────
    commute_enabled       = Column(Boolean, default=False)
    home_name             = Column(String(128), nullable=True)
    home_lat              = Column(Float, nullable=True)
    home_lon              = Column(Float, nullable=True)
    work_name             = Column(String(128), nullable=True)
    work_lat              = Column(Float, nullable=True)
    work_lon              = Column(Float, nullable=True)
    morning_departure     = Column(String(5), nullable=True)   # "08:00"
    evening_departure     = Column(String(5), nullable=True)   # "18:00"
    commute_morning_sent  = Column(DateTime, nullable=True)    # ป้องกันส่งซ้ำ
    commute_evening_sent  = Column(DateTime, nullable=True)


class UserLocation(Base):
    __tablename__ = "user_locations"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    line_user_id = Column(String(64), nullable=False, index=True)
    label        = Column(String(64), default="ตำแหน่งปัจจุบัน")
    latitude     = Column(Float, nullable=False)
    longitude    = Column(Float, nullable=False)
    is_primary   = Column(Boolean, default=True)
    updated_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                          onupdate=lambda: datetime.now(timezone.utc))


class AlertLog(Base):
    __tablename__ = "alert_logs"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    line_user_id    = Column(String(64), nullable=False, index=True)
    rain_intensity  = Column(String(32))
    minutes_to_rain = Column(Integer)
    source          = Column(String(32))
    message_sent    = Column(Text)
    sent_at         = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    user_feedback   = Column(String(16), nullable=True)   # 'rain' | 'no_rain'
    feedback_at     = Column(DateTime, nullable=True)


class Feedback(Base):
    """ข้อเสนอแนะ/ติชมจากผู้ใช้ — พิมพ์ 'ติชม <ข้อความ>' ใน LINE"""
    __tablename__ = "feedbacks"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    line_user_id = Column(String(64), nullable=False, index=True)
    message      = Column(Text, nullable=False)
    created_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def init_db():
    Base.metadata.create_all(bind=engine)


def get_or_create_user(db, line_user_id: str, display_name: str = None) -> User:
    user = db.query(User).filter(User.line_user_id == line_user_id).first()
    if not user:
        user = User(line_user_id=line_user_id, display_name=display_name)
        db.add(user)
        db.commit()
        db.refresh(user)
    elif display_name and user.display_name != display_name:
        user.display_name = display_name
        db.commit()
    return user


def upsert_location(db, line_user_id: str, lat: float, lon: float, label: str = "ตำแหน่งปัจจุบัน"):
    loc = (
        db.query(UserLocation)
        .filter(UserLocation.line_user_id == line_user_id, UserLocation.is_primary == True)
        .first()
    )
    if loc:
        loc.latitude   = lat
        loc.longitude  = lon
        loc.label      = label
        loc.updated_at = datetime.now(timezone.utc)
    else:
        loc = UserLocation(line_user_id=line_user_id, latitude=lat, longitude=lon,
                           label=label, is_primary=True)
        db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc


def get_minutes_since_last_alert(db, line_user_id: str) -> int:
    last = (
        db.query(AlertLog)
        .filter(AlertLog.line_user_id == line_user_id)
        .order_by(AlertLog.sent_at.desc())
        .first()
    )
    if not last:
        return 9999
    sent = last.sent_at
    # ทำให้ timezone-aware ถ้ายังเป็น naive
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - sent
    return int(delta.total_seconds() / 60)
