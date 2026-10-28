"""Run the real shell launcher against isolated fake Conda installations."""
import os
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts/run_perception.sh"


def interpreter(path, label):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{label}' \"$@\"\n")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("active", ["base", "unrelated", "none"])
def test_installed_project_runtime_is_used_despite_unrelated_conda_environment(tmp_path, active):
    interpreter(tmp_path / "miniconda3/envs/n4s_env/bin/python", "project-runtime")
    wrong = interpreter(tmp_path / "wrong/bin/python3", "wrong-runtime")
    env = {**os.environ, "HOME": str(tmp_path), "PATH": str(wrong.parent) + ":" + os.environ["PATH"]}
    env.pop("PERCEPTION_PYTHON", None)
    env["CONDA_DEFAULT_ENV"] = active if active != "none" else ""
    env["CONDA_PREFIX"] = str(tmp_path / "wrong") if active != "none" else ""
    result = subprocess.run(["bash", str(LAUNCHER), "ycc-avtp", "--check"], env=env,
                            capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["project-runtime", "-m", "perception_latency", "ycc-avtp", "--check"]


def test_explicit_python_selection_is_preserved(tmp_path):
    interpreter(tmp_path / "miniconda3/envs/n4s_env/bin/python", "project-runtime")
    selected = interpreter(tmp_path / "custom env/bin/python", "explicit-runtime")
    env = {**os.environ, "HOME": str(tmp_path), "CONDA_DEFAULT_ENV": "base", "PERCEPTION_PYTHON": str(selected)}
    result = subprocess.run(["bash", str(LAUNCHER), "doctor"], env=env, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["explicit-runtime", "-m", "perception_latency", "doctor"]


def test_active_named_project_environment_can_live_elsewhere(tmp_path):
    interpreter(tmp_path / "miniconda3/envs/n4s_env/bin/python", "default-runtime")
    selected = interpreter(tmp_path / "other-install/envs/n4s_env/bin/python", "active-project-runtime")
    env = {**os.environ, "HOME": str(tmp_path), "CONDA_DEFAULT_ENV": "n4s_env", "CONDA_PREFIX": str(selected.parent.parent)}
    env.pop("PERCEPTION_PYTHON", None)
    result = subprocess.run(["bash", str(LAUNCHER), "doctor"], env=env, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines()[0] == "active-project-runtime"
