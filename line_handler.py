"""
line_handler.py — Rain Alert Bot
✅ FIX: ใช้ WebhookParser + async handlers แทน WebhookHandler (sync)
       เพื่อให้ทำงานได้ถูกต้องใน FastAPI async context
"""

import os
import re
import logging
from datetime import datetime, timezone, timedelta

from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    AsyncApiClient, AsyncMessagingApi, Configuration,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, FlexMessage, FlexContainer,
)
from linebot.v3.webhooks import (
    MessageEvent, FollowEvent, UnfollowEvent,
    LocationMessageContent, TextMessageContent,
)

from database import (
    SessionLocal, get_or_create_user, upsert_location,
    AlertLog, UserLocation, User, get_minutes_since_last_alert,
)
from weather import get_rain_forecast, get_rain_forecast_at_time, build_recommendation
from geocoding import geocode

logger = logging.getLogger(__name__)

LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LIFF_ID                   = os.getenv("LIFF_ID", "")
RENDER_URL                = os.getenv("RENDER_EXTERNAL_URL", "https://rain-alert-bot-a34m.onrender.com")

parser        = WebhookParser(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)


# ─────────────────────────────────────────────
#  Entry point (เรียกจาก main.py)
# ─────────────────────────────────────────────

async def handle_events(body: str, signature: str):
    """Parse และจัดการ events ทั้งหมดแบบ async"""
    events = parser.parse(body, signature)   # raises InvalidSignatureError ถ้า signature ผิด
    for event in events:
        if isinstance(event, FollowEvent):
            await _on_follow(event)
        elif isinstance(event, UnfollowEvent):
            _on_unfollow(event)
        elif isinstance(event, MessageEvent):
            if isinstance(event.message, LocationMessageContent):
                await _on_location(event)
            elif isinstance(event.message, TextMessageContent):
                await _on_text(event)


# ─────────────────────────────────────────────
#  Event Handlers
# ─────────────────────────────────────────────

async def _on_follow(event: FollowEvent):
    uid = event.source.user_id
    db = SessionLocal()
    try:
        get_or_create_user(db, uid)
    finally:
        db.close()
    await _reply(event.reply_token, [FlexMessage(
        alt_text="ยินดีต้อนรับสู่ Rain Alert 🌧️",
        contents=_welcome_flex(),
    )])


def _on_unfollow(event: UnfollowEvent):
    uid = event.source.user_id
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.line_user_id == uid).first()
        if user:
            user.is_active = False
            db.commit()
    finally:
        db.close()


async def _on_location(event: MessageEvent):
    uid = event.source.user_id
    lat = event.message.latitude
    lon = event.message.longitude
    db = SessionLocal()
    try:
        get_or_create_user(db, uid)
        upsert_location(db, uid, lat, lon)
    finally:
        db.close()
    forecast = await get_rain_forecast(lat, lon)
    rec = build_recommendation(forecast)
    await _reply(event.reply_token, [FlexMessage(
        alt_text=f"{forecast.emoji} {forecast.intensity_th}",
        contents=_rain_flex(forecast, rec),
    )])


