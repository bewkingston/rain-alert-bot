"""
geocoding.py — Rain Alert Bot
แปลงชื่อสถานที่ → lat/lon ผ่าน Nominatim (OpenStreetMap, ฟรี)
"""

import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_HEADERS   = {"User-Agent": "RainAlertBot/1.0 (bewkingstoni@gmail.com)"}


async def geocode(place_name: str) -> Optional[tuple]:
    """
    Returns (lat, lon, short_name) หรือ None ถ้าไม่พบ
    ค้นใน Thailand ก่อน ถ้าไม่มีจึงค้น global
    """
    for query in [f"{place_name} Thailand", place_name]:
        result = await _search(query)
        if result:
            return result
    return None


async def _search(query: str) -> Optional[tuple]:
    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "th",
        "accept-language": "th",
    }
    try:
        async with httpx.AsyncClient(timeout=8, headers=_HEADERS) as client:
            resp = await client.get(_NOMINATIM, params=params)
            resp.raise_for_status()
            data = resp.json()
        if not data:
            return None
        hit = data[0]
        lat  = float(hit["lat"])
        lon  = float(hit["lon"])
        name = hit.get("display_name", query).split(",")[0].strip()
        return (lat, lon, name)
    except Exception as e:
        logger.error(f"Geocode error '{query}': {e}")
        return None
