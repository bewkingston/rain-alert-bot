"""
route_weather.py — Rain Route / Rain Navigator
วิเคราะห์สภาพอากาศตลอดเส้นทางเดินทาง

Flow:
  1. Google Maps Directions API → route steps (waypoints + ETAs)
  2. Tomorrow.io → rain forecast ที่แต่ละ waypoint
  3. สรุป risk + คำแนะนำเวลาออกเดินทาง
"""

import os
import httpx
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from weather import get_rain_forecast_at_time, RainForecast, _classify, _no_rain

logger = logging.getLogger(__name__)

GMAPS_KEY        = os.getenv("GOOGLE_MAPS_API_KEY", "")
TOMORROW_API_KEY = os.getenv("TOMORROW_IO_API_KEY", "")
THAI_TZ          = timezone(timedelta(hours=7))


# ─────────────────────────────────────────────
#  Data classes
# ─────────────────────────────────────────────

@dataclass
class WaypointWeather:
    name: str
    lat: float
    lon: float
    eta_str: str       # "18:12"
    intensity: str     # none|light|moderate|heavy|violent
    intensity_th: str
    emoji: str
    mm: float


@dataclass
class RouteWeatherResult:
    origin: str
    destination: str
    departure_str: str
    duration_min: int
    waypoints: List[WaypointWeather]
    rain_waypoints: List[WaypointWeather]  # เฉพาะที่มีฝน
    risk_pct: int          # 0–100
    max_intensity: str     # intensity สูงสุดตลอดเส้นทาง
    max_emoji: str
    recommendation: str
    best_alt_departure: Optional[str]  # เวลาทางเลือกถ้ามีฝน


# ─────────────────────────────────────────────
#  Main Analysis
# ─────────────────────────────────────────────

async def analyze_route(
    origin: str,
    destination: str,
    departure_dt: datetime,
) -> RouteWeatherResult:
    """
    วิเคราะห์ฝนตลอดเส้นทาง origin → destination
    departure_dt ควรเป็น timezone-aware (Thai time หรือ UTC ก็ได้)
    """
    if not GMAPS_KEY:
        raise ValueError("GOOGLE_MAPS_API_KEY not set")

    dep_utc = departure_dt.astimezone(timezone.utc)
    dep_thai = departure_dt.astimezone(THAI_TZ)

    # 1. ดึง route จาก Google Maps
    steps = await _get_route_steps(origin, destination, dep_utc)
    if not steps:
        raise RuntimeError("ไม่พบเส้นทาง กรุณาตรวจสอบชื่อสถานที่")

    # 2. เช็คฝนที่แต่ละ step
    elapsed_sec = 0
    waypoints: List[WaypointWeather] = []

    for step in steps:
        elapsed_sec += step["duration_sec"]
        eta = dep_utc + timedelta(seconds=elapsed_sec)
        eta_thai = eta.astimezone(THAI_TZ)

        forecast = await _weather_at_latlon_dt(
            step["end_lat"], step["end_lon"], eta_thai
        )
        waypoints.append(WaypointWeather(
            name       = step["name"],
            lat        = step["end_lat"],
            lon        = step["end_lon"],
            eta_str    = eta_thai.strftime("%H:%M"),
            intensity  = forecast.intensity,
            intensity_th = forecast.intensity_th,
            emoji      = forecast.emoji,
            mm         = forecast.precipitation_mm,
        ))

    # 3. สรุป
    rain_wps  = [w for w in waypoints if w.intensity not in ("none", "out_of_range")]
    risk_pct  = int(len(rain_wps) / max(len(waypoints), 1) * 100)

    intensity_order = ["none", "light", "moderate", "heavy", "violent"]
    all_intensities = [w.intensity for w in waypoints if w.intensity in intensity_order]
    max_int = max(all_intensities, key=lambda x: intensity_order.index(x), default="none")
    _, max_int_th, max_emoji = _classify(_mm_from_intensity(max_int))

    total_min = sum(s["duration_sec"] for s in steps) // 60
    dep_str   = dep_thai.strftime("%H:%M")

    recommendation, best_alt = _build_recommendation(risk_pct, max_int, rain_wps, dep_thai)

    return RouteWeatherResult(
        origin           = origin,
        destination      = destination,
        departure_str    = dep_str,
        duration_min     = total_min,
        waypoints        = waypoints,
        rain_waypoints   = rain_wps,
        risk_pct         = risk_pct,
        max_intensity    = max_int,
        max_emoji        = max_emoji,
        recommendation   = recommendation,
        best_alt_departure = best_alt,
    )


# ─────────────────────────────────────────────
#  Routing: Google Directions → OSRM fallback
# ─────────────────────────────────────────────

import re as _re

async def _get_route_steps(origin: str, destination: str, dep_utc: datetime) -> list:
    """ลอง Google Directions ก่อน; ถ้าไม่ได้ใช้ Nominatim + OSRM"""
    if GMAPS_KEY:
        steps = await _google_directions(origin, destination, dep_utc)
        if steps:
            return steps
        logger.warning("Google Directions failed — switching to OSRM")
    return await _osrm_route(origin, destination)