async def _on_text(event: MessageEvent):
    uid  = event.source.user_id
    text = event.message.text.strip()
    text_lower = text.lower()

    # ── ตรวจฝนตอนนี้ ──────────────────────────────
    if text_lower in ["ฝน", "ฝนตกไหม", "ฝนไหม", "rain", "🌧️", "🌧"]:
        await _reply_rain_status(event.reply_token, uid)

    # ── ขอ template ตั้งค่าเดินทาง ─────────────────
    elif text_lower in ["ตั้งค่า", "ตั้งค่าเดินทาง", "commute", "เดินทาง"]:
        await _reply(event.reply_token, [FlexMessage(
            alt_text="ตั้งค่าเส้นทางเดินทาง",
            contents=_commute_setup_template_flex(),
        )])

    # ── ตั้งค่าเส้นทางเดินทาง (กรอกมาพร้อมกัน) ──────
    elif _is_commute_setup(text):
        await _handle_commute_setup(event.reply_token, uid, text)

    # ── ออกช้า X นาที (จากปุ่มใน Flex) ─────────────
    elif re.match(r'ออกช้า\s*(\d+)\s*นาที', text_lower):
        m_delay = re.search(r'ออกช้า\s*(\d+)', text_lower)
        delay_min = int(m_delay.group(1)) if m_delay else 30
        await _reply_delayed_commute(event.reply_token, uid, delay_min)

    # ── ตรวจฝนตามเวลาเดินทาง (เช่น "ออกบ้าน 7.00 ฝนตกไหม") ──
    elif _has_commute_keywords(text_lower) and _parse_time(text) is not None:
        hour, minute = _parse_time(text)
        await _reply_commute_forecast(event.reply_token, uid, hour, minute, text)

    # ── เปิด/ปิดแจ้งเตือน ─────────────────────────
    elif text_lower in ["เปิด", "เปิดแจ้งเตือน", "on"]:
        _set_alert(uid, True)
        await _reply(event.reply_token, [TextMessage(text="✅ เปิดการแจ้งเตือนฝนแล้วครับ")])

    elif text_lower in ["ปิด", "ปิดแจ้งเตือน", "off"]:
        _set_alert(uid, False)
        await _reply(event.reply_token, [TextMessage(
            text="🔕 ปิดการแจ้งเตือนแล้วครับ\nพิมพ์ 'เปิด' เมื่อต้องการเปิดอีกครั้ง"
        )])

    else:
        await _reply(event.reply_token, [FlexMessage(
            alt_text="🌧️ Rain Alert — คำสั่งที่ใช้ได้",
            contents=_help_flex(),
        )])


async def _reply_rain_status(reply_token: str, uid: str):
    db = SessionLocal()
    try:
        loc = (db.query(UserLocation)
               .filter(UserLocation.line_user_id == uid, UserLocation.is_primary == True)
               .first())
    finally:
        db.close()

    if not loc:
        await _reply(reply_token, [TextMessage(
            text="📍 ยังไม่มีตำแหน่งของคุณครับ\nกด ＋ → ตำแหน่ง → ส่งตำแหน่งปัจจุบันมาก่อนนะครับ"
        )])
        return

    forecast = await get_rain_forecast(loc.latitude, loc.longitude)
    rec = build_recommendation(forecast)
    await _reply(reply_token, [FlexMessage(
        alt_text=f"{forecast.emoji} {forecast.intensity_th}",
        contents=_rain_flex(forecast, rec),
    )])


# ─────────────────────────────────────────────
#  Push Alert (เรียกจาก scheduler)
# ─────────────────────────────────────────────

async def push_rain_alert(uid: str, lat: float, lon: float):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.line_user_id == uid).first()
        if not user or not user.alert_enabled or not user.is_active:
            return

        if get_minutes_since_last_alert(db, uid) < user.alert_cooldown:
            return

        forecast = await get_rain_forecast(lat, lon)
        if not forecast.will_rain:
            return

        rec     = build_recommendation(forecast)
        alt     = f"{forecast.emoji} ฝนกำลังมา! — {forecast.intensity_th}"
        flex    = _rain_flex(forecast, rec)

        async with AsyncApiClient(configuration) as api_client:
            await AsyncMessagingApi(api_client).push_message(PushMessageRequest(
                to=uid,
                messages=[FlexMessage(alt_text=alt, contents=flex)],
            ))

        db.add(AlertLog(
            line_user_id=uid,
            rain_intensity=forecast.intensity,
            minutes_to_rain=forecast.minutes_to_rain,
            source=forecast.source,
            message_sent=alt,
        ))
        db.commit()
        logger.info(f"✅ Pushed alert to {uid} | {forecast.intensity}")

    except Exception as e:
        logger.error(f"push_rain_alert error {uid}: {e}")
    finally:
        db.close()


# ─────────────────────────────────────────────
#  Commute Setup
# ─────────────────────────────────────────────

def _is_commute_setup(text: str) -> bool:
    """ตรวจว่าเป็นข้อความตั้งค่าเส้นทาง เช่น 'บ้าน: บางพลี ที่ทำงาน: อโศก'"""
    has_home = bool(re.search(r'บ้าน\s*[:：]', text))
    has_work = bool(re.search(r'ทำงาน\s*[:：]|ออฟ\s*[:：]|งาน\s*[:：]', text))
    return has_home and has_work


