"""Validator + regression test for FINDING 6.2 — restart double-fires overdue
scheduled tasks.

Demonstrates the bug: TaskScheduler.start() aborts stale TaskRun rows but never
advances ScheduledTask.next_run, so the in-memory _executing guard resets
across a restart and _check_due_tasks will re-dispatch any task whose
next_run is still in the past.

After the fix (start() advances overdue next_run to now + 60s), the regression
test asserts the opposite: the task fires at most once across two consecutive
polls.
"""
import sys, types, asyncio
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import MagicMock
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Boolean, Text
from sqlalchemy.orm import sessionmaker, declarative_base


def _test_utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stub_heavy(monkeypatch):
    for name in [
        "src.builtin_actions", "src.ai_interaction", "src.endpoint_resolver",
        "src.agent_loop", "src.session_manager",
    ]:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))


def _setup_isolated_db():
    import core.database as cd
    B = declarative_base()

    class ScheduledTask(B):
        __tablename__ = "scheduled_tasks"
        id = Column(String, primary_key=True)
        owner = Column(String)
        name = Column(String, default="t")
        prompt = Column(Text)
        task_type = Column(String, default="llm")
        next_run = Column(DateTime, index=True)
        last_run = Column(DateTime)
        status = Column(String, default="active")
        run_count = Column(Integer, default=0)
        trigger_type = Column(String, default="schedule")
        schedule = Column(String, default="daily")
        scheduled_time = Column(String, default="09:00")
        scheduled_day = Column(Integer)
        scheduled_date = Column(String)
        cron_expression = Column(String)

    class TaskRun(B):
        __tablename__ = "task_runs"
        id = Column(String, primary_key=True)
        task_id = Column(String)
        started_at = Column(DateTime)
        finished_at = Column(DateTime)
        status = Column(String, default="queued")
        result = Column(Text)
        error = Column(Text)
        steps = Column(Text)

    eng = create_engine("sqlite:///:memory:")
    B.metadata.create_all(eng)
    cd.engine = eng
    cd.SessionLocal = sessionmaker(bind=eng, autocommit=False, autoflush=False)
    cd.ScheduledTask = ScheduledTask
    cd.TaskRun = TaskRun
    return cd, ScheduledTask, TaskRun


def test_scheduler_utcnow_preserves_naive_utc_contract():
    from src.task_scheduler import _utcnow

    now = _utcnow()

    assert now.tzinfo is None
    assert abs((now - _test_utcnow()).total_seconds()) < 2


def _drive_scheduler(monkeypatch, pre_start_setup=None):
    """Build a TaskScheduler bypassing __init__ and run start() + two polls."""
    _stub_heavy(monkeypatch)
    cd, ScheduledTask, TaskRun = _setup_isolated_db()

    from src.task_scheduler import TaskScheduler
    sch = TaskScheduler.__new__(TaskScheduler)
    sch._executing = set()
    sch._executing_lock = asyncio.Lock()
    sch._concurrency_cap = 1
    sch._run_semaphore = asyncio.Semaphore(1)
    sch._running = False
    sch._task = None
    sch._note_pings_task = None
    sch._task_handles = {}
    sch._shutting_down = False
    sch._known_task_owners = lambda: []
    sch._task_defer_counts = {}

    if pre_start_setup:
        pre_start_setup(cd, ScheduledTask, TaskRun)

    async def _never():
        await asyncio.sleep(3600)
    monkeypatch.setattr(sch, "_loop", _never)
    monkeypatch.setattr(sch, "_note_pings_loop", _never)

    dispatched = []
    def _fake_create_task(coro):
        if coro.__name__ == "_never":
            coro.close()
        dispatched.append(coro)
        class _T:
            def cancel(self): pass
        return _T()
    monkeypatch.setattr("src.task_scheduler.asyncio.create_task", _fake_create_task)

    async def _drive():
        await sch.start()
        await sch._check_due_tasks()
        await sch._check_due_tasks()
        return dispatched

    try:
        all_dispatched = asyncio.run(_drive())
    finally:
        sch._release_process_lock()
    # start() also fires the long-lived _loop and _note_pings_loop as tasks
    # (stubbed to _never here); filter those out so the test only counts
    # real per-poll task dispatches.
    real_dispatches = [c for c in all_dispatched if c.__name__ != "_never"]
    return cd, ScheduledTask, TaskRun, real_dispatches