async def _google_directions(origin: str, destination: str, dep_utc: datetime) -> list:
    dep_unix = int(dep_utc.timestamp())
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/directions/json",
                params={
                    "origin": origin, "destination": destination,
                    "departure_time": dep_unix, "mode": "driving",
                    "language": "th", "key": GMAPS_KEY,
                },
            )
            data = resp.json()
        if data.get("status") != "OK":
            logger.warning(f"Directions API: {data.get('status')} — {data.get('error_message','')}")
            return []
        steps = []
        for leg in data["routes"][0]["legs"]:
            for step in leg["steps"]:
                name = _re.sub(r'<[^>]+>', '', step.get("html_instructions", "")).strip()[:30]
                steps.append({
                    "name"        : name or "จุดระหว่างทาง",
                    "end_lat"     : step["end_location"]["lat"],
                    "end_lon"     : step["end_location"]["lng"],
                    "duration_sec": step["duration"]["value"],
                })
        return steps
    except Exception as e:
        logger.warning(f"Google Directions error: {e}")
        return []


async def _nominatim_geocode(query: str) -> tuple:
    """Text → (lat, lon) ด้วย Nominatim/OpenStreetMap"""
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "RainAlertBot/1.0"}) as c:
        res  = await c.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1,
                    "countrycodes": "th", "accept-language": "th"},
        )
        data = res.json()
    if not data:
        raise ValueError(f"ไม่พบสถานที่: {query}")
    return float(data[0]["lat"]), float(data[0]["lon"])


async def _osrm_route(origin: str, destination: str) -> list:
    """Nominatim geocoding + OSRM driving route (ฟรี ไม่ต้อง billing)"""
    try:
        orig_lat, orig_lon = await _nominatim_geocode(origin)
        dest_lat, dest_lon = await _nominatim_geocode(destination)

        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(
                f"http://router.project-osrm.org/route/v1/driving/"
                f"{orig_lon},{orig_lat};{dest_lon},{dest_lat}",
                params={"overview": "false", "steps": "true"},
            )
            data = resp.json()

        if data.get("code") != "Ok" or not data.get("routes"):
            logger.error(f"OSRM error: {data.get('code')}")
            return []

        steps = []
        for leg in data["routes"][0]["legs"]:
            for step in leg.get("steps", []):
                loc = step["maneuver"]["location"]   # [lon, lat]
                dur = int(step.get("duration", 0))
                if dur < 10:
                    continue     # ตัด micro-steps ออก
                steps.append({
                    "name"        : step.get("name") or "จุดระหว่างทาง",
                    "end_lat"     : loc[1],
                    "end_lon"     : loc[0],
                    "duration_sec": dur,
                })
        return steps

    except ValueError as e:
        raise RuntimeError(str(e))
    except Exception as e:
        logger.error(f"OSRM route error: {e}")
        return []


# ─────────────────────────────────────────────
#  Weather at specific lat/lon/datetime
# ─────────────────────────────────────────────

async def _weather_at_latlon_dt(lat: float, lon: float, dt_thai: datetime) -> RainForecast:
    """เรียก Tomorrow.io สำหรับตำแหน่ง+เวลาเฉพาะ (Thai timezone)"""
    # ใช้ function ที่มีอยู่แล้วใน weather.py
    return await get_rain_forecast_at_time(lat, lon, dt_thai.hour, dt_thai.minute)


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def _mm_from_intensity(intensity: str) -> float:
    return {"none": 0.0, "light": 1.0, "moderate": 5.0, "heavy": 20.0, "violent": 60.0}.get(intensity, 0.0)


def _build_recommendation(risk_pct: int, max_intensity: str,
                           rain_wps: list, dep_thai: datetime) -> tuple:
    """Returns (recommendation_text, best_alt_departure_str | None)"""
    if risk_pct == 0:
        return "เส้นทางแจ่มใส ออกเดินทางได้เลย ☀️", None

    first_rain_eta = rain_wps[0].eta_str if rain_wps else None

    if max_intensity == "light":
        rec = f"มีฝนเล็กน้อยบางช่วง พกร่มไว้ด้วย 🌂"
    elif max_intensity == "moderate":
        rec = f"ฝนปานกลาง แนะนำพกเสื้อกันฝน หรือรอฝนซาก่อน 🧥"
    else:
        rec = f"ฝนหนัก ควรเลื่อนเวลาออกเดินทาง ⛈️"

    # หาเวลาออกเดินทางทางเลือก (+30 หรือ +60 นาที)
    best_alt = None
    if risk_pct >= 50:
        alt30 = dep_thai + timedelta(minutes=30)
        alt60 = dep_thai + timedelta(minutes=60)
        best_alt = f"{alt30.strftime('%H:%M')} หรือ {alt60.strftime('%H:%M')}"

    return rec, best_alt