async def _handle_commute_setup(reply_token: str, uid: str, text: str):
    """Parse + geocode + บันทึก commute profile"""
    home_m = re.search(r'บ้าน\s*[:：]\s*(.+)', text)
    work_m = re.search(r'(?:ที่ทำงาน|ออฟ|งาน)\s*[:：]\s*(.+)', text)
    morn_m = re.search(r'(?:เช้า|ออก(?:จากบ้าน)?)\s*[:：]\s*(\d{1,2}[.:h]\d{2})', text)
    eve_m  = re.search(r'(?:เย็น|เลิก(?:งาน)?)\s*[:：]\s*(\d{1,2}[.:h]\d{2})', text)

    if not home_m or not work_m:
        await _reply(reply_token, [TextMessage(text=(
            "📝 รูปแบบที่ถูกต้อง:\n\n"
            "บ้าน: บางพลี\n"
            "ที่ทำงาน: อโศก\n"
            "เช้า: 08:00\n"
            "เย็น: 18:00"
        ))])
        return

    home_raw = home_m.group(1).strip().splitlines()[0].strip()
    work_raw = work_m.group(1).strip().splitlines()[0].strip()
    morning  = _normalise_time(morn_m.group(1)) if morn_m else "08:00"
    evening  = _normalise_time(eve_m.group(1))  if eve_m  else "18:00"

    await _reply(reply_token, [TextMessage(
        text=f"🔍 กำลัง geocode:\n📍 บ้าน: {home_raw}\n🏢 ที่ทำงาน: {work_raw}\nรอสักครู่..."
    )])

    home_geo = await geocode(home_raw)
    work_geo = await geocode(work_raw)

    if not home_geo or not work_geo:
        failed = home_raw if not home_geo else work_raw
        await _push(uid, [TextMessage(text=f"❌ หาตำแหน่ง '{failed}' ไม่เจอ ลองพิมพ์ให้ละเอียดขึ้น เช่น 'บางพลี สมุทรปราการ'")])
        return

    home_lat, home_lon, home_name = home_geo
    work_lat, work_lon, work_name = work_geo

    db = SessionLocal()
    try:
        user = get_or_create_user(db, uid)
        user.home_name         = home_raw
        user.home_lat          = home_lat
        user.home_lon          = home_lon
        user.work_name         = work_raw
        user.work_lat          = work_lat
        user.work_lon          = work_lon
        user.morning_departure = morning
        user.evening_departure = evening
        user.commute_enabled   = True
        db.commit()
    finally:
        db.close()

    await _push(uid, [FlexMessage(
        alt_text="✅ ตั้งค่าเส้นทางเดินทางสำเร็จ",
        contents=_commute_setup_confirm_flex(home_raw, work_raw, morning, evening),
    )])


def _normalise_time(t: str) -> str:
    """'8.30' / '8:30' / '8h30' → '08:30'"""
    m = re.match(r'(\d{1,2})[.:h](\d{2})', t)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return t


# ─────────────────────────────────────────────
#  Commute Push (เรียกจาก scheduler)
# ─────────────────────────────────────────────

async def push_commute_alert(uid: str, trip: str):
    """
    trip = 'morning' (เช็คที่บ้าน ณ เวลาออกเดินทาง)
           'evening' (เช็คที่ทำงาน ณ เวลาเลิกงาน)
    """
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.line_user_id == uid).first()
        if not user or not user.commute_enabled:
            return
        if trip == "morning":
            lat, lon     = user.home_lat, user.home_lon
            place        = user.home_name or "บ้าน"
            dest         = user.work_name or "ที่ทำงาน"
            depart_str   = user.morning_departure or "08:00"
        else:
            lat, lon     = user.work_lat, user.work_lon
            place        = user.work_name or "ที่ทำงาน"
            dest         = user.home_name or "บ้าน"
            depart_str   = user.evening_departure or "18:00"

        if not lat or not lon:
            return
    finally:
        db.close()

    h, m = map(int, depart_str.split(":"))
    forecast = await get_rain_forecast_at_time(lat, lon, h, m)

    thai_tz  = timezone(timedelta(hours=7))
    now_thai = datetime.now(thai_tz)
    alert_label = now_thai.strftime("%H:%M")

    await _push(uid, [FlexMessage(
        alt_text=f"{'🌧️' if forecast.will_rain else '☀️'} แจ้งเตือนเดินทาง{('เช้า' if trip=='morning' else 'เย็น')}นี้",
        contents=_commute_alert_flex(forecast, trip, place, dest, depart_str, alert_label),
    )])


# ─────────────────────────────────────────────
#  Commute Forecast
# ─────────────────────────────────────────────

