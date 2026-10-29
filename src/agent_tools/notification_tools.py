"""
src/agent_tools/notification_tools.py

Agent tools for immediate push notifications and scheduled reminders via ntfy.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from services.notification.ntfy import (
    send_ntfy_notification,
    schedule_ntfy_reminder,
)

logger = logging.getLogger(__name__)


class SendNotificationTool:
    """Send an immediate push notification via ntfy."""

    async def execute(self, content: str, ctx: Optional[dict] = None) -> dict:
        raw = (content or "").strip()
        message = ""
        title = "Jarvis Notification"
        priority = None
        tags = None
        topic = None

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    message = str(parsed.get("message") or parsed.get("msg") or parsed.get("text") or "").strip()
                    title = str(parsed.get("title") or title).strip()
                    priority = parsed.get("priority")
                    tags = parsed.get("tags")
                    topic = parsed.get("topic")
            except (json.JSONDecodeError, TypeError):
                message = ""
        else:
            message = raw

        if not message:
            err_msg = "send_notification: Notification message cannot be empty. Please provide a message."
            return {
                "error": err_msg,
                "output": err_msg,
                "exit_code": 1,
            }

        res = await send_ntfy_notification(
            message=message,
            title=title,
            priority=priority,
            tags=tags,
            topic=topic,
        )

        if res.get("status") == "sent":
            out = (
                f"Notification sent successfully via ntfy.\n"
                f"Topic: {res.get('topic')}\n"
                f"Title: {res.get('title')}\n"
                f"Message: {res.get('message')}\n"
                f"Notification ID: {res.get('id')}"
            )
            return {
                "output": out,
                "exit_code": 0,
                "result": res,
            }
        else:
            err_msg = res.get("error") or "Failed to send notification via ntfy."
            return {
                "error": err_msg,
                "output": f"send_notification error: {err_msg}",
                "exit_code": 1,
            }


class ScheduleReminderTool:
    """Schedule a reminder to be sent at a specific time in the future via ntfy."""

    async def execute(self, content: str, ctx: Optional[dict] = None) -> dict:
        raw = (content or "").strip()
        message = ""
        run_at = ""
        title = "Reminder"
        priority = None
        tags = None
        topic = None

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    message = str(parsed.get("message") or parsed.get("msg") or parsed.get("text") or "").strip()
                    run_at = str(parsed.get("run_at") or parsed.get("time") or parsed.get("datetime") or "").strip()
                    title = str(parsed.get("title") or title).strip()
                    priority = parsed.get("priority")
                    tags = parsed.get("tags")
                    topic = parsed.get("topic")
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            # If plain text, handle line splitting or error
            lines = [line.strip() for line in raw.split("\n") if line.strip()]
            if len(lines) >= 2:
                message = lines[0]
                run_at = lines[1]
            elif lines:
                message = lines[0]

        if not message:
            err_msg = "schedule_reminder: Message parameter is required and cannot be empty."
            return {
                "error": err_msg,
                "output": err_msg,
                "exit_code": 1,
            }

        if not run_at:
            err_msg = (
                "schedule_reminder: run_at parameter (ISO-8601 datetime) is required. "
                "E.g. {\"message\": \"submit assignment\", \"run_at\": \"2026-09-24T20:00:00+05:30\"}."
            )
            return {
                "error": err_msg,
                "output": err_msg,
                "exit_code": 1,
            }

        owner = ctx.get("owner") if isinstance(ctx, dict) else ""

        res = schedule_ntfy_reminder(
            message=message,
            run_at=run_at,
            title=title,
            priority=priority,
            tags=tags,
            topic=topic,
            owner=owner,
        )

        if res.get("status") == "scheduled":
            out = (
                f"Reminder scheduled successfully!\n"
                f"Message: {res.get('message')}\n"
                f"Scheduled local time: {res.get('run_at_local')}\n"
                f"Scheduled UTC: {res.get('run_at_utc')}\n"
                f"Channel: ntfy (topic: {res.get('topic')})\n"
                f"Task ID: {res.get('task_id')}"
            )
            return {
                "output": out,
                "exit_code": 0,
                "result": res,
            }
        else:
            err_msg = res.get("error") or "Failed to schedule reminder."
            return {
                "error": err_msg,
                "output": f"schedule_reminder error: {err_msg}",
                "exit_code": 1,
            }
