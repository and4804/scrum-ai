from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import logging
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from agent import ProjectManagerAgent
from checkin import CHECKIN_STATE, handle_checkin_collection_message, maybe_record_early_checkin
from config import get_settings
from notion_tools import NotionTools
from router import TenantRouter, group_jid_discovery_hint
from scheduler import build_scheduler
from telegram_utils import TelegramClient, extract_clean_message, is_bot_mentioned

logger = logging.getLogger("scrum_ai")
logging.getLogger("scrum_ai").setLevel(logging.INFO)

settings = get_settings()
router = TenantRouter()
telegram = TelegramClient()
pm_agent = ProjectManagerAgent()
notion = NotionTools()

_seen_group_jids: set[str] = set()
_health_task: asyncio.Task[None] | None = None


class WAMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    group_jid: str
    sender_jid: str
    sender_name: str
    text: str = ""
    message_id: str
    timestamp: int
    message_kind: str | None = None


async def _wa_sidecar_health_loop() -> None:
    s = get_settings()
    base = s.wa_sidecar_url.rstrip("/")
    url = f"{base}/health"
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url)
                data = r.json()
            qr = bool(data.get("qr_pending") or data.get("last_qr_pending"))
            if (
                not data.get("connection_open")
                and not qr
                and data.get("wa_connection_state") != "connecting"
            ):
                logger.warning("wa_sidecar health: not connected (GET %s → %s)", url, data)
        except Exception as exc:
            logger.warning("wa_sidecar health: probe failed err=%s", exc)
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _health_task
    sched = build_scheduler()
    sched.start()
    _health_task = asyncio.create_task(_wa_sidecar_health_loop())
    try:
        yield
    finally:
        if _health_task:
            _health_task.cancel()
            with suppress(asyncio.CancelledError):
                await _health_task
        sched.shutdown(wait=False)


app = FastAPI(title="Autonomous AI Project Manager", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def wa_webhook(payload: dict[str, Any]) -> JSONResponse:
    try:
        wa = WAMessage.model_validate(payload)
    except ValidationError:
        return JSONResponse({"ok": True, "ignored": "invalid-wa-payload"})

    if wa.group_jid not in _seen_group_jids:
        _seen_group_jids.add(wa.group_jid)
        logger.info(
            "wa.group_jid observed=%s (add this to router.CHAT_DB_MAP). %s",
            wa.group_jid,
            group_jid_discovery_hint(),
        )

    notion_db_id = router.resolve_database_id(wa.group_jid)
    if not notion_db_id:
        return JSONResponse({"ok": True, "ignored": "unauthorized-chat"})

    message = {
        "message_id": wa.message_id,
        "text": wa.text or "",
        "from": {
            "username": wa.sender_jid or None,
            "first_name": wa.sender_name or "",
            "last_name": "",
        },
    }
    chat_id = wa.group_jid
    text = wa.text or ""

    if wa.message_kind and not text.strip():
        state = CHECKIN_STATE.get(str(chat_id))
        if state and state.get("phase") == "collecting":
            await telegram.send_message(
                chat_id,
                "Please send a text update 🙏",
                parse_mode=None,
            )
            return JSONResponse({"ok": True, "handled": "media-during-checkin"})

    if not text.strip():
        return JSONResponse({"ok": True, "ignored": "empty-text"})

    if await maybe_record_early_checkin(chat_id, notion_db_id, message, text):
        return JSONResponse({"ok": True, "handled": "early-checkin-captured"})

    if await handle_checkin_collection_message(
        chat_id, message, text, settings.wa_trigger_prefix
    ):
        return JSONResponse({"ok": True, "handled": "checkin-collection"})

    if not is_bot_mentioned(text, settings.wa_trigger_prefix):
        return JSONResponse({"ok": True, "ignored": "trigger-not-used"})

    cleaned_message = extract_clean_message(text, settings.wa_trigger_prefix)
    sender_name = (wa.sender_name or "").strip() or "Unknown User"

    typing_task = asyncio.create_task(_typing_heartbeat(chat_id))
    try:
        assistant_response = await pm_agent.run(
            user_message=cleaned_message,
            notion_db_id=notion_db_id,
            sender_name=sender_name,
            chat_id=str(chat_id),
        )
    except Exception:
        assistant_response = (
            "I hit an internal error while processing that request. "
            "Please verify Notion schema/integration access and try again."
        )
    finally:
        typing_task.cancel()
        with suppress(asyncio.CancelledError):
            await typing_task
    await telegram.send_message(chat_id, assistant_response)

    return JSONResponse({"ok": True})


@app.get("/debug/notion-schema")
async def debug_notion_schema(chat_id: str) -> JSONResponse:
    notion_db_id = router.resolve_database_id(chat_id)
    if not notion_db_id:
        return JSONResponse(
            {"ok": False, "error": "unauthorized-chat", "chat_id": chat_id},
            status_code=404,
        )

    try:
        schema = await notion.get_database_schema(notion_db_id)
        smoke_test = await notion.query_smoke_test(notion_db_id)
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "chat_id": chat_id,
                "database_id": notion_db_id,
                "error": "notion_debug_failed",
                "detail": str(exc),
            },
            status_code=500,
        )

    return JSONResponse(
        {
            "ok": True,
            "chat_id": chat_id,
            "database_id": notion_db_id,
            "schema": schema,
            "query_smoke_test": {
                "ok": True,
                "result_count": smoke_test["result_count"],
            },
        }
    )


@app.get("/debug/notion-tasks")
async def debug_notion_tasks(chat_id: str, assignee: str | None = None) -> JSONResponse:
    notion_db_id = router.resolve_database_id(chat_id)
    if not notion_db_id:
        return JSONResponse(
            {"ok": False, "error": "unauthorized-chat", "chat_id": chat_id},
            status_code=404,
        )

    try:
        raw = await notion.debug_list_tasks(notion_db_id)
        if assignee:
            filtered = [
                task for task in raw["tasks"] if notion._assignee_matches(task, assignee)
            ]
        else:
            filtered = raw["tasks"]
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "chat_id": chat_id,
                "database_id": notion_db_id,
                "error": "notion_task_debug_failed",
                "detail": str(exc),
            },
            status_code=500,
        )

    return JSONResponse(
        {
            "ok": True,
            "chat_id": chat_id,
            "database_id": notion_db_id,
            "assignee_filter": assignee,
            "total_tasks": raw["total_tasks"],
            "unique_assignees": raw["unique_assignees"],
            "filtered_count": len(filtered),
            "tasks": filtered,
        }
    )


async def _typing_heartbeat(chat_id: int | str) -> None:
    while True:
        try:
            await telegram.send_typing(chat_id)
        except Exception:
            pass
        await asyncio.sleep(4)
