"""
line_handler.py — Rain Alert Bot
✅ FIX: ใช้ WebhookParser + async handlers แทน WebhookHandler (sync)
       เพื่อให้ทำงานได้ถูกต้องใน FastAPI async context
"""

import os
import logging
from datetime import datetime, timezone

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
from weather import get_rain_forecast, build_recommendation

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
    text = event.message.text.strip().lower()

    if text in ["ฝน", "ฝนตกไหม", "ฝนไหม", "rain", "🌧️", "🌧"]:
        await _reply_rain_status(event.reply_token, uid)

    elif text in ["เปิด", "เปิดแจ้งเตือน", "on"]:
        _set_alert(uid, True)
        await _reply(event.reply_token, [TextMessage(text="✅ เปิดการแจ้งเตือนฝนแล้วครับ")])

    elif text in ["ปิด", "ปิดแจ้งเตือน", "off"]:
        _set_alert(uid, False)
        await _reply(event.reply_token, [TextMessage(
            text="🔕 ปิดการแจ้งเตือนแล้วครับ\nพิมพ์ 'เปิด' เมื่อต้องการเปิดอีกครั้ง"
        )])

    else:
        await _reply(event.reply_token, [TextMessage(text=(
            "🌧️ Rain Alert — คำสั่งที่ใช้ได้\n\n"
            "📍 ส่งตำแหน่ง → บันทึกและตรวจฝนทันที\n"
            "ฝน / ฝนตกไหม → ตรวจสถานะฝนตอนนี้\n"
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
                {"type": "text", "text": now_str, "color": "#FFFFFF88", "size": "xs", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "⏱ เวลา", "color": "#888", "size": "sm", "flex": 2},
                    {"type": "text", "text": mins_text, "color": "#111", "size": "sm", "weight": "bold", "flex": 3},
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "💧 ปริมาณ", "color": "#888", "size": "sm", "flex": 2},
                    {"type": "text", "text": f"{forecast.precipitation_mm} mm/hr",
                     "color": "#111", "size": "sm", "weight": "bold", "flex": 3},
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "📡 แหล่งข้อมูล", "color": "#888", "size": "sm", "flex": 2},
                    {"type": "text", "text": forecast.source.replace("_", " ").title(),
                     "color": "#111", "size": "sm", "flex": 3},
                ]},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"💡 {rec}", "wrap": True,
                 "color": "#333", "size": "sm", "margin": "md"},
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
                {"type": "text", "wrap": True, "size": "sm", "color": "#444",
                 "text": "1️⃣  กด 'ส่งตำแหน่งของฉัน' ด้านล่าง\n2️⃣  ระบบจะแจ้งเตือนอัตโนมัติเมื่อฝนกำลังมา\n3️⃣  พิมพ์ 'ฝน' เพื่อตรวจสถานะตลอดเวลา"},
                {"type": "separator"},
                {"type": "text", "text": "ข้อมูลจาก Tomorrow.io + กรมอุตุนิยมวิทยา",
                 "color": "#888", "size": "xs", "align": "center"},
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