_COMMUTE_KEYWORDS = ["ออก", "เดินทาง", "ไป", "กลับ", "ถึง", "commute", "เช้า", "เย็น"]

def _has_commute_keywords(text: str) -> bool:
    return any(kw in text for kw in _COMMUTE_KEYWORDS)


def _parse_time(text: str):
    """แยกเวลาจากข้อความ เช่น '7.00', '07:00', '19.30', '7โมง'
    Returns (hour, minute) หรือ None"""
    # รูปแบบ: 7.00 / 07.00 / 7:00 / 19.30
    m = re.search(r'\b(\d{1,2})[.:h](\d{2})\b', text)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return (h, mn)
    # รูปแบบ: 7โมง / 7 โมง / ตี7
    m = re.search(r'ตี\s*(\d{1,2})', text)
    if m:
        h = int(m.group(1))
        if 1 <= h <= 6:
            return (h, 0)  # ตี1-ตี6 = 01:00-06:00
    m = re.search(r'(\d{1,2})\s*โมง', text)
    if m:
        h = int(m.group(1))
        # บ่าย/เย็น/ค่ำ context
        if any(kw in text for kw in ["บ่าย", "เย็น", "ค่ำ"]):
            if 1 <= h <= 6:
                h += 12
        elif 1 <= h <= 6:
            h += 6  # 1โมงเช้า=7, 6โมงเช้า=12
        return (h % 24, 0)
    return None


async def _reply_commute_forecast(reply_token: str, uid: str,
                                   hour: int, minute: int, original_text: str):
    """ตอบกลับด้วย forecast ณ เวลาเดินทางที่ระบุ"""
    db = SessionLocal()
    try:
        loc = (db.query(UserLocation)
               .filter(UserLocation.line_user_id == uid, UserLocation.is_primary == True)
               .first())
    finally:
        db.close()

    time_str = f"{hour:02d}:{minute:02d}"

    if not loc:
        await _reply(reply_token, [TextMessage(
            text=f"📍 ยังไม่มีตำแหน่งของคุณครับ\n"
                 f"กด ＋ → ตำแหน่ง → ส่งตำแหน่งปัจจุบันมาก่อน\n"
                 f"แล้วถามใหม่ว่า 'ออกบ้าน {time_str} ฝนตกไหม' ได้เลยครับ"
        )])
        return

    forecast = await get_rain_forecast_at_time(loc.latitude, loc.longitude, hour, minute)

    # คำนวณว่าเป็นเวลากี่ชั่วโมงข้างหน้า
    thai_tz = timezone(timedelta(hours=7))
    now = datetime.now(thai_tz)
    target = now.replace(hour=hour, minute=minute, second=0)
    if target <= now:
        target = target + timedelta(days=1)
    mins_until = int((target - now).total_seconds() / 60)
    hours_until = mins_until // 60
    mins_rem = mins_until % 60

    if forecast.intensity == "out_of_range":
        await _reply(reply_token, [TextMessage(
            text=f"⏰ {time_str} น. ยังอีก {hours_until} ชม. {mins_rem} นาที\n\n"
                 f"❓ Tomorrow.io ฟรีดูล่วงหน้าได้ถึง 6 ชม. เท่านั้น\n"
                 f"ลองถามใหม่ใกล้เวลาขึ้นนะครับ"
        )])
        return

    time_label = f"อีก {hours_until} ชม. {mins_rem} นาที" if hours_until > 0 else f"อีก {mins_rem} นาที"

    await _reply(reply_token, [FlexMessage(
        alt_text=f"{forecast.emoji} เวลา {time_str} น. — {forecast.intensity_th}",
        contents=_commute_flex(forecast, time_str, time_label),
    )])


