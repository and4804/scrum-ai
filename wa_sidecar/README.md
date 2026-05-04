# WhatsApp sidecar (Baileys)

Bridges WhatsApp groups to the Python FastAPI app.

## Steps

1. `cd wa_sidecar && npm install`
2. `node index.js` — a **QR code** appears in this terminal on first run.
3. On the spare number’s phone: **Linked devices** → scan the QR.
4. Session is saved under `auth_state/` — later restarts usually **do not** need a new QR.
5. Add the spare number to your WhatsApp group; give it **admin** if your group restricts who can post.
6. Send any message in the group; watch **Python logs** for `group_jid` (or hit your `/webhook` logs) and copy the JID.
7. Put that JID into `router.py` → `CHAT_DB_MAP` (and matching keys in `SKIP_WEEKENDS_MAP` / `member_registry.MEMBER_MAP`).

## Env

See `.env`: `PYTHON_APP_URL` (Python app base URL), `PORT` (sidecar HTTP, default 3000).

## Endpoints

- `GET /health` — connection / session snapshot
- `POST /send` — `{ "group_jid": "…@g.us", "text": "…" }`
- `POST /react` — `{ "group_jid", "message_id", "emoji" }`
