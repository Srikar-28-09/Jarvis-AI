import asyncio

import pytest


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    import src.app_initializer as app_initializer
    from src.api_key_manager import APIKeyManager

    data_dir = tmp_path_factory.mktemp("app-lifecycle-data")
    APIKeyManager(str(data_dir)).mark_installation_state("first_run_pending")
    patch = pytest.MonkeyPatch()
    patch.setattr(app_initializer, "DATA_DIR", str(data_dir))
    patch.setattr(
        app_initializer,
        "AGENT_WORKSPACE_DIR",
        str(data_dir / "agent_workspace"),
    )
    try:
        import app

        yield app
    finally:
        patch.undo()


@pytest.mark.asyncio
async def test_startup_event_does_not_duplicate_an_active_lifecycle(app_module, monkeypatch):
    state = app_module.app.state
    monkeypatch.setattr(state, "_lifecycle_running", True, raising=False)
    existing_tasks = []
    monkeypatch.setattr(state, "_startup_tasks", existing_tasks, raising=False)

    assert await app_module._startup_event() is False
    assert state._startup_tasks is existing_tasks


@pytest.mark.asyncio
async def test_shutdown_awaits_owned_tasks_and_continues_after_cleanup_errors(app_module, monkeypatch):
    import src.builtin_mcp as builtin_mcp

    cleaned = asyncio.Event()
    unrelated = asyncio.create_task(asyncio.Event().wait())

    async def managed_task():
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    owned = asyncio.create_task(managed_task(), name="test-owned")
    await asyncio.sleep(0)
    app_module.app.state._startup_tasks = [owned]
    app_module.app.state._lifecycle_running = True
    cleanup_calls = []

    async def scheduler_stop():
        cleanup_calls.append("scheduler")
        raise RuntimeError("scheduler stop failed")

    async def webhook_close():
        cleanup_calls.append("webhook")
        raise RuntimeError("webhook close failed")

    async def mcp_disconnect():
        cleanup_calls.append("mcp")

    async def stop_builtin_tasks():
        cleanup_calls.append("builtin-mcp")

    monkeypatch.setattr(app_module.task_scheduler, "stop", scheduler_stop)
    monkeypatch.setattr(app_module.webhook_manager, "close", webhook_close)
    monkeypatch.setattr(app_module.mcp_manager, "disconnect_all", mcp_disconnect)
    monkeypatch.setattr(
        builtin_mcp,
        "cancel_builtin_server_startup_tasks",
        stop_builtin_tasks,
    )

    try:
        await app_module._shutdown_event()
        assert owned.cancelled()
        assert cleaned.is_set()
        assert not unrelated.cancelled()
        assert cleanup_calls == ["scheduler", "builtin-mcp", "webhook", "mcp"]
        assert app_module.app.state._startup_tasks == []
        assert app_module.app.state._lifecycle_running is False
    finally:
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_propagates_cancellation_after_remaining_cleanup(app_module, monkeypatch):
    import src.builtin_mcp as builtin_mcp

    cleanup_calls = []

    async def scheduler_stop():
        raise asyncio.CancelledError

    async def cleanup(name):
        async def run():
            cleanup_calls.append(name)
        return run

    async def stop_builtin_tasks():
        cleanup_calls.append("builtin-mcp")

    monkeypatch.setattr(app_module.task_scheduler, "stop", scheduler_stop)
    monkeypatch.setattr(app_module.webhook_manager, "close", await cleanup("webhook"))
    monkeypatch.setattr(app_module.mcp_manager, "disconnect_all", await cleanup("mcp"))
    monkeypatch.setattr(
        builtin_mcp,
        "cancel_builtin_server_startup_tasks",
        stop_builtin_tasks,
    )
    app_module.app.state._lifecycle_running = True

    with pytest.raises(asyncio.CancelledError):
        await app_module._shutdown_event()

    assert cleanup_calls == ["builtin-mcp", "webhook", "mcp"]


@pytest.mark.asyncio
async def test_disconnect_all_cancels_and_awaits_inflight_mcp_connections():
    from src.mcp_manager import McpManager

    manager = McpManager()
    stopped = asyncio.Event()

    async def connecting():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = asyncio.create_task(connecting())
    await asyncio.sleep(0)
    manager._connect_tasks["pending"] = task

    await manager.disconnect_all()

    assert task.cancelled()
    assert stopped.is_set()
    assert "pending" not in manager._connect_tasks


@pytest.mark.asyncio
async def test_scheduler_stop_awaits_all_owned_loops_after_one_fails():
    from src.task_scheduler import TaskScheduler

    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._running = True
    stopped = asyncio.Event()

    async def fail():
        raise RuntimeError("scheduler loop failed")

    async def wait_for_stop():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    scheduler._task = asyncio.create_task(fail())
    scheduler._note_pings_task = asyncio.create_task(wait_for_stop())
    scheduler._event_pings_task = asyncio.create_task(wait_for_stop())
    await asyncio.sleep(0)

    await scheduler.stop()

    assert scheduler._running is False
    assert scheduler._task.done()
    assert scheduler._note_pings_task.cancelled()
    assert scheduler._event_pings_task.cancelled()
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_builtin_mcp_registration_returns_its_startup_tasks(tmp_path, monkeypatch):
    import src.builtin_mcp as builtin_mcp

    (tmp_path / "server.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(builtin_mcp, "MCP_DISABLED", False)
    monkeypatch.setattr(builtin_mcp, "LAZY_MCP_ENABLED", False)
    monkeypatch.setattr(
        builtin_mcp,
        "_BUILTIN_SERVERS",
        {"test": ("server.py", "Test server")},
    )
    monkeypatch.setattr(builtin_mcp, "_BUILTIN_NPX_SERVERS", {})
    monkeypatch.setattr(builtin_mcp, "get_app_root", lambda: str(tmp_path))
    monkeypatch.setattr(builtin_mcp, "builtin_python_env", lambda _root: {})
    monkeypatch.setattr(builtin_mcp, "_find_npx", lambda: "npx")

    async def wait_for_shutdown(**_kwargs):
        await asyncio.Event().wait()

    class Manager:
        connect_server = staticmethod(wait_for_shutdown)

    startup_tasks = await builtin_mcp.register_builtin_servers(Manager())

    assert {task.get_name() for task in startup_tasks} == {
        "builtin-mcp-connect:test",
        "builtin-mcp-npx-startup",
    }

    await asyncio.sleep(0)
    await builtin_mcp.cancel_builtin_server_startup_tasks()
    assert all(task.cancelled() for task in startup_tasks)
