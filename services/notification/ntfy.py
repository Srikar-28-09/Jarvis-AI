"""
services/notification/ntfy.py

Integration with the ntfy service for push notifications and scheduled reminders.
Communicates over the Docker network (http://ntfy:80) or configured integration URL.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx

logger = logging.getLogger(__name__)

# Valid priority levels accepted by ntfy (1 to 5)
PRIORITY_MAP: Dict[str, int] = {
    "min": 1,
    "1": 1,
    "low": 2,
    "2": 2,
    "default": 3,
    "3": 3,
    "normal": 3,
    "high": 4,
    "4": 4,
    "urgent": 5,
    "max": 5,
    "5": 5,
}

DEFAULT_NTFY_URL = "http://ntfy:80"
DEFAULT_TOPIC = "reminders"


def get_ntfy_url() -> str:
    """Resolve base URL for the ntfy service.
    
    Prefers:
    1. NTFY_INTERNAL_URL or NTFY_URL environment variable.
    2. Enabled integration in integrations.json if configured.
    3. Default internal Docker network URL http://ntfy:80.
    """
    env_url = (os.getenv("NTFY_INTERNAL_URL") or os.getenv("NTFY_URL") or "").strip()
    if env_url:
        return env_url.rstrip("/")

    try:
        from src.integrations import load_integrations
        intgs = load_integrations()
        ntfy_intg = next(
            (i for i in intgs if (i.get("preset") == "ntfy" or i.get("name", "").lower() == "ntfy") and i.get("enabled", True) and i.get("base_url")),
            None,
        )
        if ntfy_intg and ntfy_intg.get("base_url"):
            return ntfy_intg["base_url"].rstrip("/")
    except Exception:
        pass

    return DEFAULT_NTFY_URL


def get_ntfy_topic(requested_topic: Optional[str] = None) -> str:
    """Resolve and sanitize the ntfy topic name."""
    if requested_topic and str(requested_topic).strip():
        clean = re.sub(r"[^a-zA-Z0-9_\-]", "", str(requested_topic).strip())
        if clean:
            return clean

    try:
        from src.settings import get_setting
        topic_setting = (get_setting("reminder_ntfy_topic") or "").strip()
        if topic_setting:
            clean = re.sub(r"[^a-zA-Z0-9_\-]", "", topic_setting)
            if clean:
                return clean.lower()
    except Exception:
        pass

    return DEFAULT_TOPIC


def validate_priority(priority: Optional[Any]) -> int:
    """Validate and normalize priority to an integer between 1 and 5."""
    if priority is None or priority == "":
        return 3  # default
    
    p_str = str(priority).strip().lower()
    if p_str in PRIORITY_MAP:
        return PRIORITY_MAP[p_str]
    
    raise ValueError(
        f"Invalid priority '{priority}'. Valid priorities are: min (1), low (2), default (3), high (4), urgent (5)."
    )


def validate_tags(tags: Optional[Union[List[str], str]]) -> List[str]:
    """Normalize tags to a list of strings."""
    if not tags:
        return ["bell"]
    if isinstance(tags, list):
        clean = [str(t).strip() for t in tags if str(t).strip()]
        return clean or ["bell"]
    if isinstance(tags, str):
        clean = [t.strip() for t in tags.split(",") if t.strip()]
        return clean or ["bell"]
    return ["bell"]


async def send_ntfy_notification(
    message: str,
    title: Optional[str] = None,
    priority: Optional[Any] = None,
    tags: Optional[Union[List[str], str]] = None,
    topic: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """Publish an immediate push notification to ntfy.
    
    Returns a structured dictionary with success or error details.
    """
    msg = (message or "").strip()
    if not msg:
        return {
            "error": "Notification message cannot be empty.",
            "exit_code": 1,
            "status": "failed",
        }
    
    try:
        prio_int = validate_priority(priority)
    except ValueError as e:
        return {
            "error": str(e),
            "exit_code": 1,
            "status": "failed",
        }
    
    clean_tags = validate_tags(tags)
    clean_title = (title or "Jarvis Notification").strip()[:200]
    target_topic = get_ntfy_topic(topic)
    base_url = get_ntfy_url()
    
    payload = {
        "topic": target_topic,
        "title": clean_title,
        "message": msg,
        "priority": prio_int,
        "tags": clean_tags,
    }
    
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{base_url}/", json=payload)
            
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "status": "sent",
                    "id": data.get("id"),
                    "topic": target_topic,
                    "title": clean_title,
                    "message": msg,
                    "priority": prio_int,
                    "tags": clean_tags,
                    "time": data.get("time"),
                    "exit_code": 0,
                }
            else:
                err_msg = f"ntfy returned HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning(f"Failed to send notification: {err_msg}")
                return {
                    "error": err_msg,
                    "status": "failed",
                    "exit_code": 1,
                }
    except httpx.TimeoutException:
        err_msg = f"ntfy request timed out after {timeout}s at {base_url}"
        logger.warning(err_msg)
        return {
            "error": err_msg,
            "status": "failed",
            "exit_code": 1,
        }
    except httpx.ConnectError as e:
        err_msg = f"ntfy service unavailable at {base_url}: {e}"
        logger.warning(err_msg)
        return {
            "error": err_msg,
            "status": "failed",
            "exit_code": 1,
        }
    except Exception as e:
        err_msg = f"ntfy notification failed: {type(e).__name__}: {e}"
        logger.warning(err_msg)
        return {
            "error": err_msg,
            "status": "failed",
            "exit_code": 1,
        }


def parse_run_at(run_at_str: str) -> Tuple[datetime, datetime]:
    """Parse an ISO-8601 or datetime string into (local_datetime, utc_datetime).
    
    Accurately handles:
    - Strings with timezone offsets, e.g. "2026-09-24T20:00:00+05:30" or "...Z".
    - Naive datetimes, associating them with the user's local timezone (defaults to Asia/Kolkata).
    """
    raw = (run_at_str or "").strip()
    if not raw:
        raise ValueError("run_at parameter is required and cannot be empty.")
    
    # Handle trailing Z
    if raw.endswith("Z"):
        dt = datetime.fromisoformat(raw[:-1]).replace(tzinfo=timezone.utc)
    else:
        dt = datetime.fromisoformat(raw)
    
    # Resolve user timezone
    try:
        from src.user_time import user_timezone
        user_tz = user_timezone()
    except Exception:
        user_tz = None
        
    if user_tz == timezone.utc or user_tz is None:
        try:
            from zoneinfo import ZoneInfo
            user_tz = ZoneInfo("Asia/Kolkata")
        except Exception:
            user_tz = timezone(timedelta(hours=5, minutes=30))
            
    if dt.tzinfo is None:
        local_dt = dt.replace(tzinfo=user_tz)
    else:
        local_dt = dt.astimezone(user_tz)
        
    utc_dt = local_dt.astimezone(timezone.utc)
    return local_dt, utc_dt


def schedule_ntfy_reminder(
    message: str,
    run_at: str,
    title: Optional[str] = None,
    priority: Optional[Any] = None,
    tags: Optional[Union[List[str], str]] = None,
    topic: Optional[str] = None,
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    """Schedule a reminder to be executed and sent via ntfy.
    
    Persists the task in the database so it survives container restarts.
    """
    msg = (message or "").strip()
    if not msg:
        return {
            "error": "Reminder message cannot be empty.",
            "exit_code": 1,
            "status": "failed",
        }
    
    try:
        prio_int = validate_priority(priority)
    except ValueError as e:
        return {
            "error": str(e),
            "exit_code": 1,
            "status": "failed",
        }

    try:
        local_dt, utc_dt = parse_run_at(run_at)
    except Exception as e:
        return {
            "error": f"Invalid run_at format: {e}. Please provide a valid ISO-8601 datetime.",
            "exit_code": 1,
            "status": "failed",
        }
        
    now_utc = datetime.now(timezone.utc)
    delta_sec = (utc_dt - now_utc).total_seconds()
    
    # If more than 60 seconds in the past, reject
    if delta_sec < -60:
        return {
            "error": (
                f"Cannot schedule a reminder in the past. Requested time was "
                f"{local_dt.strftime('%Y-%m-%d %H:%M:%S %Z')} ({int(-delta_sec)}s ago)."
            ),
            "exit_code": 1,
            "status": "failed",
        }
    
    # If within 5 seconds of now (or slightly in the past due to transmission latency), clamp to +5s
    scheduled_utc = utc_dt
    if delta_sec < 5:
        scheduled_utc = now_utc + timedelta(seconds=5)
    
    clean_title = (title or "Reminder").strip()[:100]
    clean_tags = validate_tags(tags)
    target_topic = get_ntfy_topic(topic)
    
    payload = json.dumps({
        "title": clean_title,
        "message": msg,
        "priority": prio_int,
        "tags": clean_tags,
        "topic": target_topic,
    })
    
    from core.database import SessionLocal, ScheduledTask
    
    task_id = f"rem_{uuid.uuid4().hex[:12]}"
    db_scheduled_date = scheduled_utc.replace(tzinfo=None)
    
    db = SessionLocal()
    try:
        task = ScheduledTask(
            id=task_id,
            owner=owner or "",
            name=f"Reminder: {clean_title} - {msg[:30]}",
            task_type="action",
            action="send_notification",
            prompt=payload,
            schedule="once",
            scheduled_date=db_scheduled_date,
            next_run=db_scheduled_date,
            status="active",
            notifications_enabled=True,
        )
        db.add(task)
        db.commit()
    finally:
        db.close()
        
    # Wake the scheduler so it recalibrates sleep_for immediately
    try:
        from src.event_bus import get_task_scheduler
        sch = get_task_scheduler()
        if sch and hasattr(sch, "wake"):
            sch.wake()
    except Exception as e:
        logger.debug(f"Could not wake scheduler: {e}")
        
    return {
        "status": "scheduled",
        "task_id": task_id,
        "title": clean_title,
        "message": msg,
        "run_at_local": local_dt.isoformat(),
        "run_at_utc": scheduled_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "priority": prio_int,
        "tags": clean_tags,
        "topic": target_topic,
        "exit_code": 0,
    }
