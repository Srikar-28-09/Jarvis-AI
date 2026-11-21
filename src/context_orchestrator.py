"""Privacy-aware shared context selection for chat, voice, tasks, and recovery.

This module selects from existing memory and task systems. It does not create
another store, queue, or model manager.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any, Iterable

_SECRET_RE = re.compile(
    r"(?i)(?:api[_ -]?key|authorization|password|passwd|secret|private[_ -]?key|"
    r"bearer\s+[A-Za-z0-9._~-]+|(?:sk|pk)[-_][A-Za-z0-9]{12,})"
)


def memory_confidence(memory: dict) -> float:
    metadata = memory.get("metadata") if isinstance(memory.get("metadata"), dict) else {}
    raw = metadata.get("confidence", memory.get("confidence"))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        source = str(memory.get("source") or "unknown").lower()
        value = 0.9 if source in {"user", "user_explicit"} else 0.55 if source == "auto" else 0.4
    return max(0.0, min(1.0, value))


def is_memory_stale(memory: dict, now: float | None = None, max_age_days: float = 365.0) -> bool:
    try:
        timestamp = float(memory.get("timestamp") or 0)
    except (TypeError, ValueError):
        return True
    if timestamp <= 0:
        return True
    if timestamp < 100000:
        return False
    age_days = max(0.0, ((now or time.time()) - timestamp) / 86400.0)
    return age_days > max_age_days


def is_sensitive_memory(memory: dict) -> bool:
    text = str(memory.get("text") or "")
    return bool(_SECRET_RE.search(text))


def memory_is_inferred(memory: dict) -> bool:
    metadata = memory.get("metadata") if isinstance(memory.get("metadata"), dict) else {}
    return bool(
        metadata.get("inferred")
        or str(memory.get("source") or "").lower() in {"auto", "inferred", "llm"}
    )


def owner_visible(memory: dict, owner: str | None) -> bool:
    if owner is None:
        return True
    return str(memory.get("owner") or "") == str(owner)


def _jaccard(a: str, b: str) -> float:
    tokens_a = set(re.findall(r"[a-z0-9]+", a.lower()))
    tokens_b = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / max(1, len(tokens_a | tokens_b))


def _contradictory(a: str, b: str) -> bool:
    a_low = a.lower()
    b_low = b.lower()
    pairs = (
        ("likes ", "dislikes "),
        ("prefers ", "does not prefer "),
        ("uses ", "does not use "),
        ("is active", "is inactive"),
        ("completed", "not completed"),
    )
    return any(
        (left in a_low and right in b_low) or (right in a_low and left in b_low)
        for left, right in pairs
    )


def rank_memories(
    memories: Iterable[dict],
    query: str,
    *,
    owner: str | None,
    limit: int = 5,
    now: float | None = None,
) -> list[dict]:
    """Rank existing memories with owner, privacy, provenance, confidence and recency."""
    now = now or time.time()
    query_words = {
        word for word in re.findall(r"[a-z0-9]+", str(query or "").lower())
        if len(word) >= 3
    }
    candidates = []
    seen = set()
    selected_text = []
    for memory in memories:
        if not isinstance(memory, dict) or not owner_visible(memory, owner):
            continue
        if is_sensitive_memory(memory):
            continue
        text = str(memory.get("text") or "").strip()
        if not text:
            continue
        if is_memory_stale(memory, now=now):
            continue
        text_key = re.sub(r"\s+", " ", text.lower()).strip()
        if text_key in seen:
            continue
        if any(_jaccard(text_key, prior) >= 0.82 for prior in selected_text):
            continue
        seen.add(text_key)
        selected_text.append(text_key)

        words = set(re.findall(r"[a-z0-9]+", text_key))
        overlap = len(query_words & words) / max(1, len(query_words))
        try:
            age_days = max(0.0, (now - float(memory.get("timestamp") or 0)) / 86400.0)
        except (TypeError, ValueError):
            age_days = 730.0
        recency = math.exp(-age_days / 180.0)
        confidence = memory_confidence(memory)
        stale = is_memory_stale(memory, now=now)
        if stale:
            confidence *= 0.35
        contradiction = any(
            _contradictory(text_key, prior)
            for prior in selected_text[:-1]
        )
        if contradiction:
            confidence *= 0.60
        provenance = 0.15 if str(memory.get("source") or "").lower() in {"user", "user_explicit"} else 0.08
        if memory_is_inferred(memory):
            confidence *= 0.8
        score = (0.50 * overlap) + (0.25 * confidence) + (0.15 * recency) + provenance
        candidates.append((score, memory))

    candidates.sort(key=lambda item: (item[0], item[1].get("timestamp", 0)), reverse=True)
    return [memory for score, memory in candidates[:max(0, limit)] if score >= 0.12]


def format_selected_memories(memories: Iterable[dict]) -> str:
    lines = []
    for memory in memories:
        source = str(memory.get("source") or "unknown")
        confidence = round(memory_confidence(memory), 2)
        qualifier = "inferred" if memory_is_inferred(memory) else "stored fact"
        stale = " stale" if is_memory_stale(memory) else ""
        text = str(memory.get("text") or "").strip()
        if text:
            lines.append(f"- [{qualifier}; source={source}; confidence={confidence:.2f}{stale}] {text}")
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(str(text or "")) / 4))


def assemble_context(
    *,
    query: str,
    mode: str,
    owner: str | None,
    session: Any = None,
    memory_manager: Any = None,
    max_tokens: int = 1200,
    include_recent: bool = True,
    include_tasks: bool = True,
) -> dict:
    """Build a bounded shared context bundle for chat, voice, tasks, or recovery."""
    max_tokens = max(128, min(int(max_tokens or 1200), 4000))
    selected_memories = []
    try:
        if memory_manager is not None:
            entries = memory_manager.load(owner=owner) if owner is not None else memory_manager.load_all()
            selected_memories = rank_memories(entries, query, owner=owner, limit=5)
    except Exception:
        selected_memories = []

    recent = []
    if include_recent and session is not None:
        try:
            messages = session.get_context_messages()
            for message in messages[-6:]:
                if isinstance(message, dict):
                    role = str(message.get("role") or "")
                    content = str(message.get("content") or "")
                    if role in {"user", "assistant"} and content:
                        recent.append({"role": role, "content": content[:1000]})
        except Exception:
            recent = []

    tasks = []
    if include_tasks:
        try:
            from core.database import SessionLocal, ScheduledTask, TaskRun
            db = SessionLocal()
            try:
                query_db = db.query(ScheduledTask).filter(
                    ScheduledTask.status == "active"
                )
                if owner is not None:
                    query_db = query_db.filter(ScheduledTask.owner == owner)
                active = query_db.order_by(ScheduledTask.updated_at.desc()).limit(8).all()
                for task in active:
                    run = db.query(TaskRun).filter(
                        TaskRun.task_id == task.id
                    ).order_by(TaskRun.started_at.desc()).first()
                    checkpoint = ""
                    if run:
                        try:
                            import json
                            raw = json.loads(run.steps or "{}")
                            checkpoint = str(raw.get("stage") or "")
                        except Exception:
                            checkpoint = ""
                    tasks.append({
                        "id": task.id,
                        "name": str(task.name or "")[:120],
                        "status": str(task.status or ""),
                        "checkpoint": checkpoint[:80],
                    })
            finally:
                db.close()
        except Exception:
            tasks = []

    sections = []
    if selected_memories:
        sections.append("Stored and inferred memory candidates:\n" + format_selected_memories(selected_memories))
    if tasks:
        task_lines = [
            f"- {task['name']} (id={task['id']}; status={task['status']}; checkpoint={task['checkpoint'] or 'none'})"
            for task in tasks
        ]
        sections.append("Active tasks:\n" + "\n".join(task_lines))
    if recent:
        recent_lines = [f"{item['role']}: {item['content']}" for item in recent]
        sections.append("Recent conversation:\n" + "\n".join(recent_lines))

    text = "\n\n".join(sections)
    while text and estimate_tokens(text) > max_tokens:
        # Trim recent conversation first, then memories/tasks through bounded content.
        if recent:
            recent.pop(0)
            recent_lines = [f"{item['role']}: {item['content']}" for item in recent]
            sections = [section for section in sections if not section.startswith("Recent conversation:")]
            if recent_lines:
                sections.append("Recent conversation:\n" + "\n".join(recent_lines))
        elif tasks:
            tasks.pop()
            sections = [section for section in sections if not section.startswith("Active tasks:")]
            if tasks:
                sections.append("Active tasks:\n" + "\n".join(
                    f"- {task['name']} (id={task['id']}; status={task['status']}; checkpoint={task['checkpoint'] or 'none'})"
                    for task in tasks
                ))
        elif selected_memories:
            selected_memories.pop()
            sections = [section for section in sections if not section.startswith("Stored and inferred memory")]
            if selected_memories:
                sections.append("Stored and inferred memory candidates:\n" + format_selected_memories(selected_memories))
        else:
            break
        text = "\n\n".join(sections)

    return {
        "mode": mode,
        "owner": owner,
        "memories": selected_memories,
        "recent": recent,
        "tasks": tasks,
        "text": text,
        "estimated_tokens": estimate_tokens(text),
    }
