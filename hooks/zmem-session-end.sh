#!/usr/bin/env bash
# zmem-session-end.sh — Claude Code SessionEnd delivery-state cleanup
# (issue #117 scope 5).
#
# Clears the session delivery ledger (and any fallback pending sidecar)
# for the ending session: "already delivered" is false once the session
# is gone, so nothing survives past session end except the backup
# sweep orphan reaper. Never recalls; runs BEFORE the ZMEM_INJECT=0
# kill switch inside the body (cleanup is not an injection).
# Fail-open: every error path exits 0 and emits "{}".
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

# Resolve python like the sibling hooks (PRR-021 fix): python3 first on
# POSIX, python first on Windows (the Store python3 stub is a no-op).
# Direct execution probe — NOT `command -v python --version` (non-portable).
IS_WINDOWS=0
if [[ "$(uname -s 2>/dev/null)" == MINGW* ]] || [[ "$(uname -s 2>/dev/null)" == CYGWIN* ]] || [[ "$(uname -s 2>/dev/null)" == MSYS* ]]; then
  IS_WINDOWS=1
fi
PYTHON_BIN=""
if [ "$IS_WINDOWS" -eq 1 ]; then
  if python --version >/dev/null 2>&1; then
    PYTHON_BIN="python"
  elif python3 --version >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  fi
else
  if python3 --version >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif python --version >/dev/null 2>&1; then
    PYTHON_BIN="python"
  fi
fi
if [ -z "$PYTHON_BIN" ]; then
    echo '{}'
    exit 0
fi

OUT="$($PYTHON_BIN "$BODY" "$STORE_PY" "$NS" "$BUDGET" "session_end" 2>/dev/null || echo '{}')"

printf '%s\n' "$OUT"
exit 0
