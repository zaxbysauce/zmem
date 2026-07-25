#!/usr/bin/env bash
# zmem-convention-capture.sh — ZCode PostToolUse hook for continuous reflection.
#
# Fires on successful tool calls (PostToolUse event). Uses a turn counter
# stored in the meta table to fire every N successful Edit/Write/Bash calls
# (default N=10, matching Hermes background_review cadence). Only fires once
# per session (cooldown via marker file, same pattern as capture-failure).
#
# Reads JSON from stdin: {"tool_name":"...", "tool_input":{...}, ...}
#
# Envelope: emits a bare {"additionalContext": …} wrapped in the
# <<<ZMEM_JSON>>>…<<<END>>> sentinel, exactly like the other injecting hooks.
# The host adapter (zmem-launch.js) extracts it and rewraps per host (Claude
# Code: hookSpecificOutput.additionalContext for PostToolUse — CC only honors
# that shape, so the previous bare passthrough was never injected at all; ZCode:
# bare additionalContext) and enforces the encoded context budget.
#
# Canonical env (from zmem-launch.js): ZMEM_ROOT, ZMEM_DATA, ZMEM_SESSION,
# ZMEM_PROJECT, ZMEM_NAMESPACE. Legacy vars kept as fallbacks for manual /
# pre-adapter runs. The suggested `store.py add --namespace …` command uses the
# canonical git-remote-derived $ZMEM_NAMESPACE — the old basename-derived
# NS_HINT pointed captured conventions at a namespace the unified recall path
# never queries, making them invisible to the shared store.
#
# Non-blocking: always exits 0. Fail-open on any error.

set -u

# Every exit path must emit the sentinel: the launcher now buffers this hook's
# stdout, so a bare `exit 0` yields no payload at all.
emit_empty() {
  printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' '{}'
  exit 0
}

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
  *) emit_empty ;;  # Skip non-convention-revealing tools
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
  local sep; if [ "$IS_WINDOWS" -eq 1 ]; then sep='\'; else sep='/'; fi
  printf '%s' "$base"
  for part in "$@"; do printf '%s%s' "$sep" "$part"; done
}

# Canonical env from the host adapter first; legacy vars as fallback. The
# launcher derives ZMEM_SESSION from the stdin payload's session_id, which is
# the only session value guaranteed to be present under Claude Code —
# CLAUDE_SESSION_ID is not exported to the hook process.
SESSION_ID="${ZMEM_SESSION:-${CLAUDE_SESSION_ID:-${ZCODE_SESSION_ID:-}}}"

if [ -z "$SESSION_ID" ]; then emit_empty; fi

