import src.proactive as proactive


def test_proactive_disabled_is_suppressed(monkeypatch):
    monkeypatch.setattr(proactive, "get_setting", lambda key, default=None: False)
    result = proactive.evaluate_proactive_event(
        "task_failed",
        "alice",
        reason="test",
        task_id="task-1",
    )
    assert result == {"enabled": False, "created": False, "next_steps": []}


def test_proactive_meaningful_event_is_deduplicated(monkeypatch):
    proactive._last_events.clear()
    monkeypatch.setattr(
        proactive,
        "get_setting",
        lambda key, default=None: (
            True if key == "proactive_intelligence_enabled"
            else 6
        ),
    )
    created = []

    def fake_create_attention_event(**kwargs):
        created.append(kwargs)
        return type("Event", (), {"id": "atn-1"})()

    monkeypatch.setitem(
        __import__("sys").modules,
        "src.attention",
        type(
            "AttentionModule",
            (),
            {"create_attention_event": staticmethod(fake_create_attention_event)},
        ),
    )

    first = proactive.evaluate_proactive_event(
        "task_failed",
        "alice",
        reason="timeout",
        task_id="task-1",
        run_id="run-1",
    )
    second = proactive.evaluate_proactive_event(
        "task_failed",
        "alice",
        reason="timeout",
        task_id="task-1",
        run_id="run-1",
    )

    assert first["created"] is True
    assert first["next_steps"]
    assert second["deduplicated"] is True
    assert len(created) == 1


def test_proactive_routine_event_is_suppressed(monkeypatch):
    monkeypatch.setattr(
        proactive,
        "get_setting",
        lambda key, default=None: True if key == "proactive_intelligence_enabled" else default,
    )
    result = proactive.evaluate_proactive_event(
        "message_sent",
        "alice",
        reason="ordinary observation",
    )
    assert result["created"] is False
    assert result["next_steps"] == []


def test_consequential_blocker_requires_decision_not_auto_action(monkeypatch):
    proactive._last_events.clear()
    monkeypatch.setattr(
        proactive,
        "get_setting",
        lambda key, default=None: (
            True if key == "proactive_intelligence_enabled"
            else 6
        ),
    )
    captured = {}

    def fake_create_attention_event(**kwargs):
        captured.update(kwargs)
        return type("Event", (), {"id": "atn-decision"})()

    monkeypatch.setitem(
        __import__("sys").modules,
        "src.attention",
        type(
            "AttentionModule",
            (),
            {"create_attention_event": staticmethod(fake_create_attention_event)},
        ),
    )

    result = proactive.evaluate_proactive_event(
        "task_blocked",
        "alice",
        reason="exact email action waiting",
        task_id="task-2",
        approval_id="approval-2",
    )
    assert result["created"] is True
    assert captured["attention_class"] == "decision_required"
    assert "Approve or deny" in " ".join(result["next_steps"])
