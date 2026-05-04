# Autonomous AI Project Manager (Telegram + Notion + OpenAI)

An async Python webhook service that turns Telegram group updates into autonomous project management actions on Notion, with strict chat-level tenant isolation.

## What it does?

- Processes only messages where your bot is tagged (e.g. `@yourbot`)
- Enforces strict routing: Telegram `chat_id` -> one authorized Notion database
- Ignores unregistered chats before any LLM/tool execution
- Uses GPT-4o-mini with tool-calling for multi-step reasoning
- Updates Notion task deadlines/status and explains actions in natural language

## Architecture

1. Telegram sends update to `/webhook`
2. Backend checks `chat_id` in hardcoded routing map
3. Unauthorized chats are ignored
4. Bot mention is stripped from text
5. System prompt injects real-time IST context (`Asia/Kolkata`, `Pune, India`)
6. LLM runs tool-calling loop:
   - `get_workload` (read pending tasks for person)
   - `update_deadline`
   - `update_status`
7. Backend sends concise reply back to Telegram group

## Project Files

- `main.py`: FastAPI webhook entrypoint
- `agent.py`: OpenAI multi-turn tool-calling loop
- `notion_tools.py`: Notion read/write functions (scoped by db_id)
- `router.py`: Chat ID to Notion DB mapping guard
- `telegram_utils.py`: Telegram typing/message helpers
- `context.py`: Dynamic system prompt with date/time/location
- `config.py`: Environment variable loading and validation

## Prerequisites

- Python 3.11+
- Telegram Bot token and username
- OpenAI API key
- Notion integration token with access to each client database

## Setup

1. Create and activate a virtual environment:

   - Windows (PowerShell):
     - `python -m venv .venv`
     - `.venv\\Scripts\\Activate.ps1`

2. Install dependencies:

   - `pip install -r requirements.txt`

3. Copy env template and fill values:

   - `Copy-Item .env.example .env`

4. Configure `.env`:

   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_BOT_USERNAME` (include `@`)
   - `OPENAI_API_KEY`
   - `NOTION_API_KEY`
   - `NOTION_API_VERSION` (optional, default `2022-06-28`)
   - `APP_TIMEZONE` (optional, default `Asia/Kolkata`)
   - `APP_LOCATION` (optional, default `Pune, India`)

5. Configure hardcoded tenant routing in `router.py`:

   - Populate `CHAT_DB_MAP` constant with explicit `chat_id -> database_id` entries.
   - Unlisted chats are ignored automatically.

6. Start service:

   - `uvicorn main:app --host 0.0.0.0 --port 8000`

## Telegram Webhook

Set webhook URL (replace placeholders):

`https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=<PUBLIC_HTTPS_URL>/webhook`

You can use any HTTPS tunnel/reverse proxy in development.

## Notion Database Schema (Required)

Each task row must use these exact properties:

- `Task Name`: Title
- `Project`: Select
- `Client Name`: Select
- `Client Info`: Rich Text
- `Deadline`: Date (ISO 8601 when updated by tool)
- `Assigned To`: Select (match Telegram user names/handles used in chat)
- `Priority`: Select (`Low`, `Medium`, `High`)
- `Status`: Status (`Yet to Start`, `In Progress`, `Completed`)
- `Comments`: Rich Text

## Security and Isolation

- Routing occurs before LLM invocation
- Only `group` and `supergroup` messages are processed
- LLM never receives global Notion access
- Every Notion function requires explicit `db_id` from router
- Unknown chat IDs are ignored with no side effects

## Tool Behavior

- Timeline change requests are backend-enforced to call `get_workload` first
- Backend computes a deterministic low-capacity recommendation over the next 14 days
- Deadline updates for timeline shifts are constrained to the recommended date
- Task completion/progress updates use `update_status`
- New task requests use `create_task`; missing fields are auto-filled by backend defaults
- Task listing requests use `list_tasks` and can be scoped to the sender ("for me"/"my tasks")
- Advanced tools included: `get_task_details`, `search_tasks`, `get_team_workload`, `reassign_task`,
  `update_priority`, `add_comment`, `get_overdue_tasks`, `get_upcoming_deadlines`, `bulk_update_status`,
  `generate_standup_report`, `detect_blockers`, `suggest_reassignment`, `get_project_summary`
- Destructive writes (`update_deadline`, `reassign_task`, `bulk_update_status`) use a confirmation gate:
  agent asks for confirmation and executes only after `yes/confirm/ok` from same chat

## Notes

- Keep `Task Name` values unique per database to avoid ambiguity.
- Ensure Telegram display names/handles align with Notion `Assigned To` values.
- Consider adding request signing verification or a secret token on webhook ingress in production.
