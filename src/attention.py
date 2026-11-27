"""Unified remote-attention events, notifications, and safe escalation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from core.database import AttentionEvent, SessionLocal
from src.settings import get_setting
from services.notification.ntfy import send_ntfy_notification
from src.telephony import (
    DisabledTelephonyProvider,
    MockTelephonyProvider,
    TelephonyError,
    TwilioTelephonyProvider,
)

logger = logging.getLogger(__name__)

ATTENTION_CLASSES = frozenset({"routine", "important", "decision_required", "critical"})
TERMINAL_STATUSES = frozenset({"resolved", "acknowledged", "expired", "cancelled"})
CALL_STATUS_RANK = {
    "queued": 1,
    "initiated": 2,
    "ringing": 3,
    "in-progress": 4,
    "answered": 5,
    "completed": 6,
    "busy": 4,
    "failed": 6,
    "no-answer": 6,
    "canceled": 6,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_list(value: Any) -> list:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _bounded_json_list(value: Any, limit: int) -> list:
    entries = _json_list(value)
    return entries[-limit:]


def _append_json(event: AttentionEvent, column: str, entry: dict, limit: int) -> None:
    entries = _bounded_json_list(getattr(event, column), limit)
    entries.append(entry)
    setattr(event, column, json.dumps(entries[-limit:], ensure_ascii=False))


def _normalized_owner(owner: Any) -> str:
    return str(owner or "").strip().casefold()


def classify_attention(
    attention_class: str,
    *,
    reason: str = "",
) -> tuple[str, str]:
    normalized = str(attention_class or "").strip().lower()
    if normalized not in ATTENTION_CLASSES:
        raise ValueError("Invalid attention class")
    urgency = {
        "routine": "low",
        "important": "normal",
        "decision_required": "high",
        "critical": "critical",
    }[normalized]
    return normalized, urgency


def create_attention_event(
    *,
    owner: Any,
    attention_class: str,
    reason: str,
    summary: str,
    task_id: str = "",
    run_id: str = "",
    approval_id: str = "",
    dedupe_key: str = "",
    ttl_minutes: int | None = None,
) -> AttentionEvent | None:
    """Create or return a single pending event for an owner/dedupe key."""
    normalized_owner = _normalized_owner(owner)
    normalized_class, urgency = classify_attention(attention_class, reason=reason)
    ttl = (
        max(1, min(int(ttl_minutes), 1440))
        if ttl_minutes is not None
        else max(1, min(int(get_setting("attention_event_ttl_minutes", 60) or 60), 1440))
    )
    dedupe = str(dedupe_key or "").strip()[:160]
    db = SessionLocal()
    try:
        if dedupe:
            existing = db.query(AttentionEvent).filter(
                AttentionEvent.owner == normalized_owner,
                AttentionEvent.dedupe_key == dedupe,
                AttentionEvent.status.notin_(tuple(TERMINAL_STATUSES)),
            ).order_by(AttentionEvent.created_at.desc()).first()
            if existing:
                return existing
        event = AttentionEvent(
            id=f"atn_{secrets.token_urlsafe(18)}",
            owner=normalized_owner or None,
            attention_class=normalized_class,
            urgency=urgency,
            reason=str(reason or "")[:500],
            summary=str(summary or "")[:500],
            task_id=str(task_id or "")[:128] or None,
            run_id=str(run_id or "")[:128] or None,
            approval_id=str(approval_id or "")[:128] or None,
            dedupe_key=dedupe or None,
            status="pending",
            expires_at=utcnow() + timedelta(minutes=ttl),
            deliveries="[]",
            audit_history="[]",
        )
        _append_json(event, "audit_history", {
            "at": utcnow().isoformat() + "Z",
            "action": "created",
            "attention_class": normalized_class,
        }, 50)
        db.add(event)
        db.commit()
        db.refresh(event)
        return event
    except Exception:
        db.rollback()
        logger.exception("Failed to persist remote attention event")
        return None
    finally:
        db.close()


def record_attention_audit(event_id: str, action: str, **details: Any) -> None:
    safe_details = {
        str(key)[:50]: str(value)[:500]
        for key, value in details.items()
        if str(key).lower() not in {"authorization", "secret", "token", "password", "api_key"}
    }
    db = SessionLocal()
    try:
        event = db.query(AttentionEvent).filter(AttentionEvent.id == event_id).first()
        if not event:
            return
        _append_json(event, "audit_history", {
            "at": utcnow().isoformat() + "Z",
            "action": str(action)[:80],
            **safe_details,
        }, 50)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to record attention audit")
    finally:
        db.close()


def list_attention_events(owner: str, *, include_resolved: bool = False, limit: int = 50) -> list[dict]:
    db = SessionLocal()
    try:
        query = db.query(AttentionEvent).filter(AttentionEvent.owner == _normalized_owner(owner))
        if not include_resolved:
            query = query.filter(AttentionEvent.status.notin_(tuple(TERMINAL_STATUSES)))
        rows = query.order_by(AttentionEvent.created_at.desc()).limit(max(1, min(limit, 200))).all()
        return [_event_public(row) for row in rows]
    finally:
        db.close()


def _event_public(event: AttentionEvent) -> dict:
    return {
        "id": event.id,
        "owner": event.owner,
        "attention_class": event.attention_class,
        "urgency": event.urgency,
        "reason": event.reason,
        "summary": event.summary,
        "task_id": event.task_id,
        "run_id": event.run_id,
        "approval_id": event.approval_id,
        "status": event.status,
        "created_at": event.created_at.isoformat() + "Z" if event.created_at else None,
        "expires_at": event.expires_at.isoformat() + "Z" if event.expires_at else None,
        "acknowledged_at": event.acknowledged_at.isoformat() + "Z" if event.acknowledged_at else None,
        "resolved_at": event.resolved_at.isoformat() + "Z" if event.resolved_at else None,
        "deliveries": _json_list(event.deliveries),
        "audit_history": _json_list(event.audit_history),
        "challenge": {
            "id": event.challenge_id,
            "status": event.challenge_status,
            "expires_at": event.challenge_expires_at.isoformat() + "Z" if event.challenge_expires_at else None,
            "attempts": event.challenge_attempts,
        } if event.challenge_id else None,
    }


def get_attention_event(event_id: str) -> dict | None:
    db = SessionLocal()
    try:
        event = db.query(AttentionEvent).filter(AttentionEvent.id == event_id).first()
        return _event_public(event) if event else None
    finally:
        db.close()


def acknowledge_attention_event(event_id: str, owner: str) -> bool:
    db = SessionLocal()
    try:
        event = db.query(AttentionEvent).filter(
            AttentionEvent.id == event_id,
            AttentionEvent.owner == _normalized_owner(owner),
        ).first()
        if not event or event.status in TERMINAL_STATUSES:
            return False
        event.status = "acknowledged"
        event.acknowledged_at = utcnow()
        _append_json(event, "audit_history", {
            "at": utcnow().isoformat() + "Z",
            "action": "acknowledged",
        }, 50)
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False
    finally:
        db.close()


def resolve_attention_event(event_id: str, owner: str, *, action: str = "resolved") -> bool:
    from src.tool_approvals import tool_approval_store
    db = SessionLocal()
    try:
        event = db.query(AttentionEvent).filter(
            AttentionEvent.id == event_id,
            AttentionEvent.owner == _normalized_owner(owner),
        ).first()
        if not event:
            return False
        if event.approval_id:
            pending = tool_approval_store.peek(
                event.approval_id,
                owner=owner,
            )
            if pending and pending.owner == _normalized_owner(owner):
                tool_approval_store.consume(
                    event.approval_id,
                    decision="deny",
                    owner=owner,
                    session_id=pending.session_id,
                    allow_continuation=False,
                )
        event.status = "resolved"
        event.resolved_at = utcnow()
        _append_json(event, "audit_history", {
            "at": utcnow().isoformat() + "Z",
            "action": str(action)[:80],
        }, 50)
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False
    finally:
        db.close()


def start_attention_challenge(event_id: str, owner: str) -> tuple[dict | None, str | None]:
    from src.tool_approvals import tool_approval_store

    db = SessionLocal()
    try:
        event = db.query(AttentionEvent).filter(
            AttentionEvent.id == event_id,
            AttentionEvent.owner == _normalized_owner(owner),
        ).first()
        if not event or event.status in TERMINAL_STATUSES or not event.approval_id:
            return None, None
        pending = tool_approval_store.peek(
            event.approval_id,
            owner=owner,
        )
        if not pending or pending.owner != _normalized_owner(owner):
            return None, None
        code = f"{secrets.randbelow(900000) + 100000}"
        ttl = max(1, min(int(get_setting("attention_challenge_ttl_minutes", 10) or 10), 60))
        event.challenge_id = f"chg_{secrets.token_urlsafe(18)}"
        event.challenge_code_hash = hashlib.sha256(
            f"{event.id}:{code}".encode("utf-8")
        ).hexdigest()
        event.challenge_status = "pending"
        event.challenge_attempts = 0
        event.challenge_expires_at = utcnow() + timedelta(minutes=ttl)
        _append_json(event, "audit_history", {
            "at": utcnow().isoformat() + "Z",
            "action": "challenge_started",
            "challenge_id": event.challenge_id,
        }, 50)
        db.commit()
        public = _event_public(event)
        return public, code
    except Exception:
        db.rollback()
        return None, None
    finally:
        db.close()


def verify_attention_challenge(
    event_id: str,
    owner: str,
    code: str,
) -> tuple[bool, str]:
    """Verify and consume the existing exact tool approval; no phone answer grants anything."""
    from src.tool_approvals import tool_approval_store

    db = SessionLocal()
    try:
        event = db.query(AttentionEvent).filter(
            AttentionEvent.id == event_id,
            AttentionEvent.owner == _normalized_owner(owner),
        ).first()
        if not event or event.challenge_status != "pending":
            return False, "challenge_invalid"
        if not event.challenge_expires_at or event.challenge_expires_at <= utcnow():
            event.challenge_status = "expired"
            event.status = "acknowledged"
            _append_json(event, "audit_history", {
                "at": utcnow().isoformat() + "Z",
                "action": "challenge_expired",
            }, 50)
            db.commit()
            return False, "challenge_expired"
        event.challenge_attempts = int(event.challenge_attempts or 0) + 1
        if event.challenge_attempts > 5:
            event.challenge_status = "locked"
            _append_json(event, "audit_history", {
                "at": utcnow().isoformat() + "Z",
                "action": "challenge_locked",
            }, 50)
            db.commit()
            return False, "challenge_locked"
        supplied = hashlib.sha256(
            f"{event.id}:{str(code or '').strip()}".encode("utf-8")
        ).hexdigest()
        if not secrets.compare_digest(supplied, str(event.challenge_code_hash or "")):
            db.commit()
            return False, "challenge_invalid"
        pending = tool_approval_store.peek(
            event.approval_id,
            owner=owner,
        )
        if not pending or pending.owner != _normalized_owner(owner):
            event.challenge_status = "rejected"
            db.commit()
            return False, "approval_not_owner"
        exact = tool_approval_store.consume(
            event.approval_id,
            decision="approve",
            owner=owner,
            session_id=pending.session_id,
            allow_continuation=False,
        )
        if exact is None:
            event.challenge_status = "rejected"
            db.commit()
            return False, "approval_invalid"
        event.challenge_status = "approved"
        event.status = "resolved"
        event.resolved_at = utcnow()
        _append_json(event, "audit_history", {
            "at": utcnow().isoformat() + "Z",
            "action": "challenge_approved",
            "challenge_id": event.challenge_id,
        }, 50)
        db.commit()
        return True, "approved"
    except Exception:
        db.rollback()
        return False, "challenge_invalid"
    finally:
        db.close()


def _delivery_entries(event: AttentionEvent) -> list:
    return _json_list(event.deliveries)


def _call_attempt_count(event: AttentionEvent) -> int:
    return sum(
        1 for item in _delivery_entries(event)
        if item.get("channel") == "phone" and item.get("action") == "initiated"
    )


def _hourly_call_count(db) -> int:
    cutoff = utcnow() - timedelta(hours=1)
    events = db.query(AttentionEvent).filter(
        AttentionEvent.last_call_attempt_at >= cutoff
    ).all()
    count = 0
    for event in events:
        count += sum(
            1 for item in _delivery_entries(event)
            if item.get("channel") == "phone"
            and item.get("action") == "initiated"
            and str(item.get("at", "")) >= cutoff.isoformat()
        )
    return count


def _in_quiet_hours() -> bool:
    start = str(get_setting("attention_quiet_hours_start", "") or "").strip()
    end = str(get_setting("attention_quiet_hours_end", "") or "").strip()
    if not start or not end:
        return False
    try:
        now = _tz_now().time()
        sh, sm = (int(part) for part in start.split(":", 1))
        eh, em = (int(part) for part in end.split(":", 1))
        start_minutes = sh * 60 + sm
        end_minutes = eh * 60 + em
        now_minutes = now.hour * 60 + now.minute
        if start_minutes <= end_minutes:
            return start_minutes <= now_minutes < end_minutes
        return now_minutes >= start_minutes or now_minutes < end_minutes
    except (TypeError, ValueError):
        return False


def _tz_now() -> datetime:
    try:
        from src.user_time import user_timezone
        tz = user_timezone()
        if tz is not None:
            return datetime.now(tz)
    except Exception:
        pass
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))


class AttentionManager:
    def __init__(self, api_key_manager=None) -> None:
        self.api_key_manager = api_key_manager
        self._provider = DisabledTelephonyProvider()
        self._loop_task: asyncio.Task | None = None

    def _load_provider(self):
        provider_name = str(get_setting("attention_telephony_provider", "disabled") or "disabled").lower()
        if provider_name == "mock":
            self._provider = MockTelephonyProvider()
            return
        if provider_name != "twilio":
            self._provider = DisabledTelephonyProvider()
            return
        secrets_map = {}
        if self.api_key_manager is not None:
            try:
                secrets_map = self.api_key_manager.load()
            except Exception as exc:
                logger.warning("Could not load telephony credentials: %s", type(exc).__name__)
                secrets_map = {}
        elif hasattr(__import__("app", fromlist=["api_key_manager"]), "api_key_manager"):
            pass
        account_sid = str(secrets_map.get("twilio_account_sid") or "").strip()
        auth_token = str(secrets_map.get("twilio_auth_token") or "").strip()
        webhook_secret = str(secrets_map.get("twilio_webhook_secret") or "").strip()
        self._provider = TwilioTelephonyProvider(
            account_sid=account_sid,
            auth_token=auth_token,
            webhook_secret=webhook_secret,
        )

    def get_provider(self):
        self._load_provider()
        return self._provider

    async def process_event(self, event_id: str) -> None:
        db = SessionLocal()
        try:
            event = db.query(AttentionEvent).filter(AttentionEvent.id == event_id).first()
            if not event or event.status in TERMINAL_STATUSES:
                return
            if event.expires_at and event.expires_at <= utcnow():
                event.status = "expired"
                event.resolved_at = utcnow()
                _append_json(event, "audit_history", {
                    "at": utcnow().isoformat() + "Z",
                    "action": "expired",
                }, 50)
                db.commit()
                return

            ntfy_delivered = any(
                item.get("channel") == "ntfy"
                and item.get("action") in {"sent", "failed"}
                for item in _delivery_entries(event)
            )
            if not ntfy_delivered:
                result = await send_ntfy_notification(
                    message=event.summary,
                    title=f"Jarvis {event.attention_class.replace('_', ' ').title()}",
                    priority={"routine": "default", "important": "high", "decision_required": "urgent", "critical": "urgent"}[event.attention_class],
                    tags=["jarvis", event.attention_class],
                )
                action = "sent" if result.get("status") == "sent" else "failed"
                _append_json(event, "deliveries", {
                    "at": utcnow().isoformat() + "Z",
                    "channel": "ntfy",
                    "action": action,
                }, 20)
                db.commit()

            should_call = (
                bool(get_setting("attention_phone_escalation_enabled", False))
                and (
                    (event.attention_class == "decision_required" and bool(get_setting("attention_decision_required_call", True)))
                    or (event.attention_class == "critical" and bool(get_setting("attention_critical_call", True)))
                )
            )
            if not should_call:
                return
            attempts = _call_attempt_count(event)
            max_attempts = max(1, min(int(get_setting("attention_max_call_attempts", 1) or 1), 3))
            if attempts >= max_attempts:
                return
            if event.last_call_attempt_at:
                cooldown = max(1, min(int(get_setting("attention_call_cooldown_minutes", 30) or 30), 1440))
                if event.last_call_attempt_at > utcnow() - timedelta(minutes=cooldown):
                    return
            quiet = _in_quiet_hours()
            if quiet and not (
                event.attention_class == "critical"
                and bool(get_setting("attention_critical_quiet_hours_override", False))
            ):
                return
            db.commit()

            secrets_map = {}
            if self.api_key_manager is not None:
                try:
                    secrets_map = self.api_key_manager.load()
                except Exception:
                    secrets_map = {}
            to_number = str(secrets_map.get("twilio_to_number") or "").strip()
            from_number = str(secrets_map.get("twilio_from_number") or "").strip()
            voice_url = str(secrets_map.get("twilio_voice_url") or "").strip()
            status_callback_url = str(secrets_map.get("twilio_status_callback_url") or "").strip()
            try:
                from src.telephony import resolve_voice_callback_url
                voice_url = resolve_voice_callback_url(voice_url, event.id)
            except Exception:
                pass
            provider_name = str(get_setting("attention_telephony_provider", "disabled") or "disabled").lower()

            if provider_name == "mock":
                provider = MockTelephonyProvider()
            elif provider_name == "twilio":
                provider = TwilioTelephonyProvider(
                    account_sid=str(secrets_map.get("twilio_account_sid") or "").strip(),
                    auth_token=str(secrets_map.get("twilio_auth_token") or "").strip(),
                    webhook_secret=str(secrets_map.get("twilio_webhook_secret") or "").strip(),
                )
            else:
                provider = DisabledTelephonyProvider()

            if not provider.configured():
                _append_json(event, "deliveries", {
                    "at": utcnow().isoformat() + "Z",
                    "channel": "phone",
                    "action": "disabled",
                }, 20)
                db.commit()
                return
            if _hourly_call_count(db) >= max(1, min(int(get_setting("attention_max_calls_per_hour", 2) or 2), 20)):
                _append_json(event, "deliveries", {
                    "at": utcnow().isoformat() + "Z",
                    "channel": "phone",
                    "action": "rate_limited",
                }, 20)
                db.commit()
                return
            try:
                result = await provider.initiate_call(
                    event_id=event.id,
                    to_number=to_number,
                    from_number=from_number,
                    voice_url=voice_url,
                    status_callback_url=status_callback_url,
                )
                event.last_call_sid = result.call_id
                event.last_call_attempt_at = utcnow()
                event.status = "calling"
                _append_json(event, "deliveries", {
                    "at": utcnow().isoformat() + "Z",
                    "channel": "phone",
                    "action": "initiated",
                    "provider": result.provider,
                }, 20)
                _append_json(event, "audit_history", {
                    "at": utcnow().isoformat() + "Z",
                    "action": "phone_call_initiated",
                    "provider": result.provider,
                }, 50)
            except TelephonyError as exc:
                event.last_call_attempt_at = utcnow()
                _append_json(event, "deliveries", {
                    "at": utcnow().isoformat() + "Z",
                    "channel": "phone",
                    "action": "failed",
                    "reason": type(exc).__name__,
                }, 20)
                _append_json(event, "audit_history", {
                    "at": utcnow().isoformat() + "Z",
                    "action": "phone_call_failed",
                }, 50)
            db.commit()
        finally:
            db.close()

    async def process_twilio_callback(self, call_sid: str, status: str) -> bool:
        db = SessionLocal()
        try:
            event = db.query(AttentionEvent).filter(
                AttentionEvent.last_call_sid == str(call_sid or "")
            ).first()
            if not event:
                return False
            normalized = str(status or "").strip().lower()
            deliveries = _delivery_entries(event)
            for item in reversed(deliveries):
                if item.get("provider_call_status"):
                    previous = str(item["provider_call_status"])
                    if normalized == previous:
                        return True
                    if CALL_STATUS_RANK.get(normalized, 0) < CALL_STATUS_RANK.get(previous, 0):
                        return True
            _append_json(event, "deliveries", {
                "at": utcnow().isoformat() + "Z",
                "channel": "phone",
                "action": "status",
                "provider_call_status": normalized,
            }, 20)
            if normalized in {"completed", "busy", "failed", "no-answer", "canceled"}:
                if event.status != "resolved":
                    event.status = "acknowledged"
            db.commit()
            record_attention_audit(event.id, "phone_status", status=normalized)
            return True
        except Exception:
            db.rollback()
            return False
        finally:
            db.close()

    async def sweep(self) -> None:
        db = SessionLocal()
        try:
            now = utcnow()
            expired = db.query(AttentionEvent).filter(
                AttentionEvent.status.notin_(tuple(TERMINAL_STATUSES)),
                AttentionEvent.expires_at <= now,
            ).all()
            for event in expired:
                event.status = "expired"
                event.resolved_at = now
                _append_json(event, "audit_history", {
                    "at": now.isoformat() + "Z",
                    "action": "expired",
                }, 50)
            db.commit()
            pending = db.query(AttentionEvent).filter(
                AttentionEvent.status.in_(("pending", "calling")),
            ).all()
            ids = [event.id for event in pending]
        finally:
            db.close()
        for event_id in ids:
            await self.process_event(event_id)

    async def loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Remote attention sweep failed")
            await asyncio.sleep(30)


_attention_manager: AttentionManager | None = None


def get_attention_manager(api_key_manager=None) -> AttentionManager:
    global _attention_manager
    if _attention_manager is None:
        _attention_manager = AttentionManager(api_key_manager)
    return _attention_manager
