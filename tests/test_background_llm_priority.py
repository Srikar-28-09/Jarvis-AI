"""Background naming and memory calls yield to newly arriving foreground work."""

import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("operation", ["auto_name", "memory_extract"])
@pytest.mark.asyncio
async def test_background_naming_and_memory_extraction_are_preemptible(
    monkeypatch, operation
):
    import src.llm_core as llm_core

    local_url = "http://127.0.0.1:11434/v1/chat/completions"
    background_started = asyncio.Event()
    never_release = asyncio.Event()

    monkeypatch.setenv("ODYSSEUS_LOCAL_MODEL_GATE", "true")
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_LOCK", asyncio.Lock())
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_CURRENT", {})
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAITING_FOREGROUND", 0)

    from src import interactive_gate
    monkeypatch.setattr(interactive_gate, "has_foreground_activity", lambda: False)

    async def gated_llm_call(url, model, messages, **kwargs):
        assert kwargs["workload"] == "background"
        async with llm_core._local_model_slot(url, model, workload=kwargs["workload"]):
            background_started.set()
            await never_release.wait()
        return "[]"

    monkeypatch.setattr(llm_core, "llm_call_async", gated_llm_call)

    if operation == "auto_name":
        import src.task_endpoint as task_endpoint
        from routes.chat_helpers import auto_name_session

        monkeypatch.setattr(
            task_endpoint,
            "resolve_task_endpoint",
            lambda *args, **kwargs: (local_url, "qwen3:8b", {}),
        )
        background = asyncio.create_task(auto_name_session(
            SimpleNamespace(update_session_name=lambda *_: None),
            SimpleNamespace(
                id="session-1", owner="alice", endpoint_url=local_url,
                model="qwen3:8b", headers={}, history=[
                    SimpleNamespace(role="user", content="What is an LLM?")
                ],
            ),
        ))
    else:
        from services.memory.memory_extractor import extract_and_store

        session = SimpleNamespace(
            owner="alice",
            get_context_messages=lambda: [
                {"role": "user", "content": "My name is Sam."},
                {"role": "assistant", "content": "Understood."},
            ],
        )
        background = asyncio.create_task(extract_and_store(
            session, SimpleNamespace(), None, local_url, "qwen3:8b", {}
        ))

    foreground = None
    try:
        await asyncio.wait_for(background_started.wait(), timeout=1)

        async def foreground_chat():
            async with llm_core._local_model_slot(
                local_url, "qwen3:8b", workload="foreground"
            ):
                return "foreground acquired"

        foreground = asyncio.create_task(foreground_chat())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(background, timeout=1)
        assert await asyncio.wait_for(foreground, timeout=1) == "foreground acquired"
    finally:
        for task in (background, foreground):
            if task is not None and not task.done():
                task.cancel()
