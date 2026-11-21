import asyncio
import builtins
import threading
from io import StringIO
from types import SimpleNamespace

import pytest

from services.hwfit import hardware
from src import llm_core


LOCAL_URL = "http://127.0.0.1:11434/api/chat"
CLOUD_URL = "https://api.openai.com/v1/chat/completions"


@pytest.fixture(autouse=True)
def _reset_local_gate(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_LOCAL_MODEL_GATE", "true")
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_LOCK", threading.Lock())
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_CURRENT", {})
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAITING_FOREGROUND", 0)
    monkeypatch.setattr(
        llm_core,
        "is_local_endpoint",
        lambda url: llm_core._is_loopback_endpoint(url),
    )
    monkeypatch.setattr(
        hardware,
        "get_runtime_resources",
        lambda: {"available_ram_gb": 8.0, "available_vram_gb": 4.0},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resources",
    [
        {"available_ram_gb": 0.4, "available_vram_gb": 3.0},
        {"available_ram_gb": 8.0, "available_vram_gb": 0.4},
    ],
)
async def test_local_model_admission_rejects_low_available_resources_and_releases_slot(
    monkeypatch, resources
):
    monkeypatch.setattr(
        hardware,
        "get_runtime_resources",
        lambda: resources,
    )

    with pytest.raises(llm_core._LocalModelAdmissionError):
        async with llm_core._local_model_slot(LOCAL_URL, "local-model"):
            pytest.fail("low-memory request should not enter the model slot")

    assert not llm_core._LOCAL_MODEL_LOCK.locked()
    assert not llm_core._LOCAL_MODEL_CURRENT


@pytest.mark.asyncio
async def test_local_model_admission_keeps_single_active_request(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0

    async def request():
        nonlocal active, max_active
        async with llm_core._local_model_slot(LOCAL_URL, "local-model"):
            active += 1
            max_active = max(max_active, active)
            entered.set()
            await release.wait()
            active -= 1

    first = asyncio.create_task(request())
    await asyncio.wait_for(entered.wait(), timeout=1)
    second = asyncio.create_task(request())
    await asyncio.sleep(0)
    assert max_active == 1

    release.set()
    await asyncio.gather(first, second)
    assert max_active == 1
    assert not llm_core._LOCAL_MODEL_LOCK.locked()


@pytest.mark.asyncio
async def test_local_model_admission_timeout_is_bounded(monkeypatch):
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_ADMISSION_TIMEOUT_SECONDS", 0.02)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_slot():
        async with llm_core._local_model_slot(LOCAL_URL, "local-model"):
            entered.set()
            await release.wait()

    first = asyncio.create_task(hold_slot())
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        with pytest.raises(llm_core._LocalModelAdmissionError, match="Timed out"):
            async with llm_core._local_model_slot(LOCAL_URL, "local-model"):
                pytest.fail("timed-out request should not enter the model slot")
    finally:
        release.set()
        await first

    assert not llm_core._LOCAL_MODEL_LOCK.locked()


@pytest.mark.asyncio
async def test_cancelled_local_model_request_releases_admission():
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def request():
        async with llm_core._local_model_slot(LOCAL_URL, "local-model"):
            entered.set()
            await hold.wait()

    task = asyncio.create_task(request())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not llm_core._LOCAL_MODEL_LOCK.locked()
    assert not llm_core._LOCAL_MODEL_CURRENT


@pytest.mark.asyncio
async def test_sync_and_async_calls_share_the_same_local_model_slot():
    sync_entered = threading.Event()

    def sync_request():
        with llm_core._local_model_slot_sync(LOCAL_URL, "sync-model"):
            sync_entered.set()

    async with llm_core._local_model_slot(LOCAL_URL, "async-model"):
        sync_task = asyncio.create_task(asyncio.to_thread(sync_request))
        await asyncio.sleep(0.05)
        assert not sync_entered.is_set()

    await asyncio.wait_for(sync_task, timeout=1)
    assert sync_entered.is_set()
    assert not llm_core._LOCAL_MODEL_LOCK.locked()


def test_sync_local_model_admission_rejects_low_resources_and_releases_slot(
    monkeypatch,
):
    monkeypatch.setattr(
        hardware,
        "get_runtime_resources",
        lambda: {"available_ram_gb": 0.2, "available_vram_gb": 2.0},
    )

    with pytest.raises(llm_core._LocalModelAdmissionError):
        with llm_core._local_model_slot_sync(LOCAL_URL, "sync-model"):
            pytest.fail("low-memory request should not enter the model slot")

    assert not llm_core._LOCAL_MODEL_LOCK.locked()
    assert not llm_core._LOCAL_MODEL_CURRENT


def test_sync_llm_call_uses_shared_local_model_admission(monkeypatch):
    observed = []

    def fake_post(url, headers, **kwargs):
        observed.append(
            (
                llm_core._LOCAL_MODEL_LOCK.locked(),
                llm_core._LOCAL_MODEL_CURRENT.get("thread") is threading.current_thread(),
            )
        )
        return SimpleNamespace(
            is_success=True,
            json=lambda: {"choices": [{"message": {"content": "response"}}]},
        )

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware", fake_post)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *_args, **_kwargs: None)

    result = llm_core.llm_call(
        "http://127.0.0.1:8000/v1/chat/completions",
        "sync-model",
        [{"role": "user", "content": "hello"}],
    )

    assert result == "response"
    assert observed == [(True, True)]
    assert not llm_core._LOCAL_MODEL_LOCK.locked()


@pytest.mark.asyncio
async def test_explicit_cloud_fallback_is_used_when_local_resources_are_low(
    monkeypatch,
):
    monkeypatch.setattr(
        hardware,
        "get_runtime_resources",
        lambda: {"available_ram_gb": 0.3, "available_vram_gb": 0.2},
    )

    async def fake_inner(url, model, messages, **kwargs):
        yield 'data: {"delta": "cloud response"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(llm_core, "_stream_llm_inner", fake_inner)

    chunks = [
        chunk
        async for chunk in llm_core.stream_llm_with_fallback(
            [(LOCAL_URL, "local-model", {}), (CLOUD_URL, "cloud-model", {})],
            [{"role": "user", "content": "hello"}],
            fallback_statuses={503},
        )
    ]

    assert any('"answered_by": "cloud-model"' in chunk for chunk in chunks)
    assert any('"delta": "cloud response"' in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_cloud_fallback_is_not_used_without_explicit_permission(monkeypatch):
    monkeypatch.setattr(
        hardware,
        "get_runtime_resources",
        lambda: {"available_ram_gb": 0.3, "available_vram_gb": 0.2},
    )
    cloud_calls = []

    async def fake_inner(url, model, messages, **kwargs):
        cloud_calls.append(url)
        yield 'data: {"delta": "cloud response"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(llm_core, "_stream_llm_inner", fake_inner)

    chunks = [
        chunk
        async for chunk in llm_core.stream_llm_with_fallback(
            [(LOCAL_URL, "local-model", {}), (CLOUD_URL, "cloud-model", {})],
            [{"role": "user", "content": "private text"}],
            fallback_statuses=set(),
        )
    ]

    assert cloud_calls == []
    assert llm_core._stream_error_status(chunks[-1]) == 503


def test_runtime_vram_uses_current_free_memory_and_conservative_gpu_pool(
    monkeypatch,
):
    monkeypatch.setattr(hardware.shutil, "which", lambda _command: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        hardware.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="2048, 8192\n1024, 8192\n",
        ),
    )
    monkeypatch.setattr(hardware.glob, "glob", lambda _pattern: [])

    assert hardware._runtime_available_vram_gb() == 1.0


def test_runtime_ram_uses_current_available_memory(monkeypatch):
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        builtins,
        "open",
        lambda *_args, **_kwargs: StringIO(
            "MemTotal: 16384000 kB\nMemAvailable: 2097152 kB\n"
        ),
    )

    assert hardware._runtime_available_ram_gb() == 2.0


def test_resource_snapshot_handles_missing_telemetry(monkeypatch):
    monkeypatch.setattr(hardware, "_runtime_available_ram_gb", lambda: None)
    monkeypatch.setattr(hardware, "_runtime_available_vram_gb", lambda: None)
    monkeypatch.setattr(
        hardware,
        "get_runtime_resources",
        lambda: {
            "available_ram_gb": hardware._runtime_available_ram_gb(),
            "available_vram_gb": hardware._runtime_available_vram_gb(),
        },
    )

    assert hardware.get_runtime_resources() == {
        "available_ram_gb": None,
        "available_vram_gb": None,
    }


@pytest.mark.asyncio
async def test_missing_resource_telemetry_preserves_serialized_execution(monkeypatch):
    monkeypatch.setattr(
        hardware,
        "get_runtime_resources",
        lambda: {"available_ram_gb": None, "available_vram_gb": None},
    )

    async with llm_core._local_model_slot(LOCAL_URL, "local-model"):
        assert llm_core._LOCAL_MODEL_LOCK.locked()

    assert not llm_core._LOCAL_MODEL_LOCK.locked()


@pytest.mark.asyncio
async def test_resource_check_does_not_apply_jarvis_host_metrics_to_lan_endpoint(
    monkeypatch,
):
    checked = []
    monkeypatch.setattr(llm_core, "is_local_endpoint", lambda _url: True)
    monkeypatch.setattr(
        hardware,
        "get_runtime_resources",
        lambda: checked.append(True),
    )

    async with llm_core._local_model_slot(
        "http://192.168.1.20:11434/api/chat", "remote-model"
    ):
        assert llm_core._LOCAL_MODEL_LOCK.locked()

    assert checked == []
