"""
tests/test_ntfy_tools.py

Comprehensive tests for ntfy notification and scheduled reminder tools:
- Schema validation
- Error handling (empty message, invalid priority, timeout, network error)
- Timezone handling (Asia/Kolkata UTC+05:30)
- Reminder scheduling and persistence in ScheduledTask
- Tool handler execution
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock, patch
import pytest
import httpx

import src.agent_tools

from services.notification.ntfy import (
    validate_priority,
    validate_tags,
    parse_run_at,
    send_ntfy_notification,
    schedule_ntfy_reminder,
    get_ntfy_url,
    get_ntfy_topic,
)
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
from src.agent_tools import TOOL_HANDLERS
from src.agent_tools.notification_tools import SendNotificationTool, ScheduleReminderTool
from src.builtin_actions import action_send_notification, BUILTIN_ACTIONS
from core.database import SessionLocal, ScheduledTask


# ── A. Schema Tests ──

def test_send_notification_schema():
    schema = next((s for s in FUNCTION_TOOL_SCHEMAS if s.get("function", {}).get("name") == "send_notification"), None)
    assert schema is not None, "send_notification missing from FUNCTION_TOOL_SCHEMAS"
    fn = schema["function"]
    assert "parameters" in fn
    props = fn["parameters"]["properties"]
    assert "message" in props
    assert "title" in props
    assert "priority" in props
    assert "tags" in props
    assert fn["parameters"]["required"] == ["message"]


def test_schedule_reminder_schema():
    schema = next((s for s in FUNCTION_TOOL_SCHEMAS if s.get("function", {}).get("name") == "schedule_reminder"), None)
    assert schema is not None, "schedule_reminder missing from FUNCTION_TOOL_SCHEMAS"
    fn = schema["function"]
    assert "parameters" in fn
    props = fn["parameters"]["properties"]
    assert "message" in props
    assert "run_at" in props
    assert "title" in props
    assert set(fn["parameters"]["required"]) == {"message", "run_at"}


def test_function_call_to_tool_block_notification():
    block = function_call_to_tool_block("send_notification", {"message": "Test alert", "title": "Test Title"})
    assert block is not None
    assert block.tool_type == "send_notification"
    assert "Test alert" in block.content

    rem_block = function_call_to_tool_block("schedule_reminder", {"message": "Test reminder", "run_at": "2026-09-24T20:00:00+05:30"})
    assert rem_block is not None
    assert rem_block.tool_type == "schedule_reminder"
    assert "Test reminder" in rem_block.content


# ── B & C. Validation & Notification Delivery Tests ──

def test_priority_validation():
    assert validate_priority(None) == 3
    assert validate_priority("") == 3
    assert validate_priority("min") == 1
    assert validate_priority("low") == 2
    assert validate_priority("default") == 3
    assert validate_priority("high") == 4
    assert validate_priority("urgent") == 5
    assert validate_priority("1") == 1
    assert validate_priority(5) == 5
    
    with pytest.raises(ValueError):
        validate_priority("super_urgent")


def test_tags_validation():
    assert validate_tags(None) == ["bell"]
    assert validate_tags([]) == ["bell"]
    assert validate_tags(["warning", "fire"]) == ["warning", "fire"]
    assert validate_tags("bell, warning") == ["bell", "warning"]


@pytest.mark.asyncio
async def test_send_notification_empty_message():
    res = await send_ntfy_notification(message="")
    assert res["status"] == "failed"
    assert "empty" in res["error"].lower()
    assert res["exit_code"] == 1


@pytest.mark.asyncio
async def test_send_notification_invalid_priority():
    res = await send_ntfy_notification(message="Hello", priority="invalid_priority")
    assert res["status"] == "failed"
    assert "invalid priority" in res["error"].lower()


@pytest.mark.asyncio
async def test_send_notification_success():
    fake_resp = httpx.Response(
        200,
        json={"id": "msg_12345", "time": 1727200000},
        request=httpx.Request("POST", "http://ntfy:80/"),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = fake_resp
        res = await send_ntfy_notification(
            message="Assignment due tonight",
            title="School Reminder",
            priority="high",
            tags=["books", "warning"],
        )
        assert res["status"] == "sent"
        assert res["id"] == "msg_12345"
        assert res["priority"] == 4
        assert res["tags"] == ["books", "warning"]
        assert res["exit_code"] == 0


@pytest.mark.asyncio
async def test_send_notification_http_error():
    fake_resp = httpx.Response(
        500,
        text="Internal Server Error",
        request=httpx.Request("POST", "http://ntfy:80/"),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = fake_resp
        res = await send_ntfy_notification(message="Test")
        assert res["status"] == "failed"
        assert "HTTP 500" in res["error"]
        assert res["exit_code"] == 1


@pytest.mark.asyncio
async def test_send_notification_timeout():
    with patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("timed out")):
        res = await send_ntfy_notification(message="Test", timeout=1.0)
        assert res["status"] == "failed"
        assert "timed out" in res["error"].lower()
        assert res["exit_code"] == 1


@pytest.mark.asyncio
async def test_send_notification_connect_error():
    with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("connection refused")):
        res = await send_ntfy_notification(message="Test")
        assert res["status"] == "failed"
        assert "unavailable" in res["error"].lower()
        assert res["exit_code"] == 1


# ── D, E, F, G. Timezone & Schedule Reminder Tests ──

def test_parse_run_at_timezone():
    # 1. ISO string with explicit IST offset (+05:30)
    local_dt, utc_dt = parse_run_at("2026-09-24T20:00:00+05:30")
    assert utc_dt.hour == 14
    assert utc_dt.minute == 30
    assert (local_dt.utcoffset().total_seconds() == 5.5 * 3600)

    # 2. ISO string with UTC 'Z'
    local_dt2, utc_dt2 = parse_run_at("2026-09-24T14:30:00Z")
    assert utc_dt2.hour == 14
    assert utc_dt2.minute == 30

    # 3. Naive string should default to IST (UTC+05:30)
    local_dt3, utc_dt3 = parse_run_at("2026-09-24T20:00:00")
    assert (local_dt3.utcoffset().total_seconds() == 5.5 * 3600)
    assert utc_dt3.hour == 14
    assert utc_dt3.minute == 30


def test_schedule_reminder_past_rejected():
    past_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    res = schedule_ntfy_reminder(message="Past event", run_at=past_time)
    assert res["status"] == "failed"
    assert "in the past" in res["error"].lower()


def test_schedule_reminder_persistence():
    future_time = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    res = schedule_ntfy_reminder(
        message="Submit homework assignment",
        run_at=future_time,
        title="Homework",
        priority="high",
        tags=["school"],
        owner="test_user",
    )
    assert res["status"] == "scheduled"
    task_id = res["task_id"]
    assert task_id.startswith("rem_")

    # Verify task is in SQLite ScheduledTask table
    db = SessionLocal()
    task = None
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        assert task is not None
        assert task.action == "send_notification"
        assert task.status == "active"
        assert task.schedule == "once"
        payload = json.loads(task.prompt)
        assert payload["message"] == "Submit homework assignment"
        assert payload["title"] == "Homework"
        assert payload["priority"] == 4
        assert payload["tags"] == ["school"]
    finally:
        # Clean up
        if task:
            db.delete(task)
            db.commit()
        db.close()


# ── H & I. Scheduler Action Execution Tests ──

@pytest.mark.asyncio
async def test_action_send_notification_execution():
    assert "send_notification" in BUILTIN_ACTIONS
    assert "send_reminder" in BUILTIN_ACTIONS

    fake_resp = httpx.Response(
        200,
        json={"id": "msg_sched_99", "time": 1727200500},
        request=httpx.Request("POST", "http://ntfy:80/"),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = fake_resp
        payload = json.dumps({
            "title": "Assignment Reminder",
            "message": "Time to submit!",
            "priority": 4,
            "tags": ["bell"],
        })
        msg, success = await action_send_notification(owner="test_user", payload=payload)
        assert success is True
        assert "msg_sched_99" in msg


# ── J & K. Tool Handler Registration & Execution Tests ──

@pytest.mark.asyncio
async def test_tool_handlers_registered():
    assert "send_notification" in TOOL_HANDLERS
    send_handler = TOOL_HANDLERS["send_notification"]
    assert callable(send_handler)

    assert "schedule_reminder" in TOOL_HANDLERS
    sched_handler = TOOL_HANDLERS["schedule_reminder"]
    assert callable(sched_handler)

    fake_resp = httpx.Response(
        200,
        json={"id": "msg_tool_1", "time": 1727200600},
        request=httpx.Request("POST", "http://ntfy:80/"),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = fake_resp
        res = await send_handler(json.dumps({"message": "Hello from tool", "title": "Tool Alert"}))
        res_data = res.get("result", res)
        assert res_data["status"] == "sent"
        assert res_data["id"] == "msg_tool_1"

    future_time = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    rem_res = await sched_handler(json.dumps({"message": "Tool reminder", "run_at": future_time}))
    rem_data = rem_res.get("result", rem_res)
    assert rem_data["status"] == "scheduled"
    
    # Cleanup created task
    db = SessionLocal()
    try:
        t = db.query(ScheduledTask).filter(ScheduledTask.id == rem_data["task_id"]).first()
        if t:
            db.delete(t)
            db.commit()
    finally:
        db.close()
