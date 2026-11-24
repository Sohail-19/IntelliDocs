#!/usr/bin/env bash
set -euo pipefail
exec "${SMOKE_PYTHON:-python3}" "$(dirname "$0")/smoke_test.py" "$@"
