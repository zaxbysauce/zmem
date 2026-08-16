#!/usr/bin/env bash
# zmem-capture-correction.sh — UserPromptSubmit hook for ZMem live correction capture.
#
# ZMem's design is that hooks never write to the store (hooks nudge; the agent
# writes via the closeout bar). So this hook captures user corrections
# ("no, use X", "don't refactor unrelated code", "remember: ...") into a
# NAMESPACE-SCOPED SIDECAR QUEUE (skills/memory/scripts/correction_queue.py)
# that the `/closeout` skill reviews — writing only what clears the bar as an
# `add --signal user` row. Ported from claude-reflect (issue #47, PR 2/4).
#
# It is registered as a SECOND entry under UserPromptSubmit alongside `recall`
# (Claude Code, ZCode, and Codex), kept separate for latency + failure
# isolation: a capture failure NEVER affects recall, and vice versa.
#
# BEHAVIOR:
#   - Reads JSON from stdin: {"prompt": "...", "session_id": "...", "cwd": "...", ...}
#   - Trivial/empty prompts, non-corrections, and zmem's own injected context
#     are all silent no-ops (empty {} envelope, exit 0).
#   - On a detected correction: appends one queue item.
#   - OUTPUT: silent (empty {}) by default to preserve the context budget; when
#     ZMEM_CAPTURE_FEEDBACK=1 emit a one-line additionalContext acknowledgment.
#   - Emits the <<<ZMEM_JSON>>>…<<<END>>> sentinel; the launcher (zmem-launch.js)
#     translates it into the host envelope (this hook is in TRANSLATED_HOOKS).
#   - Non-blocking: always exits 0. Fail-open on any error.
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
if [ -z "$PYTHON_BIN" ]; then
  printf '%s\n' '<<<ZMEM_JSON>>>{}<<<END>>>'
  exit 0
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
PROJECT="${ZMEM_PROJECT:-${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}}"

# Resolve plugin root for the scripts dir (host adapter exports ZMEM_ROOT).
if [ -z "$PLUGIN_ROOT" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
SCRIPTS_DIR_PY="$(join_path "$(to_py_path "$PLUGIN_ROOT")" skills memory scripts)"

# --- Determine namespace + session + host ------------------------------
# Canonical key from the host adapter (closes the basename/remote split). The
# launcher sets ZMEM_NAMESPACE for this hook (it is a NEEDS_NAMESPACE consumer).
# Legacy fallback matches the other hooks for manual invocation.
NS="${ZMEM_NAMESPACE:-}"
if [ -z "$NS" ]; then
  if [ -n "$PROJECT" ]; then
    NS="project:$(basename "$PROJECT")"
  else
    NS="user:global"
  fi
fi
SESSION="${ZMEM_SESSION:-}"
HOST="${ZMEM_HOST:-}"

# Export the store location so correction_queue's queue dir follows the same
# chain as the store (ZMEM_STORE > ZMEM_DATA > ... > ~/.zmem).
export ZMEM_DATA="${ZMEM_DATA:-}"
export ZMEM_STORE="${ZMEM_STORE:-}"

# --- Run the capture logic via python (guaranteed-valid JSON envelope) ---
# Pipe $INPUT (already read above with `cat`) into python: the python block reads
# its stdin, mirroring the recall hook so the payload is never double-consumed.
OUT="$(printf '%s' "$INPUT" | "$PYTHON_BIN" -c '
import json, os, sys

raw_stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
prompt = ""
try:
    obj = json.loads(raw_stdin)
    prompt = obj.get("prompt", "")
except Exception:
    prompt = ""

# Bail on empty/trivial prompts — same guard as the recall hook (no value on
# one-word prompts, and nothing actionable to capture).
if not prompt or not prompt.strip() or len(prompt.strip()) < 5:
    print("{}")
    sys.exit(0)

scripts_dir = sys.argv[1]
sys.path.insert(0, scripts_dir)

try:
    import corrections
    import correction_queue as cq
except Exception:
    print("{}")
    sys.exit(0)

# zmem never captures its own injected context as a user correction.
if not corrections.should_include_message(prompt):
    print("{}")
    sys.exit(0)

item_type, patterns, confidence, sentiment, decay_days = corrections.detect_patterns(prompt)
if not item_type:
    print("{}")
    sys.exit(0)

item = cq.make_item(
    message=prompt,
    type_=item_type,
    patterns=patterns,
    confidence=confidence,
    sentiment=sentiment,
    decay_days=decay_days,
    session=sys.argv[2],
    namespace=sys.argv[3],
    host=sys.argv[4],
)
cq.append_queue(sys.argv[3], item)

# Silent by default (empty {} — preserve the context budget). ZMEM_CAPTURE_FEEDBACK=1
# emits a one-line acknowledgment (the claude-reflect "Learning captured" behavior);
# the launcher rewraps additionalContext per host.
if os.environ.get("ZMEM_CAPTURE_FEEDBACK", "") == "1":
    print(json.dumps({"additionalContext": "zmem: correction candidate captured (will be reviewed at closeout)."}))
else:
    print("{}")
' "$SCRIPTS_DIR_PY" "$SESSION" "$NS" "$HOST" 2>/dev/null || echo '{}')"

# Neutralize any sentinel tokens that could appear in captured content (the
# launcher locates the payload by scanning for the literal markers).
OUT="${OUT//<<<ZMEM_JSON>>>/<<<ZMEM_JSON_NEUTRALIZED>>>}"
OUT="${OUT//<<<END>>>/<<<END_NEUTRALIZED>>>}"

# Wrap the payload in the sentinel so the host adapter can extract it robustly.
printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$OUT"
exit 0
