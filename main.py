"""
main.py — Rain Alert Bot / Rain Route
FastAPI + Line Webhook (async) + APScheduler + LIFF
"""

import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from linebot.v3.exceptions import InvalidSignatureError

from database import init_db
from line_handler import handle_events
from scheduler import start_scheduler, stop_scheduler
from route_weather import analyze_route

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

LIFF_ID         = os.getenv("LIFF_ID", "")
GOOGLE_MAPS_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")


# ─────────────────────────────────────────────
#  Lifespan
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Rain Alert Bot starting...")
    init_db()
    start_scheduler()
    yield
    stop_scheduler()
    logger.info("👋 Rain Alert Bot stopped.")


app = FastAPI(title="Rain Alert Bot + Rain Route", version="2.0.0", lifespan=lifespan)


# ─────────────────────────────────────────────
#  LIFF  (serve HTML page)
# ─────────────────────────────────────────────

LIFF_DIR = Path(__file__).parent / "liff"

@app.get("/liff", response_class=HTMLResponse)
async def liff_page():
    """Serve LIFF app with LIFF_ID injected"""
    html = (LIFF_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__LIFF_ID__", LIFF_ID)
    html = html.replace("__MAPS_KEY__", GOOGLE_MAPS_KEY)
    return HTMLResponse(html)


# ─────────────────────────────────────────────
#  API: Route Weather Analysis
# ─────────────────────────────────────────────

class RouteRequest(BaseModel):
    origin:        str
    destination:   str
    departure_iso: str          # ISO 8601 datetime string
    uid:           str | None = None  # LINE user ID (optional — for future push)


@app.post("/api/route-weather")
async def route_weather(req: RouteRequest):
    try:
        departure_dt = datetime.fromisoformat(req.departure_iso.replace("Z", "+00:00"))
        if departure_dt.tzinfo is None:
            departure_dt = departure_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=422, detail="departure_iso format ไม่ถูกต้อง")

    try:
        result = await analyze_route(req.origin, req.destination, departure_dt)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"route-weather error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="วิเคราะห์เส้นทางไม่สำเร็จ กรุณาลองใหม่")

    # แปลง dataclass → dict สำหรับ JSON response
    return {
        "origin"            : result.origin,
        "destination"       : result.destination,
        "departure_str"     : result.departure_str,
        "duration_min"      : result.duration_min,
        "risk_pct"          : result.risk_pct,
        "max_intensity"     : result.max_intensity,
        "max_emoji"         : result.max_emoji,
        "recommendation"    : result.recommendation,
        "best_alt_departure": result.best_alt_departure,
        "waypoints"         : [
            {
                "name"        : w.name,
                "lat"         : w.lat,
                "lon"         : w.lon,
                "eta_str"     : w.eta_str,
                "intensity"   : w.intensity,
                "intensity_th": w.intensity_th,
                "emoji"       : w.emoji,
                "mm"          : w.mm,
            }
            for w in result.waypoints
        ],
        "rain_waypoints": [
            {
                "name"    : w.name,
                "eta_str" : w.eta_str,
                "emoji"   : w.emoji,
                "mm"      : w.mm,
            }
            for w in result.rain_waypoints
        ],
    }


# ─────────────────────────────────────────────
#  API: Places Autocomplete (proxy — avoids deprecated JS SDK)
# ─────────────────────────────────────────────

@app.get("/api/places-autocomplete")
async def places_autocomplete(q: str = ""):
    if not q or len(q) < 2:
        return []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(
                "https://maps.googleapis.com/maps/api/place/autocomplete/json",
                params={
                    "input"     : q,
                    "key"       : GOOGLE_MAPS_KEY,
                    "language"  : "th",
                    "components": "country:th",
                },
            )
        data = res.json()
    except Exception as e:
        logger.warning(f"Places autocomplete error: {e}")
        return []

    if data.get("status") not in ("OK", "ZERO_RESULTS"):
        return []

    return [
        {"description": p["description"], "place_id": p["place_id"]}
        for p in data.get("predictions", [])[:5]
    ]


@app.get("/api/geocode")
async def geocode(place_id: str = ""):
    """แปลง place_id → lat/lon"""
    if not place_id:
        raise HTTPException(status_code=422, detail="place_id required")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(
                "https://maps.googleapis.com/maps/api/place/details/json",
                params={
                    "place_id": place_id,
                    "key"     : GOOGLE_MAPS_KEY,
                    "fields"  : "geometry",
                    "language": "th",
                },
            )
        data = res.json()
    except Exception as e:
        logger.warning(f"Geocode error: {e}")
        raise HTTPException(status_code=500, detail="ไม่สามารถ geocode ได้")

    if data.get("status") != "OK":
        raise HTTPException(status_code=404, detail="ไม่พบสถานที่นี้")

    loc = data["result"]["geometry"]["location"]
    return {"lat": loc["lat"], "lon": loc["lng"]}


@app.get("/api/reverse-geocode")
async def reverse_geocode(lat: float, lon: float):
    """แปลง GPS coordinates → ชื่อสถานที่ภาษาไทย"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={
                    "latlng"  : f"{lat},{lon}",
                    "key"     : GOOGLE_MAPS_KEY,
                    "language": "th",
                },
            )
        data = res.json()
    except Exception as e:
        logger.warning(f"Reverse geocode error: {e}")
        raise HTTPException(status_code=500, detail="ไม่สามารถระบุตำแหน่งได้")

    if data.get("status") != "OK" or not data.get("results"):
        raise HTTPException(status_code=404, detail="ไม่พบข้อมูลตำแหน่งนี้")

    return {"address": data["results"][0]["formatted_address"]}


# ─────────────────────────────────────────────
#  LINE Webhook
# ─────────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body      = (await request.body()).decode("utf-8")
    try:
        await handle_events(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
    return JSONResponse({"status": "ok"})


# ─────────────────────────────────────────────
#  Health
# ─────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "ok", "service": "Rain Alert Bot + Rain Route 🗺️"}

@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.getenv("PORT", 8000)), reload=True)