def test_restart_does_not_re_dispatch_overdue_task(monkeypatch):
    """After restart, an overdue active task should fire at most once across
    two consecutive polls (the first poll re-fires it, but next_run is then
    advanced so the second poll does not)."""
    def _setup(cd, ScheduledTask, TaskRun):
        db = cd.SessionLocal()
        db.add(ScheduledTask(
            id="t_due_1", owner="alice", name="overdue",
            task_type="llm",
            next_run=_test_utcnow() - timedelta(hours=1),
            status="active",
        ))
        db.commit()
        db.close()

    cd, ScheduledTask, TaskRun, dispatched = _drive_scheduler(monkeypatch, _setup)

    db = cd.SessionLocal()
    t = db.query(ScheduledTask).filter(ScheduledTask.id == "t_due_1").first()
    db.close()
    assert t.next_run >= _test_utcnow() - timedelta(seconds=1), (
        f"After start(), next_run should have been pushed into the future; "
        f"got {t.next_run}"
    )
    assert len(dispatched) <= 1, (
        f"Expected at most 1 dispatch across two polls; got {len(dispatched)}. "
        "The startup next_run advance is not preventing the second poll from "
        "re-firing the same overdue task."
    )


def test_startup_does_not_advance_fresh_tasks(monkeypatch):
    """Tasks whose next_run is in the future must be untouched by the startup
    sweep — only overdue ones get pushed forward."""
    future = _test_utcnow() + timedelta(hours=2)
    def _setup(cd, ScheduledTask, TaskRun):
        db = cd.SessionLocal()
        db.add(ScheduledTask(
            id="t_fresh", owner="alice", name="fresh",
            task_type="llm", next_run=future, status="active",
        ))
        db.commit()
        db.close()

    cd, ScheduledTask, TaskRun, dispatched = _drive_scheduler(monkeypatch, _setup)

    db = cd.SessionLocal()
    t = db.query(ScheduledTask).filter(ScheduledTask.id == "t_fresh").first()
    db.close()
    assert t.next_run == future, (
        f"Fresh task's next_run was modified: expected {future}, got {t.next_run}"
    )
    assert len(dispatched) == 0


def test_restart_retries_only_a_queued_run_with_a_checkpoint(monkeypatch):
    task_time = _test_utcnow() - timedelta(hours=1)

    def _setup(cd, ScheduledTask, TaskRun):
        db = cd.SessionLocal()
        db.add(ScheduledTask(
            id="queued-task", owner="alice", name="queued",
            next_run=task_time, status="active",
        ))
        db.add(TaskRun(
            id="queued-run", task_id="queued-task", started_at=task_time,
            status="queued",
            steps=json.dumps({
                "version": 1,
                "stage": "queued",
                "attempt_count": 1,
                "recovery": "none",
            }),
        ))
        db.commit()
        db.close()

    cd, ScheduledTask, TaskRun, dispatched = _drive_scheduler(monkeypatch, _setup)

    db = cd.SessionLocal()
    try:
        run = db.query(TaskRun).filter(TaskRun.id == "queued-run").first()
        task = db.query(ScheduledTask).filter(ScheduledTask.id == "queued-task").first()
        checkpoint = json.loads(run.steps)
        assert run.status == "aborted"
        assert checkpoint["recovery"] == "retry_safe"
        assert checkpoint["attempt_count"] == 1
        assert task.status == "active"
        assert task.next_run >= _test_utcnow()
        assert dispatched == []
    finally:
        db.close()


