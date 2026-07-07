"""
scheduler.py — Rain Alert Bot
Auto-push แจ้งเตือนฝนล่วงหน้า 1 ชั่วโมง
- ทุก 5 นาที ตรวจ Tomorrow.io สำหรับผู้ใช้ทุกคนที่มีตำแหน่ง
- ส่ง push เฉพาะเมื่อจะมีฝนจริง และพ้น cooldown แล้ว และในช่วงเวลาที่อนุญาต

+ สรุปอากาศเช้า — ทุกวัน 07:00 น. ส่งให้ทุก user ที่ active โดยไม่เช็คว่าฝนจะตกหรือไม่
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Bangkok")

# สถานะ "ฝนกำลังตก/กำลังมา" ต่อ user — กันแจ้งซ้ำทุก 5 นาทีระหว่างฝนเหตุการณ์เดียวกัน
# เคลียร์เมื่อรอบเช็คพบว่าไม่มีฝนแล้ว | แจ้งซ้ำได้เฉพาะกรณีฝนแรงขึ้น (escalation)
_rain_state: dict = {}   # uid -> {"intensity": str}
_SEVERITY = {"none": 0, "light": 1, "moderate": 2, "heavy": 3, "violent": 4}

THAI_TZ = timezone(timedelta(hours=7))


async def auto_rain_alert():
    """ตรวจฝนล่วงหน้า 1 ชม. สำหรับผู้ใช้ทุกคน — ส่งเฉพาะที่จะตกจริง และในช่วงเวลาที่อนุญาต"""
    from database import (SessionLocal, User, UserLocation, AlertLog,
                          get_minutes_since_last_alert)
    from weather import get_rain_forecast
    from line_handler import push_rain_alert

    db = SessionLocal()
    try:
        users = (db.query(User)
                 .filter(User.is_active == True, User.alert_enabled == True)
                 .all())

        # ตรวจสอบเวลาไทยปัจจุบัน
        thai_now = datetime.now(THAI_TZ)
        current_hour = thai_now.hour

        for user in users:
            # เช็คช่วงเวลาที่อนุญาต (ตามตั้งค่าของผู้ใช้)
            if not (user.alert_start_hour <= current_hour < user.alert_end_hour):
                logger.debug(f"Skip {user.line_user_id}: outside alert hours {user.alert_start_hour}:00-{user.alert_end_hour}:00 (now {current_hour}:00)")
                continue

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
            forecast = await get_rain_forecast(loc.latitude, loc.longitude)
            if not forecast or not forecast.will_rain or forecast.intensity == "none":
                # ไม่มีฝนแล้ว → เคลียร์สถานะ เหตุการณ์ฝนรอบนี้จบ แจ้งใหม่ได้เมื่อฝนรอบหน้ามา
                if _rain_state.pop(user.line_user_id, None):
                    logger.info(f"Rain ended for {user.line_user_id} — state cleared")
                continue

            # ── กันแจ้งซ้ำ: ถ้าแจ้งเหตุการณ์ฝนรอบนี้ไปแล้ว ให้เงียบจนกว่าฝนหยุด ──
            state = _rain_state.get(user.line_user_id)
            if state is None and mins_since < 90:
                # หลัง restart (state ใน memory หาย): ถ้า alert ล่าสุด <90 นาที
                # ถือว่าเป็นฝนเหตุการณ์เดิม กันสแปมหลังบอทรีสตาร์ท
                last = (db.query(AlertLog)
                        .filter(AlertLog.line_user_id == user.line_user_id)
                        .order_by(AlertLog.sent_at.desc())
                        .first())
                if last:
                    state = {"intensity": last.rain_intensity or "light"}
                    _rain_state[user.line_user_id] = state

            if state is not None and mins_since > 180:
                # alert ล่าสุดนานเกิน 3 ชม. → ถือเป็นฝนรอบใหม่ (กัน state ค้างข้ามคืน)
                _rain_state.pop(user.line_user_id, None)
                state = None

            if state is not None:
                if _SEVERITY.get(forecast.intensity, 0) > _SEVERITY.get(state["intensity"], 0):
                    logger.info(f"Escalation for {user.line_user_id}: {state['intensity']} → {forecast.intensity}")
                    # ฝนแรงขึ้น → แจ้งซ้ำได้
                else:
                    logger.debug(f"Skip {user.line_user_id}: already alerted this rain event")
                    continue

            # มีฝน → บันทึก log ก่อน แล้วส่ง push (เพื่อให้ได้ log.id สำหรับปุ่ม feedback)
            try:
                log = AlertLog(
                    line_user_id    = user.line_user_id,
                    rain_intensity  = forecast.intensity,
                    minutes_to_rain = forecast.minutes_to_rain,
                    source          = forecast.source,
                    message_sent    = f"{forecast.emoji} {forecast.intensity_th}",
                    sent_at         = datetime.now(timezone.utc),
                )
                db.add(log)
                db.commit()
                db.refresh(log)
                await push_rain_alert(user.line_user_id, forecast, loc.label, alert_log_id=log.id)
                _rain_state[user.line_user_id] = {"intensity": forecast.intensity}
                logger.info(f"✅ Pushed alert → {user.line_user_id}: {forecast.intensity_th}")
            except Exception as e:
                logger.error(f"Push failed for {user.line_user_id}: {e}")

    except Exception as e:
        logger.error(f"auto_rain_alert error: {e}")
    finally:
        db.close()


async def daily_weather_summary():
    """สรุปสภาพอากาศตอน 07:00 น. — ส่งให้ทุก user ที่ active ทุกวัน ไม่ว่าฝนจะตกหรือไม่"""
    from database import SessionLocal, User, UserLocation
    from weather import get_rain_forecast
    from line_handler import push_daily_weather

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

            try:
                forecast = await get_rain_forecast(loc.latitude, loc.longitude)
                if not forecast:
                    logger.warning(f"Skip daily weather for {user.line_user_id}: no forecast")
                    continue
                await push_daily_weather(user.line_user_id, forecast, loc.label)
                logger.info(f"✅ Daily weather sent → {user.line_user_id}: {forecast.intensity_th}")
            except Exception as e:
                logger.error(f"Daily weather push failed for {user.line_user_id}: {e}")

    except Exception as e:
        logger.error(f"daily_weather_summary error: {e}")
    finally:
        db.close()


def start_scheduler():
    if scheduler.running:
        return
    scheduler.add_job(
        auto_rain_alert,
        trigger="interval",
        minutes=5,
        id="auto_rain_alert",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        daily_weather_summary,
        trigger="cron",
        hour=7,
        minute=0,
        id="daily_weather_summary",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("✅ Scheduler started — auto rain alert every 5 min + daily weather summary at 07:00")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
