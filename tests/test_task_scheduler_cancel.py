import asyncio
import json
import threading
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


def _setup_db(tmp_path, monkeypatch):
    import core.database as cd

    base = declarative_base()

    class ScheduledTask(base):
        __tablename__ = "scheduled_tasks"

        id = Column(String, primary_key=True)
        owner = Column(String)
        name = Column(String)
        prompt = Column(Text)
        task_type = Column(String, default="llm")
        action = Column(String)
        status = Column(String, default="active")
        next_run = Column(DateTime)
        last_run = Column(DateTime)
        run_count = Column(Integer, default=0)
        trigger_type = Column(String, default="schedule")
        schedule = Column(String, default="daily")
        scheduled_time = Column(String, default="09:00")
        scheduled_day = Column(Integer)
        scheduled_date = Column(String)
        cron_expression = Column(String)
        output_target = Column(String, default="session")
        notifications_enabled = Column(Boolean, default=True)
        then_task_id = Column(String)

    class TaskRun(base):
        __tablename__ = "task_runs"

        id = Column(String, primary_key=True)
        task_id = Column(String)
        started_at = Column(DateTime)
        finished_at = Column(DateTime)
        status = Column(String)
        result = Column(Text)
        error = Column(Text)
        tokens_used = Column(Integer)
        steps = Column(Text)
        model = Column(String)

    engine = create_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(cd, "SessionLocal", session_local)
    monkeypatch.setattr(cd, "ScheduledTask", ScheduledTask)
    monkeypatch.setattr(cd, "TaskRun", TaskRun)
    return session_local, ScheduledTask, TaskRun


def _process_lock_attempt(data_dir, result_queue):
    import src.constants as constants
    from src.task_scheduler import TaskScheduler

    constants.DATA_DIR = data_dir
    scheduler = TaskScheduler(session_manager=None)
    try:
        scheduler._acquire_process_lock()
    except RuntimeError:
        result_queue.put("blocked")
        return
    scheduler._release_process_lock()
    result_queue.put("acquired")


def test_stop_task_cleans_up_queued_handle_and_run(tmp_path, monkeypatch):
    session_local, ScheduledTask, TaskRun = _setup_db(tmp_path, monkeypatch)

    db = session_local()
    db.add(ScheduledTask(
        id="queued-task",
        owner="alice",
        name="Queued Task",
        task_type="llm",
        status="active",
    ))
    db.commit()
    db.close()

    from src.task_scheduler import TaskScheduler

    async def drive():
        scheduler = TaskScheduler.__new__(TaskScheduler)
        scheduler._executing = {"queued-task"}
        scheduler._executing_lock = asyncio.Lock()
        scheduler._run_semaphore = asyncio.Semaphore(1)
        scheduler._task_handles = {}
        scheduler._concurrency_cap = 1
        scheduler._task_defer_counts = {}
        await scheduler._run_semaphore.acquire()

        task = asyncio.create_task(scheduler._execute_task("queued-task"))
        try:
            for _ in range(50):
                if "queued-task" in scheduler._task_handles:
                    db2 = session_local()
                    try:
                        run = db2.query(TaskRun).filter(TaskRun.task_id == "queued-task").first()
                        if run:
                            break
                    finally:
                        db2.close()
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("queued run was not created")

            assert await scheduler.stop_task("queued-task") is True
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            scheduler._run_semaphore.release()

        assert "queued-task" not in scheduler._task_handles
        assert "queued-task" not in scheduler._executing

    asyncio.run(drive())

    db = session_local()
    try:
        run = db.query(TaskRun).filter(TaskRun.task_id == "queued-task").first()
        assert run.status == "aborted"
        assert run.error == "Stopped by user"
        assert run.finished_at is not None
        assert run.finished_at >= run.started_at
    finally:
        db.close()


def test_scheduler_shutdown_cancels_and_awaits_active_task():
    from src.task_scheduler import TaskScheduler

    async def drive():
        scheduler = TaskScheduler.__new__(TaskScheduler)
        scheduler._running = True
        scheduler._shutting_down = False
        scheduler._task = None
        scheduler._note_pings_task = None
        scheduler._event_pings_task = None
        scheduler._executing = {"task-1"}
        scheduler._executing_lock = asyncio.Lock()
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def running_task():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        active = asyncio.create_task(running_task())
        scheduler._task_handles = {"task-1": active}
        await started.wait()
        await scheduler.stop()

        assert scheduler._running is False
        assert scheduler._shutting_down is True
        assert active.done()
        assert stopped.is_set()

    asyncio.run(drive())


