from types import SimpleNamespace


def test_memory_ranking_applies_owner_confidence_recency_and_provenance():
    from src.context_orchestrator import rank_memories

    now = 2_000_000_000
    memories = [
        {
            "id": "a",
            "owner": "alice",
            "text": "Alice prefers dark mode",
            "source": "user_explicit",
            "timestamp": now - 100,
            "metadata": {"confidence": 0.95, "inferred": False},
        },
        {
            "id": "b",
            "owner": "bob",
            "text": "Alice prefers light mode",
            "source": "user_explicit",
            "timestamp": now - 100,
            "metadata": {"confidence": 0.99},
        },
        {
            "id": "c",
            "owner": "alice",
            "text": "Alice likes dark mode",
            "source": "auto",
            "timestamp": now - 100,
            "metadata": {"confidence": 0.55, "inferred": True},
        },
    ]

    ranked = rank_memories(
        memories,
        "What is Alice's preferred theme?",
        owner="alice",
        limit=5,
        now=now,
    )
    assert ranked[0]["id"] == "a"
    assert all(memory["id"] != "b" for memory in ranked)


def test_stale_and_sensitive_memories_are_not_selected():
    from src.context_orchestrator import rank_memories

    now = 2_000_000_000
    memories = [
        {
            "id": "stale",
            "owner": "alice",
            "text": "Alice worked at Example in 2010",
            "source": "auto",
            "timestamp": now - (500 * 86400),
            "metadata": {"confidence": 0.9},
        },
        {
            "id": "secret",
            "owner": "alice",
            "text": "API key sk-abcdefghijklmnop",
            "source": "user_explicit",
            "timestamp": now,
            "metadata": {"confidence": 1.0},
        },
    ]

    assert rank_memories(
        memories,
        "What is Alice's API key or old job?",
        owner="alice",
        now=now,
    ) == []


def test_context_budget_is_bounded_and_sources_are_provenance_aware(tmp_path):
    from src.context_orchestrator import assemble_context
    from src.memory import MemoryManager

    manager = MemoryManager(str(tmp_path))
    manager.save([
        {
            "id": "m1",
            "owner": "alice",
            "text": "Alice is investigating a deadline",
            "source": "user_explicit",
            "timestamp": 2_000_000_000,
            "metadata": {"confidence": 0.95},
        },
    ])
    bundle = assemble_context(
        query="deadline",
        mode="chat",
        owner="alice",
        memory_manager=manager,
        max_tokens=128,
        include_recent=False,
        include_tasks=False,
    )

    assert bundle["mode"] == "chat"
    assert bundle["owner"] == "alice"
    assert bundle["estimated_tokens"] <= 128
    assert "source=user_explicit" in bundle["text"]
    assert "confidence=" in bundle["text"]


def test_secret_memory_write_is_rejected(tmp_path):
    from src.memory import MemoryManager

    manager = MemoryManager(str(tmp_path))
    try:
        manager.add_entry(
            "Authorization: Bearer abcdefgh",
            owner="alice",
            source="user_explicit",
        )
    except ValueError as exc:
        assert "Sensitive secrets" in str(exc)
    else:
        raise AssertionError("sensitive memory write should fail closed")
