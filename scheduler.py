from __future__ import annotations

import logging

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
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
    sched.add_listener(_job_event_logger, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    for chat_id_str, db_id in CHAT_DB_MAP.items():
        chat_id = chat_id_str
        job_suffix = "".join(c if c.isalnum() or c in "-_" else "_" for c in chat_id_str)
        sched.add_job(
            morning_briefing,
            "cron",
            hour=9,
            minute=0,
            args=[chat_id, db_id],
            id=f"morning_{job_suffix}",
            replace_existing=True,
        )
        logger.info("scheduler_job_registered id=%s time=09:00 IST chat=%s", f"morning_{job_suffix}", chat_id)
        sched.add_job(
            evening_checkin_start,
            "cron",
            hour=22,
            minute=0,
            args=[chat_id, db_id],
            id=f"checkin_start_{job_suffix}",
            replace_existing=True,
        )
        logger.info("scheduler_job_registered id=%s time=22:00 IST chat=%s", f"checkin_start_{job_suffix}", chat_id)
        sched.add_job(
            evening_checkin_summarize,
            "cron",
            hour=22,
            minute=30,
            args=[chat_id],
            id=f"checkin_summary_{job_suffix}",
            replace_existing=True,
        )
        logger.info("scheduler_job_registered id=%s time=22:30 IST chat=%s", f"checkin_summary_{job_suffix}", chat_id)
    _scheduler = sched
    return sched


def _job_event_logger(event) -> None:
    if event.exception:
        logger.error("scheduler_job_failed id=%s err=%s", event.job_id, event.exception)
    else:
        logger.info("scheduler_job_ran id=%s", event.job_id)
