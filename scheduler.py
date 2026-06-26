"""
scheduler.py — Rain Alert Bot
Auto-push แจ้งเตือนฝนล่วงหน้า 1 ชั่วโมง
- ทุก 10 นาที ตรวจ Tomorrow.io สำหรับผู้ใช้ทุกคนที่มีตำแหน่ง
- ส่ง push เฉพาะเมื่อจะมีฝนจริง และพ้น cooldown แล้ว
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Bangkok")


async def auto_rain_alert():
    """ตรวจฝนล่วงหน้า 1 ชม. สำหรับผู้ใช้ทุกคน — ส่งเฉพาะที่จะตกจริง"""
    from database import (SessionLocal, User, UserLocation, AlertLog,
                          get_minutes_since_last_alert)
    from weather import get_tomorrow_forecast
    from line_handler import push_rain_alert

    db = SessionLocal()
    try:
        users = (db.query(User)
                 .filter(User.is_active == True, User.alert_enabled == True)
                 .all())

        for user in users:
            loc = (db.query(UserLocation)
                   .filter(UserLocation.line_user_id == user.line_user_id,
                           UserLocation.is_primary == True)
                   .first())
            if not loc:
                continue

            # เช็ค cooldown — ไม่ส่งซ้ำถ้ายังไม่พ้นเวลาที่กำหนด
            mins_since = get_minutes_since_last_alert(db, user.line_user_id)
            if mins_since < user.alert_cooldown:
                logger.debug(f"Skip {user.line_user_id}: cooldown {mins_since}/{user.alert_cooldown} min")
                continue

            # ตรวจฝนใน 60 นาทีข้างหน้า
            forecast = await get_tomorrow_forecast(loc.latitude, loc.longitude)
            if not forecast or not forecast.will_rain:
                continue

            # แจ้งเฉพาะ moderate ขึ้นไป (≥2.5 mm/hr) — ฝนเล็กน้อยไม่แจ้ง
            if forecast.intensity not in ("moderate", "heavy", "violent"):
                logger.debug(f"Skip {user.line_user_id}: intensity={forecast.intensity} (too light)")
                continue

            # มีฝน → ส่ง push
            try:
                await push_rain_alert(user.line_user_id, forecast, loc.label)
                db.add(AlertLog(
                    line_user_id    = user.line_user_id,
                    rain_intensity  = forecast.intensity,
                    minutes_to_rain = forecast.minutes_to_rain,
                    source          = forecast.source,
                    message_sent    = f"{forecast.emoji} {forecast.intensity_th}",
                    sent_at         = datetime.now(timezone.utc),
                ))
                db.commit()
                logger.info(f"✅ Pushed alert → {user.line_user_id}: {forecast.intensity_th}")
            except Exception as e:
                logger.error(f"Push failed for {user.line_user_id}: {e}")

    except Exception as e:
        logger.error(f"auto_rain_alert error: {e}")
    finally:
        db.close()


def start_scheduler():
    if scheduler.running:
        return
    scheduler.add_job(
        auto_rain_alert,
        trigger="interval",
        minutes=10,
        id="auto_rain_alert",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("✅ Scheduler started — auto rain alert every 10 min")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
