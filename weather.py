"""
weather.py — Rain Alert Bot
Tomorrow.io (minutely, hyperlocal) + TMD fallback
"""

import os
import httpx
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

TOMORROW_API_KEY = os.getenv("TOMORROW_IO_API_KEY", "")
TMD_API_KEY      = os.getenv("TMD_API_KEY", "")

# เกณฑ์ขั้นต่ำที่นับว่า "ฝนตกจริง" (mm/hr) — ต่ำกว่านี้ถือเป็น noise ของโมเดล
RAIN_THRESHOLD = 0.5


@dataclass
class RainForecast:
    will_rain:        bool
    minutes_to_rain:  Optional[int]
    intensity:        str   # none | light | moderate | heavy | violent
    intensity_th:     str
    precipitation_mm: float
    source:           str
    description:      str
    emoji:            str
    rain_duration_min: Optional[int] = None  # ฝนตกนานประมาณกี่นาที (None = ไม่ทราบ/เกิน 60 นาที)


async def get_tomorrow_forecast(lat: float, lon: float) -> Optional[RainForecast]:
    if not TOMORROW_API_KEY:
        return None
    params = {
        "location": f"{lat},{lon}",
        "fields": ["precipitationIntensity"],
        "units": "metric",
        "timesteps": ["1m"],
        "startTime": "now",
        "endTime": "nowPlus60m",
        "apikey": TOMORROW_API_KEY,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://api.tomorrow.io/v4/timelines", params=params)
            resp.raise_for_status()
            intervals = (resp.json().get("data", {})
                         .get("timelines", [{}])[0]
                         .get("intervals", []))

        if not intervals:
            return _no_rain("tomorrow_io")

        # ฝนต้อง ≥ RAIN_THRESHOLD ต่อเนื่องอย่างน้อย 2 นาทีถึงนับว่าตกจริง (ตัด noise)
        vals = [iv.get("values", {}).get("precipitationIntensity", 0) or 0
                for iv in intervals]
        minutes_to_rain = None
        max_mm = 0.0
        for i, mm in enumerate(vals):
            max_mm = max(max_mm, mm)
            if minutes_to_rain is None and mm >= RAIN_THRESHOLD:
                nxt = vals[i + 1] if i + 1 < len(vals) else 0
                if nxt >= RAIN_THRESHOLD:
                    minutes_to_rain = i

        if minutes_to_rain is None:
            return _no_rain("tomorrow_io")

        # หาว่าฝนจะหยุดเมื่อไหร่ (ต่ำกว่าเกณฑ์ต่อเนื่อง 2 นาที = หยุด)
        rain_duration = None
        for j in range(minutes_to_rain + 1, len(vals)):
            if vals[j] < RAIN_THRESHOLD and (j + 1 >= len(vals) or vals[j + 1] < RAIN_THRESHOLD):
                rain_duration = j - minutes_to_rain
                break

        level, level_th, emoji = _classify(max_mm)
        return RainForecast(True, minutes_to_rain, level, level_th,
                            round(max_mm, 2), "tomorrow_io",
                            f"Tomorrow.io — {max_mm:.1f} mm/hr", emoji,
                            rain_duration_min=rain_duration)
    except Exception as e:
        logger.error(f"Tomorrow.io error: {e}")
        return None


async def get_tmd_forecast(lat: float, lon: float) -> Optional[RainForecast]:
    if not TMD_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://data.tmd.go.th/nwpapi/v1/forecast/location/hourly/at",
                params={"lat": lat, "lon": lon, "fields": "rain", "dur": 3},
                headers={"authorization": f"Bearer {TMD_API_KEY}",
                         "accept": "application/json"},
            )
            resp.raise_for_status()
            forecasts = (resp.json()
                         .get("WeatherForecastLocation", {})
                         .get("forecasts", []))

        if not forecasts:
            return _no_rain("tmd")

        max_mm = max(
            float(f.get("data", {}).get("rain", {}).get("value", 0) or 0)
            for f in forecasts[:2]
        )
        if max_mm < 0.1:
            return _no_rain("tmd")

        level, level_th, emoji = _classify(max_mm)
        return RainForecast(True, None, level, level_th,
                            round(max_mm, 2), "tmd",
                            f"กรมอุตุฯ — {max_mm:.1f} mm ใน 1-2 ชม.", emoji)
    except Exception as e:
        logger.error(f"TMD error: {e}")
        return None


# Cache ต่อพิกัด (~100m) — ประหยัดโควตา Tomorrow.io (ฟรี 25 calls/ชม.)
import time as _time
_forecast_cache: dict = {}
_CACHE_TTL = 240  # วินาที (สั้นกว่ารอบ scheduler 5 นาที)