def test_restart_pauses_a_running_task_with_uncertain_outcome(monkeypatch):
    task_time = _test_utcnow() - timedelta(hours=1)

    def _setup(cd, ScheduledTask, TaskRun):
        db = cd.SessionLocal()
        db.add(ScheduledTask(
            id="running-task", owner="alice", name="running",
            next_run=task_time, status="active",
        ))
        db.add(TaskRun(
            id="running-run", task_id="running-task", started_at=task_time,
            status="running",
            steps=json.dumps({
                "version": 1,
                "stage": "execution_started",
                "attempt_count": 2,
                "recovery": "none",
            }),
        ))
        db.commit()
        db.close()

    cd, ScheduledTask, TaskRun, dispatched = _drive_scheduler(monkeypatch, _setup)

    db = cd.SessionLocal()
    try:
        run = db.query(TaskRun).filter(TaskRun.id == "running-run").first()
        task = db.query(ScheduledTask).filter(ScheduledTask.id == "running-task").first()
        checkpoint = json.loads(run.steps)
        assert run.status == "aborted"
        assert checkpoint["recovery"] == "reconciliation_required"
        assert checkpoint["attempt_count"] == 2
        assert task.status == "paused"
        assert task.next_run is None
        assert dispatched == []
    finally:
        db.close()


def test_restart_pauses_after_maximum_safe_recovery_attempts(monkeypatch):
    task_time = _test_utcnow() - timedelta(hours=1)

    def _setup(cd, ScheduledTask, TaskRun):
        db = cd.SessionLocal()
        db.add(ScheduledTask(
            id="retries-exhausted", owner="alice", name="retries",
            next_run=task_time, status="active",
        ))
        db.add(TaskRun(
            id="last-queued-run", task_id="retries-exhausted", started_at=task_time,
            status="queued",
            steps=json.dumps({
                "version": 1,
                "stage": "queued",
                "attempt_count": 3,
                "recovery": "none",
            }),
        ))
        db.commit()
        db.close()

    cd, ScheduledTask, TaskRun, dispatched = _drive_scheduler(monkeypatch, _setup)

    db = cd.SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(
            ScheduledTask.id == "retries-exhausted"
        ).first()
        assert task.status == "paused"
        assert task.next_run is None
        assert dispatched == []
    finally:
        db.close()


def test_restart_recovery_is_scoped_to_the_interrupted_task_owner(monkeypatch):
    task_time = _test_utcnow() - timedelta(hours=1)
    bob_next_run = _test_utcnow() + timedelta(hours=2)

    def _setup(cd, ScheduledTask, TaskRun):
        db = cd.SessionLocal()
        db.add(ScheduledTask(
            id="alice-task", owner="alice", name="uncertain",
            next_run=task_time, status="active",
        ))
        db.add(ScheduledTask(
            id="bob-task", owner="bob", name="unrelated",
            next_run=bob_next_run, status="active",
        ))
        db.add(TaskRun(
            id="alice-run", task_id="alice-task", started_at=task_time,
            status="running",
        ))
        db.commit()
        db.close()

    cd, ScheduledTask, TaskRun, dispatched = _drive_scheduler(monkeypatch, _setup)

    db = cd.SessionLocal()
    try:
        alice = db.query(ScheduledTask).filter(ScheduledTask.id == "alice-task").first()
        bob = db.query(ScheduledTask).filter(ScheduledTask.id == "bob-task").first()
        assert alice.status == "paused"
        assert bob.status == "active"
        assert bob.next_run == bob_next_run
        assert dispatched == []
    finally:
        db.close()


def test_startup_does_not_advance_paused_tasks(monkeypatch):
    """A paused task with an old next_run is not overdue for execution —
    it should not be advanced by the startup sweep."""
    def _setup(cd, ScheduledTask, TaskRun):
        db = cd.SessionLocal()
        db.add(ScheduledTask(
            id="t_paused", owner="alice", name="paused",
            task_type="llm",
            next_run=_test_utcnow() - timedelta(hours=1),
            status="paused",
        ))
        db.commit()
        db.close()

    cd, ScheduledTask, TaskRun, dispatched = _drive_scheduler(monkeypatch, _setup)

    db = cd.SessionLocal()
    t = db.query(ScheduledTask).filter(ScheduledTask.id == "t_paused").first()
    db.close()
    # The stored next_run should still be ~1h in the past (the startup sweep
    # only advances active overdue tasks; a paused task with an old next_run
    # is left alone). Allow a small delta to absorb the time the sweep took.
    one_hour_ago = _test_utcnow() - timedelta(hours=1)
    assert abs((t.next_run - one_hour_ago).total_seconds()) < 5, (
        f"Paused task's next_run was modified: "
        f"expected ~{one_hour_ago}, got {t.next_run}"
    )