async def _reply_delayed_commute(reply_token: str, uid: str, delay_min: int):
    """คำนวณเวลาออกใหม่ = เวลาออกที่ตั้งไว้ + delay_min"""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.line_user_id == uid).first()
        if not user or not user.commute_enabled or not user.morning_departure:
            # fallback: ออกช้าจากปัจจุบัน
            thai_tz = timezone(timedelta(hours=7))
            now = datetime.now(thai_tz)
            new_time = now + timedelta(minutes=delay_min)
            hour, minute = new_time.hour, new_time.minute
            home_lat = user.home_lat if user else None
            home_lon = user.home_lon if user else None
        else:
            h, m = map(int, user.morning_departure.split(":"))
            new_m = h * 60 + m + delay_min
            hour, minute = (new_m // 60) % 24, new_m % 60
            home_lat, home_lon = user.home_lat, user.home_lon
    finally:
        db.close()

    time_str = f"{hour:02d}:{minute:02d}"

    if not home_lat or not home_lon:
        await _reply(reply_token, [TextMessage(
            text=f"📍 ยังไม่มีตำแหน่งบ้านครับ ลองตั้งค่าเดินทางก่อนนะครับ"
        )])
        return

    forecast = await get_rain_forecast_at_time(home_lat, home_lon, hour, minute)

    thai_tz = timezone(timedelta(hours=7))
    now = datetime.now(thai_tz)
    target = now.replace(hour=hour, minute=minute, second=0)
    if target <= now:
        target += timedelta(days=1)
    mins_until = max(0, int((target - now).total_seconds() / 60))
    h_u = mins_until // 60
    m_u = mins_until % 60
    time_label = f"อีก {h_u} ชม. {m_u} นาที" if h_u > 0 else f"อีก {m_u} นาที"

    await _reply(reply_token, [FlexMessage(
        alt_text=f"{'🌧️' if forecast.will_rain else '☀️'} ถ้าออก {time_str} น.",
        contents=_commute_flex(forecast, time_str, time_label),
    )])


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

async def _reply(reply_token: str, messages: list):
    async with AsyncApiClient(configuration) as api_client:
        await AsyncMessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
        )


async def _push(uid: str, messages: list):
    async with AsyncApiClient(configuration) as api_client:
        await AsyncMessagingApi(api_client).push_message(
            PushMessageRequest(to=uid, messages=messages)
        )


def _set_alert(uid: str, enabled: bool):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.line_user_id == uid).first()
        if user:
            user.alert_enabled = enabled
            db.commit()
    finally:
        db.close()


# ─────────────────────────────────────────────
#  Flex Message Builders
# ─────────────────────────────────────────────

def _rain_flex(forecast, rec: str) -> FlexContainer:
    color = {"none": "#4CAF50", "light": "#2196F3",
             "moderate": "#FF9800", "heavy": "#F44336",
             "violent": "#9C27B0"}.get(forecast.intensity, "#2196F3")

    mins_text = (
        "กำลังตกอยู่" if forecast.minutes_to_rain == 0
        else f"ถึงใน {forecast.minutes_to_rain} นาที" if forecast.minutes_to_rain is not None
        else "ใน 1-2 ชั่วโมง"
    )
    now_str = datetime.now(timezone.utc).strftime("%d/%m %H:%M UTC")

    return FlexContainer.from_dict({
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": color, "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": f"{forecast.emoji} {forecast.intensity_th}",
                 "color": "#FFFFFF", "weight": "bold", "size": "xl"},
                {"type": "text", "text": now_str, "color": "#FFFFFFCC", "size": "xs", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "⏱ เวลา", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": mins_text, "color": "#111111", "size": "sm", "weight": "bold", "flex": 3},
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "💧 ปริมาณ", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": f"{forecast.precipitation_mm} mm/hr",
                     "color": "#111111", "size": "sm", "weight": "bold", "flex": 3},
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "📡 แหล่งข้อมูล", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": forecast.source.replace("_", " ").title(),
                     "color": "#111111", "size": "sm", "flex": 3},
                ]},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"💡 {rec}", "wrap": True,
                 "color": "#333333", "size": "sm", "margin": "md"},
            ],
        },
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "secondary", "height": "sm", "flex": 1,
                 "action": {"type": "message", "label": "🔄 ตรวจอีกครั้ง", "text": "ฝนตกไหม"}},
                {"type": "button", "style": "secondary", "height": "sm", "flex": 1,
                 "action": {"type": "message", "label": "🔕 ปิดแจ้งเตือน", "text": "ปิด"}},
            ],
        },
    })


