#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Reuse the Python environment and cache configuration of the panel.
exec "$task_root/scripts/run_perception.sh" ycc-avtp "$@"
