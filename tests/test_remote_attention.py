import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


def _setup_attention_db(monkeypatch, tmp_path):
    import core.database as cdb
    import src.attention as attention

    base = declarative_base()

    class AttentionEvent(base):
        __tablename__ = "attention_events"

        id = Column(String, primary_key=True)
        owner = Column(String)
        attention_class = Column(String)
        urgency = Column(String)
        reason = Column(String)
        summary = Column(Text)
        task_id = Column(String)
        run_id = Column(String)
        approval_id = Column(String)
        dedupe_key = Column(String)
        status = Column(String)
        created_at = Column(DateTime)
        updated_at = Column(DateTime)
        expires_at = Column(DateTime)
        acknowledged_at = Column(DateTime)
        resolved_at = Column(DateTime)
        deliveries = Column(Text)
        audit_history = Column(Text)
        challenge_id = Column(String)
        challenge_code_hash = Column(String)
        challenge_status = Column(String)
        challenge_attempts = Column(Integer)
        challenge_expires_at = Column(DateTime)
        last_call_sid = Column(String)
        last_call_attempt_at = Column(DateTime)

    engine = create_engine(f"sqlite:///{tmp_path / 'attention.db'}")
    base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(attention, "SessionLocal", session_local)
    monkeypatch.setattr(attention, "AttentionEvent", AttentionEvent)
    monkeypatch.setattr(cdb, "AttentionEvent", AttentionEvent)
    return session_local, AttentionEvent, attention


def test_attention_classification_and_deduplication(tmp_path, monkeypatch):
    _, _, attention = _setup_attention_db(monkeypatch, tmp_path)

    assert attention.classify_attention("critical") == ("critical", "critical")
    with pytest.raises(ValueError):
        attention.classify_attention("urgent")

    first = attention.create_attention_event(
        owner="alice",
        attention_class="decision_required",
        reason="Approval required",
        summary="A safe summary",
        approval_id="approval-1",
        dedupe_key="approval:approval-1",
    )
    second = attention.create_attention_event(
        owner="alice",
        attention_class="critical",
        reason="Duplicate",
        summary="Duplicate",
        dedupe_key="approval:approval-1",
    )
    assert first is not None
    assert second is not None
    assert first.id == second.id
    assert attention.list_attention_events("alice")[0]["owner"] == "alice"
    assert attention.list_attention_events("bob") == []


def test_owner_isolation_acknowledge_and_expiry(tmp_path, monkeypatch):
    session_local, AttentionEvent, attention = _setup_attention_db(monkeypatch, tmp_path)
    event = attention.create_attention_event(
        owner="alice",
        attention_class="important",
        reason="Task needs review",
        summary="Review the task",
    )
    assert event is not None

    assert attention.acknowledge_attention_event(event.id, "bob") is False
    assert attention.acknowledge_attention_event(event.id, "alice") is True

    db = session_local()
    try:
        stored = db.query(AttentionEvent).filter(AttentionEvent.id == event.id).first()
        assert stored.status == "acknowledged"
        assert stored.acknowledged_at is not None
    finally:
        db.close()


def test_owner_bound_challenge_replay_and_wrong_action(tmp_path, monkeypatch):
    session_local, AttentionEvent, attention = _setup_attention_db(monkeypatch, tmp_path)
    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import ToolCapabilities, ToolEffect, ResultIntegrity

    store = ToolApprovalStore(ttl_seconds=600, max_pending=10)
    pending = store.create(
        owner="alice",
        session_id="session-1",
        origin_run_id="run-1",
        tool_name="bash",
        content="echo exact",
        workspace="/tmp",
        external_untrusted_context_seen=True,
        capabilities=ToolCapabilities(
            frozenset({ToolEffect.EXTERNAL_SIDE_EFFECT}),
            ResultIntegrity.EXTERNAL_UNTRUSTED,
        ),
    )
    monkeypatch.setattr("src.tool_approvals.tool_approval_store", store)

    event = attention.create_attention_event(
        owner="alice",
        attention_class="decision_required",
        reason="Approval required",
        summary="Safe summary",
        approval_id=pending.approval_id,
        dedupe_key=f"approval:{pending.approval_id}",
    )
    public, code = attention.start_attention_challenge(event.id, "alice")
    assert public is not None
    assert code is not None and len(code) == 6

    assert attention.verify_attention_challenge(event.id, "bob", code)[0] is False
    assert attention.verify_attention_challenge(event.id, "alice", "000000")[0] is False
    assert attention.verify_attention_challenge(event.id, "alice", code)[0] is True
    assert attention.verify_attention_challenge(event.id, "alice", code)[0] is False
    assert store.peek(pending.approval_id) is None


