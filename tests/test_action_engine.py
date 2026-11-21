import asyncio
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet


def _plan_data():
    return {
        "interpretation": "Gather current public information.",
        "clarification_question": None,
        "completion_criteria": ["Two public sources were checked."],
        "steps": [
            {
                "id": "first",
                "objective": "Search the first public source.",
                "dependencies": [],
                "tools": ["web_search"],
                "expected_output": "A source result.",
                "verification": "The search returned a successful result.",
                "timeout_seconds": 10,
            },
            {
                "id": "second",
                "objective": "Search the second public source.",
                "dependencies": ["first"],
                "tools": ["web_search"],
                "expected_output": "A second source result.",
                "verification": "The search returned a successful result.",
                "timeout_seconds": 10,
            },
        ],
    }


def test_plan_validation_rejects_bad_dependencies_and_tools():
    from src.action_engine import parse_plan

    invalid_dependency = _plan_data()
    invalid_dependency["steps"][0]["dependencies"] = ["second"]
    with pytest.raises(ValueError, match="ordered after"):
        parse_plan(invalid_dependency)

    unsupported_tool = _plan_data()
    unsupported_tool["steps"][0]["tools"] = ["invented_tool"]
    with pytest.raises(ValueError, match="unsupported tool"):
        parse_plan(unsupported_tool)


def test_plan_is_read_only_only_for_trusted_read_effects():
    from src.action_engine import parse_plan, plan_is_read_only, plan_requires_approval

    assert plan_is_read_only(parse_plan(_plan_data()))

    consequential = _plan_data()
    consequential["steps"][0]["tools"] = ["send_notification"]
    plan = parse_plan(consequential)
    assert plan_requires_approval(plan)
    assert not plan_is_read_only(plan)


def test_action_goal_prompt_is_encrypted_and_authentication_fail_closed(monkeypatch):
    import src.secret_storage as secret_storage
    from src.action_engine import (
        decode_task_prompt,
        encode_task_prompt,
        parse_plan,
    )

    monkeypatch.setattr(secret_storage, "_fernet", Fernet(Fernet.generate_key()))
    prompt = encode_task_prompt("Find two sources", parse_plan(_plan_data()))
    assert prompt.startswith("jarvis-action-goal:v1:enc:v2:")
    payload = decode_task_prompt(prompt)
    assert payload["goal"] == "Find two sources"

    corrupted = prompt[:-1] + ("A" if prompt[-1] != "A" else "B")
    with pytest.raises(ValueError, match="authenticated"):
        decode_task_prompt(corrupted)


def test_action_plan_runs_sequentially_with_step_tool_allowlists(monkeypatch):
    import src.agent_loop
    from src.action_engine import parse_plan
    from src.task_scheduler import TaskScheduler

    calls = []

    async def fake_stream_agent_loop(**kwargs):
        calls.append(kwargs)
        yield "data: " + json.dumps({
            "type": "tool_output",
            "tool": "web_search",
            "success": True,
            "output": f"result-{len(calls)}",
        })
        yield "data: " + json.dumps({"delta": "step complete"})

    async def no_wait(*_args, **_kwargs):
        return None

    monkeypatch.setattr(src.agent_loop, "stream_agent_loop", fake_stream_agent_loop)
    monkeypatch.setattr(
        "src.interactive_gate.wait_for_interactive_quiet",
        no_wait,
    )
    monkeypatch.setattr(
        "src.task_endpoint.resolve_task_candidates",
        lambda **_kwargs: [],
    )
    scheduler = TaskScheduler.__new__(TaskScheduler)

    async def verify(**_kwargs):
        return True

    monkeypatch.setattr(scheduler, "_verify_action_step", verify)
    task = SimpleNamespace(
        prompt="encrypted marker",
        name="Action goal",
        owner="alice",
        max_steps=20,
    )

    result = asyncio.run(scheduler._run_agent_loop(
        endpoint_url="https://model.invalid/v1",
        model="test-model",
        task=task,
        session_id="session-1",
        action_goal="Find two sources",
        action_plan=parse_plan(_plan_data()),
    ))

    assert len(calls) == 2
    assert [call["tool_allowlist"] for call in calls] == [
        {"web_search"},
        {"web_search"},
    ]
    assert "Current step (first)" in calls[0]["messages"][-1]["content"]
    assert "Current step (second)" in calls[1]["messages"][-1]["content"]
    assert all(
        "encrypted marker" not in str(call["messages"])
        for call in calls
    )
    assert "step complete" in result


def test_failed_action_step_stops_dependent_steps(monkeypatch):
    import src.agent_loop
    from src.action_engine import parse_plan
    from src.task_scheduler import ActionCheckpointError, TaskScheduler

    calls = []

    async def fake_stream_agent_loop(**kwargs):
        calls.append(kwargs)
        yield "data: " + json.dumps({
            "type": "tool_output",
            "tool": "web_search",
            "success": False,
            "output": "search unavailable",
        })

    async def no_wait(*_args, **_kwargs):
        return None

    monkeypatch.setattr(src.agent_loop, "stream_agent_loop", fake_stream_agent_loop)
    monkeypatch.setattr("src.interactive_gate.wait_for_interactive_quiet", no_wait)
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda **_kwargs: [])
    scheduler = TaskScheduler.__new__(TaskScheduler)
    task = SimpleNamespace(
        prompt="encrypted marker",
        name="Action goal",
        owner="alice",
        max_steps=20,
    )

    async def run():
        with pytest.raises(ActionCheckpointError, match="first"):
            await scheduler._run_agent_loop(
                endpoint_url="https://model.invalid/v1",
                model="test-model",
                task=task,
                session_id="session-1",
                action_goal="Find two sources",
                action_plan=parse_plan(_plan_data()),
            )

    asyncio.run(run())
    assert len(calls) == 1


