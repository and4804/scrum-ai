from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from config import get_settings
from context import build_system_prompt
from notion_tools import NotionTools

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_workload",
            "description": "Fetch all pending tasks for a specific person in this team's Notion DB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "assigned_to": {
                        "type": "string",
                        "description": "Team member name/handle exactly matching Notion Assigned To value.",
                    }
                },
                "required": ["assigned_to"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_deadline",
            "description": "Update a task deadline by task name (ISO date format YYYY-MM-DD).",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string"},
                    "new_deadline": {"type": "string"},
                },
                "required": ["task_name", "new_deadline"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a new task in the team's Notion DB. Missing fields are auto-filled by backend defaults.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string"},
                    "assigned_to": {"type": "string"},
                    "deadline": {"type": "string"},
                    "priority": {"type": "string"},
                    "status": {"type": "string"},
                    "project": {"type": "string"},
                    "client_name": {"type": "string"},
                    "client_info": {"type": "string"},
                    "comments": {"type": "string"},
                },
                "required": ["task_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "List tasks from this team's Notion DB for all members or a specific assignee.",
            "parameters": {
                "type": "object",
                "properties": {
                    "assigned_to": {"type": "string"},
                    "include_completed": {"type": "boolean"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task_details",
            "description": "Get complete details for one task. Uses exact then fuzzy matching.",
            "parameters": {
                "type": "object",
                "properties": {"task_name": {"type": "string"}},
                "required": ["task_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tasks",
            "description": "Search tasks by keyword and optional filters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "filters": {"type": "object"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_workload",
            "description": "Get per-assignee workload counts and nearest deadlines.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reassign_task",
            "description": "Reassign a task to another team member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string"},
                    "new_assignee": {"type": "string"},
                },
                "required": ["task_name", "new_assignee"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_priority",
            "description": "Set task priority (Low, Medium, High).",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string"},
                    "priority": {"type": "string"},
                },
                "required": ["task_name", "priority"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_comment",
            "description": "Append a timestamped comment to a task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string"},
                    "comment_text": {"type": "string"},
                },
                "required": ["task_name", "comment_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_today_tasks",
            "description": "List tasks due today (IST), excluding completed.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_overdue_tasks",
            "description": "List overdue tasks, optionally filtered by assignee.",
            "parameters": {
                "type": "object",
                "properties": {"assignee": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_upcoming_deadlines",
            "description": "List tasks due in the next N days (default 3).",
            "parameters": {
                "type": "object",
                "properties": {"days": {"type": "integer"}, "assignee": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bulk_update_status",
            "description": "Update status for multiple tasks by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_names": {"type": "array", "items": {"type": "string"}},
                    "new_status": {"type": "string"},
                },
                "required": ["task_names", "new_status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_standup_report",
            "description": "Generate markdown standup report, optional assignee scope.",
            "parameters": {
                "type": "object",
                "properties": {"assignee": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_blockers",
            "description": "Detect likely blockers from task status/deadlines.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_reassignment",
            "description": "Recommend least-loaded reassignment options for a task.",
            "parameters": {
                "type": "object",
                "properties": {"task_name": {"type": "string"}},
                "required": ["task_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_summary",
            "description": "Return project-level summary and status distribution.",
            "parameters": {
                "type": "object",
                "properties": {"project_name": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_status",
            "description": "Update a task status by task name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string"},
                    "new_status": {"type": "string"},
                },
                "required": ["task_name", "new_status"],
            },
        },
    },
]

logger = logging.getLogger("scrum_ai.agent")


class ProjectManagerAgent:
    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.openai_model
        self._timezone = settings.app_timezone
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._notion = NotionTools()
        self._pending_actions: dict[str, dict[str, Any]] = {}

    async def run(self, user_message: str, notion_db_id: str, sender_name: str, chat_id: str) -> str:
        logger.info(
            "agent_run_started sender=%s db_id=%s message=%s",
            sender_name,
            notion_db_id,
            user_message,
        )
        pending_result = await self._handle_pending_action_reply(
            chat_id=chat_id,
            notion_db_id=notion_db_id,
            user_message=user_message,
        )
        if pending_result:
            return pending_result

        proactive_warning = await self._build_proactive_alert_block(notion_db_id)
        enforcement_state: dict[str, Any] = {
            "timeline_change_requested": self._is_timeline_change_request(user_message),
            "workload_checked": False,
            "recommended_deadline": None,
            "explicit_deadline": self._extract_explicit_deadline(user_message),
            "sender_name": sender_name,
            "raw_user_message": user_message,
            "chat_id": chat_id,
            "task_details_checked": False,
            "team_workload_checked": False,
            "reassignment_suggested": False,
        }

        system_prompt = build_system_prompt()
        if proactive_warning:
            system_prompt = f"{system_prompt}\n\n{proactive_warning}"

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Sender: {sender_name}\n"
                    f"Message: {user_message}\n\n"
                    "Manage tasks using available tools when needed."
                ),
            },
        ]

        for _ in range(8):
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.2,
            )
            choice = response.choices[0]
            assistant_message = choice.message

            if assistant_message.tool_calls:
                messages.append(assistant_message.model_dump(exclude_none=True))
                for tool_call in assistant_message.tool_calls:
                    logger.info(
                        "tool_call_requested name=%s raw_args=%s",
                        tool_call.function.name,
                        tool_call.function.arguments,
                    )
                    result = await self._dispatch_tool_call(
                        tool_call.function.name,
                        tool_call.function.arguments,
                        notion_db_id,
                        enforcement_state,
                    )
                    logger.info(
                        "tool_call_result name=%s result=%s",
                        tool_call.function.name,
                        json.dumps(result, ensure_ascii=False),
                    )
                    if result.get("confirmation_required"):
                        return result.get("message", "Please confirm this action.")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(result),
                        }
                    )
                continue

            logger.info("agent_final_response content=%s", assistant_message.content)
            return assistant_message.content or "I could not generate a response."

        logger.warning("agent_tool_limit_reached")
        return "I hit the tool-call limit for this request. Please try again with a shorter update."

    async def _dispatch_tool_call(
        self,
        tool_name: str,
        raw_arguments: str,
        notion_db_id: str,
        enforcement_state: dict[str, Any],
    ) -> dict[str, Any]:
        args = json.loads(raw_arguments or "{}")

        if tool_name == "get_workload":
            assigned_to = args.get("assigned_to") or enforcement_state.get("sender_name")
            if not assigned_to:
                return {"ok": False, "error": "missing_required_argument:assigned_to"}

            try:
                result = await self._notion.get_workload(
                    assigned_to=assigned_to,
                    db_id=notion_db_id,
                )
            except Exception as exc:
                return {"ok": False, "error": "notion_get_workload_failed", "detail": str(exc)}
            enforcement_state["workload_checked"] = True
            enforcement_state["recommended_deadline"] = result.get("recommended_deadline")
            return result
        if tool_name == "get_task_details":
            task_name = args.get("task_name")
            if not task_name:
                return {"ok": False, "error": "missing_required_argument:task_name"}
            try:
                result = await self._notion.get_task_details(db_id=notion_db_id, task_name=task_name)
            except Exception as exc:
                return {"ok": False, "error": "notion_get_task_details_failed", "detail": str(exc)}
            if result.get("ok"):
                enforcement_state["task_details_checked"] = True
            return result
        if tool_name == "search_tasks":
            query = args.get("query")
            if not query:
                return {"ok": False, "error": "missing_required_argument:query"}
            try:
                return await self._notion.search_tasks(
                    db_id=notion_db_id,
                    query=query,
                    filters=args.get("filters"),
                )
            except Exception as exc:
                return {"ok": False, "error": "notion_search_tasks_failed", "detail": str(exc)}
        if tool_name == "get_team_workload":
            try:
                result = await self._notion.get_team_workload(db_id=notion_db_id)
            except Exception as exc:
                return {"ok": False, "error": "notion_get_team_workload_failed", "detail": str(exc)}
            enforcement_state["team_workload_checked"] = True
            return result
        if tool_name == "update_deadline":
            task_name = args.get("task_name")
            requested_deadline = args.get("new_deadline")
            if not task_name or not requested_deadline:
                return {
                    "ok": False,
                    "error": "missing_required_arguments:task_name,new_deadline",
                }

            if enforcement_state.get("timeline_change_requested") and not enforcement_state.get(
                "workload_checked"
            ):
                return {
                    "ok": False,
                    "error": "workload_assessment_required",
                    "message": "Call get_workload first before update_deadline.",
                }
            if enforcement_state.get("timeline_change_requested") and not enforcement_state.get(
                "task_details_checked"
            ):
                return {
                    "ok": False,
                    "error": "task_details_required",
                    "message": "Call get_task_details first before update_deadline.",
                }

            # Respect explicit user-requested dates (e.g. "to 9th May") when present.
            final_deadline = enforcement_state.get("explicit_deadline") or requested_deadline

            return self._queue_confirmation(
                enforcement_state=enforcement_state,
                tool_name="update_deadline",
                action_args={"task_name": task_name, "new_deadline": final_deadline},
                message=(
                    f"Please confirm: update deadline of '{task_name}' to {final_deadline}? "
                    "Reply with yes/confirm/ok to proceed."
                ),
            )
        if tool_name == "update_status":
            task_name = args.get("task_name")
            new_status = args.get("new_status")
            if not task_name or not new_status:
                return {
                    "ok": False,
                    "error": "missing_required_arguments:task_name,new_status",
                }
            try:
                return await self._notion.update_status(
                    task_name=task_name,
                    new_status=new_status,
                    db_id=notion_db_id,
                )
            except Exception as exc:
                return {"ok": False, "error": "notion_update_status_failed", "detail": str(exc)}
        if tool_name == "reassign_task":
            task_name = args.get("task_name")
            new_assignee = args.get("new_assignee")
            if not task_name or not new_assignee:
                return {
                    "ok": False,
                    "error": "missing_required_arguments:task_name,new_assignee",
                }
            if not enforcement_state.get("team_workload_checked"):
                return {
                    "ok": False,
                    "error": "team_workload_required",
                    "message": "Call get_team_workload before reassign_task.",
                }
            if not enforcement_state.get("reassignment_suggested"):
                return {
                    "ok": False,
                    "error": "reassignment_suggestion_required",
                    "message": "Call suggest_reassignment before reassign_task.",
                }
            return self._queue_confirmation(
                enforcement_state=enforcement_state,
                tool_name="reassign_task",
                action_args={"task_name": task_name, "new_assignee": new_assignee},
                message=(
                    f"Please confirm: reassign '{task_name}' to {new_assignee}? "
                    "Reply with yes/confirm/ok to proceed."
                ),
            )
        if tool_name == "update_priority":
            try:
                return await self._notion.update_priority(
                    db_id=notion_db_id,
                    task_name=args.get("task_name", ""),
                    priority=args.get("priority", ""),
                )
            except Exception as exc:
                return {"ok": False, "error": "notion_update_priority_failed", "detail": str(exc)}
        if tool_name == "add_comment":
            try:
                return await self._notion.add_comment(
                    db_id=notion_db_id,
                    task_name=args.get("task_name", ""),
                    comment_text=args.get("comment_text", ""),
                )
            except Exception as exc:
                return {"ok": False, "error": "notion_add_comment_failed", "detail": str(exc)}
        if tool_name == "get_today_tasks":
            try:
                return await self._notion.get_today_tasks(db_id=notion_db_id)
            except Exception as exc:
                return {"ok": False, "error": "notion_get_today_tasks_failed", "detail": str(exc)}
        if tool_name == "get_overdue_tasks":
            try:
                return await self._notion.get_overdue_tasks(
                    db_id=notion_db_id,
                    assignee=args.get("assignee"),
                )
            except Exception as exc:
                return {"ok": False, "error": "notion_get_overdue_tasks_failed", "detail": str(exc)}
        if tool_name == "get_upcoming_deadlines":
            try:
                return await self._notion.get_upcoming_deadlines(
                    db_id=notion_db_id,
                    days=int(args.get("days", 3)),
                    assignee=args.get("assignee"),
                )
            except Exception as exc:
                return {"ok": False, "error": "notion_get_upcoming_deadlines_failed", "detail": str(exc)}
        if tool_name == "bulk_update_status":
            task_names = args.get("task_names") or []
            new_status = args.get("new_status")
            if not task_names or not new_status:
                return {"ok": False, "error": "missing_required_arguments:task_names,new_status"}
            return self._queue_confirmation(
                enforcement_state=enforcement_state,
                tool_name="bulk_update_status",
                action_args={"task_names": task_names, "new_status": new_status},
                message=(
                    f"Please confirm: update status to '{new_status}' for {len(task_names)} tasks? "
                    "Reply with yes/confirm/ok to proceed."
                ),
            )
        if tool_name == "generate_standup_report":
            try:
                return await self._notion.generate_standup_report(
                    db_id=notion_db_id,
                    assignee=args.get("assignee"),
                )
            except Exception as exc:
                return {"ok": False, "error": "notion_generate_standup_report_failed", "detail": str(exc)}
        if tool_name == "detect_blockers":
            try:
                return await self._notion.detect_blockers(db_id=notion_db_id)
            except Exception as exc:
                return {"ok": False, "error": "notion_detect_blockers_failed", "detail": str(exc)}
        if tool_name == "suggest_reassignment":
            try:
                result = await self._notion.suggest_reassignment(
                    db_id=notion_db_id,
                    task_name=args.get("task_name", ""),
                )
            except Exception as exc:
                return {"ok": False, "error": "notion_suggest_reassignment_failed", "detail": str(exc)}
            enforcement_state["reassignment_suggested"] = True
            return result
        if tool_name == "get_project_summary":
            try:
                return await self._notion.get_project_summary(
                    db_id=notion_db_id,
                    project_name=args.get("project_name"),
                )
            except Exception as exc:
                return {"ok": False, "error": "notion_get_project_summary_failed", "detail": str(exc)}
        if tool_name == "create_task":
            task_name = args.get("task_name")
            if not task_name:
                return {"ok": False, "error": "missing_required_argument:task_name"}

            explicit_deadline = enforcement_state.get("explicit_deadline")
            deadline = args.get("deadline") or explicit_deadline
            assigned_to = args.get("assigned_to") or enforcement_state.get("sender_name")

            try:
                return await self._notion.create_task(
                    task_name=task_name,
                    db_id=notion_db_id,
                    assigned_to=assigned_to,
                    deadline=deadline,
                    priority=args.get("priority"),
                    status=args.get("status"),
                    project=args.get("project"),
                    client_name=args.get("client_name"),
                    client_info=args.get("client_info"),
                    comments=args.get("comments"),
                )
            except Exception as exc:
                return {"ok": False, "error": "notion_create_task_failed", "detail": str(exc)}
        if tool_name == "list_tasks":
            requested_assigned_to = args.get("assigned_to")
            include_completed = args.get("include_completed", True)
            if not isinstance(include_completed, bool):
                include_completed = True

            message_lower = str(enforcement_state.get("raw_user_message", "")).lower()
            if (
                not requested_assigned_to
                and ("for me" in message_lower or "my tasks" in message_lower or "my task" in message_lower)
            ):
                requested_assigned_to = enforcement_state.get("sender_name")

            try:
                return await self._notion.list_tasks(
                    db_id=notion_db_id,
                    assigned_to=requested_assigned_to,
                    include_completed=include_completed,
                )
            except Exception as exc:
                return {"ok": False, "error": "notion_list_tasks_failed", "detail": str(exc)}

        return {"ok": False, "error": f"Unknown tool: {tool_name}"}

    @staticmethod
    def _is_timeline_change_request(user_message: str) -> bool:
        pattern = (
            r"\b(delay|delayed|postpone|reschedule|push|move|shift|extend|can't finish|cannot finish)\b"
        )
        return re.search(pattern, user_message.lower()) is not None

    def _extract_explicit_deadline(self, user_message: str) -> str | None:
        text = user_message.lower()
        today = datetime.now(ZoneInfo(self._timezone)).date()
        year = today.year

        if "day after tomorrow" in text:
            return (today + timedelta(days=2)).isoformat()
        if "tomorrow" in text:
            return (today + timedelta(days=1)).isoformat()
        if re.search(r"\btoday\b", text):
            return today.isoformat()

        in_days_match = re.search(r"\bin\s+(\d{1,3})\s+day(?:s)?\b", text)
        if in_days_match:
            days = int(in_days_match.group(1))
            return (today + timedelta(days=days)).isoformat()

        weekday_map = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }

        next_weekday_match = re.search(
            r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            text,
        )
        if next_weekday_match:
            weekday_name = next_weekday_match.group(1)
            return self._resolve_relative_weekday(
                today=today,
                target_weekday=weekday_map[weekday_name],
                force_next_week=True,
            ).isoformat()

        weekday_match = re.search(
            r"\b(?:on\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            text,
        )
        if weekday_match:
            weekday_name = weekday_match.group(1)
            return self._resolve_relative_weekday(
                today=today,
                target_weekday=weekday_map[weekday_name],
                force_next_week=False,
            ).isoformat()

        month_pattern = (
            r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
            r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        )
        month_map = {
            "jan": 1,
            "january": 1,
            "feb": 2,
            "february": 2,
            "mar": 3,
            "march": 3,
            "apr": 4,
            "april": 4,
            "may": 5,
            "jun": 6,
            "june": 6,
            "jul": 7,
            "july": 7,
            "aug": 8,
            "august": 8,
            "sep": 9,
            "sept": 9,
            "september": 9,
            "oct": 10,
            "october": 10,
            "nov": 11,
            "november": 11,
            "dec": 12,
            "december": 12,
        }

        # Matches "9th May" / "9 May"
        day_month_match = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{month_pattern}\b", text)
        # Matches "May 9th" / "May 9"
        month_day_match = re.search(rf"\b{month_pattern}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text)

        try:
            if day_month_match:
                day = int(day_month_match.group(1))
                month = month_map[day_month_match.group(2)]
                candidate = datetime(year=year, month=month, day=day)
                if candidate.date() < today:
                    candidate = candidate.replace(year=year + 1)
                return candidate.date().isoformat()
            if month_day_match:
                month = month_map[month_day_match.group(1)]
                day = int(month_day_match.group(2))
                candidate = datetime(year=year, month=month, day=day)
                if candidate.date() < today:
                    candidate = candidate.replace(year=year + 1)
                return candidate.date().isoformat()
        except ValueError:
            return None

        return None

    @staticmethod
    def _resolve_relative_weekday(
        today: date,
        target_weekday: int,
        force_next_week: bool,
    ) -> date:
        current_weekday = today.weekday()
        delta = (target_weekday - current_weekday) % 7
        if force_next_week or delta == 0:
            delta += 7
        return today + timedelta(days=delta)

    async def _build_proactive_alert_block(self, notion_db_id: str) -> str:
        try:
            overdue = await self._notion.get_overdue_tasks(db_id=notion_db_id)
            upcoming = await self._notion.get_upcoming_deadlines(db_id=notion_db_id, days=1)
        except Exception:
            return ""
        overdue_count = int(overdue.get("count", 0))
        upcoming_count = int(upcoming.get("count", 0))
        if overdue_count == 0 and upcoming_count == 0:
            return ""
        return (
            "⚠️ AGENT ALERT: "
            f"{overdue_count} tasks are overdue. "
            f"{upcoming_count} tasks are due tomorrow."
        )

    def _queue_confirmation(
        self,
        enforcement_state: dict[str, Any],
        tool_name: str,
        action_args: dict[str, Any],
        message: str,
    ) -> dict[str, Any]:
        chat_id = str(enforcement_state.get("chat_id"))
        self._pending_actions[chat_id] = {
            "tool_name": tool_name,
            "args": action_args,
            "expires_at": datetime.now(ZoneInfo(self._timezone)) + timedelta(minutes=10),
        }
        return {"ok": False, "confirmation_required": True, "message": message}

    async def _handle_pending_action_reply(
        self,
        chat_id: str,
        notion_db_id: str,
        user_message: str,
    ) -> str | None:
        pending = self._pending_actions.get(chat_id)
        if not pending:
            return None
        expires_at = pending.get("expires_at")
        if isinstance(expires_at, datetime) and datetime.now(ZoneInfo(self._timezone)) > expires_at:
            self._pending_actions.pop(chat_id, None)
            return "Pending action expired. Please request the change again."

        text = user_message.strip().lower()
        if any(token in text.split() for token in {"yes", "confirm", "ok"}):
            self._pending_actions.pop(chat_id, None)
            result = await self._execute_pending_action(
                tool_name=pending.get("tool_name"),
                db_id=notion_db_id,
                args=pending.get("args", {}),
            )
            if result.get("ok"):
                return "✅ Done. Want me to log a comment on this task?"
            return f"I could not complete the confirmed action: {result.get('error', 'unknown_error')}"

        self._pending_actions.pop(chat_id, None)
        return "Action cancelled."

    async def _execute_pending_action(
        self,
        tool_name: str,
        db_id: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name == "update_deadline":
            return await self._notion.update_deadline(
                task_name=args.get("task_name", ""),
                new_deadline=args.get("new_deadline", ""),
                db_id=db_id,
            )
        if tool_name == "reassign_task":
            return await self._notion.reassign_task(
                db_id=db_id,
                task_name=args.get("task_name", ""),
                new_assignee=args.get("new_assignee", ""),
            )
        if tool_name == "bulk_update_status":
            return await self._notion.bulk_update_status(
                db_id=db_id,
                task_names=args.get("task_names", []),
                new_status=args.get("new_status", ""),
            )
        return {"ok": False, "error": f"unknown_pending_tool:{tool_name}"}
