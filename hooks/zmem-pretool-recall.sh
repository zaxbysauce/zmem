#!/usr/bin/env bash
# zmem-pretool-recall.sh — ZCode/Claude PreToolUse hook for operation recall.
#
# Issue #90 / #85 direction C: the five #85 misses were operation-adjacent,
# and the only event that sees `git stash pop` BEFORE it runs is a pre-tool
# hook. This hook derives the recall query from the TOOL INPUT ITSELF (the
# command or file path about to run) and injects matching hazard lessons as
# additionalContext before the tool executes.
#
# Reads JSON from stdin: {"tool_name": "...", "tool_input": {...},
# "session_id": "...", ...}. Registered with matcher
# Edit|Write|MultiEdit|NotebookEdit|Bash — the same tool set whose PostToolUse
# events feed the query-context ring (#88).
#
# Contract (per the #85 C spec, host-probed):
#   - ZCode: additionalContext is documented honored pre-tool → direct emit.
#   - Claude: additionalContext pre-tool is documented (CC >= 2.1.9 injects
#     it alongside the tool result; pausing is driven by the permission
#     decision field only) → direct emit, PLUS a pending sidecar the next UserPromptSubmit
#     run must deliver — the sidecar covers older hosts that ignore the
#     field (worst case one duplicate, never lost).
#   - Codex: NOT registered — the host rejects hookSpecificOutput.additionalContext
#     (openai/codex#19385); documented gap in issue #90's matrix.
#   - NEVER denies: surfacing a hazard is information for the model, not
#     grounds to block a legitimate command. No permission decision is emitted.
#
# Non-blocking: always exits 0. Fail-open on any error. Silent when nothing
# qualified (a per-tool-call one-liner would be noise — see the body).

set -u

# --- Read stdin (one JSON line) ---------------------------------------------
INPUT="$(cat)"

# --- Cross-platform setup ---
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

to_py_path() {
  if [ "$IS_WINDOWS" -eq 0 ]; then printf '%s' "$1"; return; fi
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"
  else
    local p="$1"
    if [[ "$p" =~ ^/([a-zA-Z])/(.*)$ ]]; then
      printf '%s:\\%s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]//\//\\}"
    else printf '%s' "$p"; fi
  fi
}

join_path() {
  local base="$1"; shift
  local sep; if [ "$IS_WINDOWS" -eq 1 ]; then sep='\'; else sep='/'; fi
  printf '%s' "$base"
  for part in "$@"; do printf '%s%s' "$sep" "$part"; done
}

# Canonical env from the host adapter (zmem-launch.js); legacy vars as fallback.
PLUGIN_ROOT="${ZMEM_ROOT:-${ZCODE_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}}"
DATA_DIR="${ZMEM_DATA:-${ZCODE_PLUGIN_DATA:-}}"
PROJECT="${ZMEM_PROJECT:-${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}}"

if [ -n "$DATA_DIR" ]; then
  DATA_DIR_PY="$(to_py_path "$DATA_DIR")"
else
  DATA_DIR_PY="$(join_path "$(to_py_path "$HOME")" .zmem)"
fi

if [ -z "$PLUGIN_ROOT" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
STORE_PY_PY="$(join_path "$(to_py_path "$PLUGIN_ROOT")" skills memory scripts store.py)"

export ZMEM_DATA="${ZMEM_DATA:-$DATA_DIR}"
export ZCODE_PLUGIN_DATA="${ZCODE_PLUGIN_DATA:-}"

NS="${ZMEM_NAMESPACE:-}"
if [ -z "$NS" ]; then
  if [ -n "$PROJECT" ]; then
    NS="project:$(basename "$PROJECT")"
  else
    NS="user:global"
  fi
fi
BUDGET="${ZMEM_CTX_BUDGET:-25000}"

# --- Build the recall payload via the shared body ---------------------------
# Same single source of truth as recall/precompact/subagent-recall: mode
# "pretool" derives the query from the tool input in the body, applies the
# identical gate/budget/fence/#87 silent-reason contract, and decides there
# whether to park a pending sidecar (older hosts that ignore pre-tool
# additionalContext).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECALL_BODY="$SCRIPT_DIR/lib/zmem-recall-body.py"
if [ ! -f "$RECALL_BODY" ]; then
    echo '{}'
    exit 0
fi

OUT="$(printf '%s' "$INPUT" | "$PYTHON_BIN" "$RECALL_BODY" "$STORE_PY_PY" "$NS" "$BUDGET" "pretool" 2>/dev/null || echo '{}')"

# Neutralize sentinel/fence tokens a memory's own content might contain
# (same defense as zmem-recall.sh — the launcher locates the payload by
# scanning for the literal markers).
OUT="${OUT//<<<ZMEM_JSON>>>/<<<ZMEM_JSON_NEUTRALIZED>>>}"
OUT="${OUT//<<<END>>>/<<<END_NEUTRALIZED>>>}"
OUT="${OUT//<<<ZMEM_UNTRUSTED_FENCE>>>/<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"
OUT="${OUT//<<<END_ZMEM_UNTRUSTED_FENCE>>>/<<<END_ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"

printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$OUT"
exit 0
