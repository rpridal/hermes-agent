"""Deterministic protocol fixture worker — never a live autonomy worker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def kanban(*args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "--board", os.environ["HERMES_KANBAN_BOARD"], *args],
        check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=os.environ.copy(), timeout=10,
    )


def marker(name: str, **payload: object) -> None:
    workspace = Path(os.environ["HERMES_KANBAN_WORKSPACE"])
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / name
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def main() -> None:
    task_id = os.environ["HERMES_KANBAN_TASK"]
    action = os.environ["HERMES_AUTONOMY_PROTOCOL_ACTION"]
    if os.environ.get("HERMES_AUTONOMY_PROTOCOL_FORCE_HANG") == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen(
            [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        marker("hung-worker.json", worker_pid=os.getpid(), child_pid=child.pid)
        while True:
            time.sleep(1)
    if os.environ.get("HERMES_AUTONOMY_PROTOCOL_FORCE_FAILURE") == "1":
        raise RuntimeError("forced protocol worker failure")
    kanban("heartbeat", task_id, "--note", f"protocol {action} started")
    if action == "developer-v1":
        marker("acceptance-marker.json", revision=1, task_id=task_id)
        kanban("request-review", task_id, "--reviewer", "reviewer", "--summary", "protocol developer v1 ready for review")
    elif action == "reviewer-change":
        kanban("request-changes", task_id, "Acceptance marker must contain revision 2")
    elif action == "developer-remediation":
        marker("acceptance-marker.json", revision=2, task_id=task_id)
        kanban("request-review", task_id, "--reviewer", "reviewer", "--summary", "protocol remediation ready")
    elif action == "remediation-review":
        marker("review-evidence.json", approved=True, task_id=task_id)
        kanban("complete", task_id, "--summary", "protocol remediation approved")
    elif action == "reviewer-complete":
        marker("final-review.json", approved=True, task_id=task_id)
        kanban("complete", task_id, "--summary", "protocol reviewer complete")
    else:
        raise RuntimeError(f"unknown protocol action: {action}")


if __name__ == "__main__":
    main()
