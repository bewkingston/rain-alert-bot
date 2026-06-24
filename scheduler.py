"""
scheduler.py — Rain Alert Bot
APScheduler: ตรวจฝนทุก 10 นาที และ push แจ้งเตือน
"""

import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import SessionLocal, UserLocation, User
from line_handler import push_rain_alert

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Bangkok")


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
    scheduler.start()
    logger.info("✅ Scheduler started — every 10 minutes")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
