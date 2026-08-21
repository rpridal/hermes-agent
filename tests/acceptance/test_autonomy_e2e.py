"""Acceptance contract for the isolated two-mode autonomy harness.

`protocol` is deterministic lifecycle coverage.  It deliberately uses a fixture
worker and must never be represented as live autonomous E2E.  `live` is an
explicit, cost-bounded opt-in and is intentionally not exercised by CI.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from tests.acceptance.autonomy_harness import (
    LIVE_COST_BUDGET_ENV,
    LIVE_OPT_IN_ENV,
    LIVE_TIMEOUT_SECONDS_ENV,
    _TrackedWorker,
    ProtocolHarness,
    run_live,
)


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="autonomy acceptance workers require POSIX process groups",
)


def _outside_snapshot(*paths: Path) -> dict[str, tuple[object, ...]]:
    """Return content-sensitive state for explicitly outside-root paths."""
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in paths:
        if path.is_dir():
            entries = [path, *sorted(path.rglob("*"))]
        else:
            entries = [path]
        for entry in entries:
            key = str(entry)
            try:
                stat = entry.lstat()
            except FileNotFoundError:
                snapshot[key] = ("missing",)
                continue
            digest = (
                hashlib.sha256(entry.read_bytes()).hexdigest()
                if entry.is_file() and not entry.is_symlink()
                else None
            )
            snapshot[key] = (
                "present", stat.st_mode, stat.st_size, stat.st_mtime_ns, digest,
            )
    return snapshot


def test_protocol_harness_is_hermetic_and_proves_lifecycle(tmp_path: Path, monkeypatch):
    """Real SQLite/subprocess protocol run stays entirely below its temp root."""
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "a-live-board")
    monkeypatch.setenv("HERMES_KANBAN_DB", "/tmp/not-allowed.db")
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_CRON_SESSION", "leaked")
    monkeypatch.setenv("HERMES_UPDATER_RUNNING", "1")

    root = tmp_path / "autonomy-e2e"
    outside_sentinel = tmp_path / "outside-root-sentinel"
    outside_sentinel.write_text("must not change", encoding="utf-8")
    checkout_acceptance = Path(__file__).parent
    host_db = Path.home() / ".hermes" / "kanban.db"
    before_outside = _outside_snapshot(checkout_acceptance, outside_sentinel, host_db)
    initialized_paths_before = set(kb._INITIALIZED_PATHS)
    try:
        result = ProtocolHarness(root=root).run()

        assert result.mode == "protocol"
        assert result.is_live is False
        assert result.root == root.resolve()
        assert result.board == "autonomy-e2e"
        assert result.task_statuses
        assert set(result.task_statuses.values()) == {"done"}
        assert result.heartbeat_count >= 1
        assert result.request_changes_count == 1
        assert result.remediation_review_completed is True
        assert result.workspace_identity_verified is True
        assert result.all_paths_within_root is True
        assert result.env_was_sanitized is True
        assert all(path.is_relative_to(root.resolve()) for path in result.artifacts)
        assert _outside_snapshot(checkout_acceptance, outside_sentinel, host_db) == before_outside
        assert kb._INITIALIZED_PATHS == initialized_paths_before
    finally:
        kb._INITIALIZED_PATHS.clear()
        kb._INITIALIZED_PATHS.update(initialized_paths_before)


def test_protocol_timeout_terminates_and_reaps_the_entire_worker_group(tmp_path: Path, monkeypatch):
    """A hung fixture worker and its child cannot outlive a timed-out run."""
    original_home = os.environ.get("HOME")
    original_hermes_home = os.environ.get("HERMES_HOME")
    monkeypatch.setenv("HERMES_AUTONOMY_PROTOCOL_FORCE_HANG", "1")
    root = tmp_path / "hung-autonomy-e2e"
    initialized_paths_before = set(kb._INITIALIZED_PATHS)

    with pytest.raises(AssertionError, match="protocol lifecycle timed out"):
        ProtocolHarness(root=root, timeout_seconds=0.2).run()
    assert kb._INITIALIZED_PATHS == initialized_paths_before

    identity = root / "workspaces" / "implementation" / "hung-worker.json"
    deadline = time.monotonic() + 2
    while not identity.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    payload = json.loads(identity.read_text(encoding="utf-8"))
    for pid in (payload["worker_pid"], payload["child_pid"]):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    with pytest.raises(ProcessLookupError):
        os.killpg(payload["worker_pid"], 0)

    assert all(path.resolve().is_relative_to(root.resolve()) for path in root.rglob("*"))
    assert os.environ.get("HOME") == original_home
    assert os.environ.get("HERMES_HOME") == original_hermes_home


def test_cleanup_never_signals_a_reused_numeric_group_after_term_reaps_leader(
    monkeypatch,
):
    """The SIGKILL escalation must precede reaping its launch identity.

    ``killpg`` below treats a signal issued after ``wait`` as delivery to a
    reused numeric PGID.  The old cleanup did SIGTERM → wait → SIGKILL and
    therefore reached that stale target; a safe cleanup must never do so.
    """
    class SimulatedLeader:
        returncode = None
        reaped = False
        wait_calls = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            del timeout
            self.wait_calls += 1
            self.reaped = True
            self.returncode = 0
            return 0

    leader = SimulatedLeader()
    signals: list[int] = []

    def killpg_reusing_after_reap(pgid: int, sig: int) -> None:
        assert pgid == 4242
        if leader.reaped:
            pytest.fail(f"signal {sig} targeted a reused numeric PGID")
        signals.append(sig)

    monkeypatch.setattr(os, "killpg", killpg_reusing_after_reap)

    ProtocolHarness._cleanup_processes([_TrackedWorker(process=leader, pgid=4242)])

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert leader.wait_calls == 1

    # A later cleanup pass sees the already-reaped leader and must not send
    # even a fresh SIGTERM to the now-reusable numeric group.
    ProtocolHarness._cleanup_processes([_TrackedWorker(process=leader, pgid=4242)])
    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_cleanup_kills_group_after_leader_exits_without_touching_unrelated_group(tmp_path: Path):
    """The launch-recorded group survives a leader-exit race without broad kills."""
    child_marker = tmp_path / "orphan-child.json"
    leader_code = (
        "import json, pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
        f"pathlib.Path({str(child_marker)!r}).write_text(json.dumps({{'pid': child.pid}}))"
    )
    leader = subprocess.Popen([sys.executable, "-c", leader_code], start_new_session=True)
    tracked = _TrackedWorker.from_process(leader)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
        start_new_session=True,
    )
    try:
        # Do not reap the launch-owned leader here: while it is an unreaped
        # direct child its PID still anchors the original group identity for
        # the cleanup escalation below.
        deadline = time.monotonic() + 2
        while not child_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid = json.loads(child_marker.read_text(encoding="utf-8"))["pid"]

        ProtocolHarness._cleanup_processes([tracked])

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail("descendant survived cleanup after its leader exited")
        os.kill(unrelated.pid, 0)
        assert os.getpgid(unrelated.pid) == unrelated.pid
    finally:
        try:
            os.killpg(unrelated.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        unrelated.wait(timeout=2)


def test_live_mode_refuses_to_run_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv(LIVE_OPT_IN_ENV, raising=False)
    monkeypatch.delenv(LIVE_TIMEOUT_SECONDS_ENV, raising=False)

    with pytest.raises(RuntimeError, match="explicit opt-in"):
        run_live()


def test_live_mode_requires_bounded_timeout(monkeypatch):
    monkeypatch.setenv(LIVE_OPT_IN_ENV, "1")
    monkeypatch.setenv(LIVE_TIMEOUT_SECONDS_ENV, "0")

    with pytest.raises(RuntimeError, match="timeout"):
        run_live()


def test_live_mode_requires_positive_cost_guard(monkeypatch):
    monkeypatch.setenv(LIVE_OPT_IN_ENV, "1")
    monkeypatch.setenv(LIVE_TIMEOUT_SECONDS_ENV, "1")
    monkeypatch.delenv("HERMES_AUTONOMY_E2E_COST_BUDGET_USD", raising=False)

    with pytest.raises(RuntimeError, match="cost guard"):
        run_live()


@pytest.mark.parametrize("budget", ["nan", "inf", "-inf"])
def test_live_mode_rejects_non_finite_cost_guard(monkeypatch, budget: str):
    monkeypatch.setenv(LIVE_OPT_IN_ENV, "1")
    monkeypatch.setenv(LIVE_TIMEOUT_SECONDS_ENV, "1")
    monkeypatch.setenv(LIVE_COST_BUDGET_ENV, budget)

    with pytest.raises(RuntimeError, match="cost guard"):
        run_live()
