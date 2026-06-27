"""
create_rich_menu.py
สร้าง LINE Rich Menu 4 ปุ่ม:
A (ซ้าย) → ฝน (ส่งข้อความ "ฝน")
B → แชร์ Location (postback)
C → แจ้งเตือน (ส่งข้อความ "แจ้งเตือน")
D (ขวา) → Rain Route (เปิด LIFF)

เรียกผ่าน POST /admin/setup-rich-menu หลัง deploy ครั้งเดียวพอ
"""

import os
import json
import httpx

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LIFF_ID = os.getenv("LIFF_ID", "2010502960-lFLq5Es1")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://forsi.co")

LINE_API = "https://api.line.me/v2/bot"

def _headers(content_type="application/json"):
    return {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": content_type,
    }

def make_rich_menu_config() -> dict:
    liff_url = f"https://liff.line.me/{LIFF_ID}" if LIFF_ID else f"{RENDER_URL}/liff"
    return {
        "size": {"width": 2500, "height": 810},
        "selected": True,
        "name": "Rain Alert Menu",
        "chatBarText": "เมนู",
        "areas": [
            {
                "bounds": {"x": 0, "y": 0, "width": 625, "height": 810},
                "action": {
                    "type": "message",
                    "label": "ฝน",
                    "text": "ฝน",
                },
            },
            {
                "bounds": {"x": 625, "y": 0, "width": 625, "height": 810},
                "action": {
                    "type": "postback",
                    "label": "Share Location",
                    "data": "action=share_location",
                },
            },
            {
                "bounds": {"x": 1250, "y": 0, "width": 625, "height": 810},
                "action": {
                    "type": "message",
                    "label": "แจ้งเตือน",
                    "text": "แจ้งเตือน",
                },
            },
            {
                "bounds": {"x": 1875, "y": 0, "width": 625, "height": 810},
                "action": {
                    "type": "uri",
                    "label": "Rain Route",
                    "uri": liff_url,
                },
            },
        ],
    }

async def setup(client: httpx.AsyncClient | None = None) -> dict:
    """สร้าง Rich Menu + ตั้งเป็น default (ไม่มีรูป — ตั้งผ่าน LINE OA Manager แทน)"""
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=30)

    try:
        # 1. ลบ menu เก่า (ถ้ามี)
        res = await client.get(f"{LINE_API}/richmenu/list", headers=_headers())
        for m in res.json().get("richmenus", []):
            await client.delete(f"{LINE_API}/richmenu/{m['richMenuId']}",
                                headers=_headers())

        # 2. สร้าง menu config
        cfg = make_rich_menu_config()
        res = await client.post(f"{LINE_API}/richmenu",
                                headers=_headers(),
                                content=json.dumps(cfg))
        res.raise_for_status()
        menu_id = res.json()["richMenuId"]

        # 3. ตั้งเป็น default สำหรับผู้ใช้ทุกคน
        res = await client.post(
            f"{LINE_API}/user/all/richmenu/{menu_id}",
            headers=_headers(),
        )
        res.raise_for_status()

        return {"status": "ok", "richMenuId": menu_id}

    except httpx.HTTPStatusError as e:
        return {"status": "error", "detail": e.response.text}
    finally:
        if own_client:
            await client.aclose()

if __name__ == "__main__":
    import asyncio
    result = asyncio.run(setup())
    print(result)
