from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .logging_config import configure_logging

logger = logging.getLogger(__name__)


class ReconcileRunError(RuntimeError):
    def __init__(self, script_path: str, exit_code: int) -> None:
        super().__init__(f"reconcile script {script_path} exited with code {exit_code}")
        self.exit_code = exit_code


def _write_heartbeat(
    report_dir: str,
    script_path: str,
    status: str,
    exit_code: int | None,
    error: str | None = None,
) -> None:
    reports = Path(report_dir)
    reports.mkdir(parents=True, exist_ok=True)
    heartbeat = {
        "time": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "script": script_path,
        "last_exit_code": exit_code,
    }
    if error:
        heartbeat["error"] = error
    (reports / "reconcile_heartbeat.json").write_text(
        json.dumps(heartbeat, ensure_ascii=False), encoding="utf-8"
    )


def run_reconcile_once(script_path: str, report_dir: str) -> None:
    script = Path(script_path)
    if not script.is_file():
        raise FileNotFoundError(
            f"reconcile script not found at {script}; mount the repository root scripts/reconcile.py there"
        )
    # The root script owns reconciliation and its CLI argument contract. Run its
    # normal Python entrypoint rather than importing and guessing run()/main().
    completed = subprocess.run([sys.executable, str(script)], check=False)
    _write_heartbeat(report_dir, script_path, "ok" if completed.returncode == 0 else "error", completed.returncode)
    if completed.returncode != 0:
        raise ReconcileRunError(script_path, completed.returncode)
    logger.info("root reconciliation completed", extra={"event": "reconcile_completed"})


def main() -> None:
    # This process deliberately does not load payment database or secret settings.
    report_dir = os.getenv("REPORT_DIR", "/tmp/payment-adapter-reports")
    script_path = os.getenv("RECONCILE_SCRIPT", "/app/scripts/reconcile.py")
    interval = int(os.getenv("RECONCILE_INTERVAL_SECONDS", "86400"))
    if interval <= 0:
        raise ValueError("RECONCILE_INTERVAL_SECONDS must be positive")
    configure_logging(os.getenv("LOG_LEVEL", "INFO").upper())
    Path(report_dir).mkdir(parents=True, exist_ok=True)
    while True:
        try:
            run_reconcile_once(script_path, report_dir)
        except Exception as exc:
            _write_heartbeat(
                report_dir,
                script_path,
                "error",
                getattr(exc, "exit_code", None),
                f"{type(exc).__name__}: {exc}",
            )
            logger.error("root reconciliation failed: %s", exc, extra={"event": "reconcile_failed"})
        time.sleep(interval)


if __name__ == "__main__":
    main()