async def get_rain_forecast(lat: float, lon: float) -> RainForecast:
    """Tomorrow.io เป็น primary, TMD เป็น fallback (มี cache 4 นาที)"""
    key = (round(lat, 3), round(lon, 3))
    hit = _forecast_cache.get(key)
    if hit and _time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    fc = (await get_tomorrow_forecast(lat, lon)
          or await get_tmd_forecast(lat, lon))
    if fc is None:
        return _no_rain("none")  # ไม่ cache — ให้ลองใหม่รอบหน้า
    _forecast_cache[key] = (_time.time(), fc)
    return fc


async def get_rain_forecast_at_time(lat: float, lon: float,
                                    target_hour: int, target_minute: int) -> RainForecast:
    """ตรวจฝนสำหรับเวลาที่กำหนด (เวลาไทย UTC+7) ล่วงหน้าสูงสุด 6 ชม."""
    from datetime import datetime, timezone, timedelta

    thai_tz = timezone(timedelta(hours=7))
    now_thai = datetime.now(thai_tz)

    # ตั้งเวลาเป้าหมาย (ถ้าผ่านแล้ว → ใช้วันพรุ่งนี้)
    target = now_thai.replace(hour=target_hour, minute=target_minute,
                              second=0, microsecond=0)
    if target <= now_thai:
        target += timedelta(days=1)

    hours_ahead = (target - now_thai).total_seconds() / 3600
    if hours_ahead > 6:
        return RainForecast(
            False, None, "out_of_range", f"ไม่สามารถดูล่วงหน้า {int(hours_ahead)} ชม.",
            0.0, "none",
            f"Tomorrow.io ฟรีดูได้ถึง 6 ชม. ล่วงหน้าเท่านั้น", "❓"
        )

    if not TOMORROW_API_KEY:
        return _no_rain("none")

    target_utc = target.astimezone(timezone.utc)
    start_iso = (target_utc - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso   = (target_utc + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    params = {
        "location": f"{lat},{lon}",
        "fields": ["precipitationIntensity"],
        "units": "metric",
        "timesteps": ["1h"],
        "startTime": start_iso,
        "endTime": end_iso,
        "apikey": TOMORROW_API_KEY,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://api.tomorrow.io/v4/timelines", params=params)
            resp.raise_for_status()
            intervals = (resp.json().get("data", {})
                         .get("timelines", [{}])[0]
                         .get("intervals", []))

        if not intervals:
            return _no_rain("tomorrow_io")

        max_mm = max(iv.get("values", {}).get("precipitationIntensity", 0)
                     for iv in intervals)
        if max_mm < 0.1:
            return _no_rain("tomorrow_io")

        level, level_th, emoji = _classify(max_mm)
        return RainForecast(True, None, level, level_th,
                            round(max_mm, 2), "tomorrow_io",
                            f"Tomorrow.io — {max_mm:.1f} mm/hr", emoji)
    except Exception as e:
        logger.error(f"Tomorrow.io at_time error: {e}")
        return _no_rain("none")


def build_recommendation(forecast: RainForecast) -> str:
    if not forecast.will_rain:
        return "ฟ้าใสเลย ออกได้เลยนะ ☀️"
    mins = forecast.minutes_to_rain
    if mins is None or mins == 0:
        return "ฝนตกแล้วนะ รีบหาที่หลบด้วย 🏃"
    if mins <= 10:
        return f"อีกแค่ {mins} นาทีนะ รีบหน่อยก็ดี ⚡"
    if mins <= 20:
        return f"อีก {mins} นาทีนะ เอาร่มไปด้วยก็ดีนะ 🌂"
    return f"ยังอีก {mins} นาทีอยู่นะ แต่เอาร่มไปด้วยก็ดี ☂️"


def _no_rain(source: str) -> RainForecast:
    return RainForecast(False, None, "none", "ไม่มีฝน", 0.0, source,
                        "ท้องฟ้าแจ่มใส ไม่มีฝนใน 1 ชม.", "☀️")


def _classify(mm: float) -> tuple:
    if mm < 0.5:  return "none",     "ไม่มีฝน",    "☀️"
    if mm < 2.5:  return "light",    "ฝนเล็กน้อย", "🌦️"
    if mm < 10.0: return "moderate", "ฝนปานกลาง",  "🌧️"
    if mm < 50.0: return "heavy",    "ฝนหนัก",      "⛈️"
    return            "violent",  "ฝนหนักมาก",   "🌩️"
