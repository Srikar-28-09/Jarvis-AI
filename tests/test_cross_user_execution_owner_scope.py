import asyncio


def test_detached_agent_run_is_owner_bound():
    from src import agent_runs

    async def scenario():
        async def stream():
            yield "data: hello\n\n"

        run = agent_runs.start(
            "session-alice",
            stream(),
            owner="alice",
        )
        assert agent_runs.get_active_run("session-alice", owner="bob") is None
        assert agent_runs.stop(
            "session-alice",
            run.run_id,
            owner="bob",
        ) is False
        events = [
            event
            async for event in agent_runs.subscribe(
                "session-alice",
                run,
                owner="bob",
            )
        ]
        assert events == []

    asyncio.run(scenario())


def test_background_job_get_list_and_kill_are_owner_bound(monkeypatch):
    from src import bg_jobs

    record = {
        "job-alice": {
            "id": "job-alice",
            "session_id": "session-alice",
            "owner": "alice",
            "status": "running",
            "pid": 123456,
            "started_at": 1.0,
        }
    }
    monkeypatch.setattr(bg_jobs, "_load", lambda: dict(record))
    monkeypatch.setattr(bg_jobs, "refresh", lambda: dict(record))
    monkeypatch.setattr(bg_jobs, "_save", lambda _jobs: None)
    monkeypatch.setattr(bg_jobs, "_read_output", lambda _rec: "")
    monkeypatch.setattr(bg_jobs, "_kill", lambda _pid: None)

    assert bg_jobs.get("job-alice", owner="bob") is None
    assert bg_jobs.list_for_session("session-alice", owner="bob") == []
    assert bg_jobs.kill("job-alice", owner="bob") is None
    assert bg_jobs.get("job-alice", owner="alice") is not None


def test_detached_run_rejects_unowned_and_cross_owner_request_ids():
    from src import agent_runs

    async def stream(label):
        yield f"data: {label}\n\n"

    try:
        agent_runs.start(
            "session-unowned",
            stream("none"),
            request_id="request-unowned",
            owner=None,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Unowned detached run was accepted")

    async def scenario():
        first = agent_runs.start(
            "session-request",
            stream("alice"),
            request_id="request-cross-owner",
            owner="alice",
        )
        try:
            agent_runs.start(
                "session-request",
                stream("bob"),
                request_id="request-cross-owner",
                owner="bob",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Cross-owner request ID was accepted")
        assert first.owner == "alice"

    asyncio.run(scenario())


def test_detached_run_per_owner_limit(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_MAX_CONCURRENT_AGENT_RUNS_PER_USER", "1")

    async def scenario():
        from src import agent_runs

        async def stream():
            await asyncio.sleep(1)
            yield "data: done\n\n"

        first = agent_runs.start("limit-a", stream(), owner="alice")
        try:
            agent_runs.start("limit-b", stream(), owner="alice")
        except agent_runs.PerUserLimitError:
            limited = True
        else:
            limited = False
        assert limited
        first.task.cancel()
        await asyncio.gather(first.task, return_exceptions=True)

    asyncio.run(scenario())


def test_background_job_per_owner_limit(monkeypatch):
    from src import bg_jobs

    monkeypatch.setenv("ODYSSEUS_MAX_CONCURRENT_BACKGROUND_JOBS_PER_USER", "1")
    monkeypatch.setattr(bg_jobs, "_load", lambda: {
        "existing": {
            "id": "existing",
            "owner": "alice",
            "status": "running",
        }
    })
    try:
        bg_jobs.launch("echo test", "session-a", owner="alice")
    except ValueError as exc:
        assert "limit" in str(exc).lower()
    else:
        raise AssertionError("Per-owner background-job limit was not enforced")
