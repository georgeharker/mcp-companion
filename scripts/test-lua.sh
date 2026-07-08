#!/usr/bin/env bash
# Run the self-contained Lua tests headlessly.
#
#   scripts/test-lua.sh          # all self-contained tests
#   scripts/test-lua.sh <file>   # a single tests/<file>.lua
#
# Self-contained = no running combiner or external servers required. The
# combiner-dependent Lua tests (test_cc_tools, test_real_servers,
# test_multi_session, test_client) are excluded; run those manually against a
# live combiner.
set -euo pipefail

cd "$(dirname "$0")/.."

SELF_CONTAINED=(
    test_schema
    test_native
    test_project
    test_cc_resolve_session
    test_acp_tools_filter
)

run_one() {
    local name="$1"
    echo "── tests/${name}.lua"
    nvim --headless --noplugin -u NONE \
        -c "set rtp+=$PWD" \
        -c "luafile tests/${name}.lua" \
        -c "qa!" 2>&1
    echo
}

if [[ $# -ge 1 ]]; then
    run_one "${1%.lua}"
    exit 0
fi

fail=0
for name in "${SELF_CONTAINED[@]}"; do
    if ! out="$(run_one "$name")"; then
        fail=1
    fi
    printf '%s\n' "$out"
    if grep -qE '(^|[[:space:]])FAIL:|E5108|[1-9][0-9]* failed' <<<"$out"; then
        fail=1
    fi
done

if [[ $fail -ne 0 ]]; then
    echo "LUA TESTS FAILED" >&2
    exit 1
fi
echo "ALL LUA TESTS PASSED"