# --- Resolve plugin root (needed below: the DATA_DIR fallback may shell out
# to host.py, which lives under it) ---
PLUGIN_ROOT="${ZMEM_ROOT:-${ZCODE_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}}"
if [ -z "$PLUGIN_ROOT" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
STORE_PY_PY="$(join_path "$(to_py_path "$PLUGIN_ROOT")" skills memory scripts store.py)"

# --- Resolve DATA_DIR ---
# Must match host.py:resolve_store_path()'s precedence chain exactly:
#   ZMEM_STORE > ZMEM_DATA > CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA >
#   ~/.zmem > ~/.zcode/memory > newest ~/.zcode/cli/plugins/data/*zmem*/
# so a manual/pre-adapter invocation of this hook targets the same store
# store.py itself would open. The four explicit-env cases below are cheap
# checks with no subprocess (the common case, since the host adapter always
# exports ZMEM_DATA). Only the filesystem-dependent tail of the chain — which
# of ~/.zmem / ~/.zcode/memory / the legacy per-plugin scan applies — is
# genuinely ambiguous without re-walking the disk, so that (and only that) is
# delegated to host.py itself via a tiny subprocess, rather than reimplemented
# in bash where it would inevitably drift from host.py again.
DATA_DIR=""
DATA_DIR_IS_NATIVE=0
if [ -n "${ZMEM_STORE:-}" ]; then
  DATA_DIR="$(dirname "$ZMEM_STORE")"
elif [ -n "${ZMEM_DATA:-}" ]; then
  DATA_DIR="$ZMEM_DATA"
elif [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
  DATA_DIR="$CLAUDE_PLUGIN_DATA"
elif [ -n "${ZCODE_PLUGIN_DATA:-}" ]; then
  DATA_DIR="$ZCODE_PLUGIN_DATA"
elif [ -n "$PYTHON_BIN" ]; then
  HOST_PY_DIR_PY="$(join_path "$(to_py_path "$PLUGIN_ROOT")" skills memory scripts)"
  RESOLVED="$("$PYTHON_BIN" -c '
import sys
sys.path.insert(0, sys.argv[1])
try:
    import host
    print(host.resolve_store_path().parent)
except Exception:
    pass
' "$HOST_PY_DIR_PY" 2>/dev/null)"
  if [ -n "$RESOLVED" ]; then
    # host.py ran under $PYTHON_BIN and returned an already-native path for
    # this platform (e.g. a Windows path under python.exe) — do not run it
    # back through to_py_path, which would attempt a Cygwin-path conversion
    # on a string that is not one.
    DATA_DIR="$RESOLVED"
    DATA_DIR_IS_NATIVE=1
  fi
fi
if [ -z "$DATA_DIR" ]; then
  # host.py unavailable/failed (e.g. no python) — last-resort default,
  # matching host.py's own ultimate fallback.
  DATA_DIR="$HOME/.zmem"
fi

if [ "$DATA_DIR_IS_NATIVE" -eq 1 ]; then
  DATA_DIR_PY="$DATA_DIR"
else
  DATA_DIR_PY="$(to_py_path "$DATA_DIR")"
fi

# Keep store.py resolving the same store (full chain documented above).
export ZMEM_DATA="${ZMEM_DATA:-$DATA_DIR}"
export ZCODE_PLUGIN_DATA="${ZCODE_PLUGIN_DATA:-}"

PROJECT="${ZMEM_PROJECT:-${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}}"

# Canonical namespace from the host adapter (git-remote-derived, the same key
# every recall path queries). The legacy basename fallback only applies when
# the adapter did not run at all.
NS_HINT="${ZMEM_NAMESPACE:-}"
if [ -z "$NS_HINT" ]; then
  if [ -n "$PROJECT" ]; then
    NS_HINT="project:$(basename "$PROJECT")"
  else
    NS_HINT="user:global"
  fi
fi

# --- Per-session cooldown marker (same pattern as capture-failure) ---
MARKER="$(join_path "$DATA_DIR_PY" ".convention-prompted-${SESSION_ID}")"

# No python → cannot count turns; fail open (no injection).
if [ -z "$PYTHON_BIN" ]; then emit_empty; fi

# --- Turn counter + cooldown check via Python (atomic meta table update) ---
CTX_JSON="$("$PYTHON_BIN" -c '
import json, os, shlex, sys, sqlite3

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

# ns_hint is git-remote-derived (repo-controlled: a hostile origin URL can
# embed quotes / $(...) / backticks) and store_py_hint / session_id are
# interpolated the same way, so shell-quote all three before rendering them
# into the suggested command. shlex.quote wraps in single quotes (and escapes
# any embedded single quote) only when needed, so ordinary values still read
# as a plain, copy-pasteable command while a hostile value cannot break out of
# its argument position.
store_py_arg = shlex.quote(store_py_hint)
namespace_arg = shlex.quote(ns_hint)
source_ref_arg = shlex.quote("session:" + session_id)

msg = (
    "ZMem convention capture: you just completed several successful code edits. "
    "If you discovered a reusable convention, pattern, or workaround during this "
    "session — something that would help a future session facing a similar task — "
    "capture it now: `%s add --namespace %s --type convention --content \"...\" "
    "--signal <test|compile|lint|reviewer|user|none> --source-ref %s`. "
    "If nothing generalizable applies, do nothing. "
    "(This prompt fires at most once per session.)"
) % (store_py_arg, namespace_arg, source_ref_arg)
print(json.dumps({"additionalContext": msg}))
' "$SESSION_ID" "$DATA_DIR_PY" "$MARKER" "$STORE_PY_PY" "$NS_HINT" 2>/dev/null || echo '{}')"

if [ -z "$CTX_JSON" ]; then
  CTX_JSON='{}'
fi

# Neutralize any sentinel token the payload happens to contain before wrapping
# (same defense as zmem-recall.sh): the launcher locates the payload by scanning
# stdout for the literal markers, so an embedded marker would move the
# extraction boundary and silently degrade this hook to {}. Both replacements
# are safe inside the serialized JSON string: neither introduces a quote or a
# backslash.
CTX_JSON="${CTX_JSON//<<<ZMEM_JSON>>>/<<<ZMEM_JSON_NEUTRALIZED>>>}"
CTX_JSON="${CTX_JSON//<<<END>>>/<<<END_NEUTRALIZED>>>}"

# Wrap the payload in the sentinel so the host adapter can extract + rewrap it.
printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$CTX_JSON"
exit 0
