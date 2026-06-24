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

logger = logging.getLogger(__name__)

LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

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
        await _reply(event.reply_token, [TextMessage(text=(
            "🌧️ Rain Alert — คำสั่งที่ใช้ได้\n\n"
            "📍 ส่งตำแหน่ง → บันทึกและตรวจฝนทันที\n"
            "ฝน / ฝนตกไหม → ตรวจสถานะฝนตอนนี้\n"
            "ออกบ้าน 7.00 ฝนตกไหม → ตรวจฝนตามเวลา\n"
            "เปิด → เปิดการแจ้งเตือนอัตโนมัติ\n"
            "ปิด → ปิดการแจ้งเตือนอัตโนมัติ"
        ))])


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


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

async def _reply(reply_token: str, messages: list):
    async with AsyncApiClient(configuration) as api_client:
        await AsyncMessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
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
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "button", "style": "primary", "color": "#1565C0",
                 "action": {"type": "location", "label": "📍 ส่งตำแหน่งของฉัน"}},
            ],
        },
    })
