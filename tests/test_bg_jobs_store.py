import json
import time

from src import bg_jobs


def test_load_ignores_non_object_store(tmp_path, monkeypatch):
    store = tmp_path / "bg_jobs.json"
    store.write_text(json.dumps(["not", "a", "job", "store"]), encoding="utf-8")
    monkeypatch.setattr(bg_jobs, "_STORE", store)

    assert bg_jobs._load() == {}


def test_load_keeps_only_object_job_records(tmp_path, monkeypatch):
    store = tmp_path / "bg_jobs.json"
    store.write_text(
        json.dumps(
            {
                "good": {"id": "good", "status": "done"},
                "bad-list": ["not", "a", "job"],
                "bad-null": None,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bg_jobs, "_STORE", store)

    assert bg_jobs._load() == {"good": {"id": "good", "status": "done"}}


def test_followup_claim_is_durable_and_not_retried_after_uncertain_failure(
    tmp_path, monkeypatch
):
    store = tmp_path / "bg_jobs.json"
    monkeypatch.setattr(bg_jobs, "_STORE", store)
    bg_jobs._save({
        "job-1": {
            "id": "job-1",
            "status": "done",
            "followed_up": False,
        }
    })

    assert [job["id"] for job in bg_jobs.pending_followups()] == ["job-1"]
    assert bg_jobs.claim_followup("job-1") is True
    assert bg_jobs.claim_followup("job-1") is False
    assert bg_jobs.pending_followups() == []

    bg_jobs.mark_followup_uncertain("job-1")

    saved = bg_jobs._load()["job-1"]
    assert saved["followup_state"] == "reconciliation_required"
    assert saved["followup_checkpoint"] == "interrupted"
    assert saved["followup_attempts"] == 1
    assert bg_jobs.pending_followups() == []


def test_followup_completion_and_interrupted_owner_recovery(tmp_path, monkeypatch):
    store = tmp_path / "bg_jobs.json"
    monkeypatch.setattr(bg_jobs, "_STORE", store)
    bg_jobs._save({
        "completed": {
            "id": "completed",
            "status": "done",
            "followed_up": False,
        },
        "interrupted": {
            "id": "interrupted",
            "status": "failed",
            "followed_up": False,
            "followup_state": "running",
            "followup_pid": -1,
        },
    })
    monkeypatch.setattr(bg_jobs, "_pid_alive", lambda pid: False)

    assert bg_jobs.claim_followup("completed") is True
    bg_jobs.mark_followed_up("completed")

    pending = bg_jobs.pending_followups()
    assert pending == []
    saved = bg_jobs._load()
    assert saved["completed"]["followup_state"] == "complete"
    assert saved["interrupted"]["followup_state"] == "reconciliation_required"


def test_refresh_recovers_launching_job_from_persisted_pid(tmp_path, monkeypatch):
    store = tmp_path / "bg_jobs.json"
    pid_file = tmp_path / "job.pid"
    pid_file.write_text("1234", encoding="utf-8")
    monkeypatch.setattr(bg_jobs, "_STORE", store)
    monkeypatch.setattr(bg_jobs, "_pid_alive", lambda pid: pid == 1234)

    def unexpected_kill(pid):
        raise AssertionError(f"live job {pid} was killed")

    monkeypatch.setattr(bg_jobs, "_kill", unexpected_kill)
    bg_jobs._save({
        "job-1": {
            "id": "job-1",
            "status": "launching",
            "pid": None,
            "pid_path": str(pid_file),
            "exit_path": str(tmp_path / "job.exit"),
            "started_at": time.time(),
        }
    })

    refreshed = bg_jobs.refresh()["job-1"]

    assert refreshed["status"] == "running"
    assert refreshed["pid"] == 1234


def test_refresh_marks_unresolved_launch_for_manual_reconciliation(
    tmp_path, monkeypatch
):
    store = tmp_path / "bg_jobs.json"
    monkeypatch.setattr(bg_jobs, "_STORE", store)
    monkeypatch.setattr(bg_jobs, "_pid_alive", lambda pid: False)
    bg_jobs._save({
        "job-1": {
            "id": "job-1",
            "status": "launching",
            "pid": None,
            "pid_path": str(tmp_path / "missing.pid"),
            "exit_path": str(tmp_path / "missing.exit"),
            "started_at": 1,
            "followed_up": False,
        }
    })

    refreshed = bg_jobs.refresh()["job-1"]

    assert refreshed["status"] == "failed"
    assert refreshed["launch_uncertain"] is True
    assert refreshed["followup_state"] == "reconciliation_required"
    assert bg_jobs.pending_followups() == []
