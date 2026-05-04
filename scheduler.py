from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from checkin import evening_checkin_start, evening_checkin_summarize, morning_briefing
from router import CHAT_DB_MAP

logger = logging.getLogger("scrum_ai.scheduler")

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


def build_scheduler() -> AsyncIOScheduler:
    global _scheduler
    sched = AsyncIOScheduler(timezone="Asia/Kolkata")
    for chat_id_str, db_id in CHAT_DB_MAP.items():
        chat_id = chat_id_str
        job_suffix = "".join(c if c.isalnum() or c in "-_" else "_" for c in chat_id_str)
        sched.add_job(
            morning_briefing,
            "cron",
            hour=7,
            minute=0,
            args=[chat_id, db_id],
            id=f"morning_{job_suffix}",
            replace_existing=True,
        )
        sched.add_job(
            evening_checkin_start,
            "cron",
            hour=22,
            minute=0,
            args=[chat_id, db_id],
            id=f"checkin_start_{job_suffix}",
            replace_existing=True,
        )
        sched.add_job(
            evening_checkin_summarize,
            "cron",
            hour=22,
            minute=30,
            args=[chat_id],
            id=f"checkin_summary_{job_suffix}",
            replace_existing=True,
        )
    _scheduler = sched
    return sched