def test_action_plan_skips_persisted_completed_steps(monkeypatch):
    import src.agent_loop
    from src.action_engine import parse_plan
    from src.task_scheduler import TaskScheduler

    calls = []

    async def fake_stream_agent_loop(**kwargs):
        calls.append(kwargs)
        yield "data: " + json.dumps({
            "type": "tool_output",
            "tool": "web_search",
            "success": True,
            "output": "resumed result",
        })

    async def no_wait(*_args, **_kwargs):
        return None

    monkeypatch.setattr(src.agent_loop, "stream_agent_loop", fake_stream_agent_loop)
    monkeypatch.setattr("src.interactive_gate.wait_for_interactive_quiet", no_wait)
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda **_kwargs: [])
    scheduler = TaskScheduler.__new__(TaskScheduler)
    monkeypatch.setattr(
        scheduler,
        "_action_step_checkpoints",
        lambda _run_id: {"first"},
    )
    monkeypatch.setattr(
        scheduler,
        "_action_step_outputs",
        lambda _run_id: {"first": "prior verified result"},
    )
    monkeypatch.setattr(
        scheduler,
        "_persist_action_plan",
        lambda *_args, **_kwargs: None,
    )

    async def verify(**_kwargs):
        return True

    monkeypatch.setattr(scheduler, "_verify_action_step", verify)
    task = SimpleNamespace(
        prompt="encrypted marker",
        name="Action goal",
        owner="alice",
        max_steps=20,
    )

    asyncio.run(scheduler._run_agent_loop(
        endpoint_url="https://model.invalid/v1",
        model="test-model",
        task=task,
        session_id="session-1",
        run_id="resumed-run",
        action_goal="Find two sources",
        action_plan=parse_plan(_plan_data()),
    ))

    assert len(calls) == 1
    assert "Current step (second)" in calls[0]["messages"][-1]["content"]
    assert "prior verified result" in str(calls[0]["messages"])


def test_unverified_action_step_stops_dependent_steps(monkeypatch):
    import src.agent_loop
    from src.action_engine import parse_plan
    from src.task_scheduler import ActionCheckpointError, TaskScheduler

    calls = []

    async def fake_stream_agent_loop(**kwargs):
        calls.append(kwargs)
        yield "data: " + json.dumps({
            "type": "tool_output",
            "tool": "web_search",
            "success": True,
            "output": "insufficient evidence",
        })

    async def no_wait(*_args, **_kwargs):
        return None

    async def reject_verification(**_kwargs):
        return False

    monkeypatch.setattr(src.agent_loop, "stream_agent_loop", fake_stream_agent_loop)
    monkeypatch.setattr("src.interactive_gate.wait_for_interactive_quiet", no_wait)
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda **_kwargs: [])
    scheduler = TaskScheduler.__new__(TaskScheduler)
    monkeypatch.setattr(scheduler, "_verify_action_step", reject_verification)
    task = SimpleNamespace(
        prompt="encrypted marker",
        name="Action goal",
        owner="alice",
        max_steps=20,
    )

    async def run():
        with pytest.raises(ActionCheckpointError, match="verified"):
            await scheduler._run_agent_loop(
                endpoint_url="https://model.invalid/v1",
                model="test-model",
                task=task,
                session_id="session-1",
                action_goal="Find two sources",
                action_plan=parse_plan(_plan_data()),
            )

    asyncio.run(run())
    assert len(calls) == 1


def test_action_step_timeout_cancels_the_agent_stream(monkeypatch):
    import src.agent_loop
    from src.action_engine import parse_plan
    from src.task_scheduler import ActionCheckpointError, TaskScheduler

    cancelled = asyncio.Event()

    async def stalled_stream(**_kwargs):
        try:
            await asyncio.Event().wait()
            yield "data: " + json.dumps({"delta": "unreachable"})
        finally:
            cancelled.set()

    async def no_wait(*_args, **_kwargs):
        return None

    monkeypatch.setattr(src.agent_loop, "stream_agent_loop", stalled_stream)
    monkeypatch.setattr("src.interactive_gate.wait_for_interactive_quiet", no_wait)
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda **_kwargs: [])
    scheduler = TaskScheduler.__new__(TaskScheduler)
    task = SimpleNamespace(
        prompt="encrypted marker",
        name="Action goal",
        owner="alice",
        max_steps=20,
    )
    plan_data = _plan_data()
    plan_data["steps"] = plan_data["steps"][:1]
    plan_data["steps"][0]["timeout_seconds"] = 1

    async def run():
        with pytest.raises(ActionCheckpointError, match="timed out"):
            await scheduler._run_agent_loop(
                endpoint_url="https://model.invalid/v1",
                model="test-model",
                task=task,
                session_id="session-1",
                action_goal="Find two sources",
                action_plan=parse_plan(plan_data),
            )

    asyncio.run(run())
    assert cancelled.is_set()