def test_force_run_does_not_allow_duplicate_execution(monkeypatch):
    from src.task_scheduler import TaskScheduler

    async def drive():
        scheduler = TaskScheduler.__new__(TaskScheduler)
        scheduler._executing = set()
        scheduler._executing_lock = asyncio.Lock()
        scheduler._task_handles = {}
        scheduler._running = True
        scheduler._shutting_down = False
        scheduler._task = None
        scheduler._note_pings_task = None
        scheduler._event_pings_task = None
        started = asyncio.Event()

        async def execute(*_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(scheduler, "_execute_task", execute)
        assert await scheduler.run_task_now("same-task", force=True) is True
        await started.wait()
        assert await scheduler.run_task_now("same-task", force=True) is False
        assert len(scheduler._task_handles) == 1
        await scheduler.stop()
        assert await scheduler.run_task_now("same-task") is False

    asyncio.run(drive())


def test_scheduler_process_lock_allows_only_one_owner(tmp_path, monkeypatch):
    import multiprocessing

    import src.constants as constants
    from src.task_scheduler import TaskScheduler

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    first = TaskScheduler(session_manager=None)
    second = TaskScheduler(session_manager=None)

    first._acquire_process_lock()
    try:
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        process = context.Process(
            target=_process_lock_attempt,
            args=(str(tmp_path), result_queue),
        )
        process.start()
        assert result_queue.get(timeout=10) == "blocked"
        process.join(timeout=10)
        assert process.exitcode == 0
        assert asyncio.run(second.run_task_now("other-task")) is False

        try:
            second._acquire_process_lock()
        except RuntimeError as exc:
            assert "only one scheduler-enabled app process" in str(exc)
        else:
            raise AssertionError("second scheduler process unexpectedly acquired lock")
    finally:
        first._release_process_lock()

    second._acquire_process_lock()
    second._release_process_lock()


def test_scheduler_recovers_orphaned_action_start_claim(tmp_path, monkeypatch):
    import src.constants as constants
    from src.task_scheduler import TaskScheduler

    session_local, TaskModel, _ = _setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    db = session_local()
    db.add(TaskModel(
        id="orphaned-action-goal",
        owner="alice",
        name="Action goal",
        prompt="jarvis-action-goal:v1:encrypted",
        task_type="llm",
        status="starting",
        trigger_type="event",
    ))
    db.commit()
    db.close()

    scheduler = TaskScheduler(session_manager=None)
    async def start_and_stop():
        await scheduler.start()
        await scheduler.stop()

    asyncio.run(start_and_stop())

    db = session_local()
    try:
        task = db.query(TaskModel).filter(
            TaskModel.id == "orphaned-action-goal"
        ).one()
        assert task.status == "paused"
    finally:
        db.close()


def _make_execution_scheduler(session_local, monkeypatch):
    import sys
    import types

    from src.task_scheduler import TaskScheduler

    builtin_actions = types.ModuleType("src.builtin_actions")

    class TaskDeferred(Exception):
        pass

    class TaskNoop(Exception):
        pass

    builtin_actions.TaskDeferred = TaskDeferred
    builtin_actions.TaskNoop = TaskNoop
    monkeypatch.setitem(sys.modules, "src.builtin_actions", builtin_actions)

    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._task_handles = {}
    scheduler._executing = set()
    scheduler._executing_lock = asyncio.Lock()
    scheduler._task_defer_counts = {}
    scheduler._pending_notifications = []
    scheduler._shutting_down = False
    scheduler._session_manager = None
    return scheduler


def _seed_running_task(session_local, TaskModel, TaskRunModel, task_id, run_id):
    db = session_local()
    db.add(TaskModel(
        id=task_id,
        owner="alice",
        name="Task",
        prompt="run safely",
        task_type="llm",
        status="active",
        trigger_type="schedule",
    ))
    db.add(TaskRunModel(
        id=run_id,
        task_id=task_id,
        started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        status="running",
    ))
    db.commit()
    db.close()


def test_execution_timeout_pauses_task_and_persists_reconciliation(
    tmp_path, monkeypatch
):
    session_local, TaskModel, TaskRunModel = _setup_db(tmp_path, monkeypatch)
    _seed_running_task(
        session_local, TaskModel, TaskRunModel, "timeout-task", "timeout-run"
    )
    scheduler = _make_execution_scheduler(session_local, monkeypatch)
    monkeypatch.setattr(
        "src.task_scheduler._TASK_EXECUTION_TIMEOUT_SECONDS",
        0.01,
    )

    async def timeout_run(*args, **kwargs):
        await asyncio.Event().wait()

    scheduler._execute_llm_task = timeout_run
    asyncio.run(scheduler._execute_task_locked(
        "timeout-task",
        "timeout-run",
        gate_foreground=False,
    ))

    db = session_local()
    try:
        task = db.query(TaskModel).filter(TaskModel.id == "timeout-task").first()
        run = db.query(TaskRunModel).filter(TaskRunModel.id == "timeout-run").first()
        import json
        checkpoint = json.loads(run.steps)
        assert task.status == "paused"
        assert task.next_run is None
        assert run.status == "aborted"
        assert "outcome is uncertain" in run.error
        assert checkpoint["recovery"] == "reconciliation_required"
    finally:
        db.close()


def test_action_plan_checkpoint_survives_later_recovery_state_updates():
    from types import SimpleNamespace

    from src.task_scheduler import (
        _write_action_plan_checkpoint,
        _write_run_checkpoint,
    )

    run = SimpleNamespace(steps=json.dumps({
        "version": 1,
        "stage": "execution_started",
        "attempt_count": 2,
        "recovery": "none",
    }))
    plan = "- [x] Inspect sources\n- [ ] Verify the result"

    _write_action_plan_checkpoint(run, plan)
    _write_run_checkpoint(
        run,
        stage="interrupted",
        attempt_count=2,
        recovery="reconciliation_required",
    )

    checkpoint = json.loads(run.steps)
    from src.secret_storage import decrypt
    assert decrypt(checkpoint["action_plan_encrypted"]) == plan
    assert "action_plan" not in checkpoint
    assert checkpoint["plan_progress"] == {"completed": 1, "total": 2}
    assert checkpoint["stage"] == "interrupted"
    assert checkpoint["recovery"] == "reconciliation_required"
    assert checkpoint["attempt_count"] == 2


def test_scheduler_persists_plan_updates_in_existing_task_run(
    tmp_path, monkeypatch
):
    session_local, TaskModel, TaskRunModel = _setup_db(tmp_path, monkeypatch)
    _seed_running_task(
        session_local, TaskModel, TaskRunModel, "plan-task", "plan-run"
    )
    db = session_local()
    try:
        run = db.query(TaskRunModel).filter(TaskRunModel.id == "plan-run").first()
        run.steps = json.dumps({
            "version": 1,
            "stage": "execution_started",
            "attempt_count": 1,
            "recovery": "none",
        })
        db.commit()
    finally:
        db.close()
    from src.task_scheduler import TaskScheduler

    scheduler = TaskScheduler.__new__(TaskScheduler)
    plan = "- [x] Gather context\n- [ ] Complete and verify"

    scheduler._persist_action_plan("plan-run", plan)

    db = session_local()
    try:
        run = db.query(TaskRunModel).filter(TaskRunModel.id == "plan-run").first()
        checkpoint = json.loads(run.steps)
        from src.secret_storage import decrypt
        assert decrypt(checkpoint["action_plan_encrypted"]) == plan
        assert plan not in run.steps
        assert checkpoint["plan_progress"] == {"completed": 1, "total": 2}
        assert checkpoint["stage"] == "execution_started"
    finally:
        db.close()


def test_scheduler_persists_completed_action_step_ids_across_instances(
    tmp_path, monkeypatch
):
    session_local, TaskModel, TaskRunModel = _setup_db(tmp_path, monkeypatch)
    _seed_running_task(
        session_local, TaskModel, TaskRunModel, "checkpoint-task", "checkpoint-run"
    )
    from src.task_scheduler import TaskScheduler

    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._persist_action_plan(
        "checkpoint-run",
        "- [x] first\n- [ ] second",
        {"first"},
        {"first": "verified first result"},
    )
    resumed_scheduler = TaskScheduler.__new__(TaskScheduler)

    assert resumed_scheduler._action_step_checkpoints("checkpoint-run") == {"first"}

    db = session_local()
    try:
        run = db.query(TaskRunModel).filter(
            TaskRunModel.id == "checkpoint-run"
        ).first()
        checkpoint = json.loads(run.steps)
        assert checkpoint["action_step_ids"] == ["first"]
        assert "verified first result" not in run.steps
        from src.secret_storage import decrypt
        assert decrypt(checkpoint["action_plan_encrypted"]) == (
            "- [x] first\n- [ ] second"
        )
        assert decrypt(checkpoint["action_step_outputs_encrypted"]) == (
            '{"first":"verified first result"}'
        )
    finally:
        db.close()


def test_task_run_api_decrypts_action_plan(monkeypatch):
    from types import SimpleNamespace

    import src.secret_storage as secret_storage
    from routes.task.task_routes import _run_to_dict

    checkpoint = json.dumps({
        "version": 1,
        "action_plan_encrypted": "enc:v2:sealed",
        "plan_progress": {"completed": 1, "total": 2},
    })
    run = SimpleNamespace(
        id="run-1",
        task_id="task-1",
        started_at=None,
        finished_at=None,
        status="running",
        result=None,
        error=None,
        tokens_used=None,
        model=None,
        steps=checkpoint,
    )
    monkeypatch.setattr(secret_storage, "decrypt", lambda _: "- [x] first\n- [ ] second")

    payload = _run_to_dict(run)

    assert payload["action_plan"] == "- [x] first\n- [ ] second"
    assert payload["plan_progress"] == {"completed": 1, "total": 2}
    assert payload["action_plan_error"] is None


def test_task_run_api_does_not_expose_unauthenticated_action_plan(monkeypatch):
    from types import SimpleNamespace

    import src.secret_storage as secret_storage
    from routes.task.task_routes import _run_to_dict
    from src.secret_storage import SecretDecryptionError

    checkpoint = json.dumps({
        "version": 1,
        "action_plan_encrypted": "enc:v2:invalid",
    })
    run = SimpleNamespace(
        id="run-2",
        task_id="task-1",
        started_at=None,
        finished_at=None,
        status="running",
        result=None,
        error=None,
        tokens_used=None,
        model=None,
        steps=checkpoint,
    )

    def reject_ciphertext(_value):
        raise SecretDecryptionError("invalid test ciphertext")

    monkeypatch.setattr(secret_storage, "decrypt", reject_ciphertext)

    payload = _run_to_dict(run)

    assert payload["action_plan"] is None
    assert payload["action_plan_error"] == "Stored plan checkpoint could not be authenticated."


def test_execution_cancellation_pauses_task_for_reconciliation(
    tmp_path, monkeypatch
):
    session_local, TaskModel, TaskRunModel = _setup_db(tmp_path, monkeypatch)
    _seed_running_task(
        session_local, TaskModel, TaskRunModel, "cancel-task", "cancel-run"
    )
    scheduler = _make_execution_scheduler(session_local, monkeypatch)
    started = asyncio.Event()

    async def wait_for_cancel(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    scheduler._execute_llm_task = wait_for_cancel

    async def drive():
        task = asyncio.create_task(scheduler._execute_task_locked(
            "cancel-task",
            "cancel-run",
            gate_foreground=False,
        ))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        await task

    asyncio.run(drive())

    db = session_local()
    try:
        task = db.query(TaskModel).filter(TaskModel.id == "cancel-task").first()
        run = db.query(TaskRunModel).filter(TaskRunModel.id == "cancel-run").first()
        import json
        checkpoint = json.loads(run.steps)
        assert task.status == "paused"
        assert run.status == "aborted"
        assert checkpoint["recovery"] == "reconciliation_required"
    finally:
        db.close()


def test_action_goal_api_hides_other_owners_goal(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routes.task.task_routes as task_routes
    import src.secret_storage as secret_storage
    from cryptography.fernet import Fernet
    from src.action_engine import encode_task_prompt, parse_plan

    session_local, TaskModel, TaskRunModel = _setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(task_routes, "SessionLocal", session_local)
    monkeypatch.setattr(task_routes, "ScheduledTask", TaskModel)
    monkeypatch.setattr(task_routes, "TaskRun", TaskRunModel)
    monkeypatch.setattr(secret_storage, "_fernet", Fernet(Fernet.generate_key()))
    monkeypatch.setattr(
        task_routes,
        "get_current_user",
        lambda request: request.headers.get("X-Test-Owner"),
    )
    db = session_local()
    try:
        db.add(TaskModel(
            id="goal_owned_by_alice",
            owner="alice",
            name="Action goal",
            prompt=encode_task_prompt(
                "Find two public sources",
                parse_plan({
                    "interpretation": "Find public sources.",
                    "completion_criteria": ["Two sources."],
                    "steps": [{
                        "id": "search",
                        "objective": "Search the web.",
                        "dependencies": [],
                        "tools": ["web_search"],
                        "expected_output": "A result.",
                        "verification": "The search succeeded.",
                        "timeout_seconds": 10,
                    }],
                }),
                goal_id="goal_owned_by_alice",
            ),
            task_type="llm",
            status="paused",
        ))
        db.add(TaskModel(
            id="goal_with_side_effect",
            owner="alice",
            name="Action goal",
            prompt=encode_task_prompt(
                "Send a notification",
                parse_plan({
                    "interpretation": "Send a notification.",
                    "completion_criteria": ["Notification sent."],
                    "steps": [{
                        "id": "notify",
                        "objective": "Send the notification.",
                        "dependencies": [],
                        "tools": ["send_notification"],
                        "expected_output": "A notification was sent.",
                        "verification": "The notification send succeeded.",
                        "timeout_seconds": 10,
                    }],
                }),
                goal_id="goal_with_side_effect",
            ),
            task_type="llm",
            status="paused",
        ))
        db.commit()
    finally:
        db.close()

    app = FastAPI()
    app.include_router(task_routes.setup_task_routes(task_scheduler=object()))
    client = TestClient(app)

    assert client.get(
        "/api/tasks/action-goals/goal_owned_by_alice",
        headers={"X-Test-Owner": "alice"},
    ).status_code == 200
    assert client.get(
        "/api/tasks/action-goals/goal_owned_by_alice",
        headers={"X-Test-Owner": "bob"},
    ).status_code == 404
    assert client.post(
        "/api/tasks/action-goals/goal_with_side_effect/start",
        headers={"X-Test-Owner": "alice"},
    ).status_code == 409


def test_concurrent_action_goal_start_does_not_suppress_winner(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routes.task.task_routes as task_routes
    import src.secret_storage as secret_storage
    from cryptography.fernet import Fernet
    from src.action_engine import encode_task_prompt, parse_plan

    session_local, TaskModel, TaskRunModel = _setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(task_routes, "SessionLocal", session_local)
    monkeypatch.setattr(task_routes, "ScheduledTask", TaskModel)
    monkeypatch.setattr(task_routes, "TaskRun", TaskRunModel)
    monkeypatch.setattr(secret_storage, "_fernet", Fernet(Fernet.generate_key()))
    monkeypatch.setattr(
        task_routes,
        "get_current_user",
        lambda request: request.headers.get("X-Test-Owner"),
    )

    db = session_local()
    try:
        db.add(TaskModel(
            id="concurrent-goal",
            owner="alice",
            name="Action goal",
            prompt=encode_task_prompt(
                "Find two public sources",
                parse_plan({
                    "interpretation": "Find public sources.",
                    "completion_criteria": ["Two sources."],
                    "steps": [{
                        "id": "search",
                        "objective": "Search the web.",
                        "dependencies": [],
                        "tools": ["web_search"],
                        "expected_output": "A result.",
                        "verification": "The search succeeded.",
                        "timeout_seconds": 10,
                    }],
                }),
                goal_id="concurrent-goal",
            ),
            task_type="llm",
            status="paused",
        ))
        db.commit()
    finally:
        db.close()

    scheduler_entered = threading.Event()
    release_scheduler = threading.Event()

    class BlockingScheduler:
        calls = 0

        async def run_task_now(self, task_id):
            self.calls += 1
            scheduler_entered.set()
            await asyncio.to_thread(release_scheduler.wait, 5)
            db = session_local()
            try:
                db.add(TaskRunModel(
                    id="winning-run",
                    task_id=task_id,
                    started_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    status="queued",
                ))
                db.commit()
            finally:
                db.close()
            return True

    scheduler = BlockingScheduler()
    app = FastAPI()
    app.include_router(task_routes.setup_task_routes(task_scheduler=scheduler))
    client = TestClient(app)
    headers = {"X-Test-Owner": "alice"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(
            client.post,
            "/api/tasks/action-goals/concurrent-goal/start",
            headers=headers,
        )
        assert scheduler_entered.wait(5), "first start did not acquire the scheduler"
        loser = client.post(
            "/api/tasks/action-goals/concurrent-goal/start",
            headers=headers,
        )
        assert loser.status_code == 409

        db = session_local()
        try:
            task = db.query(TaskModel).filter(
                TaskModel.id == "concurrent-goal"
            ).one()
            assert task.status == "starting"
            assert db.query(TaskRunModel).filter(
                TaskRunModel.task_id == "concurrent-goal"
            ).count() == 0
        finally:
            db.close()

        release_scheduler.set()
        assert winner.result(timeout=5).status_code == 200

    assert scheduler.calls == 1
    db = session_local()
    try:
        run = db.query(TaskRunModel).filter(
            TaskRunModel.id == "winning-run"
        ).one()
        assert run.status == "queued"
    finally:
        db.close()
    assert client.get(
        "/api/tasks/action-goals/concurrent-goal",
        headers=headers,
    ).json()["status"] == "running"