def _commute_flex(forecast, time_str: str, time_label: str) -> FlexContainer:
    color = {"none": "#4CAF50", "light": "#2196F3",
             "moderate": "#FF9800", "heavy": "#F44336",
             "violent": "#9C27B0"}.get(forecast.intensity, "#2196F3")

    if not forecast.will_rain:
        advice = "ไม่ต้องกังวล ออกได้เลยครับ 😊"
        header_text = f"☀️ เวลา {time_str} น. ฟ้าใส"
    elif forecast.intensity == "light":
        advice = "มีฝนเล็กน้อย พกร่มไว้ด้วยนะครับ 🌂"
        header_text = f"🌦️ เวลา {time_str} น. ฝนเล็กน้อย"
    elif forecast.intensity == "moderate":
        advice = "ฝนปานกลาง แนะนำรอหรือเตรียมเสื้อกันฝน 🧥"
        header_text = f"🌧️ เวลา {time_str} น. ฝนปานกลาง"
    else:
        advice = "ฝนหนักมาก หลีกเลี่ยงถ้าทำได้ หรือรอฝนซา ⛈️"
        header_text = f"⛈️ เวลา {time_str} น. ฝนหนัก"

    return FlexContainer.from_dict({
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": color, "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": header_text,
                 "color": "#FFFFFF", "weight": "bold", "size": "lg", "wrap": True},
                {"type": "text", "text": f"({time_label})",
                 "color": "#FFFFFFCC", "size": "xs", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "💧 ปริมาณฝน", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": f"{forecast.precipitation_mm} mm/hr" if forecast.will_rain else "0 mm/hr",
                     "color": "#111111", "size": "sm", "weight": "bold", "flex": 3},
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "📡 แหล่งข้อมูล", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": forecast.source.replace("_", " ").title(),
                     "color": "#111111", "size": "sm", "flex": 3},
                ]},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"💡 {advice}", "wrap": True,
                 "color": "#333333", "size": "sm", "margin": "md"},
            ],
        },
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "secondary", "height": "sm", "flex": 1,
                 "action": {"type": "message", "label": "🔄 ตรวจอีกครั้ง",
                            "text": f"ออกบ้าน {time_str} ฝนตกไหม"}},
                {"type": "button", "style": "secondary", "height": "sm", "flex": 1,
                 "action": {"type": "message", "label": "🌧️ ฝนตอนนี้", "text": "ฝนตกไหม"}},
            ],
        },
    })


def _commute_setup_template_flex() -> FlexContainer:
    """Flex ที่แสดง template ให้ user copy แล้วกรอก"""
    return FlexContainer.from_dict({
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1565C0", "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": "🗺️ ตั้งค่าเส้นทางเดินทาง",
                 "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "แจ้งเตือนก่อนออกเดินทาง 1 ชั่วโมง",
                 "color": "#BBDEFB", "size": "xs", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "text", "text": "คัดลอกข้อความด้านล่าง แล้วแก้เป็นข้อมูลของคุณ",
                 "size": "sm", "color": "#555555", "wrap": True},
                {"type": "separator", "margin": "md"},
                {"type": "text", "wrap": True, "size": "sm",
                 "color": "#1565C0", "weight": "bold", "margin": "md",
                 "text": "บ้าน: บางพลี\nที่ทำงาน: อโศก\nเช้า: 08:00\nเย็น: 18:00"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "size": "xs", "color": "#888888", "wrap": True,
                 "text": "* ระบบจะแจ้งเตือนล่วงหน้า 1 ชั่วโมงก่อนออกเดินทางทุกวัน"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "button", "style": "primary", "color": "#1565C0",
                 "action": {"type": "clipboard", "label": "📋 คัดลอก template",
                            "clipboardText": "บ้าน: บางพลี\nที่ทำงาน: อโศก\nเช้า: 08:00\nเย็น: 18:00"}},
            ],
        },
    })


