#!/usr/bin/env bash
# zmem-session-end.sh — Claude Code SessionEnd delivery-state cleanup
# (issue #117 scope 5).
#
# Clears the session delivery ledger (and any fallback pending sidecar)
# for the ending session: "already delivered" is false once the session
# is gone, so nothing survives past session end except the backup
# sweep orphan reaper. The store-side terminal clear also removes the
# session's .tasktext stash (issue #336) and the closeout nudge consults
# the .wrote/.closeout session markers (issue #398) - see the body. Never recalls; runs BEFORE the ZMEM_INJECT=0
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

# Evidence-gated retention stage (issue #177): default-off; the exact
# ZMEM_CAPTURE=0 kill switch and the exact ZMEM_AUTO_RETAIN=0 override skip
# it entirely. One subprocess driver call (hooks/lib/zmem-retain-process.py)
# runs retain-adapt then retain with the transcript path; this script never
# touches the queue or the store directly. The driver consults the auto_retain
# gate itself and no-ops before reading the transcript.
#
# stdout stays discarded but stderr is forwarded so a failure is diagnosable.
# The stage is bounded by `timeout` because it otherwise had NO bound at all:
# a slow transcript read could block past the 12s launcher watchdog and kill
# the delivery-ledger cleanup below, which is the job of this hook. 3s is the
# remainder of launcher_watchdog_ms after the store recall budget.
RETAIN_STAGE=1
if [ "$(printenv ZMEM_CAPTURE 2>/dev/null)" = "0" ]; then RETAIN_STAGE=0; fi
if [ "${ZMEM_AUTO_RETAIN:-}" = "0" ]; then RETAIN_STAGE=0; fi
if [ "$RETAIN_STAGE" = "1" ] && [ -n "${ZMEM_TRANSCRIPT:-}" ] \
   && [ -n "${ZMEM_SESSION:-}" ] \
   && [ -f "$HOOKS_DIR/lib/zmem-retain-process.py" ]; then
    RHOST="${ZMEM_HOST:-zcode}"
    timeout 3 "$PYTHON_BIN" "$HOOKS_DIR/lib/zmem-retain-process.py" \
        --host "$RHOST" \
        --session "$ZMEM_SESSION" \
        --namespace "$NS" \
        --transcript "$ZMEM_TRANSCRIPT" >/dev/null || true
fi

OUT="$($PYTHON_BIN "$BODY" "$STORE_PY" "$NS" "$BUDGET" "session_end" 2>/dev/null || echo '{}')"

printf '%s\n' "$OUT"
exit 0
