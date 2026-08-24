#!/usr/bin/env bash
# zmem-precompact.sh — Claude Code PreCompact re-inject (issue #58, 3.9).
#
# Re-issues the same fenced, --no-bump, selective-inject payload as
# zmem-recall.sh on UserPromptSubmit. Read-only: no add/consolidate/queue
# writes; the inner Python body uses --no-bump exclusively. Fail-open:
# every error path exits 0 and emits "{}".
#
# Acceptance criteria (from issue #58 3.9):
#   - hooks.claude.json lists PreCompact.
#   - argv passed to store.py contains --no-bump.
#   - Store mtime / row count unchanged after invocation.
#
# The body lives in hooks/lib/zmem-recall-body.py so this script
# cannot drift from the UserPromptSubmit recall path.

set -euo pipefail

# Resolve store.py from the plugin layout (siblings resolve the same
# way from ZMEM_ROOT / plugin-root env, falling back to this script's
# own location — the launcher does NOT export a store.py path).
PLUGIN_ROOT="${ZMEM_ROOT:-${ZCODE_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}}"
if [ -z "$PLUGIN_ROOT" ]; then
    PLUGIN_ROOT="$(cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.." && pwd)"
fi
if command -v cygpath >/dev/null 2>&1 && [[ "$(uname -s 2>/dev/null)" == MINGW* || "$(uname -s 2>/dev/null)" == MSYS* || "$(uname -s 2>/dev/null)" == CYGWIN* ]]; then
    STORE_PY="$(cygpath -w "$PLUGIN_ROOT")\\skills\\memory\\scripts\\store.py"
else
    STORE_PY="$PLUGIN_ROOT/skills/memory/scripts/store.py"
fi
NS="${ZMEM_NAMESPACE:-user:global}"
BUDGET="${ZMEM_CTX_BUDGET:-25000}"

if [ -z "$STORE_PY" ] || [ ! -f "$STORE_PY" ]; then
    echo '{}'
    exit 0
fi

HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BODY="$HOOKS_DIR/lib/zmem-recall-body.py"

if [ ! -f "$BODY" ]; then
    echo '{}'
    exit 0
fi

# Resolve python via direct execution probe (NOT `command -v python
# --version` — non-portable: some shells treat --version as a second
# command name for -v lookup). Tries `python` first; the fail-open
# `|| echo '{}'` below bounds a wrong-interpreter pick to a silent
# no-op. (Siblings add an IS_WINDOWS branch for python3 ordering; this
# hook's probe stays flat — both interpreters run the same body.)
PYTHON_BIN=""
if python --version >/dev/null 2>&1; then
    PYTHON_BIN="python"
elif python3 --version >/dev/null 2>&1; then
    PYTHON_BIN="python3"
fi
if [ -z "$PYTHON_BIN" ]; then
    echo '{}'
    exit 0
fi

OUT="$($PYTHON_BIN "$BODY" "$STORE_PY" "$NS" "$BUDGET" "precompact" 2>/dev/null || echo '{}')"

# Neutralize any sentinel-token that a MEMORY'S OWN CONTENT happens to
# contain so the host adapter's `<<<END>>>` extraction is never broken
# by stored data. I7 critic-fix: also neutralize the new fence markers.
OUT="${OUT//<<<ZMEM_JSON>>>/<<<ZMEM_JSON_NEUTRALIZED>>>}"
OUT="${OUT//<<<END>>>/<<<END_NEUTRALIZED>>>}"
OUT="${OUT//<<<ZMEM_UNTRUSTED_FENCE>>>/<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"
OUT="${OUT//<<<END_ZMEM_UNTRUSTED_FENCE>>>/<<<END_ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"

printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$OUT"
exit 0