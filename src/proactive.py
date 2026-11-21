"""Event-driven proactive next-step evaluation.

This is intentionally local and bounded. It uses existing AttentionEvent,
TaskRun, memory, approval, and notification systems; it never calls tools or
bypasses approval for consequential actions.
"""

from __future__ import annotations

import time
from typing import Any

from src.settings import get_setting

_last_events: dict[tuple[str, str], float] = {}


def evaluate_proactive_event(
    event_name: str,
    owner: str | None,
    *,
    reason: str = "",
    task_id: str = "",
    run_id: str = "",
    approval_id: str = "",
) -> dict:
    normalized = str(event_name or "").strip().lower()
    owner_key = str(owner or "").strip().casefold()
    if not get_setting("proactive_intelligence_enabled", False):
        return {"enabled": False, "created": False, "next_steps": []}

    allowed = {
        "task_failed",
        "task_blocked",
        "task_deadline",
        "critical_failure",
    }
    if normalized not in allowed:
        return {"enabled": True, "created": False, "next_steps": []}

    now = time.time()
    dedupe_key = f"proactive:{normalized}:{task_id or run_id or reason[:80]}"
    previous = _last_events.get((owner_key, dedupe_key), 0)
    if now - previous < 3600:
        return {"enabled": True, "created": False, "next_steps": [], "deduplicated": True}
    max_events = max(1, min(int(get_setting("proactive_max_events_per_hour", 6) or 6), 30))
    current_hour = int(now // 3600)
    relevant = [
        key for key, timestamp in _last_events.items()
        if key[0] == owner_key and int(timestamp // 3600) == current_hour
    ]
    if len(relevant) >= max_events:
        return {"enabled": True, "created": False, "next_steps": [], "rate_limited": True}

    attention_class = "critical" if normalized == "critical_failure" else "important"
    if normalized == "task_blocked" and approval_id:
        attention_class = "decision_required"

    summary = {
        "task_failed": "Jarvis found a scheduled task failure that needs review.",
        "task_blocked": "Jarvis found a task blocked on a decision.",
        "task_deadline": "Jarvis found an approaching task deadline.",
        "critical_failure": "Jarvis found a critical failure requiring attention.",
    }[normalized]
    if reason:
        summary = f"{summary} Reason: {str(reason)[:240]}"

    next_steps = []
    if normalized == "task_failed":
        next_steps = ["Review the failed task checkpoint", "Resume only after outcome reconciliation"]
    elif normalized == "task_blocked":
        next_steps = ["Review the exact pending action", "Approve or deny through the existing approval flow"]
    elif normalized == "task_deadline":
        next_steps = ["Review the approaching deadline", "Prepare the task inputs without executing consequential actions"]
    elif normalized == "critical_failure":
        next_steps = ["Review the critical failure", "Verify local executor and recovery state"]

    try:
        from src.attention import create_attention_event
        event = create_attention_event(
            owner=owner,
            attention_class=attention_class,
            reason=reason or normalized,
            summary=summary,
            task_id=task_id,
            run_id=run_id,
            approval_id=approval_id,
            dedupe_key=dedupe_key,
        )
        if event is not None:
            _last_events[(owner_key, dedupe_key)] = now
            return {"enabled": True, "created": True, "next_steps": next_steps, "event_id": event.id}
    except Exception:
        pass
    return {"enabled": True, "created": False, "next_steps": next_steps}


def list_next_steps(owner: str | None) -> list[dict]:
    """Read existing attention events as next-step suggestions, not a new queue."""
    try:
        from src.attention import list_attention_events
        events = list_attention_events(owner or "", include_resolved=False, limit=20)
        return [
            {
                "event_id": event["id"],
                "attention_class": event["attention_class"],
                "summary": event["summary"],
                "task_id": event["task_id"],
                "approval_id": event["approval_id"],
                "expires_at": event["expires_at"],
            }
            for event in events
        ]
    except Exception:
        return []
