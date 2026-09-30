"""Check scheduler status propagation with a fake interpreter, without QFC."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("status", [0, 3])
def test_run_wrapper_logs_and_propagates_status(tmp_path, status):
    root = Path(__file__).resolve().parents[1]
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(root / "scripts/run.sh", scripts / "run.sh")
    venv = tmp_path / ".venv/bin"
    venv.mkdir(parents=True)
    # No real interpreter, clipper entry point, profile, or config is touched.
    (venv / "activate").write_text(f'python() {{ return {status}; }}\n')
    result = subprocess.run(["bash", str(scripts / "run.sh"), "--no-wait-login"],
                            capture_output=True, text=True)
    assert result.returncode == status
    assert f"finished qfc_clipper (exit {status})" in (tmp_path / "logs/qfc_clipper.log").read_text()
