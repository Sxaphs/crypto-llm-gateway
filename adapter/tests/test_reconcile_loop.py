import json
import sys

import pytest

from app.reconcile_loop import ReconcileRunError, run_reconcile_once


def test_reconcile_loop_executes_root_script_entrypoint_and_writes_heartbeat(tmp_path):
    marker = tmp_path / "called.json"
    script = tmp_path / "reconcile.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(json.dumps({{'executable': sys.executable, 'argv': sys.argv}}))\n"
    )
    reports = tmp_path / "reports"
    run_reconcile_once(str(script), str(reports))
    invocation = json.loads(marker.read_text())
    assert invocation["executable"] == sys.executable
    assert invocation["argv"] == [str(script)]
    heartbeat = json.loads((reports / "reconcile_heartbeat.json").read_text())
    assert heartbeat["status"] == "ok"
    assert heartbeat["last_exit_code"] == 0


def test_reconcile_nonzero_exit_raises_and_records_exit_code(tmp_path):
    script = tmp_path / "reconcile.py"
    script.write_text("import sys\nsys.exit(7)\n")
    reports = tmp_path / "reports"
    with pytest.raises(ReconcileRunError, match="code 7") as caught:
        run_reconcile_once(str(script), str(reports))
    assert caught.value.exit_code == 7
    heartbeat = json.loads((reports / "reconcile_heartbeat.json").read_text())
    assert heartbeat["status"] == "error"
    assert heartbeat["last_exit_code"] == 7


def test_missing_root_script_has_clear_error(tmp_path):
    missing = tmp_path / "scripts" / "reconcile.py"
    with pytest.raises(FileNotFoundError, match="mount the repository root scripts/reconcile.py"):
        run_reconcile_once(str(missing), str(tmp_path / "reports"))
