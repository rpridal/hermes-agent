"""Isolated autonomy acceptance harness.

Protocol mode uses deterministic fixture workers with real SQLite, dispatcher,
CLI subprocesses, and lifecycle transitions.  It is *not* live LLM E2E.
Live mode is deliberately an opt-in stub boundary: it refuses unless both a
positive opt-in and a bounded timeout/cost budget are supplied by the caller.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Iterator

from hermes_cli import kanban_db as kb


BOARD = "autonomy-e2e"
LIVE_OPT_IN_ENV = "HERMES_AUTONOMY_E2E_LIVE"
LIVE_TIMEOUT_SECONDS_ENV = "HERMES_AUTONOMY_E2E_TIMEOUT_SECONDS"
LIVE_COST_BUDGET_ENV = "HERMES_AUTONOMY_E2E_COST_BUDGET_USD"
_SCRUBBED_ENV = (
    "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_LOGS_ROOT",
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_DISPATCH_IN_GATEWAY",
    "HERMES_DELEGATED_CHILD_CONTEXT", "_HERMES_GATEWAY", "HERMES_GATEWAY_SESSION", "HERMES_CRON_SESSION",
    "HERMES_UPDATER_RUNNING", "HERMES_UPDATE_IN_PROGRESS", "TERMINAL_CWD",
)
_PATH_CONTAINMENT_ENV = (
    "HOME", "HERMES_HOME", "HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_LOGS_ROOT", "TERMINAL_CWD", "PYTHONPYCACHEPREFIX",
    "XDG_CACHE_HOME", "UV_CACHE_DIR", "PIP_CACHE_DIR", "TMPDIR", "TMP", "TEMP",
)


def _snapshot_outside_root(*paths: Path) -> dict[str, tuple[object, ...]]:
    """Capture metadata and bytes for designated paths outside the harness root."""
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in paths:
        entries = [path, *sorted(path.rglob("*"))] if path.is_dir() else [path]
        for entry in entries:
            try:
                stat = entry.lstat()
            except FileNotFoundError:
                snapshot[str(entry)] = ("missing",)
                continue
            digest = (
                hashlib.sha256(entry.read_bytes()).hexdigest()
                if entry.is_file() and not entry.is_symlink()
                else None
            )
            snapshot[str(entry)] = (
                "present", stat.st_mode, stat.st_size, stat.st_mtime_ns, digest,
            )
    return snapshot


@dataclass(frozen=True)
class ProtocolResult:
    mode: str
    is_live: bool
    root: Path
    board: str
    task_statuses: dict[str, str]
    heartbeat_count: int
    request_changes_count: int
    remediation_review_completed: bool
    workspace_identity_verified: bool
    all_paths_within_root: bool
    env_was_sanitized: bool
    artifacts: tuple[Path, ...]


@dataclass(frozen=True)
class _TrackedWorker:
    """A worker plus the isolated POSIX group created for it at launch."""

    process: subprocess.Popen
    pgid: int

    @classmethod
    def from_process(cls, process: subprocess.Popen) -> _TrackedWorker:
        # ``start_new_session=True`` makes the exec'd worker both session and
        # process-group leader, so this launch identity is its PID.  POSIX
        # reserves a live process group's ID until its final member exits;
        # therefore that ID cannot designate an unrelated group while a
        # descendant still needs cleanup.  We never infer a group from a later
        # (potentially recycled) PID.
        return cls(process=process, pgid=process.pid)


@contextmanager
def _temporary_env(values: dict[str, str]) -> Iterator[None]:
    saved = {name: os.environ.get(name) for name in set(values) | set(_SCRUBBED_ENV)}
    try:
        for name in _SCRUBBED_ENV:
            os.environ.pop(name, None)
        os.environ.update(values)
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class ProtocolHarness:
    """Run the deterministic, isolated Kanban lifecycle acceptance scenario."""

    def __init__(self, *, root: Path, timeout_seconds: float = 30) -> None:
        self.root = root.resolve()
        self.timeout_seconds = timeout_seconds
        self.hermes_home = self.root / "hermes-home"
        self.board_db = self.hermes_home / "kanban" / "boards" / BOARD / "kanban.db"
        self.workspaces_root = self.root / "workspaces"
        self.artifacts: list[Path] = []

    def _env(self) -> dict[str, str]:
        cache_root = self.root / "cache"
        temp_root = self.root / "tmp"
        return {
            "HOME": str(self.root / "home"),
            "HERMES_HOME": str(self.hermes_home),
            "HERMES_KANBAN_BOARD": BOARD,
            "HERMES_KANBAN_DB": str(self.board_db),
            "HERMES_KANBAN_WORKSPACES_ROOT": str(self.workspaces_root),
            "HERMES_KANBAN_LOGS_ROOT": str(self.board_db.parent / "logs"),
            "TERMINAL_CWD": str(self.root / "workspace"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(cache_root / "pycache"),
            "XDG_CACHE_HOME": str(cache_root / "xdg"),
            "UV_CACHE_DIR": str(cache_root / "uv"),
            "PIP_CACHE_DIR": str(cache_root / "pip"),
            "TMPDIR": str(temp_root),
            "TMP": str(temp_root),
            "TEMP": str(temp_root),
        }

    def _configured_paths_are_contained(self, env: dict[str, str]) -> bool:
        return all(
            Path(env[name]).resolve().is_relative_to(self.root)
            for name in _PATH_CONTAINMENT_ENV
        )

    def run(self) -> ProtocolResult:
        self.root.mkdir(parents=True, exist_ok=False)
        env = self._env()
        checkout_acceptance = Path(__file__).parent
        host_kanban_db = Path.home() / ".hermes" / "kanban.db"
        outside_before = _snapshot_outside_root(checkout_acceptance, host_kanban_db)
        initialized_paths_before = set(kb._INITIALIZED_PATHS)
        for path in (
            self.hermes_home / "profiles",
            self.root / "workspace",
            self.root / "cache",
            self.root / "tmp",
            self.root / "home",
        ):
            path.mkdir(parents=True, exist_ok=True)
        for name in ("architect", "developer", "tester", "reviewer"):
            (self.hermes_home / "profiles" / name).mkdir(parents=True, exist_ok=True)
        with _temporary_env(env):
            env_was_sanitized = (
                all(os.environ.get(name) == value for name, value in env.items())
                and all(name in env or name not in os.environ for name in _SCRUBBED_ENV)
            )
            try:
                kb._INITIALIZED_PATHS.clear()
                kb.create_board(BOARD)
                with kb.connect_closing(board=BOARD) as conn:
                    implementation = kb.create_task(
                        conn, title="protocol implementation", assignee="developer",
                        workspace_kind="dir", workspace_path=str(self.workspaces_root / "implementation"),
                    )
                    self._drive(conn, implementation)
                    tasks = kb.list_tasks(conn)
                    statuses = {task.id: task.status for task in tasks}
                    events = [event for task in tasks for event in kb.list_events(conn, task.id)]
                    runs = kb.list_runs(conn, implementation)
                    remediation_review_completed = (
                        any(run.outcome == "changes_requested" for run in runs)
                        and any(run.outcome == "completed" and run.profile == "reviewer" for run in runs)
                    )
            finally:
                kb._INITIALIZED_PATHS.clear()
                kb._INITIALIZED_PATHS.update(initialized_paths_before)
        self.artifacts = sorted(path for path in self.root.rglob("*") if path.is_file())
        outside_unchanged = (
            _snapshot_outside_root(checkout_acceptance, host_kanban_db) == outside_before
        )
        paths_ok = (
            self._configured_paths_are_contained(env)
            and outside_unchanged
            and all(path.resolve().is_relative_to(self.root) for path in self.artifacts)
        )
        workspace_ok = self._verify_workspace_identity(implementation)
        return ProtocolResult(
            mode="protocol", is_live=False, root=self.root, board=BOARD, task_statuses=statuses,
            heartbeat_count=sum(event.kind == "heartbeat" for event in events),
            request_changes_count=sum(event.kind == "changes_requested" for event in events),
            remediation_review_completed=remediation_review_completed,
            workspace_identity_verified=workspace_ok, all_paths_within_root=paths_ok,
            env_was_sanitized=env_was_sanitized,
            artifacts=tuple(self.artifacts),
        )

    def _drive(self, conn, implementation: str) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        processes: list[_TrackedWorker] = []

        def spawn(task, workspace, _board=None):
            assert task.id == implementation, "protocol must stay on its one isolated card"
            events = kb.list_events(conn, implementation)
            if task.assignee == "developer":
                action = "developer-remediation" if any(e.kind == "changes_requested" for e in events) else "developer-v1"
            else:
                action = "remediation-review" if any(e.kind == "changes_requested" for e in events) else "reviewer-change"
            child_env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED_ENV}
            child_env.update(self._env())
            child_env.update({"PYTHONPATH": str(Path(__file__).resolve().parents[2]), "HERMES_KANBAN_TASK": task.id,
                              "HERMES_KANBAN_WORKSPACE": str(workspace), "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
                              "HERMES_KANBAN_CLAIM_LOCK": str(task.claim_lock), "HERMES_AUTONOMY_PROTOCOL_ACTION": action})
            log = self.board_db.parent / "logs" / f"{task.id}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("ab") as out:
                process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("_protocol_worker.py"))], env=child_env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            processes.append(_TrackedWorker.from_process(process))
            return process.pid

        try:
            while time.monotonic() < deadline:
                kb.dispatch_once(conn, spawn_fn=spawn, board=BOARD, max_spawn=1)
                current = kb.get_task(conn, implementation)
                assert current is not None, "protocol card disappeared"
                if current.status == "done":
                    break
                time.sleep(0.05)
            completed = kb.get_task(conn, implementation)
            assert completed is not None and completed.status == "done", "protocol lifecycle timed out"
        finally:
            self._cleanup_processes(processes)

    @staticmethod
    def _cleanup_processes(processes: list[_TrackedWorker]) -> None:
        """Boundedly terminate every launch-recorded POSIX worker group.

        This acceptance harness is intentionally POSIX-only (the test module
        skips elsewhere).  The direct ``Popen`` child is the launch-owned
        identity for the group.  Do not reap it between TERM and KILL: even if
        it has exited, its unreaped child PID cannot be recycled, so the group
        number cannot become an unrelated group before the escalation.  Once
        it has already been reaped, the numeric group identity is stale and
        must receive no escalation signal.
        """
        for worker in processes:
            # A prior cleanup may already have reaped this direct child.  That
            # releases its PID/PGID number, so it no longer authenticates any
            # numeric process group and no group signal is safe.
            if worker.process.returncode is not None:
                continue
            try:
                os.killpg(worker.pgid, signal.SIGTERM)
                # ``Popen.returncode`` changes only when this parent reaps (or
                # polls) its direct child.  It is therefore a retained,
                # launch-owned identity without the racy ``killpg(pgid, 0)``
                # probe.  Escalate before the final wait can release that ID.
                if worker.process.returncode is None:
                    os.killpg(worker.pgid, signal.SIGKILL)
                worker.process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                # Cleanup must never replace the original lifecycle failure.
                pass

    def _verify_workspace_identity(self, implementation: str) -> bool:
        markers = list(self.workspaces_root.rglob("*.json"))
        if not markers or not all(path.resolve().is_relative_to(self.root) for path in markers):
            return False
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in markers]
        return (
            all(payload.get("task_id") == implementation for payload in payloads)
            and any(payload.get("revision") == 2 for payload in payloads)
            and any(payload.get("approved") is True for payload in payloads)
        )


def run_live() -> None:
    """Guard live model E2E; its actual runner is intentionally outside CI."""
    if os.environ.get(LIVE_OPT_IN_ENV) != "1":
        raise RuntimeError("live autonomy E2E requires explicit opt-in")
    try:
        timeout = int(os.environ.get(LIVE_TIMEOUT_SECONDS_ENV, "0"))
    except ValueError as exc:
        raise RuntimeError("live autonomy E2E requires a positive timeout") from exc
    if timeout <= 0:
        raise RuntimeError("live autonomy E2E requires a positive timeout")
    try:
        budget = float(os.environ.get(LIVE_COST_BUDGET_ENV, "0"))
    except ValueError as exc:
        raise RuntimeError("live autonomy E2E requires a positive cost guard") from exc
    if not math.isfinite(budget) or budget <= 0:
        raise RuntimeError("live autonomy E2E requires a positive cost guard")
    raise RuntimeError("live autonomy E2E is opt-in only and is not run by protocol CI")
