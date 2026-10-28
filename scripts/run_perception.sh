#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
task_python="${PERCEPTION_PYTHON:-python3}"
if [[ -z "${PERCEPTION_PYTHON:-}" ]]; then
    # An unrelated active environment (including Conda base) must not hide the
    # project's installed runtime. PERCEPTION_PYTHON remains the explicit override.
    if [[ "${CONDA_DEFAULT_ENV:-}" == n4s_env && -x "${CONDA_PREFIX:-}/bin/python" ]]; then
        task_python="$CONDA_PREFIX/bin/python"
    elif [[ -x "$HOME/miniconda3/envs/n4s_env/bin/python" ]]; then
        task_python="$HOME/miniconda3/envs/n4s_env/bin/python"
    fi
fi
export PYTHONPATH="$task_root/.perception/python:$task_root${PYTHONPATH:+:$PYTHONPATH}"
export MPLCONFIGDIR="$task_root/.perception/cache/matplotlib"
export YOLOV5_CONFIG_DIR="$task_root/.perception/cache/yolov5"
export YOLOv5_AUTOINSTALL=false
export MPLBACKEND="${MPLBACKEND:-Agg}"
exec "$task_python" -m perception_latency "$@"
