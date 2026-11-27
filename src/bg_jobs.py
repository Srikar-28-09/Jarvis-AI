"""Background job execution for the agent's `bash` tool.

Long commands (installs, ffmpeg, model downloads) should NOT block the chat
stream — a multi-minute held SSE connection is fragile (model-stops-early,
timeouts, tab suspend). Instead we launch them **detached** and let an
always-on monitor re-invoke the agent when they finish ("auto-continue").

Design goals:
  * Restart-safe: status is derived from an on-disk exit-code file, not a live
    PID, so a uvicorn restart never loses a job or its result.
  * Idempotent follow-up: a job stays {done, followed_up: False} until the
    agent has actually been re-invoked, so completion can never silently
    "do nothing" — the monitor retries on the next tick.
  * Bounded: a hard max-runtime marks a runaway job failed and STILL triggers
    a follow-up ("timed out"), so you always hear back.

This module only owns launch + state. The monitor / agent re-invocation lives
in the caller (so this stays import-light and unit-testable).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from core.platform_compat import (
    detached_popen_kwargs,
    find_bash,
    git_bash_path,
    kill_process_tree,
    pid_alive,
)

from src.constants import BG_JOBS_DIR, BG_JOBS_FILE

_JOBS_DIR = Path(BG_JOBS_DIR)
_STORE = Path(BG_JOBS_FILE)
_FOLLOWUP_LOCK = threading.Lock()
_JOBS_LOCK = threading.Lock()

# A job that runs longer than this is presumed stuck and reaped (the agent
# still gets a "timed out" follow-up so nothing hangs forever).
DEFAULT_MAX_RUNTIME_S = 3600  # 1 hour
# Cap how much captured output we keep / feed back to the model.
_MAX_OUTPUT_CHARS = 16000
# How long a finished-and-followed-up job (record + its .sh/.cmd.sh/.log/.exit
# files) is kept before pruning, so neither the store nor data/bg_jobs/ grows
# without bound. The agent has already consumed the result by then.
_RETENTION_S = 3600  # 1 hour after follow-up


def _max_concurrent_jobs_per_owner() -> int:
    raw = os.getenv("ODYSSEUS_MAX_CONCURRENT_BACKGROUND_JOBS_PER_USER", "4").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 4
    return max(1, value)


def _load() -> Dict[str, Dict[str, Any]]:
    try:
        if _STORE.exists():
            data = json.loads(_STORE.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                return {}
            return {str(job_id): rec for job_id, rec in data.items() if isinstance(rec, dict)}
    except Exception:
        pass
    return {}


def _save(jobs: Dict[str, Dict[str, Any]]) -> None:
    atomic_write_json(str(_STORE), jobs, indent=2)


def _pid_alive(pid: Optional[int]) -> bool:
    # Delegates to the platform-safe probe. NB: a bare os.kill(pid, 0) is unsafe
    # on Windows — CPython routes it to TerminateProcess, which would KILL the
    # job we're only trying to check. core.platform_compat.pid_alive handles
    # both OSes correctly.
    return pid_alive(pid)


def launch(command: str, session_id: str, cwd: Optional[str] = None,
           max_runtime_s: int = DEFAULT_MAX_RUNTIME_S,
           owner: Optional[str] = None) -> Dict[str, Any]:
    """Launch `command` detached. Returns the job record (status='running').

    Output + the final exit code are written to files so status survives a
    server restart. The process is put in its own session (setsid) so it
    outlives the request/stream that started it.
    """
    from src.owner_identity import effective_storage_owner

    resolved_owner = effective_storage_owner(owner)
    if not resolved_owner:
        raise ValueError("Background job requires a canonical owner")
    with _JOBS_LOCK:
        active_for_owner = sum(
            1
            for rec in _load().values()
            if rec.get("owner") == resolved_owner
            and rec.get("status") in ("launching", "running")
        )
        if active_for_owner >= _max_concurrent_jobs_per_owner():
            raise ValueError("Per-user concurrent background-job limit reached")
    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    log_path = _JOBS_DIR / f"{job_id}.log"
    exit_path = _JOBS_DIR / f"{job_id}.exit"
    pid_path = _JOBS_DIR / f"{job_id}.pid"

    # The user command goes in its OWN script file, run as a child `bash`. This
    # is what isolates it: an `exit` inside it only ends that child (so the
    # wrapper still records the exit code), and — unlike textually wrapping the
    # command in `( … )` — the wrapper can't be broken by an unbalanced paren or
    # a trailing line-continuation in the command. `$?` is the child's real
    # exit status.
    bash = find_bash()
    if bash:
        # POSIX, or Windows with Git Bash/WSL. The user command goes in its OWN
        # script file, run as a child `bash` — an `exit` inside it only ends
        # that child (so the wrapper still records the exit code), and an
        # unbalanced paren / trailing line-continuation in the command can't
        # break the wrapper. `$?` is the child's real exit status. Paths are
        # emitted as POSIX (forward-slash) + shell-quoted so Git Bash on Windows
        # handles drive paths and spaces correctly.
        cmd_path = _JOBS_DIR / f"{job_id}.cmd.sh"
        cmd_path.write_text(command + "\n", encoding="utf-8")
        lp, xp, cp, pp = (
            shlex.quote(git_bash_path(p))
            for p in (log_path, exit_path, cmd_path, pid_path)
        )
        script_path = _JOBS_DIR / f"{job_id}.sh"
        script_path.write_text(
            f"echo $$ > {pp}\n"
            f"bash {cp} > {lp} 2>&1\n"
            f"echo $? > {xp}\n",
            encoding="utf-8",
        )
        argv = [bash, str(script_path)]
    else:
        # Windows without any bash installed: cmd.exe wrapper. The command runs
        # in its own child .cmd so %ERRORLEVEL% is the command's real exit code.
        child_path = _JOBS_DIR / f"{job_id}.child.cmd"
        child_path.write_text("@echo off\r\n" + command + "\r\n", encoding="utf-8")
        script_path = _JOBS_DIR / f"{job_id}.cmd"
        script_path.write_text(
            "@echo off\r\n"
            f'call "{child_path}" > "{log_path}" 2>&1\r\n'
            f'echo %ERRORLEVEL%> "{exit_path}"\r\n',
            encoding="utf-8",
        )
        argv = [os.environ.get("ComSpec", "cmd.exe"), "/c", str(script_path)]

    rec = {
        "id": job_id,
        "session_id": session_id,
        "owner": resolved_owner,
        "command": command,
        "status": "launching",     # launching | running | done | failed
        "pid": None,
        "started_at": time.time(),
        "ended_at": None,
        "exit_code": None,
        "max_runtime_s": max_runtime_s,
        "followed_up": False,       # has the agent been re-invoked with the result?
        "log_path": str(log_path),
        "exit_path": str(exit_path),
        "pid_path": str(pid_path),
    }
    with _JOBS_LOCK:
        jobs = _load()
        jobs[job_id] = rec
        _save(jobs)

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            cwd=cwd or None,
            **detached_popen_kwargs(),  # detach from the request lifecycle (setsid / DETACHED_PROCESS)
        )
    except Exception as exc:
        rec["status"] = "failed"
        rec["exit_code"] = -1
        rec["ended_at"] = time.time()
        rec["launch_error"] = type(exc).__name__
        rec["followed_up"] = True
        rec["followup_state"] = "complete"
        rec["followup_checkpoint"] = "launch_failed"
        jobs[job_id] = rec
        _save(jobs)
        raise

    rec["status"] = "running"
    rec["pid"] = proc.pid
    jobs[job_id] = rec
    _save(jobs)
    return rec


def _read_output(rec: Dict[str, Any]) -> str:
    try:
        txt = Path(rec["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if len(txt) > _MAX_OUTPUT_CHARS:
        # Keep head + tail — the interesting bits are usually at both ends.
        head = txt[: _MAX_OUTPUT_CHARS // 2]
        tail = txt[-_MAX_OUTPUT_CHARS // 2:]
        txt = head + "\n…[truncated]…\n" + tail
    return txt


def _prune(jobs: Dict[str, Dict[str, Any]], now: float) -> bool:
    """Drop records (and their on-disk files) for jobs that finished, were
    followed up, and are older than the retention window. Mutates `jobs`."""
    stale = [jid for jid, rec in jobs.items()
             if rec.get("followed_up") and rec.get("ended_at")
             and (now - rec["ended_at"]) > _RETENTION_S]
    for jid in stale:
        jobs.pop(jid, None)
        for p in _JOBS_DIR.glob(f"{jid}.*"):   # .sh .cmd.sh .log .exit
            try:
                p.unlink()
            except Exception:
                pass
    return bool(stale)


def refresh() -> Dict[str, Dict[str, Any]]:
    """Reconcile every running job against disk. Marks done/failed (incl.
    timeout). Idempotent — safe to call from a poll loop. Returns the store."""
    jobs = _load()
    changed = False
    now = time.time()
    for rec in jobs.values():
        if rec.get("status") == "launching":
            exit_path = Path(rec.get("exit_path", ""))
            pid = rec.get("pid")
            if not pid:
                try:
                    pid = int(
                        Path(rec["pid_path"])
                        .read_text(encoding="utf-8", errors="replace")
                        .strip()
                    )
                except (KeyError, OSError, ValueError):
                    pid = None
            if exit_path.exists():
                try:
                    code = int(exit_path.read_text(encoding="utf-8", errors="replace").strip() or "1")
                except Exception:
                    code = 1
                rec["pid"] = pid
                rec["exit_code"] = code
                rec["status"] = "done" if code == 0 else "failed"
                rec["ended_at"] = now
            elif pid and _pid_alive(pid):
                rec["pid"] = pid
                rec["status"] = "running"
            else:
                rec["status"] = "failed"
                rec["exit_code"] = -1
                rec["ended_at"] = now
                rec["launch_uncertain"] = True
                rec["followup_state"] = "reconciliation_required"
                rec["followup_checkpoint"] = "launch_outcome_uncertain"
            changed = True
            if rec.get("status") != "running":
                continue
        if rec.get("status") != "running":
            continue
        exit_path = Path(rec.get("exit_path", ""))
        if exit_path.exists():
            try:
                code = int(exit_path.read_text(encoding="utf-8", errors="replace").strip() or "1")
            except Exception:
                code = 1
            rec["exit_code"] = code
            rec["status"] = "done" if code == 0 else "failed"
            rec["ended_at"] = now
            changed = True
        elif (now - rec.get("started_at", now)) > rec.get("max_runtime_s", DEFAULT_MAX_RUNTIME_S):
            # Runaway / stuck — reap it but STILL surface a follow-up.
            _kill(rec.get("pid"))
            rec["status"] = "failed"
            rec["exit_code"] = -1
            rec["ended_at"] = now
            rec["timed_out"] = True
            changed = True
        elif not _pid_alive(rec.get("pid")) and not exit_path.exists():
            # Process vanished without writing an exit code (killed, OOM,
            # crash). Don't leave it "running" forever.
            rec["status"] = "failed"
            rec["exit_code"] = -1
            rec["ended_at"] = now
            rec["died"] = True
            changed = True
    if _prune(jobs, now):
        changed = True
    if changed:
        _save(jobs)
    return jobs


def _kill(pid: Optional[int]) -> None:
    # Cross-platform process-tree teardown (POSIX killpg / Windows taskkill /T).
    kill_process_tree(pid)


def pending_followups() -> List[Dict[str, Any]]:
    """Finished jobs with no follow-up claim or recorded outcome."""
    jobs = refresh()
    changed = False
    for rec in jobs.values():
        if rec.get("followup_state") != "running":
            continue
        pid = rec.get("followup_pid")
        if pid == os.getpid() or _pid_alive(pid):
            continue
        rec["followup_state"] = "reconciliation_required"
        rec["followup_checkpoint"] = "interrupted"
        rec["followup_updated_at"] = time.time()
        changed = True
    if changed:
        _save(jobs)
    return [
        r for r in jobs.values()
        if r.get("status") in ("done", "failed")
        and not r.get("followed_up")
        and r.get("followup_state") not in ("running", "reconciliation_required", "complete")
    ]


def claim_followup(job_id: str) -> bool:
    """Persist an at-most-once follow-up claim before agent actions begin."""
    with _FOLLOWUP_LOCK:
        jobs = _load()
        rec = jobs.get(job_id)
        if (
            not rec
            or rec.get("status") not in ("done", "failed")
            or rec.get("followed_up")
            or rec.get("followup_state") in (
                "running",
                "reconciliation_required",
                "complete",
            )
        ):
            return False
        rec["followup_state"] = "running"
        rec["followup_checkpoint"] = "agent_execution_started"
        rec["followup_attempts"] = int(rec.get("followup_attempts", 0)) + 1
        rec["followup_pid"] = os.getpid()
        rec["followup_started_at"] = time.time()
        rec["followup_updated_at"] = rec["followup_started_at"]
        _save(jobs)
        return True


def mark_followup_uncertain(job_id: str) -> None:
    jobs = _load()
    rec = jobs.get(job_id)
    if rec and rec.get("followup_state") == "running":
        rec["followup_state"] = "reconciliation_required"
        rec["followup_checkpoint"] = "interrupted"
        rec["followup_updated_at"] = time.time()
        _save(jobs)


def mark_followed_up(job_id: str) -> None:
    jobs = _load()
    if job_id in jobs:
        jobs[job_id]["followed_up"] = True
        jobs[job_id]["followup_state"] = "complete"
        jobs[job_id]["followup_checkpoint"] = "complete"
        jobs[job_id]["followup_updated_at"] = time.time()
        _save(jobs)


def get(job_id: str, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if owner is None:
        return None
    from src.owner_identity import effective_storage_owner
    normalized_owner = effective_storage_owner(owner)
    if not normalized_owner:
        return None
    refresh()  # reconcile against disk so status/exit_code are current
    rec = _load().get(job_id)
    if rec:
        if rec.get("owner") != normalized_owner:
            return None
        rec = dict(rec)
        rec["output"] = _read_output(rec)
    return rec


def list_for_session(session_id: str, owner: Optional[str] = None) -> List[Dict[str, Any]]:
    if owner is None:
        return []
    from src.owner_identity import effective_storage_owner
    normalized_owner = effective_storage_owner(owner)
    if not normalized_owner:
        return []
    return [
        r for r in refresh().values()
        if r.get("session_id") == session_id
        and r.get("owner") == normalized_owner
    ]


def kill(job_id: str, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Terminate a running job's process tree and mark it killed. Returns the
    updated record, or None if the id is unknown. Idempotent: a job that already
    finished is returned unchanged. Sets followed_up so the monitor does not also
    fire an auto-continue for a job the agent deliberately stopped."""
    if owner is None:
        return None
    from src.owner_identity import effective_storage_owner
    normalized_owner = effective_storage_owner(owner)
    if not normalized_owner:
        return None
    jobs = _load()
    rec = jobs.get(job_id)
    if rec is None:
        return None
    if rec.get("owner") != normalized_owner:
        return None
    if rec.get("status") == "running":
        _kill(rec.get("pid"))
        rec["status"] = "failed"
        rec["exit_code"] = -1
        rec["ended_at"] = time.time()
        rec["killed"] = True
        rec["followed_up"] = True
        _save(jobs)
    return rec


