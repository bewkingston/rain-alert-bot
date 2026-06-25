"""
line_handler.py — Rain Alert Bot (pull-based)
User มาเช็คเอง ไม่มี auto push
  1. ส่ง location pin → เห็นฝนตอนนี้
  2. พิมพ์ 'ฝน' → เห็นฝนที่ตำแหน่งที่บันทึกไว้
  3. พิมพ์ 'ออกบ้าน 8.00' → forecast ณ เวลานั้น
  4. กด Rain Route → เปิด LIFF วางแผนเส้นทาง
"""

import os
import re
import logging
from datetime import datetime, timezone, timedelta

from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    AsyncApiClient, AsyncMessagingApi, Configuration,
    ReplyMessageRequest,
    TextMessage, FlexMessage, FlexContainer,
)
from linebot.v3.webhooks import (
    MessageEvent, FollowEvent, UnfollowEvent,
    LocationMessageContent, TextMessageContent,
)

from database import SessionLocal, get_or_create_user, upsert_location, User, UserLocation
from weather import get_rain_forecast, get_rain_forecast_at_time, build_recommendation

logger = logging.getLogger(__name__)

LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LIFF_ID                   = os.getenv("LIFF_ID", "")
RENDER_URL                = os.getenv("RENDER_EXTERNAL_URL", "https://rain-alert-bot-a34m.onrender.com")

parser        = WebhookParser(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

_THAI_TZ = timezone(timedelta(hours=7))


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

async def handle_events(body: str, signature: str):
    events = parser.parse(body, signature)
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
    """รับ location pin → บันทึก + แสดงฝนตอนนี้"""
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
    tl   = text.lower()

    # ── เช็คฝนตอนนี้ ─────────────────────────────
    if tl in ["ฝน", "ฝนตกไหม", "ฝนไหม", "ฝนตก", "rain", "🌧️", "🌧"]:
        await _reply_rain_now(event.reply_token, uid)

    # ── เช็คฝนตามเวลา เช่น "ออกบ้าน 8.00" ────────
    elif _parse_time(tl) is not None and any(
        kw in tl for kw in ["ออก", "ไป", "กลับ", "เดินทาง", "เช้า", "เย็น", "ถึง"]
    ):
        hour, minute = _parse_time(tl)
        await _reply_rain_at_time(event.reply_token, uid, hour, minute)

    # ── ทุกอย่างอื่น → help ───────────────────────
    else:
        await _reply(event.reply_token, [FlexMessage(
            alt_text="วิธีใช้ Rain Alert 🌧️",
            contents=_help_flex(),
        )])


# ─────────────────────────────────────────────
#  Reply Builders
# ─────────────────────────────────────────────

async def _reply_rain_now(reply_token: str, uid: str):
    db = SessionLocal()
    try:
        loc = (db.query(UserLocation)
               .filter(UserLocation.line_user_id == uid, UserLocation.is_primary == True)
               .first())
    finally:
        db.close()

    if not loc:
        await _reply(reply_token, [FlexMessage(
            alt_text="ส่ง location ก่อนนะครับ",
            contents=_no_location_flex(),
        )])
        return

    forecast = await get_rain_forecast(loc.latitude, loc.longitude)
    rec = build_recommendation(forecast)
    await _reply(reply_token, [FlexMessage(
        alt_text=f"{forecast.emoji} {forecast.intensity_th}",
        contents=_rain_flex(forecast, rec),
    )])


async def _reply_rain_at_time(reply_token: str, uid: str, hour: int, minute: int):
    db = SessionLocal()
    try:
        loc = (db.query(UserLocation)
               .filter(UserLocation.line_user_id == uid, UserLocation.is_primary == True)
               .first())
    finally:
        db.close()

    time_str = f"{hour:02d}:{minute:02d}"

    if not loc:
        await _reply(reply_token, [FlexMessage(
            alt_text="ส่ง location ก่อนนะครับ",
            contents=_no_location_flex(),
        )])
        return

    forecast = await get_rain_forecast_at_time(loc.latitude, loc.longitude, hour, minute)

    if forecast.intensity == "out_of_range":
        now = datetime.now(_THAI_TZ)
        target = now.replace(hour=hour, minute=minute, second=0)
        if target <= now:
            target += timedelta(days=1)
        mins = int((target - now).total_seconds() / 60)
        await _reply(reply_token, [TextMessage(
            text=f"⏰ {time_str} น. ยังอีก {mins//60} ชม. {mins%60} นาที\n\n"
                 f"Tomorrow.io ฟรีดูล่วงหน้าได้ถึง 6 ชม. ครับ"
        )])
        return

    now = datetime.now(_THAI_TZ)
    target = now.replace(hour=hour, minute=minute, second=0)
    if target <= now:
        target += timedelta(days=1)
    mins = int((target - now).total_seconds() / 60)
    time_label = f"อีก {mins//60} ชม. {mins%60} นาที" if mins >= 60 else f"อีก {mins} นาที"

    await _reply(reply_token, [FlexMessage(
        alt_text=f"{forecast.emoji} เวลา {time_str} น. — {forecast.intensity_th}",
        contents=_time_rain_flex(forecast, time_str, time_label),
    )])


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

async def _reply(reply_token: str, messages: list):
    async with AsyncApiClient(configuration) as api_client:
        await AsyncMessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
        )


