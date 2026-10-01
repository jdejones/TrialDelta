"""Exercise the Windows launcher shutdown against a disposable fake worker."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher integration")
def test_stop_script_marks_verified_worker_stopped_without_touching_real_installation(tmp_path):
    project = Path(__file__).resolve().parents[1]
    runtime = Path(sys.prefix)
    if not (runtime / "pyvenv.cfg").exists():
        pytest.skip("Requires the project's Windows virtual environment")
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    python = scripts / "python.exe"
    shutil.copy2(sys.executable, python)
    shutil.copy2(runtime / "pyvenv.cfg", tmp_path / ".venv" / "pyvenv.cfg")
    package = tmp_path / "trialdelta"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(project / "trialdelta" / "worker_health.py", package / "worker_health.py")
    shutil.copy2(project / "stop.ps1", tmp_path / "stop.ps1")
    # This substitute cannot import database or collection code: those modules
    # do not exist in the temporary installation.
    (package / "worker.py").write_text(
        "import time\nfrom .worker_health import WorkerHealth\n"
        "with WorkerHealth(heartbeat_seconds=0.05) as health:\n"
        "    health.update('waiting', next_run_at=time.time() + 86400)\n"
        "    time.sleep(120)\n",
        encoding="utf-8",
    )
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    env.pop("PYTHONPATH", None)
    flags = subprocess.CREATE_NO_WINDOW
    with (tmp_path / "child-output.log").open("w", encoding="utf-8") as output:
        child = subprocess.Popen(
            [str(python), "-B", "-m", "trialdelta.worker"], cwd=tmp_path,
            env=env, stdout=output, stderr=output, creationflags=flags,
        )
        worker_pid = None
        try:
            deadline = time.monotonic() + 10
            health_files = []
            while time.monotonic() < deadline:
                health_files = list((tmp_path / "data" / "worker-health").glob("*.json"))
                if health_files:
                    break
                if child.poll() is not None:
                    pytest.fail((tmp_path / "child-output.log").read_text(encoding="utf-8"))
                time.sleep(0.05)
            assert health_files, "Isolated worker did not report health"
            worker_pid = json.loads(health_files[0].read_text(encoding="utf-8"))["pid"]
            (tmp_path / "data" / "processes.json").write_text(
                json.dumps({"worker": child.pid}), encoding="utf-8",
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp_path / "stop.ps1")],
                cwd=tmp_path, env=env, capture_output=True, text=True,
                timeout=30, creationflags=flags,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "Stopped TrialDelta worker." in result.stdout
            child.wait(timeout=5)
            saved = json.loads(health_files[0].read_text(encoding="utf-8"))
            assert saved["state"] == "stopped"
            assert saved["next_run_at"] is None
            assert json.loads((tmp_path / "data" / "processes.json").read_text(encoding="utf-8-sig")) == {}
        finally:
            if child.poll() is None:
                subprocess.run(["taskkill.exe", "/PID", str(child.pid), "/T", "/F"], capture_output=True, creationflags=flags)
                child.wait(timeout=5)
            # The venv parent can exit before its application child on failure.
            if worker_pid is not None and worker_pid != child.pid:
                subprocess.run(["taskkill.exe", "/PID", str(worker_pid), "/F"], capture_output=True, creationflags=flags)
