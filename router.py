from __future__ import annotations

# PRD-strict hardcoded routing dictionary:
# WhatsApp group JID -> Notion Database ID
#
# How to find group_jid: send any message in the target group while the Baileys
# sidecar + Python app are running; the first payload logged under
# `wa.group_jid observed=...` is the value to paste here (ends with @g.us).
CHAT_DB_MAP: dict[str, str] = {
    "120363426424851434@g.us": "3561be702f5680eca62af485b2731007",  # Team 1: Ameya/Aniruddha/Shreya
    "120363408322000439@g.us": "0761be702f5682bb913a81e7551e7db1",  # Team 2: Ameya/Bhushan/Nishant
}

# Per-chat weekend skip: keys must match CHAT_DB_MAP (same WhatsApp group JID string).
SKIP_WEEKENDS_MAP: dict[str, bool] = {
    "120363426424851434@g.us": False,
    "120363408322000439@g.us": False,
}


def group_jid_discovery_hint() -> str:
    """Human-readable note for populating CHAT_DB_MAP keys (WhatsApp group JIDs)."""
    return (
        "Send any message in your WhatsApp group with wa_sidecar + API running; "
        "check Python logs for 'wa.group_jid observed=' and copy the JID into CHAT_DB_MAP."
    )


class TenantRouter:
    def __init__(self) -> None:
        self._chat_db_map = CHAT_DB_MAP

    def resolve_database_id(self, chat_id: int | str) -> str | None:
        return self._chat_db_map.get(str(chat_id))