def test_challenge_expiry_fails_closed(tmp_path, monkeypatch):
    session_local, AttentionEvent, attention = _setup_attention_db(monkeypatch, tmp_path)
    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import ToolCapabilities, ToolEffect

    store = ToolApprovalStore(ttl_seconds=600, max_pending=10)
    pending = store.create(
        owner="alice",
        session_id="session-1",
        origin_run_id="run-1",
        tool_name="bash",
        content="echo exact",
        workspace="/tmp",
        external_untrusted_context_seen=False,
        capabilities=ToolCapabilities(frozenset({ToolEffect.EXTERNAL_SIDE_EFFECT})),
    )
    monkeypatch.setattr("src.tool_approvals.tool_approval_store", store)
    event = attention.create_attention_event(
        owner="alice",
        attention_class="decision_required",
        reason="Approval required",
        summary="Safe summary",
        approval_id=pending.approval_id,
    )
    _, code = attention.start_attention_challenge(event.id, "alice")

    db = session_local()
    try:
        stored = db.query(AttentionEvent).filter(AttentionEvent.id == event.id).first()
        stored.challenge_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    assert attention.verify_attention_challenge(event.id, "alice", code) == (
        False,
        "challenge_expired",
    )
    assert store.peek(pending.approval_id) is not None


@pytest.mark.asyncio
async def test_mock_phone_call_is_attempted_and_status_is_idempotent(tmp_path, monkeypatch):
    session_local, AttentionEvent, attention = _setup_attention_db(monkeypatch, tmp_path)

    monkeypatch.setattr(
        attention,
        "send_ntfy_notification",
        lambda **_kwargs: asyncio.sleep(0, result={"status": "sent", "id": "ntfy-1"}),
    )
    monkeypatch.setattr(attention, "get_setting", lambda key, default=None: {
        "attention_phone_escalation_enabled": True,
        "attention_telephony_provider": "mock",
        "attention_decision_required_call": True,
        "attention_critical_call": True,
        "attention_critical_quiet_hours_override": False,
        "attention_quiet_hours_start": "",
        "attention_quiet_hours_end": "",
        "attention_event_ttl_minutes": 60,
        "attention_challenge_ttl_minutes": 10,
        "attention_max_calls_per_hour": 2,
        "attention_call_cooldown_minutes": 30,
        "attention_max_call_attempts": 1,
    }.get(key, default))

    class FakeKeys:
        def load(self):
            return {
                "twilio_to_number": "+910000000000",
                "twilio_from_number": "+910000000001",
                "twilio_voice_url": "https://example.test/voice",
                "twilio_status_callback_url": "https://example.test/status",
            }

    event = attention.create_attention_event(
        owner="alice",
        attention_class="decision_required",
        reason="Approval required",
        summary="Safe summary",
        approval_id="approval-1",
    )
    manager = attention.AttentionManager(FakeKeys())
    await manager.process_event(event.id)

    db = session_local()
    try:
        stored = db.query(AttentionEvent).filter(AttentionEvent.id == event.id).first()
        assert stored.status == "calling"
        assert stored.last_call_sid == f"mock-{event.id}"
        assert sum(
            1 for item in json.loads(stored.deliveries)
            if item.get("channel") == "phone" and item.get("action") == "initiated"
        ) == 1
    finally:
        db.close()

    assert await manager.process_twilio_callback(f"mock-{event.id}", "ringing") is True
    assert await manager.process_twilio_callback(f"mock-{event.id}", "ringing") is True
    assert await manager.process_twilio_callback(f"mock-{event.id}", "completed") is True
    assert await manager.process_twilio_callback(f"mock-{event.id}", "ringing") is True
    await manager.process_event(event.id)

    db = session_local()
    try:
        stored = db.query(AttentionEvent).filter(AttentionEvent.id == event.id).first()
        assert sum(
            1 for item in json.loads(stored.deliveries)
            if item.get("channel") == "phone" and item.get("action") == "initiated"
        ) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_routine_and_unconfigured_attention_never_initiate_phone(tmp_path, monkeypatch):
    session_local, AttentionEvent, attention = _setup_attention_db(monkeypatch, tmp_path)

    async def fake_ntfy(**_kwargs):
        return {"status": "sent", "id": "ntfy-1"}

    monkeypatch.setattr(attention, "send_ntfy_notification", fake_ntfy)
    monkeypatch.setattr(attention, "get_setting", lambda key, default=None: {
        "attention_phone_escalation_enabled": False,
        "attention_telephony_provider": "disabled",
    }.get(key, default))

    event = attention.create_attention_event(
        owner="alice",
        attention_class="routine",
        reason="Task complete",
        summary="Safe completion summary",
    )
    manager = attention.AttentionManager()
    await manager.process_event(event.id)

    db = session_local()
    try:
        stored = db.query(AttentionEvent).filter(AttentionEvent.id == event.id).first()
        assert not any(
            item.get("channel") == "phone"
            for item in json.loads(stored.deliveries)
        )
    finally:
        db.close()


