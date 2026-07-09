#!/usr/bin/env bash
# Run the Python test suite.
#
#   scripts/test.sh            # everything (unit + e2e)
#   scripts/test.sh fast       # unit tier only (no subprocess spawning)
#   scripts/test.sh e2e        # e2e tier only (combiner + mock subprocesses)
#
# e2e notes: tests spawn real combiner/mock processes on localhost ports.
# sharedserver-backed tests skip automatically when the `sharedserver` binary
# is not on PATH; nvim-backed tests skip when `nvim` is missing.
set -euo pipefail

cd "$(dirname "$0")/../combiner"

tier="${1:-all}"
case "$tier" in
fast) exec uv run pytest -q -m "not e2e" "${@:2}" ;;
e2e) exec uv run pytest -q -m "e2e" "${@:2}" ;;
all) exec uv run pytest -q "${@:2}" ;;
*)
    echo "usage: scripts/test.sh [fast|e2e|all] [pytest args...]" >&2
    exit 2
    ;;
esac