def _parse_time(text: str):
    """แยกเวลาจากข้อความ '7.00' / '08:30' / '19.30' → (hour, minute) หรือ None"""
    m = re.search(r'\b(\d{1,2})[.:](\d{2})\b', text)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return (h, mn)
    return None


# ─────────────────────────────────────────────
#  Flex Builders
# ─────────────────────────────────────────────

def _welcome_flex() -> FlexContainer:
    liff_url = f"https://liff.line.me/{LIFF_ID}" if LIFF_ID else f"{RENDER_URL}/liff"
    return FlexContainer.from_dict({
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1565C0", "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": "🌧️ Rain Alert",
                 "color": "#FFFFFF", "weight": "bold", "size": "xxl"},
                {"type": "text", "text": "เช็คฝนก่อนออกเดินทาง",
                 "color": "#BBDEFB", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "lg",
            "contents": [
                {"type": "text", "text": "วิธีใช้", "weight": "bold", "size": "md", "color": "#333333"},
                _how_row("📍", "ส่ง location pin", "เห็นสภาพฝนทันที"),
                _how_row("💬", "พิมพ์ 'ฝน'", "เช็คฝนที่ตำแหน่งที่บันทึกไว้"),
                _how_row("⏰", "พิมพ์ 'ออกบ้าน 8.00'", "forecast ตามเวลาที่ระบุ"),
                _how_row("🗺️", "Rain Route", "ดูฝนตลอดเส้นทางขับรถ"),
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "color": "#1565C0",
                 "action": {"type": "uri", "label": "🗺️ เปิด Rain Route",
                            "uri": liff_url}},
            ],
        },
    })


def _help_flex() -> FlexContainer:
    liff_url = f"https://liff.line.me/{LIFF_ID}" if LIFF_ID else f"{RENDER_URL}/liff"
    return FlexContainer.from_dict({
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#37474F", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "🌧️ วิธีใช้ Rain Alert",
                 "color": "#FFFFFF", "weight": "bold", "size": "lg"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                _how_row("📍", "ส่ง location pin", "เช็คฝนที่ตำแหน่งนั้นทันที"),
                _how_row("💬", "พิมพ์ 'ฝน'", "ฝนที่ตำแหน่งที่บันทึกไว้"),
                _how_row("⏰", "'ออกบ้าน 8.00' / 'กลับบ้าน 18.00'", "forecast ตามเวลา"),
                _how_row("🗺️", "Rain Route", "ดูฝนตลอดเส้นทาง (วางแผนเดินทาง)"),
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "button", "style": "primary", "color": "#1565C0",
                 "action": {"type": "uri", "label": "🗺️ เปิด Rain Route",
                            "uri": liff_url}},
            ],
        },
    })


def _no_location_flex() -> FlexContainer:
    return FlexContainer.from_dict({
        "type": "bubble", "size": "kilo",
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": "📍 ยังไม่มีตำแหน่ง",
                 "weight": "bold", "size": "lg", "color": "#333333"},
                {"type": "text",
                 "text": "กด  ＋  → ตำแหน่ง → ส่งตำแหน่งปัจจุบัน\nแล้วกลับมาพิมพ์ 'ฝน' อีกครั้งได้เลยครับ",
                 "size": "sm", "color": "#555555", "wrap": True, "margin": "md"},
            ],
        },
    })


