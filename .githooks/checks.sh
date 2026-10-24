#!/usr/bin/env bash
# Local source validation only: never deploy, publish, or start paid builds.
set -euo pipefail
cd "$(dirname "$0")/.."
export CI=true TZ=UTC NEXT_TELEMETRY_DISABLED=1
python3 -m unittest discover -s scripts -p test_pre_push.py -v
unset BASE_URL E2E_BASE_URL PWN_E2E_SHARD
export AWS_EC2_METADATA_DISABLED=true
export UV_PYTHON_INSTALL_DIR="${PWN_PREPUSH_SCRATCH_ROOT:-$PWD/temp/prepush}/python"
if [[ -d .github/workflows ]]; then
  command -v actionlint >/dev/null || { echo 'Install actionlint before pushing.' >&2; exit 1; }
  actionlint -shellcheck=
fi
python3 -m unittest discover -s automation -p 'test_*.py' -v
