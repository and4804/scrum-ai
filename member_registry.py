from __future__ import annotations

from typing import Any, Iterable

from rapidfuzz import fuzz

# WhatsApp group JID -> member registry (Notion assignee canonical name -> WA metadata)
MEMBER_MAP: dict[str, dict[str, Any]] = {
    "120363405525137244@g.us": {
        "notion_assignees": {
            "Ameya Dusane": {
                "wa_display_names": ["Ameya", "Ameya D", "AD", "Ameya Dusane"],
                "wa_jid": "91XXXXXXXXXX@s.whatsapp.net",
            },
        },
    },
    "120363405525137244@g.us": {
        "notion_assignees": {
            "Aniruddha Sonar": {
                "wa_display_names": ["Aniruddha", "Aniruddha S", "AS", "Aniruddha Sonar"],
                "wa_jid": "91XXXXXXXXXX@s.whatsapp.net",
            },
        },
    },
    "120363405525137244@g.us": {
        "notion_assignees": {
            "Shreya Vispute": {
                "wa_display_names": ["Shreya", "Shreya V", "SV", "Shreya Vispute"],
                "wa_jid": "91XXXXXXXXXX@s.whatsapp.net",
            },
        },
    }
}


def _normalize_jid(jid: str | None) -> str | None:
    if not jid:
        return None
    j = jid.strip().lower()
    return j or None


def _notion_members(chat_id: int | str) -> dict[str, dict[str, Any]]:
    entry = MEMBER_MAP.get(str(chat_id), {})
    raw = entry.get("notion_assignees")
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    return {}


def _looks_like_wa_jid(value: str | None) -> bool:
    if not value:
        return False
    v = value.strip().lower()
    return v.endswith("@s.whatsapp.net") or v.endswith("@lid")


def _is_wa_jid_string(s: str) -> bool:
    t = s.strip().lower()
    return "@s.whatsapp.net" in t or t.endswith("@lid")


def _mention_label(raw: str | None, *, fallback: str) -> str:
    """Never surface a raw WA JID as a human-facing \"mention\" line."""
    if not raw:
        return fallback
    if _is_wa_jid_string(raw):
        return fallback
    return raw


def get_notion_name(chat_id: int | str, sender_name: str | None, sender_jid: str | None) -> str | None:
    """Resolve WhatsApp sender to Notion assignee key: JID exact match, then fuzzy display name (>=80)."""
    members = _notion_members(chat_id)
    if not members:
        return None

    jnorm = _normalize_jid(sender_jid)
    if jnorm:
        for notion_key, meta in members.items():
            mj = _normalize_jid(meta.get("wa_jid"))
            if mj and mj == jnorm:
                return notion_key

    name_in = (sender_name or "").strip()
    if not name_in:
        return None

    best: tuple[int, str | None] = (0, None)
    for notion_key, meta in members.items():
        for alias in meta.get("wa_display_names") or []:
            if not isinstance(alias, str):
                continue
            score = fuzz.token_set_ratio(name_in.lower(), alias.lower())
            if score > best[0]:
                best = (score, notion_key)
        score = fuzz.token_set_ratio(name_in.lower(), notion_key.lower())
        if score > best[0]:
            best = (score, notion_key)
    if best[0] >= 80:
        return best[1]
    return None


def get_wa_mention(chat_id: int | str, notion_name: str | None) -> str | None:
    """Plain-text mention for WhatsApp (display name); WA has no @username mentions."""
    if not notion_name:
        return None
    members = _notion_members(chat_id)
    target = notion_name.strip().lower()
    for key, meta in members.items():
        if key.strip().lower() == target:
            names = meta.get("wa_display_names") or []
            if names and isinstance(names[0], str):
                return _mention_label(names[0], fallback=key)
            return key
    best: tuple[int, str | None] = (0, None)
    for key, meta in members.items():
        score = fuzz.token_set_ratio(target, key.lower())
        if score > best[0]:
            best = (score, key)
        for alias in meta.get("wa_display_names") or []:
            if not isinstance(alias, str):
                continue
            score = fuzz.token_set_ratio(target, alias.lower())
            if score > best[0]:
                best = (score, key)
    if best[0] >= 80:
        hit_key = best[1]
        if not hit_key:
            return None
        meta = members.get(hit_key, {})
        names = meta.get("wa_display_names") or []
        if names and isinstance(names[0], str):
            return _mention_label(names[0], fallback=hit_key)
        return hit_key
    return None


def get_telegram_handle(chat_id: int | str, notion_name: str | None) -> str | None:
    """Backward-compatible alias used by checkin.py (_mention_for)."""
    return get_wa_mention(chat_id, notion_name)


def notion_name_from_candidates(
    chat_id: int | str,
    telegram_username: str | None,
    display_name: str | None,
    candidate_notion_names: Iterable[str],
) -> str | None:
    """Used by checkin: pass WA sender_jid in `telegram_username` for JID-first registry match."""
    jid = telegram_username.strip() if telegram_username and _looks_like_wa_jid(telegram_username) else None
    display = (display_name or "").strip()
    uname = (telegram_username or "").strip()
    sender_label = display or ("" if jid else uname)

    hit = get_notion_name(chat_id, sender_label or None, jid)
    if hit:
        return hit

    names = [n for n in candidate_notion_names if (n or "").strip()]
    if not names:
        return None
    blobs: list[str] = []
    if uname and not jid:
        blobs.append(uname.lstrip("@"))
    if display:
        blobs.append(display)
    if not blobs:
        return None
    best: tuple[int, str | None] = (0, None)
    for notion in names:
        for c in blobs:
            score = fuzz.token_set_ratio(c.lower(), notion.lower())
            if score > best[0]:
                best = (score, notion)
    if best[0] >= 80:
        return best[1]
    return None
