#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
task_python="${PERCEPTION_PYTHON:-python3}"
if [[ -z "${PERCEPTION_PYTHON:-}" && -z "${CONDA_PREFIX:-}" && -x "$HOME/miniconda3/envs/n4s_env/bin/python" ]]; then
    task_python="$HOME/miniconda3/envs/n4s_env/bin/python"
fi
export PYTHONPATH="$task_root/.perception/python:$task_root${PYTHONPATH:+:$PYTHONPATH}"
export MPLCONFIGDIR="$task_root/.perception/cache/matplotlib"
export YOLOV5_CONFIG_DIR="$task_root/.perception/cache/yolov5"
export YOLOv5_AUTOINSTALL=false
export MPLBACKEND="${MPLBACKEND:-Agg}"
exec "$task_python" -m perception_latency "$@"