def test_twilio_status_signature_validation_fails_closed():
    from src.telephony import TwilioTelephonyProvider

    provider = TwilioTelephonyProvider(
        account_sid="ACtest",
        auth_token="test-auth-token",
        webhook_secret="test-webhook-secret",
    )
    assert provider.validate_status_callback(
        callback_url="https://example.test/api/attention/twilio/status",
        params={"CallSid": "CAtest", "CallStatus": "ringing"},
        signature="invalid",
    ) is False


class _VoiceKeyManager:
    def __init__(self, include_config=True):
        self.include_config = include_config

    def load(self):
        if not self.include_config:
            return {}
        return {
            "twilio_account_sid": "ACtest",
            "twilio_auth_token": "test-auth-token",
            "twilio_webhook_secret": "test-webhook-secret",
            "twilio_voice_url": "https://voice.example.test/api/attention/twilio/voice/{event_id}",
        }


def _voice_test_client(keys):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.attention_routes import setup_attention_routes

    app = FastAPI()
    app.include_router(setup_attention_routes(keys))
    return TestClient(app)


def test_twilio_voice_get_valid_signature_and_no_approval():
    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        import pytest
        pytest.skip("Twilio SDK unavailable")

    client = _voice_test_client(_VoiceKeyManager())
    callback_url = (
        "https://voice.example.test/api/attention/twilio/voice/event-1"
        "?Digits=123456"
    )
    signature = RequestValidator("test-webhook-secret").compute_signature(
        callback_url,
        {},
    )
    response = client.get(
        "/api/attention/twilio/voice/event-1?Digits=123456",
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "event-1" not in response.text
    assert "does not approve or reject anything" in response.text


def test_twilio_voice_post_valid_signature_and_form_params():
    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        import pytest
        pytest.skip("Twilio SDK unavailable")

    client = _voice_test_client(_VoiceKeyManager())
    callback_url = "https://voice.example.test/api/attention/twilio/voice/event-2"
    form = {"Digits": "654321", "CallSid": "CAtest"}
    signature = RequestValidator("test-webhook-secret").compute_signature(
        callback_url,
        form,
    )
    response = client.post(
        "/api/attention/twilio/voice/event-2",
        data=form,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert "CallSid" not in response.text
    assert "654321" not in response.text


def test_twilio_voice_missing_signature_fails_closed():
    client = _voice_test_client(_VoiceKeyManager())
    response = client.post(
        "/api/attention/twilio/voice/event-3",
        data={"Digits": "123456"},
    )
    assert response.status_code == 403


def test_twilio_voice_invalid_signature_fails_closed():
    client = _voice_test_client(_VoiceKeyManager())
    response = client.post(
        "/api/attention/twilio/voice/event-4",
        data={"Digits": "123456"},
        headers={"X-Twilio-Signature": "invalid"},
    )
    assert response.status_code == 403


def test_twilio_voice_unavailable_configuration_fails_closed():
    client = _voice_test_client(None)
    response = client.get(
        "/api/attention/twilio/voice/event-5",
        headers={"X-Twilio-Signature": "anything"},
    )
    assert response.status_code == 503

    client = _voice_test_client(_VoiceKeyManager(include_config=False))
    response = client.post(
        "/api/attention/twilio/voice/event-5",
        data={"Digits": "123456"},
        headers={"X-Twilio-Signature": "anything"},
    )
    assert response.status_code == 503


def test_twilio_voice_callback_cannot_consume_approval(monkeypatch):
    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        import pytest
        pytest.skip("Twilio SDK unavailable")

    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import ToolCapabilities, ToolEffect

    store = ToolApprovalStore(ttl_seconds=600, max_pending=10)
    pending = store.create(
        owner="alice",
        session_id="session-1",
        origin_run_id="run-1",
        tool_name="bash",
        content="echo exact",
        workspace="/tmp",
        external_untrusted_context_seen=True,
        capabilities=ToolCapabilities(frozenset({ToolEffect.EXTERNAL_SIDE_EFFECT})),
    )
    monkeypatch.setattr("src.tool_approvals.tool_approval_store", store)

    client = _voice_test_client(_VoiceKeyManager())
    callback_url = "https://voice.example.test/api/attention/twilio/voice/event-6"
    form = {"Digits": "approve", "approval_id": pending.approval_id}
    signature = RequestValidator("test-webhook-secret").compute_signature(
        callback_url,
        form,
    )
    response = client.post(
        "/api/attention/twilio/voice/event-6",
        data=form,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert store.peek(pending.approval_id) is not None
