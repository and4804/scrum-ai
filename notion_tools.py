from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Any

import httpx
from rapidfuzz import fuzz

from config import get_settings
from time_utils import now_in_tz, today_in_tz

logger = logging.getLogger("scrum_ai.notion")


class NotionTools:
    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.notion_api_key
        self._version = settings.notion_api_version
        self._timezone = settings.app_timezone
        self._base_url = "https://api.notion.com/v1"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Notion-Version": self._version,
            "Content-Type": "application/json",
        }

    async def get_workload(self, assigned_to: str, db_id: str) -> dict[str, Any]:
        # Query broadly, then filter in Python to avoid hard failures when a Notion
        # workspace has slight schema-type mismatches (e.g. select vs status).
        payload: dict[str, Any] = {"page_size": 100}
        data = await self._query_database(db_id, payload)
        tasks = [self._to_task_summary(page) for page in data.get("results", [])]
        available_assignees = sorted(
            {
                assignee
                for task in tasks
                for assignee in self._coerce_people_list(task.get("assigned_to"))
                if assignee
            }
        )
        tasks = [
            task
            for task in tasks
            if self._assignee_matches(task, assigned_to) and not self._is_completed(task)
        ]
        tasks.sort(key=lambda t: (t.get("deadline") or "9999-12-31", t.get("task_name") or ""))
        recommendation = self._recommend_low_capacity_date(tasks)
        return {
            "assigned_to": assigned_to,
            "tasks": tasks,
            "recommended_deadline": recommendation["recommended_deadline"],
            "daily_load": recommendation["daily_load"],
            "analysis_window_days": recommendation["analysis_window_days"],
            "debug_available_assignees": available_assignees,
        }

    async def get_task_details(self, db_id: str, task_name: str) -> dict[str, Any]:
        tasks = await self._fetch_all_tasks(db_id)
        target = task_name.strip().lower()
        exact = [task for task in tasks if (task.get("task_name") or "").strip().lower() == target]
        if len(exact) == 1:
            return {"ok": True, "task": exact[0]}
        if len(exact) > 1:
            return {
                "ok": False,
                "needs_clarification": True,
                "reason": "multiple_exact_matches",
                "matches": [task.get("task_name") for task in exact],
            }

        fuzzy = [task for task in tasks if target in (task.get("task_name") or "").lower()]
        if len(fuzzy) == 1:
            return {"ok": True, "task": fuzzy[0]}
        if len(fuzzy) > 1:
            return {
                "ok": False,
                "needs_clarification": True,
                "reason": "multiple_fuzzy_matches",
                "matches": [task.get("task_name") for task in fuzzy],
            }

        scored: list[tuple[int, dict[str, Any]]] = []
        for task in tasks:
            name = (task.get("task_name") or "").strip()
            if not name:
                continue
            score = int(fuzz.token_set_ratio(target, name.lower()))
            scored.append((score, task))
        if scored:
            scored.sort(key=lambda item: item[0], reverse=True)
            best_score = scored[0][0]
            if best_score >= 85:
                close = [t for s, t in scored if s >= best_score - 2]
                if len(close) == 1:
                    return {"ok": True, "task": close[0]}
                return {
                    "ok": False,
                    "needs_clarification": True,
                    "reason": "multiple_fuzzy_matches",
                    "matches": [task.get("task_name") for task in close],
                }
        return {"ok": False, "error": f"Task '{task_name}' not found"}

    async def search_tasks(
        self,
        db_id: str,
        query: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        filters = filters or {}
        tasks = await self._fetch_all_tasks(db_id)
        q = query.strip().lower()
        matched = []
        for task in tasks:
            if (
                q in (task.get("task_name") or "").lower()
                or q in (task.get("project") or "").lower()
                or q in (task.get("client_name") or "").lower()
            ):
                matched.append(task)
                continue
            assignees = " ".join(self._coerce_people_list(task.get("assigned_to"))).lower()
            people = " ".join(self._coerce_people_list(task.get("people_involved"))).lower()
            if q and (q in assignees or q in people):
                matched.append(task)
        matched = self._apply_task_filters(matched, filters)
        return {
            "ok": True,
            "query": query,
            "filters": filters,
            "count": len(matched),
            "tasks": [self._to_brief_task(task) for task in matched],
        }

    async def get_team_workload(self, db_id: str) -> dict[str, Any]:
        tasks = await self._fetch_all_tasks(db_id)
        workloads: dict[str, dict[str, Any]] = {}
        for task in tasks:
            assignees = self._coerce_people_list(task.get("assigned_to"))
            if not assignees:
                assignees = ["Unassigned"]

            status = (task.get("status") or "").strip().lower()
            deadline = task.get("deadline")

            for assignee in assignees:
                bucket = workloads.setdefault(
                    assignee,
                    {
                        "yet_to_start": 0,
                        "in_progress": 0,
                        "completed": 0,
                        "nearest_deadline": None,
                    },
                )
                if status == "yet to start":
                    bucket["yet_to_start"] += 1
                elif status == "in progress":
                    bucket["in_progress"] += 1
                elif status == "completed":
                    bucket["completed"] += 1

                if deadline and status != "completed":
                    current = bucket["nearest_deadline"]
                    if current is None or deadline < current:
                        bucket["nearest_deadline"] = deadline

        return {"ok": True, "team_workload": workloads}

    async def reassign_task(self, db_id: str, task_name: str, new_assignee: str) -> dict[str, Any]:
        task = await self.get_task_details(db_id, task_name)
        if not task.get("ok"):
            return task
        existing = task["task"]
        page_id = existing.get("page_id")
        if not page_id:
            return {"ok": False, "error": "task_page_id_missing"}
        payload = {"properties": {"Assigned To": {"multi_select": [{"name": new_assignee}]}}}
        await self._patch_page(page_id, payload)
        return {
            "ok": True,
            "task_name": existing.get("task_name"),
            "old_assignee": existing.get("assigned_to"),
            "new_assignee": new_assignee,
        }

    async def update_priority(self, db_id: str, task_name: str, priority: str) -> dict[str, Any]:
        allowed = {"low": "Low", "medium": "Medium", "high": "High"}
        if priority.strip().lower() not in allowed:
            return {"ok": False, "error": "invalid_priority", "allowed": ["Low", "Medium", "High"]}
        task = await self.get_task_details(db_id, task_name)
        if not task.get("ok"):
            return task
        existing = task["task"]
        page_id = existing.get("page_id")
        if not page_id:
            return {"ok": False, "error": "task_page_id_missing"}
        normalized = allowed[priority.strip().lower()]
        payload = {"properties": {"Priority": {"select": {"name": normalized}}}}
        await self._patch_page(page_id, payload)
        return {"ok": True, "task_name": existing.get("task_name"), "new_priority": normalized}

    async def add_comment(self, db_id: str, task_name: str, comment_text: str) -> dict[str, Any]:
        task = await self.get_task_details(db_id, task_name)
        if not task.get("ok"):
            return task
        existing = task["task"]
        page_id = existing.get("page_id")
        if not page_id:
            return {"ok": False, "error": "task_page_id_missing"}

        timestamp = now_in_tz(self._timezone).strftime("%Y-%m-%d %H:%M IST")
        current_comments = existing.get("comments") or ""
        new_comment_block = f"[{timestamp}] {comment_text}"
        merged = new_comment_block if not current_comments else f"{new_comment_block}\n---\n{current_comments}"

        payload = {
            "properties": {
                "Comments": {"rich_text": [{"type": "text", "text": {"content": merged[:1900]}}]}
            }
        }
        await self._patch_page(page_id, payload)
        return {"ok": True, "task_name": existing.get("task_name"), "comment_added": new_comment_block}

    async def get_today_tasks(self, db_id: str) -> dict[str, Any]:
        today = today_in_tz(self._timezone)
        today_iso = today.isoformat()
        tasks = await self._fetch_all_tasks(db_id)
        filtered: list[dict[str, Any]] = []
        for task in tasks:
            if self._is_completed(task):
                continue
            due = self._parse_deadline_date(task.get("deadline"))
            if due and due == today:
                filtered.append(task)

        def assignee_sort_key(task: dict[str, Any]) -> tuple[str, str]:
            assignees = self._coerce_people_list(task.get("assigned_to"))
            assignee_label = assignees[0] if assignees else ""
            name = task.get("task_name") or ""
            group = "1" if assignees else "0"
            return (group, assignee_label.lower(), name.lower())

        filtered.sort(key=assignee_sort_key)
        return {
            "ok": True,
            "date": today_iso,
            "count": len(filtered),
            "tasks": [
                {
                    "task_name": t.get("task_name"),
                    "assignee": t.get("assigned_to"),
                    "priority": t.get("priority"),
                    "project": t.get("project"),
                }
                for t in filtered
            ],
        }

    async def get_overdue_tasks(self, db_id: str, assignee: str | None = None) -> dict[str, Any]:
        today = today_in_tz(self._timezone)
        tasks = await self._fetch_all_tasks(db_id)
        filtered: list[dict[str, Any]] = []
        for task in tasks:
            if assignee and not self._assignee_matches(task, assignee):
                continue
            if self._is_completed(task):
                continue
            due = self._parse_deadline_date(task.get("deadline"))
            if due and due < today:
                filtered.append(task)
        filtered.sort(key=lambda t: t.get("deadline") or "9999-12-31")
        return {"ok": True, "count": len(filtered), "tasks": [self._to_brief_task(t) for t in filtered]}

    async def get_upcoming_deadlines(
        self,
        db_id: str,
        days: int = 3,
        assignee: str | None = None,
    ) -> dict[str, Any]:
        today = today_in_tz(self._timezone)
        end = today + timedelta(days=max(days, 0))
        tasks = await self._fetch_all_tasks(db_id)
        filtered: list[dict[str, Any]] = []
        for task in tasks:
            if assignee and not self._assignee_matches(task, assignee):
                continue
            if self._is_completed(task):
                continue
            due = self._parse_deadline_date(task.get("deadline"))
            if due and today <= due <= end:
                filtered.append(task)
        filtered.sort(key=lambda t: t.get("deadline") or "9999-12-31")
        return {"ok": True, "count": len(filtered), "tasks": [self._to_brief_task(t) for t in filtered]}

    async def bulk_update_status(
        self,
        db_id: str,
        task_names: list[str],
        new_status: str,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for task_name in task_names:
            try:
                res = await self.update_status(task_name=task_name, new_status=new_status, db_id=db_id)
                results.append({"task_name": task_name, "result": res})
            except Exception as exc:
                results.append({"task_name": task_name, "result": {"ok": False, "error": str(exc)}})
        return {"ok": True, "new_status": new_status, "results": results}

    async def generate_standup_report(self, db_id: str, assignee: str | None = None) -> dict[str, Any]:
        tasks = await self._fetch_all_tasks(db_id)
        if assignee:
            tasks = [task for task in tasks if self._assignee_matches(task, assignee)]

        today = today_in_tz(self._timezone)
        week_ago = today - timedelta(days=7)

        completed = [
            task for task in tasks if self._is_completed(task) and self._in_date_range(task.get("deadline"), week_ago, today)
        ]
        in_progress = [task for task in tasks if (task.get("status") or "").lower() == "in progress"]
        overdue = [
            task
            for task in tasks
            if not self._is_completed(task)
            and (self._parse_deadline_date(task.get("deadline")) or today) < today
        ]
        upcoming_res = await self.get_upcoming_deadlines(db_id=db_id, days=3, assignee=assignee)
        upcoming = upcoming_res.get("tasks", [])

        def lines(items: list[dict[str, Any]]) -> list[str]:
            if not items:
                return ["- None"]
            return [
                f"- {item.get('task_name')} ({item.get('deadline') or 'No deadline'})"
                for item in items
            ]

        report = "\n".join(
            [
                "*Standup Report*",
                "",
                "*COMPLETED (last 7 days)*",
                *lines([self._to_brief_task(t) for t in completed]),
                "",
                "*IN PROGRESS*",
                *lines([self._to_brief_task(t) for t in in_progress]),
                "",
                "*OVERDUE*",
                *lines([self._to_brief_task(t) for t in overdue]),
                "",
                "*UPCOMING (3 days)*",
                *lines(upcoming),
            ]
        )
        return {"ok": True, "report_markdown": report}

    async def detect_blockers(self, db_id: str) -> dict[str, Any]:
        tasks = await self._fetch_all_tasks(db_id)
        today = today_in_tz(self._timezone)
        blockers: list[dict[str, Any]] = []
        for task in tasks:
            status = (task.get("status") or "").strip().lower()
            priority = (task.get("priority") or "").strip().lower()
            due = self._parse_deadline_date(task.get("deadline"))
            if status == "in progress" and due and due < today:
                blockers.append(
                    {
                        "task_name": task.get("task_name"),
                        "assignee": task.get("assigned_to"),
                        "reason": "In Progress but overdue",
                    }
                )
            if status == "yet to start" and priority == "high" and due and due <= today + timedelta(days=5):
                blockers.append(
                    {
                        "task_name": task.get("task_name"),
                        "assignee": task.get("assigned_to"),
                        "reason": "High priority and close deadline while not started",
                    }
                )
        return {"ok": True, "count": len(blockers), "blockers": blockers}

    async def suggest_reassignment(self, db_id: str, task_name: str) -> dict[str, Any]:
        task_res = await self.get_task_details(db_id, task_name)
        if not task_res.get("ok"):
            return task_res
        task = task_res["task"]
        current_list = self._coerce_people_list(task.get("assigned_to"))

        workload_res = await self.get_team_workload(db_id)
        workload = workload_res.get("team_workload", {})
        candidates: list[tuple[str, int]] = []
        for assignee, data in workload.items():
            if current_list:
                if any(
                    self._normalize_identity(assignee) == self._normalize_identity(current)
                    for current in current_list
                ):
                    continue
            score = int(data.get("in_progress", 0)) + int(data.get("yet_to_start", 0))
            candidates.append((assignee, score))
        candidates.sort(key=lambda x: x[1])

        top = [
            {
                "assignee": assignee,
                "score": score,
                "reason": f"Lower active load ({score} active tasks).",
            }
            for assignee, score in candidates[:2]
        ]
        return {
            "ok": True,
            "task_name": task.get("task_name"),
            "current_assignee": current_list,
            "recommendations": top,
        }

    async def get_project_summary(self, db_id: str, project_name: str | None = None) -> dict[str, Any]:
        tasks = await self._fetch_all_tasks(db_id)
        grouped: dict[str, dict[str, Any]] = {}
        for task in tasks:
            project = task.get("project") or "Unspecified"
            if project_name and project.strip().lower() != project_name.strip().lower():
                continue
            entry = grouped.setdefault(
                project,
                {
                    "yet_to_start": 0,
                    "in_progress": 0,
                    "completed": 0,
                    "nearest_deadline": None,
                    "tasks": [],
                },
            )
            status = (task.get("status") or "").strip().lower()
            if status == "yet to start":
                entry["yet_to_start"] += 1
            elif status == "in progress":
                entry["in_progress"] += 1
            elif status == "completed":
                entry["completed"] += 1
            deadline = task.get("deadline")
            if deadline and (entry["nearest_deadline"] is None or deadline < entry["nearest_deadline"]):
                entry["nearest_deadline"] = deadline
            entry["tasks"].append(self._to_brief_task(task))
        return {"ok": True, "project_name": project_name, "projects": grouped}

    async def update_deadline(self, task_name: str, new_deadline: str, db_id: str) -> dict[str, Any]:
        task = await self.get_task_details(db_id, task_name)
        if not task.get("ok"):
            return task
        existing = task["task"]
        page_id = existing.get("page_id")
        if not page_id:
            return {"ok": False, "error": "task_page_id_missing"}

        payload = {"properties": {"Deadline": {"date": {"start": new_deadline}}}}
        await self._patch_page(page_id, payload)
        return {"ok": True, "task_name": existing.get("task_name"), "new_deadline": new_deadline}

    async def update_status(self, task_name: str, new_status: str, db_id: str) -> dict[str, Any]:
        task = await self.get_task_details(db_id, task_name)
        if not task.get("ok"):
            return task
        existing = task["task"]
        page_id = existing.get("page_id")
        if not page_id:
            return {"ok": False, "error": "task_page_id_missing"}

        payload = {"properties": {"Status": {"status": {"name": new_status}}}}
        await self._patch_page(page_id, payload)
        return {"ok": True, "task_name": existing.get("task_name"), "new_status": new_status}

    async def create_task(
        self,
        task_name: str,
        db_id: str,
        assigned_to: Any = None,
        people_involved: Any = None,
        deadline: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        project: str | None = None,
        client_name: str | None = None,
        client_info: str | None = None,
        comments: str | None = None,
    ) -> dict[str, Any]:
        defaults = await self._get_creation_defaults(db_id)
        props = defaults["properties"]
        resolved_assigned_to = assigned_to or defaults["assigned_to"]
        resolved_deadline = deadline or defaults["deadline"]
        resolved_priority = priority or defaults["priority"]
        resolved_status = status or defaults["status"]
        resolved_project = project or defaults["project"]
        resolved_client_name = client_name or defaults["client_name"]
        resolved_client_info = client_info or ""
        resolved_comments = comments or ""

        assigned_values = self._resolve_option_list(props, "Assigned To", resolved_assigned_to)
        if not assigned_values:
            assigned_values = self._resolve_option_list(props, "Assigned To", defaults["assigned_to"])
        people_values = self._resolve_option_list(props, "People Involved", people_involved)

        resolved_priority = self._resolve_option(props, "Priority", resolved_priority)
        resolved_status = self._resolve_option(props, "Status", resolved_status)
        resolved_project = self._resolve_option(props, "Project", resolved_project)
        resolved_client_name = self._resolve_option(props, "Client Name", resolved_client_name)

        assigned_payload = self._build_people_property(props, "Assigned To", assigned_values)
        if not assigned_payload:
            assigned_payload = {"multi_select": []}
        people_payload = self._build_people_property(props, "People Involved", people_values)

        payload = {
            "parent": {"database_id": db_id},
            "properties": {
                "Task Name": {"title": [{"type": "text", "text": {"content": task_name}}]},
                "Project": {"select": {"name": resolved_project}},
                "Client Name": {"select": {"name": resolved_client_name}},
                "Client Info": {
                    "rich_text": [{"type": "text", "text": {"content": resolved_client_info}}]
                },
                "Deadline": {"date": {"start": resolved_deadline}},
                "Assigned To": assigned_payload,
                "Priority": {"select": {"name": resolved_priority}},
                "Status": {"status": {"name": resolved_status}},
                "Comments": {"rich_text": [{"type": "text", "text": {"content": resolved_comments}}]},
            },
        }
        if people_payload:
            payload["properties"]["People Involved"] = people_payload

        created = await self._create_page(payload)
        return {
            "ok": True,
            "task_name": task_name,
            "page_id": created.get("id"),
            "resolved_fields": {
                "project": resolved_project,
                "client_name": resolved_client_name,
                "client_info": resolved_client_info,
                "deadline": resolved_deadline,
                "assigned_to": assigned_values,
                "people_involved": people_values,
                "priority": resolved_priority,
                "status": resolved_status,
                "comments": resolved_comments,
            },
        }

    async def list_tasks(
        self,
        db_id: str,
        assigned_to: str | None = None,
        include_completed: bool = True,
    ) -> dict[str, Any]:
        data = await self._query_database(db_id, {"page_size": 100})
        tasks = [self._to_task_summary(page) for page in data.get("results", [])]

        if assigned_to:
            tasks = [task for task in tasks if self._assignee_matches(task, assigned_to)]
        if not include_completed:
            tasks = [task for task in tasks if not self._is_completed(task)]

        tasks.sort(
            key=lambda t: (
                t.get("deadline") or "9999-12-31",
                t.get("priority") or "",
                t.get("task_name") or "",
            )
        )
        return {
            "ok": True,
            "assigned_to_filter": assigned_to,
            "include_completed": include_completed,
            "count": len(tasks),
            "tasks": tasks,
        }

    async def _find_task_page_id(self, task_name: str, db_id: str) -> str | None:
        payload: dict[str, Any] = {"page_size": 100}
        data = await self._query_database(db_id, payload)
        target = task_name.strip().lower()
        for page in data.get("results", []):
            summary = self._to_task_summary(page)
            if (summary.get("task_name") or "").strip().lower() == target:
                return page.get("id")
        return None

    async def _query_database(self, db_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/databases/{db_id}/query"
        logger.info("notion_query db_id=%s payload=%s", db_id, payload)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers=self._headers(), json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text
            logger.error(
                "notion_query_failed db_id=%s status=%s response=%s",
                db_id,
                response.status_code,
                detail,
            )
            raise RuntimeError(
                f"Notion query failed ({response.status_code}) for db {db_id}: {detail}"
            ) from exc
        logger.info("notion_query_ok db_id=%s", db_id)
        return response.json()

    async def get_database_schema(self, db_id: str) -> dict[str, Any]:
        url = f"{self._base_url}/databases/{db_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=self._headers())
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text
            raise RuntimeError(
                f"Notion schema fetch failed ({response.status_code}) for db {db_id}: {detail}"
            ) from exc

        data = response.json()
        properties = data.get("properties", {})
        schema: dict[str, Any] = {}
        for name, value in sorted(properties.items(), key=lambda item: item[0].lower()):
            if not isinstance(value, dict):
                schema[name] = {"type": None}
                continue
            prop_type = value.get("type")
            entry: dict[str, Any] = {"type": prop_type}
            if prop_type == "select":
                entry["options"] = [
                    opt.get("name")
                    for opt in value.get("select", {}).get("options", [])
                    if isinstance(opt, dict) and opt.get("name")
                ]
            elif prop_type == "status":
                entry["options"] = [
                    opt.get("name")
                    for opt in value.get("status", {}).get("options", [])
                    if isinstance(opt, dict) and opt.get("name")
                ]
            elif prop_type == "multi_select":
                entry["options"] = [
                    opt.get("name")
                    for opt in value.get("multi_select", {}).get("options", [])
                    if isinstance(opt, dict) and opt.get("name")
                ]
            schema[name] = entry
        return {
            "database_id": data.get("id", db_id),
            "title": self._extract_database_title(data),
            "properties": schema,
        }

    async def query_smoke_test(self, db_id: str) -> dict[str, Any]:
        data = await self._query_database(db_id, {"page_size": 1})
        return {"result_count": len(data.get("results", []))}

    async def debug_list_tasks(self, db_id: str, page_size: int = 100) -> dict[str, Any]:
        data = await self._query_database(db_id, {"page_size": page_size})
        tasks = [self._to_task_summary(page) for page in data.get("results", [])]
        unique_assignees = sorted(
            {
                assignee
                for task in tasks
                for assignee in self._coerce_people_list(task.get("assigned_to"))
                if assignee
            }
        )
        return {
            "total_tasks": len(tasks),
            "unique_assignees": unique_assignees,
            "tasks": tasks,
        }

    async def _fetch_all_tasks(self, db_id: str) -> list[dict[str, Any]]:
        data = await self._query_database(db_id, {"page_size": 100})
        return [self._to_task_summary(page) for page in data.get("results", [])]

    async def _patch_page(self, page_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/pages/{page_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.patch(url, headers=self._headers(), json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text
            raise RuntimeError(
                f"Notion update failed ({response.status_code}) for page {page_id}: {detail}"
            ) from exc
        return response.json()

    async def _create_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/pages"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers=self._headers(), json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text
            raise RuntimeError(
                f"Notion create page failed ({response.status_code}): {detail}"
            ) from exc
        return response.json()

    @staticmethod
    def _to_task_summary(page: dict[str, Any]) -> dict[str, Any]:
        properties = page.get("properties", {})

        name = ""
        title_items = NotionTools._as_dict(properties.get("Task Name")).get("title", [])
        if title_items:
            name = title_items[0].get("plain_text", "")

        deadline = NotionTools._as_dict(
            NotionTools._as_dict(properties.get("Deadline")).get("date")
        )
        priority = NotionTools._as_dict(
            NotionTools._as_dict(properties.get("Priority")).get("select")
        )
        status = NotionTools._as_dict(
            NotionTools._as_dict(properties.get("Status")).get("status")
        )
        assigned_prop = NotionTools._as_dict(properties.get("Assigned To"))
        if assigned_prop.get("type") == "multi_select" or assigned_prop.get("multi_select") is not None:
            assigned_to = [
                opt.get("name")
                for opt in assigned_prop.get("multi_select", [])
                if isinstance(opt, dict) and opt.get("name")
            ]
        else:
            assigned_to = NotionTools._as_dict(assigned_prop.get("select")).get("name")
        people_prop = NotionTools._as_dict(properties.get("People Involved"))
        if people_prop.get("type") == "multi_select" or people_prop.get("multi_select") is not None:
            people_involved = [
                opt.get("name")
                for opt in people_prop.get("multi_select", [])
                if isinstance(opt, dict) and opt.get("name")
            ]
        else:
            people_involved = []
        project = NotionTools._as_dict(NotionTools._as_dict(properties.get("Project")).get("select"))
        client_name = NotionTools._as_dict(
            NotionTools._as_dict(properties.get("Client Name")).get("select")
        )
        client_info_items = NotionTools._as_dict(properties.get("Client Info")).get("rich_text", [])
        comments_items = NotionTools._as_dict(properties.get("Comments")).get("rich_text", [])

        return {
            "page_id": page.get("id"),
            "task_name": name,
            "deadline": deadline.get("start"),
            "priority": priority.get("name"),
            "status": status.get("name"),
            "assigned_to": assigned_to,
            "project": project.get("name"),
            "client_name": client_name.get("name"),
            "client_info": "".join(
                item.get("plain_text", "") for item in client_info_items if isinstance(item, dict)
            ),
            "people_involved": people_involved,
            "comments": "".join(
                item.get("plain_text", "") for item in comments_items if isinstance(item, dict)
            ),
            "last_edited_time": page.get("last_edited_time"),
        }

    @staticmethod
    def _assignee_matches(task: dict[str, Any], assigned_to: str) -> bool:
        task_assignees = NotionTools._coerce_people_list(task.get("assigned_to"))
        if not task_assignees:
            return False
        requested_assignee = NotionTools._normalize_identity(assigned_to)
        if not requested_assignee:
            return False
        return any(
            NotionTools._normalize_identity(assignee) == requested_assignee
            for assignee in task_assignees
        )

    @staticmethod
    def _is_completed(task: dict[str, Any]) -> bool:
        return (task.get("status") or "").strip().lower() == "completed"

    @staticmethod
    def _normalize_identity(raw_value: str) -> str:
        normalized = (raw_value or "").strip().lower()
        # Treat @name and name as equivalent for Telegram/Notion matching.
        if normalized.startswith("@"):
            normalized = normalized[1:]
        # Handle occasional quoted values from model-generated tool arguments.
        normalized = normalized.strip("\"'`“”")
        # Collapse repeated whitespace for display-name style identities.
        normalized = " ".join(normalized.split())
        return normalized

    @staticmethod
    def _coerce_people_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, tuple):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if "," in raw:
                parts = [p.strip() for p in raw.split(",")]
                return [p for p in parts if p]
            return [raw]
        return []

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _recommend_low_capacity_date(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        today = today_in_tz(self._timezone)
        start_date = today + timedelta(days=1)
        window_days = 14
        end_date = start_date + timedelta(days=window_days - 1)

        load_by_date: dict[date, int] = {
            start_date + timedelta(days=offset): 0 for offset in range(window_days)
        }

        for task in tasks:
            deadline_text = task.get("deadline")
            deadline_date = self._parse_deadline_date(deadline_text)
            if not deadline_date:
                continue
            if start_date <= deadline_date <= end_date:
                load_by_date[deadline_date] += 1

        min_load = min(load_by_date.values()) if load_by_date else 0
        candidate_date = min(
            (day for day, load in load_by_date.items() if load == min_load),
            default=start_date,
        )

        return {
            "recommended_deadline": candidate_date.isoformat(),
            "daily_load": {day.isoformat(): load for day, load in load_by_date.items()},
            "analysis_window_days": window_days,
        }

    @staticmethod
    def _parse_deadline_date(deadline_text: str | None) -> date | None:
        if not deadline_text:
            return None
        try:
            # Accept both date and datetime values from Notion's start field.
            if "T" in deadline_text:
                return datetime.fromisoformat(deadline_text.replace("Z", "+00:00")).date()
            return date.fromisoformat(deadline_text)
        except ValueError:
            return None

    @staticmethod
    def _extract_database_title(database_obj: dict[str, Any]) -> str:
        title_items = database_obj.get("title", [])
        if not title_items:
            return ""
        return "".join(item.get("plain_text", "") for item in title_items).strip()

    @staticmethod
    def _to_brief_task(task: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_name": task.get("task_name"),
            "assigned_to": task.get("assigned_to"),
            "people_involved": task.get("people_involved"),
            "deadline": task.get("deadline"),
            "status": task.get("status"),
            "priority": task.get("priority"),
        }

    def _apply_task_filters(self, tasks: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
        status = filters.get("status")
        assignee = filters.get("assignee")
        priority = filters.get("priority")
        date_range = filters.get("date_range") or {}
        start_date = self._parse_deadline_date(date_range.get("start")) if isinstance(date_range, dict) else None
        end_date = self._parse_deadline_date(date_range.get("end")) if isinstance(date_range, dict) else None

        filtered: list[dict[str, Any]] = []
        for task in tasks:
            if status and (task.get("status") or "").strip().lower() != str(status).strip().lower():
                continue
            if assignee and not self._assignee_matches(task, str(assignee)):
                continue
            if priority and (task.get("priority") or "").strip().lower() != str(priority).strip().lower():
                continue
            due = self._parse_deadline_date(task.get("deadline"))
            if start_date and (due is None or due < start_date):
                continue
            if end_date and (due is None or due > end_date):
                continue
            filtered.append(task)
        return filtered

    def _in_date_range(self, deadline_text: str | None, start: date, end: date) -> bool:
        due = self._parse_deadline_date(deadline_text)
        if not due:
            return False
        return start <= due <= end

    async def _get_creation_defaults(self, db_id: str) -> dict[str, Any]:
        schema = await self.get_database_schema(db_id)
        props = schema.get("properties", {})
        tasks_info = await self.debug_list_tasks(db_id, page_size=100)
        unique_assignees = tasks_info.get("unique_assignees", [])

        today = today_in_tz(self._timezone)

        assignee_options = self._extract_property_options(props, "Assigned To")
        unassigned = None
        for opt in assignee_options:
            if opt.strip().lower() == "unassigned":
                unassigned = opt
                break

        return {
            "properties": props,
            "project": self._first_option_name(props, "Project", fallback="General"),
            "client_name": self._first_option_name(props, "Client Name", fallback="Client"),
            "assigned_to": (
                unassigned
                if unassigned
                else (unique_assignees[0] if unique_assignees else self._first_option_name(props, "Assigned To", fallback="Unassigned"))
            ),
            "priority": self._first_matching_option(
                props, "Priority", preferred=["Medium", "High", "Low"], fallback="Medium"
            ),
            "status": self._first_matching_option(
                props,
                "Status",
                preferred=["Yet to Start", "In Progress", "Completed"],
                fallback="Yet to Start",
            ),
            "deadline": (today + timedelta(days=1)).isoformat(),
        }

    def _resolve_option(self, properties: dict[str, Any], property_name: str, value: str) -> str:
        if not value:
            return value
        options = self._extract_property_options(properties, property_name)
        if not options:
            return value
        target_norm = self._normalize_option_value(value)
        norm_map = {self._normalize_option_value(opt): opt for opt in options if opt}
        if target_norm in norm_map:
            return norm_map[target_norm]
        best: tuple[int, str | None] = (0, None)
        for opt in options:
            score = int(fuzz.token_set_ratio(value.lower(), opt.lower()))
            if score > best[0]:
                best = (score, opt)
        if best[0] >= 90 and best[1]:
            return best[1]
        return value

    @staticmethod
    def _normalize_option_value(value: str) -> str:
        return "".join(ch for ch in value.lower().strip() if ch.isalnum())

    def _resolve_option_list(
        self,
        properties: dict[str, Any],
        property_name: str,
        values: Any,
    ) -> list[str]:
        items = self._coerce_people_list(values)
        resolved: list[str] = []
        seen: set[str] = set()
        for value in items:
            hit = self._resolve_option(properties, property_name, value)
            norm = self._normalize_option_value(hit)
            if norm and norm not in seen:
                resolved.append(hit)
                seen.add(norm)
        return resolved

    def _build_people_property(
        self,
        properties: dict[str, Any],
        property_name: str,
        values: list[str],
    ) -> dict[str, Any] | None:
        if not values:
            return None
        if property_name not in properties:
            return None
        prop = properties.get(property_name, {})
        prop_type = prop.get("type") if isinstance(prop, dict) else None
        if prop_type == "multi_select" or len(values) > 1:
            return {"multi_select": [{"name": v} for v in values]}
        return {"select": {"name": values[0]}}

    def _first_option_name(self, properties: dict[str, Any], property_name: str, fallback: str) -> str:
        options = self._extract_property_options(properties, property_name)
        return options[0] if options else fallback

    def _first_matching_option(
        self,
        properties: dict[str, Any],
        property_name: str,
        preferred: list[str],
        fallback: str,
    ) -> str:
        options = self._extract_property_options(properties, property_name)
        option_map = {opt.lower(): opt for opt in options}
        for pref in preferred:
            if pref.lower() in option_map:
                return option_map[pref.lower()]
        if options:
            return options[0]
        return fallback

    @staticmethod
    def _extract_property_options(properties: dict[str, Any], property_name: str) -> list[str]:
        prop = properties.get(property_name, {})
        if not isinstance(prop, dict):
            return []
        if isinstance(prop.get("options"), list):
            return [opt for opt in prop.get("options", []) if isinstance(opt, str) and opt]
        prop_type = prop.get("type")
        if prop_type == "select":
            options = prop.get("select", {}).get("options", [])
            return [opt.get("name") for opt in options if isinstance(opt, dict) and opt.get("name")]
        if prop_type == "status":
            options = prop.get("status", {}).get("options", [])
            return [opt.get("name") for opt in options if isinstance(opt, dict) and opt.get("name")]
        if prop_type == "multi_select":
            options = prop.get("multi_select", {}).get("options", [])
            return [opt.get("name") for opt in options if isinstance(opt, dict) and opt.get("name")]
        return []
