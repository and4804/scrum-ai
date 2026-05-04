from __future__ import annotations

from datetime import datetime, date

import pytz


def get_timezone(name: str) -> pytz.BaseTzInfo:
    """Return a tzinfo, falling back to Asia/Kolkata then UTC."""
    try:
        return pytz.timezone(name)
    except Exception:
        try:
            return pytz.timezone("Asia/Kolkata")
        except Exception:
            return pytz.UTC


def now_in_tz(name: str) -> datetime:
    tz = get_timezone(name)
    return datetime.now(tz)


def today_in_tz(name: str) -> date:
    return now_in_tz(name).date()
