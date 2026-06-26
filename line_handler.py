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
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, FlexMessage, FlexContainer,
    QuickReply, QuickReplyItem, LocationAction,
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
        alt_text="เฮ้ยินดีต้อนรับ! รู้ก่อนเปียก 1 ชม. เลย 🌧️",
        contents=_welcome_flex(),
        quick_reply=_location_quick_reply(),
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
    uid   = event.source.user_id
    lat   = event.message.latitude
    lon   = event.message.longitude
    # ใช้ address จาก LINE message เป็น label (ย่อให้สั้นลง)
    addr  = getattr(event.message, "address", None) or "ตำแหน่งปัจจุบัน"
    label = addr.split(",")[0].strip()[:64] if "," in addr else addr[:64]
    db = SessionLocal()
    try:
        get_or_create_user(db, uid)
        upsert_location(db, uid, lat, lon, label)
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
            text=f"⏰ {time_str} น. ยังอีก {mins//60} ชม. {mins%60} นาทีนะ\n\n"
                 f"ดูล่วงหน้าได้แค่ 6 ชม. ค่อยเช็คใหม่ทีหลังได้เลย 😅"
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


def _location_quick_reply() -> QuickReply:
    """Quick Reply ปุ่ม 'อัพเดทตำแหน่ง' — LINE เปิด location picker ให้เอง"""
    return QuickReply(items=[
        QuickReplyItem(action=LocationAction(label="📍 อัพเดทตำแหน่งของฉัน")),
    ])


async def push_rain_alert(uid: str, forecast, loc_label: str = "ตำแหน่งของคุณ"):
    """Push แจ้งเตือนฝนล่วงหน้า 1 ชม. — เรียกจาก scheduler"""
    rec = build_recommendation(forecast)
    mins = forecast.minutes_to_rain
    mins_text = (
        "ตกอยู่เลย ☔" if mins == 0
        else f"อีก {mins} นาทีมาแล้ว" if mins is not None
        else "ซักพักนึง"
    )
    alt = f"⚠️ {forecast.emoji} ฝนจะมา{mins_text} — {loc_label}"
    flex_msg = FlexMessage(
        alt_text=alt,
        contents=_push_alert_flex(forecast, rec, loc_label, mins_text),
        quick_reply=_location_quick_reply(),   # ← ปุ่มอัพเดทตำแหน่งใต้ alert
    )
    async with AsyncApiClient(configuration) as api_client:
        await AsyncMessagingApi(api_client).push_message(
            PushMessageRequest(to=uid, messages=[flex_msg])
        )


