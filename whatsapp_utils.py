from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager

import httpx

from config import get_settings

logger = logging.getLogger("scrum_ai.whatsapp")

# Per group_jid / chat_id: serialize sends + 1.5s gap so two groups do not block each other.
_throttle_locks: dict[str, asyncio.Lock] = {}
_throttle_next_mono: dict[str, float] = {}


def _throttle_key(chat_id: int | str) -> str:
    return str(chat_id)


@asynccontextmanager
async def _wa_send_slot(chat_id: int | str):
    k = _throttle_key(chat_id)
    lock = _throttle_locks.setdefault(k, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        wait = _throttle_next_mono.get(k, 0.0) - now
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            yield
        finally:
            _throttle_next_mono[k] = time.monotonic() + 1.5


def _truncate_for_wa(text: str, limit: int = 4096) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n\n... (truncated)"
    room = max(0, limit - len(suffix))
    chunk = text[:room]
    cut = max(chunk.rfind("\n"), chunk.rfind(" "), chunk.rfind("\t"))
    if cut > room * 2 // 3:
        chunk = chunk[:cut]
    return chunk.rstrip() + suffix


def convert_to_wa_format(text: str) -> str:
    """Strip Telegram MarkdownV2-style escapes for WhatsApp; keep *bold* / _italic_; flatten links."""
    if not text:
        return ""

    s = text.replace("\\n", "\n")

    def link_repl(m: re.Match[str]) -> str:
        return f"{m.group(1)}: {m.group(2)}"

    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_repl, s)

    # Repeatedly strip `\X` → `X` (handles `\*`, `\.`, `\-`, etc., and collapses `\\` iteratively).
    for _ in range(12):
        t = re.sub(r"\\(.)", r"\1", s)
        if t == s:
            break
        s = t

    return _truncate_for_wa(s, 4096)


class TelegramClient:
    """WhatsApp transport via Baileys sidecar (same class name as legacy Telegram client)."""

    def __init__(self) -> None:
        settings = get_settings()
        self._sidecar = str(settings.wa_sidecar_url).rstrip("/")

    async def send_typing(self, chat_id: int | str) -> None:
        return None

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        _ = reply_to_message_id
        out = convert_to_wa_format(text) if parse_mode == "MarkdownV2" else text
        if parse_mode and parse_mode != "MarkdownV2":
            out = text
        if parse_mode != "MarkdownV2" and len(out) > 4096:
            out = _truncate_for_wa(out, 4096)

        url = f"{self._sidecar}/send"
        payload = {"group_jid": str(chat_id), "text": out}
        delay = 0.5
        last_exc: Exception | None = None

        async with _wa_send_slot(chat_id):
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        r = await client.post(url, json=payload)
                        r.raise_for_status()
                    return
                except Exception as exc:
                    last_exc = exc
                    logger.warning("wa_send_failed attempt=%s err=%s", attempt + 1, exc)
                    await asyncio.sleep(delay)
                    delay *= 2
            if last_exc:
                raise last_exc

    async def send_reaction(self, chat_id: int | str, message_id: int | str, emoji: str) -> None:
        url = f"{self._sidecar}/react"
        payload = {
            "group_jid": str(chat_id),
            "message_id": str(message_id),
            "emoji": emoji,
        }
        async with _wa_send_slot(chat_id):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.post(url, json=payload)
                    r.raise_for_status()
            except Exception as exc:
                logger.info("wa_react_failed err=%s", exc)

    async def set_message_reaction(self, chat_id: int | str, message_id: int | str) -> None:
        await self.send_reaction(chat_id, message_id, "✅")


def extract_clean_message(text: str, trigger_prefix: str) -> str:
    trigger = (trigger_prefix or "").strip()
    t = text.lstrip()
    if trigger and t.startswith(trigger):
        return t[len(trigger) :].lstrip()
    return text.strip()


def is_bot_mentioned(text: str, trigger_prefix: str) -> bool:
    trigger = (trigger_prefix or "").strip()
    if not trigger:
        return False
    return text.lstrip().startswith(trigger)


async def react_to_message(chat_id: str, message_id: str, emoji: str) -> None:
    """POST /react on the Baileys sidecar (arbitrary emoji)."""
    await TelegramClient().send_reaction(chat_id, message_id, emoji)


def escape_markdown_v2(text: str) -> str:
    """Escape text for Telegram MarkdownV2 (unchanged; checkin still emits MDV2)."""
    specials = r"_*[]()~`>#+-=|{}.!"
    escaped: list[str] = []
    for ch in text:
        if ch in specials:
            escaped.append("\\" + ch)
        else:
            escaped.append(ch)
    return "".join(escaped)


__all__ = [
    "TelegramClient",
    "convert_to_wa_format",
    "escape_markdown_v2",
    "extract_clean_message",
    "is_bot_mentioned",
    "react_to_message",
]