def _commute_setup_confirm_flex(home: str, work: str, morning: str, evening: str) -> FlexContainer:
    """Flex ยืนยันหลังตั้งค่าสำเร็จ"""
    alert_morn = f"{int(morning[:2])-1:02d}:{morning[3:]}"
    alert_eve  = f"{int(evening[:2])-1:02d}:{evening[3:]}"
    return FlexContainer.from_dict({
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#2E7D32", "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": "✅ ตั้งค่าสำเร็จ!",
                 "color": "#FFFFFF", "weight": "bold", "size": "xl"},
                {"type": "text", "text": "จะแจ้งเตือนก่อนออกเดินทางทุกวัน",
                 "color": "#C8E6C9", "size": "xs", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "🏠 บ้าน", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": home, "color": "#111111", "size": "sm", "weight": "bold", "flex": 3, "wrap": True},
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "🏢 ที่ทำงาน", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": work, "color": "#111111", "size": "sm", "weight": "bold", "flex": 3, "wrap": True},
                ]},
                {"type": "separator", "margin": "md"},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "🌅 เช้า", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": f"ออก {morning} น. → แจ้งเตือน {alert_morn} น.",
                     "color": "#111111", "size": "sm", "flex": 3, "wrap": True},
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "🌆 เย็น", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": f"เลิก {evening} น. → แจ้งเตือน {alert_eve} น.",
                     "color": "#111111", "size": "sm", "flex": 3, "wrap": True},
                ]},
            ],
        },
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "secondary", "height": "sm", "flex": 1,
                 "action": {"type": "message", "label": "✏️ แก้ไข", "text": "ตั้งค่า"}},
                {"type": "button", "style": "secondary", "height": "sm", "flex": 1,
                 "action": {"type": "message", "label": "🔕 ปิด", "text": "ปิด"}},
            ],
        },
    })


def _commute_alert_flex(forecast, trip: str, from_place: str, to_place: str,
                         depart_str: str, alert_time: str) -> FlexContainer:
    """Flex สำหรับ push alert ก่อนออกเดินทาง"""
    trip_label = "เช้า" if trip == "morning" else "เย็น"
    color = {"none": "#2E7D32", "light": "#1565C0",
             "moderate": "#E65100", "heavy": "#B71C1C",
             "violent": "#6A1B9A"}.get(forecast.intensity, "#1565C0")

    if not forecast.will_rain:
        icon      = "☀️"
        headline  = f"ฟ้าใส ออก {depart_str} ได้เลย"
        sub       = "ไม่ต้องพกร่ม"
        advice    = "สภาพอากาศดีมาก ไม่มีฝนในช่วงเดินทาง 😊"
    elif forecast.intensity == "light":
        icon      = "🌦️"
        headline  = f"ฝนเล็กน้อยช่วง {depart_str}"
        sub       = "แนะนำพกร่ม"
        advice    = "ฝนไม่หนัก แต่ควรพกร่มหรือเสื้อกันฝนไว้ 🌂"
    elif forecast.intensity == "moderate":
        icon      = "🌧️"
        headline  = f"ฝนปานกลางช่วง {depart_str}"
        sub       = "แนะนำออกให้เร็วขึ้นหรือรอฝนซา"
        advice    = "ฝนปานกลาง ถ้าทำได้ลองออกก่อนหรือรอ 30-60 นาที 🧥"
    else:
        icon      = "⛈️"
        headline  = f"ฝนหนักช่วง {depart_str}"
        sub       = "แนะนำเลื่อนเวลาออกเดินทาง"
        advice    = "ฝนหนักมาก ควรเลื่อนออกหรือหาที่หลบครับ ⛈️"

    delay30_str = f"ออกช้า 30 นาที"
    delay60_str = f"ออกช้า 60 นาที"

    return FlexContainer.from_dict({
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": color, "paddingAll": "20px",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": f"{icon} แจ้งเตือนเดินทาง{trip_label}นี้",
                     "color": "#FFFFFF", "weight": "bold", "size": "lg", "flex": 1, "wrap": True},
                    {"type": "text", "text": alert_time, "color": "#FFFFFFCC",
                     "size": "sm", "align": "end"},
                ]},
                {"type": "text", "text": f"{from_place}  →  {to_place}",
                 "color": "#FFFFFFCC", "size": "xs", "margin": "sm", "wrap": True},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "text", "text": headline,
                 "weight": "bold", "size": "md", "color": "#111111", "wrap": True},
                {"type": "text", "text": sub,
                 "size": "sm", "color": "#555555", "margin": "sm"},
                {"type": "separator", "margin": "md"},
                {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
                    {"type": "text", "text": "💧 ปริมาณ", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": f"{forecast.precipitation_mm} mm/hr" if forecast.will_rain else "0 mm/hr",
                     "color": "#111111", "size": "sm", "weight": "bold", "flex": 3},
                ]},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": advice, "wrap": True,
                 "color": "#333333", "size": "sm", "margin": "sm"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
                    {"type": "button", "style": "secondary", "height": "sm", "flex": 1,
                     "action": {"type": "message", "label": "+30 นาที", "text": delay30_str}},
                    {"type": "button", "style": "secondary", "height": "sm", "flex": 1,
                     "action": {"type": "message", "label": "+60 นาที", "text": delay60_str}},
                ]},
                {"type": "button", "style": "primary", "height": "sm",
                 "color": "#1565C0",
                 "action": {"type": "message", "label": "🔄 เช็คฝนตอนนี้", "text": "ฝนตกไหม"}},
            ],
        },
    })


