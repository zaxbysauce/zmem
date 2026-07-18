#!/usr/bin/env bash
# zmem-convention-capture.sh — ZCode PostToolUse hook for continuous reflection.
#
# Fires on successful tool calls (PostToolUse event). Uses a turn counter
# stored in the meta table to fire every N successful Edit/Write/Bash calls
# (default N=10, matching Hermes background_review cadence). Only fires once
# per session (cooldown via marker file, same pattern as capture-failure).
#
# Reads JSON from stdin: {"tool_name":"...", "tool_input":{...}, ...}
# Emits JSON to stdout: {"additionalContext": "<capture prompt or empty>"}
# Non-blocking: always exits 0. Fail-open on any error.

set -u

INPUT="$(cat)"

# Only fire for convention-revealing tools (Edit/Write/Bash), not Read/Glob/Grep.
# PostToolUse provides tool_name on stdin.
TOOL_NAME=$(printf '%s' "$INPUT" | python -c "
import json, sys
try:
    obj = json.load(sys.stdin)
    print(obj.get('tool_name', ''))
except Exception:
    print('')
" 2>/dev/null)

case "$TOOL_NAME" in
  Edit|Write|MultiEdit|NotebookEdit|Bash) ;;
  *) exit 0 ;;  # Skip non-convention-revealing tools
esac

# --- Cross-platform setup (same pattern as other hooks) ---
IS_WINDOWS=0
if [[ "$(uname -s 2>/dev/null)" == MINGW* ]] || [[ "$(uname -s 2>/dev/null)" == CYGWIN* ]] || [[ "$(uname -s 2>/dev/null)" == MSYS* ]]; then
  IS_WINDOWS=1
fi

PYTHON_BIN=""
if [ "$IS_WINDOWS" -eq 1 ]; then
  if python --version >/dev/null 2>&1; then PYTHON_BIN="python"
  elif python3 --version >/dev/null 2>&1; then PYTHON_BIN="python3"; fi
else
  if python3 --version >/dev/null 2>&1; then PYTHON_BIN="python3"
  elif python --version >/dev/null 2>&1; then PYTHON_BIN="python"; fi
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
  local sep; if [ "$IS_WINDOWS" -eq 1 ]; then sep='\\'; else sep='/'; fi
  printf '%s' "$base"
  for part in "$@"; do printf '%s%s' "$sep" "$part"; done
}

SESSION_ID="${CLAUDE_SESSION_ID:-${ZCODE_SESSION_ID:-}}"
DATA_DIR="${ZCODE_PLUGIN_DATA:-}"

if [ -z "$SESSION_ID" ]; then exit 0; fi

if [ -n "$DATA_DIR" ]; then
  DATA_DIR_PY="$(to_py_path "$DATA_DIR")"
else
  DATA_DIR_PY="$(join_path "$(to_py_path "$HOME")" .zcode memory)"
fi

export ZCODE_PLUGIN_DATA="${ZCODE_PLUGIN_DATA:-$DATA_DIR}"

# --- Resolve store.py path and namespace ---
PLUGIN_ROOT="${ZCODE_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
if [ -z "$PLUGIN_ROOT" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
STORE_PY_PY="$(join_path "$(to_py_path "$PLUGIN_ROOT")" skills memory scripts store.py)"

PROJECT="${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}"
NS_HINT="user:global"
if [ -n "$PROJECT" ]; then
  NS_HINT="project:$(basename "$PROJECT")"
fi

# --- Per-session cooldown marker (same pattern as capture-failure) ---
MARKER="$(join_path "$DATA_DIR_PY" ".convention-prompted-${SESSION_ID}")"

# --- Turn counter + cooldown check via Python (atomic meta table update) ---
"$PYTHON_BIN" -c '
import json, os, sys, sqlite3

session_id = sys.argv[1]
data_dir = sys.argv[2]
marker = sys.argv[3]
store_py_hint = sys.argv[4]
ns_hint = sys.argv[5]

# Cooldown: one convention prompt per session.
if os.path.isfile(marker):
    print("{}")
    sys.exit(0)

# Turn counter in the meta table — atomic increment via UPDATE.
store_db = os.path.join(data_dir, "store.sqlite")
try:
    conn = sqlite3.connect(store_db, timeout=3)
    # Atomic increment: INSERT OR IGNORE seeds the row, UPDATE increments it.
    key = "convention_count_" + session_id
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, '"'"'0'"'"')", (key,))
    conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = ?", (key,))
    conn.commit()
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    count = int(row[0]) if row else 0
except Exception:
    print("{}")
    sys.exit(0)

# Fire every N=10 successful Edit/Write/Bash calls.
INTERVAL = int(os.environ.get("ZMEM_CONVENTION_INTERVAL", "10"))
if count < INTERVAL:
    print("{}")
    sys.exit(0)

# Write the marker so subsequent calls in this session do not re-prompt.
try:
    with open(marker, "w") as f:
        f.write("1")
except OSError:
    pass

msg = (
    "ZMem convention capture: you just completed several successful code edits. "
    "If you discovered a reusable convention, pattern, or workaround during this "
    "session — something that would help a future session facing a similar task — "
    "capture it now: `%s add --namespace \"%s\" --type convention --content \"...\" "
    "--signal <test|compile|lint|reviewer|user|none> --source-ref \"session:%s\"`. "
    "If nothing generalizable applies, do nothing. "
    "(This prompt fires at most once per session.)"
) % (store_py_hint, ns_hint, session_id)
print(json.dumps({"additionalContext": msg}))
' "$SESSION_ID" "$DATA_DIR_PY" "$MARKER" "$STORE_PY_PY" "$NS_HINT" 2>/dev/null || echo '{}'

exit 0
