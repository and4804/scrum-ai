from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from time_utils import get_timezone, now_in_tz, today_in_tz

from openai import AsyncOpenAI
from rapidfuzz import fuzz

from config import get_settings
from member_registry import get_telegram_handle, notion_name_from_candidates
from notion_tools import NotionTools
from router import SKIP_WEEKENDS_MAP
from telegram_utils import TelegramClient, escape_markdown_v2, is_bot_mentioned

logger = logging.getLogger("scrum_ai.checkin")

CHECKIN_STATE: dict[str, dict[str, Any]] = {}
# notion assignee name -> early status text (before 22:00 check-in start)
EARLY_CHECKIN_RESPONSES: dict[str, dict[str, str]] = {}
# YYYY-MM-DD -> skip evening initiation after a totally empty morning board
EVENING_SKIP_DATE: dict[str, str] = {}

telegram = TelegramClient()
notion = NotionTools()
TZ = get_timezone(get_settings().app_timezone)

PROACTIVE_PATTERN = ("done", "completed", "finished", "blocked")

_EXPECT_DIR = Path(__file__).resolve().parent / ".checkin_expect"
_DONE_DIR = Path(__file__).resolve().parent / ".checkin_done"


def _expect_file(chat_id: int | str) -> Path:
    _EXPECT_DIR.mkdir(parents=True, exist_ok=True)
    return _EXPECT_DIR / f"{chat_id}.json"


def _done_file(chat_id: int | str) -> Path:
    _DONE_DIR.mkdir(parents=True, exist_ok=True)
    return _DONE_DIR / f"{chat_id}.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        logger.info("checkin_fs_write_failed path=%s", path)


def _set_expect_session(chat_id: int | str) -> None:
    today = today_in_tz(get_settings().app_timezone).isoformat()
    _write_json(_expect_file(chat_id), {"date": today})


def _clear_expect_session(chat_id: int | str) -> None:
    p = _expect_file(chat_id)
    try:
        if p.is_file():
            p.unlink()
    except Exception:
        logger.info("checkin_expect_unlink_failed path=%s", p)


def _expect_session_active(chat_id: int | str) -> bool:
    today = today_in_tz(get_settings().app_timezone).isoformat()
    data = _read_json(_expect_file(chat_id))
    return bool(data and data.get("date") == today)


def _summary_done_today(chat_id: int | str) -> bool:
    today = today_in_tz(get_settings().app_timezone).isoformat()
    data = _read_json(_done_file(chat_id))
    return bool(data and data.get("date") == today)


def _mark_summary_done(chat_id: int | str) -> None:
    today = today_in_tz(get_settings().app_timezone).isoformat()
    _write_json(_done_file(chat_id), {"date": today})


def _chat_key(chat_id: int | str) -> str:
    return str(chat_id)


def _is_weekend_today() -> bool:
    return datetime.now(TZ).weekday() >= 5


def _skip_weekends(chat_id: int | str) -> bool:
    return SKIP_WEEKENDS_MAP.get(_chat_key(chat_id), False)


def _mdv2(text: str) -> str:
    return escape_markdown_v2(text)


async def _send_plain(chat_id: int | str, text: str) -> None:
    await telegram.send_message(chat_id, text, parse_mode=None)