def _push_alert_flex(forecast, rec: str, loc_label: str, mins_text: str) -> FlexContainer:
    """Flex สำหรับ auto-push alert — แสดง badge 'แจ้งเตือนอัตโนมัติ'"""
    color = {"none": "#2E7D32", "light": "#1565C0",
             "moderate": "#E65100", "heavy": "#B71C1C",
             "violent": "#6A1B9A"}.get(forecast.intensity, "#1565C0")
    now_str = datetime.now(_THAI_TZ).strftime("%d/%m %H:%M น.")
    liff_url = f"https://liff.line.me/{LIFF_ID}" if LIFF_ID else f"{RENDER_URL}/liff"

    return FlexContainer.from_dict({
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": color, "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": "แค่อยากบอกว่าฝนจะมาแล้วนะ ☔",
                 "color": "#FFFFFF99", "size": "xs", "weight": "bold"},
                {"type": "text", "text": f"{forecast.emoji} {forecast.intensity_th}",
                 "color": "#FFFFFF", "weight": "bold", "size": "xxl", "margin": "sm"},
                {"type": "text", "text": f"📍 {loc_label}  •  {now_str}",
                 "color": "#FFFFFFCC", "size": "xs", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "⏱ ฝนมา", "color": "#888888",
                     "size": "sm", "flex": 2},
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
                {"type": "text", "text": "เดี๋ยวทักให้เองนะ ไม่ต้องห่วง 🌂",
                 "color": "#BBDEFB", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "lg",
            "contents": [
                {
                    "type": "box", "layout": "vertical",
                    "backgroundColor": "#EBF4FF", "cornerRadius": "4px",
                    "paddingAll": "14px",
                    "contents": [
                        {"type": "text", "text": "เซ็ตแป๊บนึง แค่นั้นเอง",
                         "weight": "bold", "size": "md", "color": "#1565C0"},
                        {"type": "text",
                         "text": "แชร์ location ให้ก่อนนะ เดี๋ยวดูแลให้เอง",
                         "size": "sm", "color": "#555555", "wrap": True, "margin": "sm"},
                    ],
                },
                {"type": "separator"},
                _how_row("🌧️", "แจ้งก่อนเปียก", "ถ้าฝนจะมา เดี๋ยวบอกเองนะ ไม่ต้องเป็นห่วง"),
                _how_row("💬", "พิมพ์ 'ฝน'", "อยากเช็คเองก็พิมพ์มาได้เลย"),
                _how_row("🗺️", "Rain Route", "เช็คก่อนออกเดินทางก็ดีนะ"),
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "color": "#1565C0",
                 "cornerRadius": "4px",
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
            "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "🌧️ ใช้ง่ายกว่าที่คิดนะ",
                 "weight": "bold", "size": "lg"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                _how_row("📍", "ส่ง location", "ส่งมาได้เลย เดี๋ยวเช็คให้"),
                _how_row("💬", "พิมพ์ 'ฝน'", "พิมพ์แค่คำเดียวก็รู้เลย"),
                _how_row("🗺️", "Rain Route", "เช็คเส้นทางก่อนออกไปก็ดีนะ"),
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "button", "style": "primary", "color": "#1565C0",
                 "cornerRadius": "4px",
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
                {"type": "text", "text": "ยังไม่รู้ว่าอยู่ที่ไหนเลยนะ 📍",
                 "weight": "bold", "size": "lg", "color": "#333333"},
                {"type": "text",
                 "text": "กด + เลือก 'ตำแหน่ง' ส่งมาก่อนได้เลยนะ แล้วค่อยพิมพ์ 'ฝน' ใหม่",
                 "size": "sm", "color": "#555555", "wrap": True, "margin": "md"},
            ],
        },
    })


def _rain_flex(forecast, rec: str) -> FlexContainer:
    color = {"none": "#2E7D32", "light": "#1565C0",
             "moderate": "#E65100", "heavy": "#B71C1C",
             "violent": "#6A1B9A"}.get(forecast.intensity, "#1565C0")

    mins_text = (
        "ตกอยู่เลยนะ ระวังด้วย ☔" if forecast.minutes_to_rain == 0
        else f"อีก {forecast.minutes_to_rain} นาทีนะ เตรียมตัวหน่อยก็ดี" if forecast.minutes_to_rain is not None
        else "อีกซักครู่นึง"
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
                 "cornerRadius": "4px",
                 "action": {"type": "message", "label": "🔄 เช็คอีกที", "text": "ฝน"}},
                {"type": "button", "style": "primary", "height": "sm", "flex": 1,
                 "color": "#1565C0", "cornerRadius": "4px",
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
        advice = "ฟ้าใสเลย ออกได้สบายๆ ☀️"
    elif forecast.intensity == "light":
        header = f"🌦️ {time_str} น. — ฝนนิดหน่อย"
        advice = "ฝนนิดหน่อยนะ เอาร่มไปด้วยก็ดี 🌂"
    elif forecast.intensity == "moderate":
        header = f"🌧️ {time_str} น. — ฝนพอสมควร"
        advice = "ฝนพอสมควรเลยนะ เอาเสื้อกันฝนไปด้วยก็ดี 🧥"
    else:
        header = f"⛈️ {time_str} น. — ฝนหนักมาก"
        advice = "ฝนหนักมากเลยนะ รอซาหน่อยก็ดีกว่า ⛈️"

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
                 "cornerRadius": "4px",
                 "action": {"type": "message", "label": "🌧️ ฝนตอนนี้", "text": "ฝน"}},
                {"type": "button", "style": "primary", "height": "sm", "flex": 1,
                 "color": "#1565C0", "cornerRadius": "4px",
                 "action": {"type": "uri", "label": "🗺️ Rain Route", "uri": liff_url}},
            ],
        },
    })


def _how_row(icon: str, label: str, desc: str) -> dict:
    return {
        "type": "box", "layout": "horizontal", "spacing": "md",
        "alignItems": "center",
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
