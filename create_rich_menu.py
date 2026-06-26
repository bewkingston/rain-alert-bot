"""
create_rich_menu.py
สร้าง LINE Rich Menu 2 ช่อง:
  ซ้าย  → วิธีใช้งาน (ส่งข้อความ "วิธีใช้งาน")
  ขวา   → Rain Route (เปิด LIFF)

Run: python create_rich_menu.py
หรือเรียกผ่าน POST /admin/setup-rich-menu
"""

import os
import io
import json
import httpx
from PIL import Image, ImageDraw, ImageFont

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LIFF_ID              = os.getenv("LIFF_ID", "")
RENDER_URL           = os.getenv("RENDER_EXTERNAL_URL", "https://forsi.co")

LINE_API = "https://api.line.me/v2/bot"


def _headers(content_type="application/json"):
    return {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": content_type,
    }


def make_image() -> bytes:
    """สร้างภาพ Rich Menu 2500×843 px"""
    W, H = 2500, 843
    img  = Image.new("RGB", (W, H), "#1565C0")
    draw = ImageDraw.Draw(img)

    # Right panel slightly darker
    draw.rectangle([W // 2, 0, W, H], fill="#0D47A1")
    # Divider
    draw.line([(W // 2, 0), (W // 2, H)], fill="#FFFFFF40", width=6)

    # Font (fallback ถ้าไม่มี)
    try:
        font_ico = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 160)
        font_lbl = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 90)
    except Exception:
        font_ico = ImageFont.load_default()
        font_lbl = font_ico

    # Left — วิธีใช้งาน
    draw.text((W // 4, H // 2 - 110), "?", font=font_ico,
              anchor="mm", fill="#FFFFFF")
    draw.text((W // 4, H // 2 + 80), "How to use", font=font_lbl,
              anchor="mm", fill="#FFFFFF")

    # Right — Rain Route
    draw.text((W * 3 // 4, H // 2 - 110), ">", font=font_ico,
              anchor="mm", fill="#FFFFFF")
    draw.text((W * 3 // 4, H // 2 + 80), "Rain Route", font=font_lbl,
              anchor="mm", fill="#FFFFFF")

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_rich_menu_config() -> dict:
    liff_url = f"https://liff.line.me/{LIFF_ID}" if LIFF_ID else f"{RENDER_URL}/liff"
    return {
        "size": {"width": 2500, "height": 843},
        "selected": True,
        "name": "Rain Alert Menu",
        "chatBarText": "เมนู",
        "areas": [
            {
                "bounds": {"x": 0, "y": 0, "width": 1250, "height": 843},
                "action": {
                    "type": "message",
                    "label": "วิธีใช้งาน",
                    "text": "วิธีใช้งาน",
                },
            },
            {
                "bounds": {"x": 1250, "y": 0, "width": 1250, "height": 843},
                "action": {
                    "type": "uri",
                    "label": "Rain Route",
                    "uri": liff_url,
                },
            },
        ],
    }


async def setup(client: httpx.AsyncClient | None = None) -> dict:
    """สร้าง Rich Menu + อัปโหลดภาพ + ตั้งเป็น default"""
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=30)

    try:
        # 1. ลบ menu เก่า (ถ้ามี)
        res = await client.get(f"{LINE_API}/richmenu/list",
                               headers=_headers())
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

        # 3. อัปโหลดภาพ
        img_bytes = make_image()
        res = await client.post(
            f"https://api-data.line.me/v2/bot/richmenu/{menu_id}/content",
            headers={
                "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "image/png",
            },
            content=img_bytes,
        )
        res.raise_for_status()

        # 4. ตั้งเป็น default
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


# ── standalone run ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio
    result = asyncio.run(setup())
    print(result)