def _rain_flex(forecast, rec: str) -> FlexContainer:
    color = {"none": "#2E7D32", "light": "#1565C0",
             "moderate": "#E65100", "heavy": "#B71C1C",
             "violent": "#6A1B9A"}.get(forecast.intensity, "#1565C0")

    mins_text = (
        "กำลังตกอยู่" if forecast.minutes_to_rain == 0
        else f"ถึงใน {forecast.minutes_to_rain} นาที" if forecast.minutes_to_rain is not None
        else "ใน 1-2 ชั่วโมง"
    )
    now_str = datetime.now(_THAI_TZ).strftime("%d/%m %H:%M น.")

    liff_url = f"https://liff.line.me/{LIFF_ID}" if LIFF_ID else f"{RENDER_URL}/liff"

    return FlexContainer.from_dict({
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": color, "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": f"{forecast.emoji} {forecast.intensity_th}",
                 "color": "#FFFFFF", "weight": "bold", "size": "xxl"},
                {"type": "text", "text": now_str,
                 "color": "#FFFFFFCC", "size": "xs", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "⏱ ฝนมา", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": mins_text,
                     "color": "#111111", "size": "sm", "weight": "bold", "flex": 3},
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
                 "action": {"type": "message", "label": "🔄 ตรวจอีกครั้ง", "text": "ฝน"}},
                {"type": "button", "style": "primary", "height": "sm", "flex": 1,
                 "color": "#1565C0",
                 "action": {"type": "uri", "label": "🗺️ Rain Route", "uri": liff_url}},
            ],
        },
    })


def _time_rain_flex(forecast, time_str: str, time_label: str) -> FlexContainer:
    color = {"none": "#2E7D32", "light": "#1565C0",
             "moderate": "#E65100", "heavy": "#B71C1C",
             "violent": "#6A1B9A"}.get(forecast.intensity, "#1565C0")

    if not forecast.will_rain:
        header = f"☀️ {time_str} น. — ฟ้าใส"
        advice = "ไม่มีฝน ออกได้เลยครับ 😊"
    elif forecast.intensity == "light":
        header = f"🌦️ {time_str} น. — ฝนเล็กน้อย"
        advice = "ฝนเล็กน้อย พกร่มไว้ด้วยนะครับ 🌂"
    elif forecast.intensity == "moderate":
        header = f"🌧️ {time_str} น. — ฝนปานกลาง"
        advice = "ฝนพอสมควร แนะนำเตรียมเสื้อกันฝน 🧥"
    else:
        header = f"⛈️ {time_str} น. — ฝนหนัก"
        advice = "ฝนหนักมาก ลองเลื่อนเวลาหรือรอฝนซาครับ ⛈️"

    liff_url = f"https://liff.line.me/{LIFF_ID}" if LIFF_ID else f"{RENDER_URL}/liff"

    return FlexContainer.from_dict({
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": color, "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": header,
                 "color": "#FFFFFF", "weight": "bold", "size": "lg", "wrap": True},
                {"type": "text", "text": f"({time_label})",
                 "color": "#FFFFFFCC", "size": "xs", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"💡 {advice}", "wrap": True,
                 "color": "#333333", "size": "sm", "margin": "md"},
            ],
        },
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "secondary", "height": "sm", "flex": 1,
                 "action": {"type": "message", "label": "🌧️ ฝนตอนนี้", "text": "ฝน"}},
                {"type": "button", "style": "primary", "height": "sm", "flex": 1,
                 "color": "#1565C0",
                 "action": {"type": "uri", "label": "🗺️ Rain Route", "uri": liff_url}},
            ],
        },
    })


def _how_row(icon: str, label: str, desc: str) -> dict:
    return {
        "type": "box", "layout": "horizontal", "spacing": "md",
        "contents": [
            {"type": "text", "text": icon, "size": "md", "flex": 0},
            {"type": "box", "layout": "vertical", "flex": 1, "contents": [
                {"type": "text", "text": label,
                 "size": "sm", "color": "#111111", "weight": "bold", "wrap": True},
                {"type": "text", "text": desc,
                 "size": "xs", "color": "#888888", "wrap": True, "margin": "xs"},
            ]},
        ],
    }