def _combine_tasks(
    today_tasks: list[dict[str, Any]], overdue_tasks: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (today_full, overdue_full) with enough fields for evenings."""
    today_full = []
    for t in today_tasks:
        today_full.append(
            {
                "task_name": t.get("task_name"),
                "assignee": t.get("assignee") or t.get("assigned_to"),
                "priority": t.get("priority"),
                "project": t.get("project"),
                "deadline": t.get("deadline"),
                "status": t.get("status"),
            }
        )
    overdue_full = []
    for t in overdue_tasks:
        overdue_full.append(
            {
                "task_name": t.get("task_name"),
                "assignee": t.get("assigned_to") or t.get("assignee"),
                "priority": t.get("priority"),
                "project": t.get("project"),
                "deadline": t.get("deadline"),
                "status": t.get("status"),
            }
        )
    return today_full, overdue_full


def _tasks_by_assignee(
    today_full: list[dict[str, Any]], overdue_full: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for task in today_full + overdue_full:
        a = (task.get("assignee") or "").strip()
        if not a:
            continue
        buckets.setdefault(a, []).append(task)
    return buckets


def _dedupe_assignees(buckets: dict[str, list[dict[str, Any]]]) -> list[str]:
    return sorted(buckets.keys(), key=lambda s: s.lower())


def _parse_deadline_text(deadline_text: str | None) -> date | None:
    if not deadline_text:
        return None
    try:
        if "T" in deadline_text:
            return datetime.fromisoformat(deadline_text.replace("Z", "+00:00")).date()
        return date.fromisoformat(deadline_text)
    except ValueError:
        return None


def _days_late(deadline: str | None, today: date) -> int | None:
    due = _parse_deadline_text(deadline)
    if not due:
        return None
    return (today - due).days


def _mention_for(chat_id: int | str, notion_name: str) -> str:
    handle = get_telegram_handle(chat_id, notion_name)
    if handle:
        return f"@{handle}"
    return notion_name


def _format_morning_message(
    chat_id: int | str,
    today_full: list[dict[str, Any]],
    overdue_full: list[dict[str, Any]],
) -> str:
    today_d = today_in_tz(get_settings().app_timezone)
    lines: list[str] = [
        "Good morning. Today's task board:",
        "",
        "Due today:",
    ]
    unassigned_today = [t for t in today_full if not (t.get("assignee") or "").strip()]
    assigned_today = [t for t in today_full if (t.get("assignee") or "").strip()]
    assigned_today.sort(
        key=lambda t: ((t.get("assignee") or "").lower(), (t.get("task_name") or "").lower())
    )
    if not assigned_today and not unassigned_today:
        lines.append("- None")
    else:
        for t in assigned_today:
            pr = (t.get("priority") or "-").strip()
            lines.append(
                f"- {t.get('task_name') or ''} | { _mention_for(chat_id, t['assignee']) } | Priority: {pr}"
            )
        for t in unassigned_today:
            pr = (t.get("priority") or "-").strip()
            lines.append(f"- {t.get('task_name') or ''} | Unassigned | Priority: {pr}")

    lines.extend(["", "Overdue (action needed):"])
    unassigned_od = [t for t in overdue_full if not (t.get("assignee") or "").strip()]
    assigned_od = [t for t in overdue_full if (t.get("assignee") or "").strip()]
    assigned_od.sort(
        key=lambda t: ((t.get("assignee") or "").lower(), (t.get("task_name") or "").lower())
    )
    if not assigned_od and not unassigned_od:
        lines.append("- None")
    else:
        for t in assigned_od:
            late = _days_late(t.get("deadline"), today_d)
            late_s = f"{late} days late" if late is not None else "late"
            lines.append(
                f"- {t.get('task_name') or ''} | { _mention_for(chat_id, t['assignee']) } | {late_s}"
            )
        for t in unassigned_od:
            late = _days_late(t.get("deadline"), today_d)
            late_s = f"{late} days late" if late is not None else "late"
            lines.append(f"- {t.get('task_name') or ''} | Unassigned | {late_s}")

    lines.extend(["", "I will check in with everyone at 10 PM for updates."])
    return "\n".join(lines)


async def morning_briefing(chat_id: int | str, db_id: str) -> None:
    if _skip_weekends(chat_id) and _is_weekend_today():
        return
    ck = _chat_key(chat_id)
    try:
        today = await notion.get_today_tasks(db_id)
        overdue = await notion.get_overdue_tasks(db_id)
    except Exception:
        logger.exception("morning_briefing_notion_failed chat=%s", ck)
        return

    today_tasks = today.get("tasks") or []
    overdue_tasks = overdue.get("tasks") or []
    today_full, overdue_full = _combine_tasks(today_tasks, overdue_tasks)

    if not today_full and not overdue_full:
        EVENING_SKIP_DATE[ck] = today_in_tz(get_settings().app_timezone).isoformat()
        text = "No deadlines today. Clean slate - stay ahead."
        await _send_plain(chat_id, text)
        return

    EVENING_SKIP_DATE.pop(ck, None)
    msg = _format_morning_message(chat_id, today_full, overdue_full)
    await _send_plain(chat_id, msg)


def _looks_proactive_checkin(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in PROACTIVE_PATTERN)


async def maybe_record_early_checkin(
    chat_id: int | str,
    db_id: str,
    message: dict[str, Any],
    text: str,
) -> bool:
    """Capture pre\\-10pm updates; returns True if consumed (no further routing)."""
    if _looks_proactive_checkin(text):
        pass  # continue
    else:
        return False

    try:
        today = await notion.get_today_tasks(db_id)
        overdue = await notion.get_overdue_tasks(db_id)
    except Exception:
        return False

    today_tasks = today.get("tasks") or []
    overdue_tasks = overdue.get("tasks") or []
    today_full, overdue_full = _combine_tasks(today_tasks, overdue_tasks)
    buckets = _tasks_by_assignee(today_full, overdue_full)
    candidates = _dedupe_assignees(buckets)
    if not candidates:
        return False

    sender = message.get("from") or {}
    username = sender.get("username")
    display = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) or None

    notion_name = notion_name_from_candidates(chat_id, username, display, candidates)
    if not notion_name:
        return False

    ck = _chat_key(chat_id)
    EARLY_CHECKIN_RESPONSES.setdefault(ck, {})[notion_name] = text
    return True


def _fuzzy_assignee_lookup(pending: set[str], sender_candidates: list[str]) -> str | None:
    if not pending or not sender_candidates:
        return None
    best: tuple[int, str | None] = (0, None)
    for p in pending:
        for c in sender_candidates:
            score = fuzz.token_set_ratio(c.lower(), p.lower())
            if score > best[0]:
                best = (score, p)
    if best[0] >= 85:
        return best[1]
    return None


def _sender_strings(sender: dict[str, Any]) -> list[str]:
    out: list[str] = []
    u = sender.get("username")
    if u:
        out.append(u.lstrip("@"))
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])).strip()
    if name:
        out.append(name)
    return out


async def handle_checkin_collection_message(
    chat_id: int | str,
    message: dict[str, Any],
    text: str,
    bot_username: str,
) -> bool:
    """If in collecting phase, maybe record response. Returns True if consumed."""
    ck = _chat_key(chat_id)
    state = CHECKIN_STATE.get(ck)
    if not state or state.get("phase") != "collecting":
        return False

    expires_at: datetime | None = state.get("expires_at")
    if expires_at and datetime.now(TZ) > expires_at and not state.get("deadline_summarize_started"):
        state["deadline_summarize_started"] = True
        asyncio.create_task(evening_checkin_summarize(chat_id))

    pending: set[str] = state["pending"]
    sender = message.get("from") or {}
    notion_assignee = _fuzzy_assignee_lookup(pending, _sender_strings(sender))
    if not notion_assignee:
        if is_bot_mentioned(text, bot_username):
            return False
        return True

    responses: dict[str, str] = state["responses"]
    message_ids: dict[str, int] = state["message_ids"]

    responses[notion_assignee] = text
    pending.discard(notion_assignee)
    mid = message.get("message_id")
    if isinstance(mid, int):
        message_ids[notion_assignee] = mid

    if mid:
        try:
            await telegram.set_message_reaction(chat_id, mid)
        except Exception:
            logger.info("checkin_reaction_failed chat=%s mid=%s", ck, mid)

    if not pending:
        asyncio.create_task(evening_checkin_summarize(chat_id))
    return True


async def evening_checkin_start(chat_id: int | str, db_id: str) -> None:
    if _skip_weekends(chat_id) and _is_weekend_today():
        return
    ck = _chat_key(chat_id)
    if EVENING_SKIP_DATE.get(ck) == today_in_tz(get_settings().app_timezone).isoformat():
        return

    dpath = _done_file(chat_id)
    try:
        if dpath.is_file():
            dpath.unlink()
    except Exception:
        logger.info("checkin_done_unlink_failed path=%s", dpath)

    try:
        today = await notion.get_today_tasks(db_id)
        overdue = await notion.get_overdue_tasks(db_id)
    except Exception:
        logger.exception("evening_start_notion_failed chat=%s", ck)
        return

    today_tasks = today.get("tasks") or []
    overdue_tasks = overdue.get("tasks") or []
    today_full, overdue_full = _combine_tasks(today_tasks, overdue_tasks)
    buckets = _tasks_by_assignee(today_full, overdue_full)
    assignees = set(_dedupe_assignees(buckets))
    early = dict(EARLY_CHECKIN_RESPONSES.get(ck, {}))
    for name in list(assignees):
        if name in early:
            assignees.discard(name)

    if not assignees and not early:
        return

    now = now_in_tz(get_settings().app_timezone)
    CHECKIN_STATE[ck] = {
        "pending": assignees,
        "responses": dict(early),
        "message_ids": {},
        "expires_at": now + timedelta(minutes=30),
        "db_id": db_id,
        "phase": "collecting",
        "summarize_started": False,
        "deadline_summarize_started": False,
    }
    _set_expect_session(chat_id)

    pending_sorted = sorted(assignees, key=lambda s: s.lower())
    pending_mentions = " ".join(_mention_for(chat_id, n) for n in pending_sorted) if pending_sorted else ""

    header_lines = ["Evening check-in time.", ""]
    if pending_mentions:
        header_lines.append(f"Pending updates from: {pending_mentions}")
    elif early:
        header_lines.append("Everyone already shared an update earlier today - thanks.")
    header_lines.extend(
        [
            "",
            "Reply with a quick update on your tasks for today.",
            "Format (optional): Done: X | In Progress: Y | Blocked: Z",
        ]
    )
    await _send_plain(chat_id, "\n".join(header_lines))

    for person in pending_sorted:
        theirs = buckets.get(person, [])
        names = [t.get("task_name") for t in theirs[:3] if t.get("task_name")]
        task_part = ", ".join(_mdv2(n) for n in names) if names else _mdv2("your tasks")
        line = f"{_mention_for(chat_id, person)} - what's your status on {task_part}?"
        await _send_plain(chat_id, line)

    EARLY_CHECKIN_RESPONSES.pop(ck, None)


async def evening_checkin_summarize(chat_id: int | str) -> None:
    ck = _chat_key(chat_id)
    if _summary_done_today(chat_id):
        return

    state = CHECKIN_STATE.get(ck)
    # APScheduler fires nightly: no-op when there was no check-in session (clean morning, weekend skip, etc.)
    if not state:
        if _expect_session_active(chat_id):
            try:
                await _send_md(
                    chat_id,
                    "Check-in state was lost due to restart. Please update tasks manually.",
                )
            except Exception:
                logger.exception("evening_summarize_empty_state_send_failed chat=%s", ck)
            finally:
                _clear_expect_session(chat_id)
                _mark_summary_done(chat_id)
        return

    if state.get("summarize_started"):
        return
    state["summarize_started"] = True
    state["phase"] = "synthesizing"

    db_id = state["db_id"]
    pending: set[str] = set(state.get("pending") or [])
    responses: dict[str, str] = dict(state.get("responses") or {})
    today_iso = today_in_tz(get_settings().app_timezone).isoformat()

    try:
        today = await notion.get_today_tasks(db_id)
        overdue = await notion.get_overdue_tasks(db_id)
    except Exception:
        logger.exception("evening_summary_notion_failed chat=%s", ck)
        CHECKIN_STATE.pop(ck, None)
        return

    today_tasks = today.get("tasks") or []
    overdue_tasks = overdue.get("tasks") or []
    today_full, overdue_full = _combine_tasks(today_tasks, overdue_tasks)
    buckets = _tasks_by_assignee(today_full, overdue_full)
    all_assignees = set(buckets.keys()) | set(responses.keys())

    workload_by_person: dict[str, Any] = {}
    for person in sorted(all_assignees, key=lambda s: s.lower()):
        try:
            workload_by_person[person] = await notion.get_workload(person, db_id)
        except Exception as exc:
            workload_by_person[person] = {"error": str(exc)}

    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    user_payload = {
        "date": today_iso,
        "responses": responses,
        "pending_no_response": sorted(pending),
        "relevant_tasks_by_assignee": {
            k: v for k, v in buckets.items() if k in all_assignees
        },
        "notion_workload": workload_by_person,
    }

    system = (
        "You are a project manager summarizing an end-of-day team check-in. "
        "Be concise, constructive, flag blockers, suggest next steps.\n"
        "Return ONLY valid JSON with keys: "
        "summary_text (plain text body without outer title line), "
        "member_lines (array of {assignee, line_text}), "
        "action_items (array of strings, plain text), "
        "health_emoji (one of GREEN,YELLOW,RED), "
        "health_label (short), "
        "notion_actions (array of objects: "
        "{type: update_status|add_comment, task_name, new_status?, comment_text?})."
    )

    try:
        comp = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = comp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
    except Exception:
        logger.exception("evening_summary_llm_failed chat=%s", ck)
        parsed = {
            "summary_text": "Could not generate an LLM summary; raw responses are logged internally.",
            "member_lines": [],
            "action_items": [],
            "health_emoji": "YELLOW",
            "health_label": "Unknown",
            "notion_actions": [],
        }

    health = parsed.get("health_emoji", "YELLOW")
    health_map = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}
    health_icon = health_map.get(str(health).upper(), "🟡")

    title = f"End-of-day summary - {today_iso}"
    body = str(parsed.get("summary_text") or "").strip()
    member_lines = parsed.get("member_lines") or []
    ml_joined = "\n".join(
        str(x.get("line_text", "")) for x in member_lines if x.get("line_text")
    )

    actions = parsed.get("action_items") or []
    if actions:
        ai = "Action items for tomorrow:\n" + "\n".join(f"- {a}" for a in actions)
    else:
        ai = ""

    health_line = f"Overall team health: {health_icon} {str(parsed.get('health_label') or 'Moderate')}"

    if pending:
        names = " ".join(_mention_for(chat_id, n) for n in sorted(pending, key=lambda s: s.lower()))
        nr = f"No response from: {names} - tasks unverified."
    else:
        nr = ""

    parts = [p for p in [title, body, ml_joined, ai, health_line, nr] if p]
    summary_text = "\n\n".join(parts)

    auto_log: list[str] = []
    for action in parsed.get("notion_actions") or []:
        if not isinstance(action, dict):
            continue
        kind = (action.get("type") or "").strip().lower()
        task_name = (action.get("task_name") or "").strip()
        if not task_name:
            continue
        try:
            if kind == "update_status":
                ns = action.get("new_status") or ""
                res = await notion.update_status(task_name, ns, db_id)
                auto_log.append(f"{task_name} → {ns} ({res.get('ok')})")
            elif kind == "add_comment":
                ct = action.get("comment_text") or ""
                res = await notion.add_comment(db_id, task_name, ct)
                auto_log.append(f"comment on {task_name} ({res.get('ok')})")
        except Exception as exc:
            auto_log.append(f"{task_name}: error {exc}")

    for person in pending:
        for task in buckets.get(person, []):
            tn = task.get("task_name")
            if not tn:
                continue
            try:
                await notion.add_comment(
                    db_id,
                    tn,
                    f"[Auto] No check-in response on {today_iso}",
                )
            except Exception:
                logger.info("nonresponder_comment_failed task=%s", tn)

    if auto_log:
        footer = f"\n\nAuto-updated {len(auto_log)} tasks in Notion based on check-in responses."
        summary_text += footer

    try:
        await _send_plain(chat_id, summary_text)
    except Exception:
        logger.exception("evening_summary_send_failed chat=%s", ck)

    CHECKIN_STATE.pop(ck, None)
    _clear_expect_session(chat_id)
    _mark_summary_done(chat_id)
