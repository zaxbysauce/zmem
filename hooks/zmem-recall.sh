#!/usr/bin/env bash
# zmem-recall.sh — ZCode UserPromptSubmit hook for ZMem relevance-based recall.
#
# When the user submits a prompt, runs store.py recall against the prompt text
# and injects matching memories as additionalContext BEFORE the agent starts
# working. This converts zmem from time-based (3 recent at session start) to
# relevance-based (memories that match THIS task) injection.
#
# Reads JSON from stdin: {"prompt": "...", "session_id": "...", "cwd": "...", ...}
# Emits JSON to stdout: {"additionalContext": "<recalled memories or empty>"}
# Non-blocking: always exits 0. Fail-open on any error.
#
# Cross-platform: uses Git Bash (invoked via full path in hooks.json). Windows
# Python cannot resolve Cygwin paths (/c/...) so we convert with cygpath.

set -u

# --- Read stdin (one JSON line) ---------------------------------------------
INPUT="$(cat)"

# --- Cross-platform setup ---
IS_WINDOWS=0
if [[ "$(uname -s 2>/dev/null)" == MINGW* ]] || [[ "$(uname -s 2>/dev/null)" == CYGWIN* ]] || [[ "$(uname -s 2>/dev/null)" == MSYS* ]]; then
  IS_WINDOWS=1
fi

# Resolve python binary. On Windows, python3 is often a Microsoft Store stub;
# prefer python. On POSIX, prefer python3. Verify it actually runs.
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

# Convert a path for the local python (Windows needs backslash, POSIX passes through).
to_py_path() {
  if [ "$IS_WINDOWS" -eq 0 ]; then
    printf '%s' "$1"
    return
  fi
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  else
    local p="$1"
    if [[ "$p" =~ ^/([a-zA-Z])/(.*)$ ]]; then
      local drive="${BASH_REMATCH[1]}"
      local rest="${BASH_REMATCH[2]}"
      printf '%s:\\%s' "$drive" "${rest//\//\\}"
    else
      printf '%s' "$p"
    fi
  fi
}

# Build a sub-path with the correct separator for the platform.
join_path() {
  local base="$1"; shift
  local sep
  if [ "$IS_WINDOWS" -eq 1 ]; then
    sep='\'
  else
    sep='/'
  fi
  printf '%s' "$base"
  for part in "$@"; do
    printf '%s%s' "$sep" "$part"
  done
}

# Canonical env from the host adapter (zmem-launch.js); legacy vars as fallback.
PLUGIN_ROOT="${ZMEM_ROOT:-${ZCODE_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}}"
DATA_DIR="${ZMEM_DATA:-${ZCODE_PLUGIN_DATA:-}}"
PROJECT="${ZMEM_PROJECT:-${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}}"

# --- Resolve data dir --------------------------------------------------------
if [ -n "$DATA_DIR" ]; then
  DATA_DIR_PY="$(to_py_path "$DATA_DIR")"
else
  DATA_DIR_PY="$(join_path "$(to_py_path "$HOME")" .zmem)"
fi

# --- Resolve store.py path --------------------------------------------------
if [ -z "$PLUGIN_ROOT" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
STORE_PY_PY="$(join_path "$(to_py_path "$PLUGIN_ROOT")" skills memory scripts store.py)"

# Export the store location so store.py resolves it (chain:
# ZMEM_STORE > ZMEM_DATA > CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA > ~/.zmem > ~/.zcode).
export ZMEM_DATA="${ZMEM_DATA:-$DATA_DIR}"
export ZCODE_PLUGIN_DATA="${ZCODE_PLUGIN_DATA:-}"

# --- Determine namespace ----------------------------------------------------
# Canonical key from the host adapter (closes the basename/remote split so
# migrated projects recall their rows). Fall back to the legacy basename key
# when the adapter did not run.
NS="${ZMEM_NAMESPACE:-}"
if [ -z "$NS" ]; then
  if [ -n "$PROJECT" ]; then
    NS="project:$(basename "$PROJECT")"
  else
    NS="user:global"
  fi
fi
BUDGET="${ZMEM_CTX_BUDGET:-25000}"

# --- Build the recall payload via the shared body (issue #58, 3.5/3.8) -----
# The Python body lives in hooks/lib/zmem-recall-body.py and is also
# invoked by zmem-precompact.sh (3.9). Single source of truth for the
# fence render, selective-inject gate, and JSON envelope — drift
# between recall and precompact is structurally impossible.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECALL_BODY="$SCRIPT_DIR/lib/zmem-recall-body.py"
if [ ! -f "$RECALL_BODY" ]; then
    echo '{}'
    exit 0
fi

OUT="$(printf '%s' "$INPUT" | "$PYTHON_BIN" "$RECALL_BODY" "$STORE_PY_PY" "$NS" "$BUDGET" "user_prompt" 2>/dev/null || echo '{}')"

# Neutralize any sentinel token that a MEMORY'S OWN CONTENT happens to contain
# before wrapping. The launcher locates the payload by scanning stdout for the
# literal markers, so a stored memory containing "<<<ZMEM_JSON>>>" would move
# the extraction boundary into the middle of the JSON, the parse would fail, and
# the whole recall would silently degrade to {} (a self-DoS of this turn's
# recall — fail-open, not an injection vector). Both replacements are safe
# inside the serialized JSON string: neither introduces a quote or a backslash.
# I7 critic-fix: also neutralize the new fence markers (issue #58, 3.5).
OUT="${OUT//<<<ZMEM_JSON>>>/<<<ZMEM_JSON_NEUTRALIZED>>>}"
OUT="${OUT//<<<END>>>/<<<END_NEUTRALIZED>>>}"
OUT="${OUT//<<<ZMEM_UNTRUSTED_FENCE>>>/<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"
OUT="${OUT//<<<END_ZMEM_UNTRUSTED_FENCE>>>/<<<END_ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"

# Wrap the payload in the sentinel so the host adapter can extract it robustly.
printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$OUT"
exit 0