def _help_flex() -> FlexContainer:
    """Flex หน้า Help"""
    return FlexContainer.from_dict({
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#37474F", "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": "🌧️ Rain Alert — คำสั่งทั้งหมด",
                 "color": "#FFFFFF", "weight": "bold", "size": "lg"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                _help_row("📍", "ส่งตำแหน่ง", "บันทึกบ้านและตรวจฝนทันที"),
                _help_row("🌧️", "ฝน", "ตรวจฝนตอนนี้"),
                _help_row("⏰", "ออกบ้าน 7.00", "เช็คฝนตามเวลาที่กำหนด"),
                {"type": "separator", "margin": "md"},
                _help_row("🗺️", "ตั้งค่า", "ตั้งค่าเส้นทางประจำ (แจ้งเตือนทุกวัน)"),
                _help_row("🔔", "เปิด / ปิด", "เปิด/ปิดการแจ้งเตือนอัตโนมัติ"),
                {"type": "separator", "margin": "md"},
                _help_row("🗺️", "Rain Route", "เช็คฝนตลอดเส้นทาง ก่อนออกเดินทาง"),
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "height": "sm",
                 "color": "#1565C0",
                 "action": {"type": "message", "label": "🌧️ ฝนตอนนี้", "text": "ฝน"}},
                {"type": "button", "style": "primary", "height": "sm",
                 "color": "#0D47A1",
                 "action": {"type": "uri", "label": "🗺️ วางแผนเดินทาง",
                            "uri": f"https://liff.line.me/{LIFF_ID}" if LIFF_ID else f"{RENDER_URL}/liff"}},
            ],
        },
    })


def _help_row(icon: str, command: str, desc: str) -> dict:
    return {
        "type": "box", "layout": "horizontal", "margin": "sm",
        "contents": [
            {"type": "text", "text": icon, "size": "sm", "flex": 0},
            {"type": "text", "text": command, "size": "sm", "weight": "bold",
             "color": "#1565C0", "flex": 3, "margin": "sm"},
            {"type": "text", "text": desc, "size": "xs", "color": "#666666",
             "flex": 5, "wrap": True},
        ],
    }


def _welcome_flex() -> FlexContainer:
    return FlexContainer.from_dict({
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1565C0", "paddingAll": "24px",
            "contents": [
                {"type": "text", "text": "🌧️ Rain Alert", "color": "#FFFFFF",
                 "weight": "bold", "size": "xxl"},
                {"type": "text", "text": "แจ้งเตือนฝนล่วงหน้า แม่นยำ ทันเหตุการณ์",
                 "color": "#BBDEFB", "size": "sm", "wrap": True, "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "text", "text": "วิธีเริ่มใช้งาน", "weight": "bold", "size": "md"},
                {"type": "text", "wrap": True, "size": "sm", "color": "#444444",
                 "text": "1️⃣  กด 'ส่งตำแหน่งของฉัน' ด้านล่าง\n2️⃣  ระบบจะแจ้งเตือนอัตโนมัติเมื่อฝนกำลังมา\n3️⃣  พิมพ์ 'ฝน' เพื่อตรวจสถานะตลอดเวลา"},
                {"type": "separator"},
                {"type": "text", "text": "ข้อมูลจาก Tomorrow.io + กรมอุตุนิยมวิทยา",
                 "color": "#888888", "size": "xs", "align": "center"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "color": "#1565C0",
                 "action": {"type": "location", "label": "📍 ส่งตำแหน่งของฉัน"}},
                {"type": "button", "style": "primary", "color": "#0D47A1",
                 "action": {"type": "uri", "label": "🗺️ วางแผนเดินทาง (Rain Route)",
                            "uri": f"https://liff.line.me/{LIFF_ID}" if LIFF_ID else f"{RENDER_URL}/liff"}},
                {"type": "button", "style": "secondary",
                 "action": {"type": "message", "label": "⚙️ ตั้งค่าเส้นทางประจำ", "text": "ตั้งค่า"}},
            ],
        },
    })