def cancel_owner(owner: Optional[str]) -> int:
    """Cancel all running jobs owned by a canonical owner."""
    if owner is None:
        return 0
    from src.owner_identity import effective_storage_owner
    normalized_owner = effective_storage_owner(owner)
    if not normalized_owner:
        return 0
    cancelled = 0
    jobs = _load()
    for rec in jobs.values():
        if rec.get("owner") != normalized_owner:
            continue
        if rec.get("status") == "running":
            _kill(rec.get("pid"))
            rec["status"] = "failed"
            rec["exit_code"] = -1
            rec["ended_at"] = time.time()
            rec["killed"] = True
            rec["followed_up"] = True
            cancelled += 1
    if cancelled:
        _save(jobs)
    return cancelled


def result_text(rec: Dict[str, Any]) -> str:
    """Human/agent-readable summary of a finished job, for the follow-up."""
    out = _read_output(rec)
    if rec.get("killed"):
        head = "Background job was killed."
    elif rec.get("timed_out"):
        head = f"Background job timed out after {rec.get('max_runtime_s')}s."
    elif rec.get("died"):
        head = "Background job process died unexpectedly (no exit code)."
    else:
        head = f"Background job finished with exit code {rec.get('exit_code')}."
    return f"{head}\nCommand: {rec.get('command')}\n\nOutput:\n{out or '(no output)'}"
