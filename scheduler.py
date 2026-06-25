"""
scheduler.py — Rain Alert Bot
APScheduler:
  - ตรวจฝนทุก 10 นาที และ push rain alert
  - ตรวจ commute alert ทุก 5 นาที (ส่งก่อนออกเดินทาง 60 นาที)
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import SessionLocal, UserLocation, User
from line_handler import push_rain_alert, push_commute_alert

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Bangkok")

_THAI_TZ = timezone(timedelta(hours=7))


# ─────────────────────────────────────────────
#  Rain Alert (ทุก 10 นาที)
# ─────────────────────────────────────────────

async def check_rain_for_all_users():
    db = SessionLocal()
    try:
        active_ids = [
            u.line_user_id for u in
            db.query(User)
            .filter(User.is_active == True, User.alert_enabled == True)
            .all()
        ]
        if not active_ids:
            return

        locations = (
            db.query(UserLocation)
            .filter(UserLocation.line_user_id.in_(active_ids),
                    UserLocation.is_primary == True)
            .all()
        )
    finally:
        db.close()

    if not locations:
        return

    logger.info(f"🔍 Checking rain for {len(locations)} user(s)")
    await asyncio.gather(
        *[push_rain_alert(loc.line_user_id, loc.latitude, loc.longitude)
          for loc in locations],
        return_exceptions=True,
    )


# ─────────────────────────────────────────────
#  Commute Alert (ทุก 5 นาที)
# ─────────────────────────────────────────────

async def check_commute_alerts():
    """ส่ง commute alert ให้ผู้ใช้ที่ตั้งค่าไว้
    เมื่อเวลาถึง (departure - 60 นาที) ±5 นาที"""
    now = datetime.now(_THAI_TZ)
    now_min = now.hour * 60 + now.minute

    db = SessionLocal()
    try:
        users = (
            db.query(User)
            .filter(User.is_active == True, User.commute_enabled == True)
            .all()
        )
        if not users:
            return

        tasks = []
        for user in users:
            uid = user.line_user_id

            # ── Morning ────────────────────────────────────────
            if user.morning_departure:
                dep_min   = _time_to_min(user.morning_departure)
                alert_min = dep_min - 60
                if _should_send(now_min, alert_min):
                    last = user.commute_morning_sent
                    if not last or _hours_ago(last, now) >= 20:
                        logger.info(f"📢 Morning commute → {uid}")
                        user.commute_morning_sent = now.astimezone(timezone.utc)
                        tasks.append(push_commute_alert(uid, "morning"))

            # ── Evening ────────────────────────────────────────
            if user.evening_departure:
                dep_min   = _time_to_min(user.evening_departure)
                alert_min = dep_min - 60
                if _should_send(now_min, alert_min):
                    last = user.commute_evening_sent
                    if not last or _hours_ago(last, now) >= 20:
                        logger.info(f"📢 Evening commute → {uid}")
                        user.commute_evening_sent = now.astimezone(timezone.utc)
                        tasks.append(push_commute_alert(uid, "evening"))

        if tasks:
            db.commit()
    finally:
        db.close()

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _time_to_min(t: str) -> int:
    """'08:00' → 480"""
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _should_send(now_min: int, alert_min: int, window: int = 5) -> bool:
    """True ถ้า now_min อยู่ใน [alert_min - window, alert_min + window]"""
    return abs(now_min - alert_min) <= window


def _hours_ago(dt: datetime, now: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    return (now_utc - dt).total_seconds() / 3600


# ─────────────────────────────────────────────
#  Start / Stop
# ─────────────────────────────────────────────

def start_scheduler():
    if scheduler.running:
        return

    scheduler.add_job(
        check_rain_for_all_users,
        trigger=IntervalTrigger(minutes=10),
        id="check_rain",
        replace_existing=True,
        misfire_grace_time=60,
    )

    scheduler.add_job(
        check_commute_alerts,
        trigger=IntervalTrigger(minutes=5),
        id="check_commute",
        replace_existing=True,
        misfire_grace_time=60,
    )

    scheduler.start()
    logger.info("✅ Scheduler started — rain:10min  commute:5min")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
