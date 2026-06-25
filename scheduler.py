"""
scheduler.py — Rain Alert Bot (pull-based)
ไม่มี auto push — user เช็คเองผ่าน LINE chat หรือ Rain Route LIFF
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Bangkok")


def start_scheduler():
    if scheduler.running:
        return
    scheduler.start()
    logger.info("✅ Scheduler started (pull-based — no auto push)")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
