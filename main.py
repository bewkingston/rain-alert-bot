"""
main.py — Rain Alert Bot
FastAPI + Line Webhook (async) + APScheduler
"""

import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from linebot.v3.exceptions import InvalidSignatureError

from database import init_db
from line_handler import handle_events
from scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Rain Alert Bot starting...")
    init_db()
    start_scheduler()
    yield
    stop_scheduler()
    logger.info("👋 Rain Alert Bot stopped.")


app = FastAPI(title="Rain Alert Bot", version="1.0.0", lifespan=lifespan)


@app.get("/")
async def root():
    return {"status": "ok", "service": "Rain Alert Bot 🌧️"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
# Forsi Version - Deployed Sat Jun 27 09:17:00 UTC 2026
# Last update: 1782553493

# ═══════════════════════════════════════════════════════
# 🗺️  AUTO GPS UPDATE ENDPOINT
# ═══════════════════════════════════════════════════════

@app.post("/api/update-location")
async def update_location_api(request: dict):
    """Auto-update user location from LIFF GPS tracking"""
    try:
        user_id = request.get("user_id")
        latitude = request.get("latitude")
        longitude = request.get("longitude")
        
        if not all([user_id, latitude, longitude]):
            return {"status": "error", "message": "Missing fields"}
        
        db = SessionLocal()
        try:
            # Update or create user location
            user_loc = db.query(UserLocation).filter(
                UserLocation.line_user_id == user_id,
                UserLocation.is_primary == True
            ).first()
            
            if user_loc:
                user_loc.latitude = latitude
                user_loc.longitude = longitude
                user_loc.updated_at = datetime.now(timezone.utc)
            else:
                user_loc = UserLocation(
                    line_user_id=user_id,
                    latitude=latitude,
                    longitude=longitude,
                    label="Auto GPS",
                    is_primary=True
                )
                db.add(user_loc)
            
            db.commit()
            logger.info(f"✅ Auto-updated location for {user_id}: ({latitude}, {longitude})")
            return {"status": "success", "message": "Location updated"}
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Location update error: {e}")
        return {"status": "error", "message": str(e)}

