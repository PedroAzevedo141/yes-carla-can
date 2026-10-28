#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$task_root/scripts/run_perception.sh" live --source carla --start-server --spawn-demo --snapshots "$@"
