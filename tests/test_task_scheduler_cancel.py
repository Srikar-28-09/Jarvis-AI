import asyncio
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
        steps = Column(Text)
        model = Column(String)

    engine = create_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(cd, "SessionLocal", session_local)
    monkeypatch.setattr(cd, "ScheduledTask", ScheduledTask)
    monkeypatch.setattr(cd, "TaskRun", TaskRun)
    return session_local, ScheduledTask, TaskRun


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
