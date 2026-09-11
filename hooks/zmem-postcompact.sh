#!/usr/bin/env bash
# zmem-postcompact.sh — Claude Code PostCompact stash (issue #118, D-2 scope 2).
#
# PostCompact receives the freshly-written compact_summary but has NO
# context-injection channel (decision control only, per the hooks reference;
# verified upstream for Codex too — its PostCompact carries only `trigger`,
# so this hook is registered on Claude only). The summary is the ideal query
# for the post-compaction moment, so this handler stashes it into the
# session's compaction sidecar (ops/<sha256(session_id)[:32]>.compact, the
# same hashed key the PreCompact snapshot writes) for the SessionStart
# (source=compact) branch to consume.
#
# Read-only for the host: always emits "{}" and exits 0. Every failure path
# fail-opens (a missing stash just degrades the post-compact moment to the
# standard recency pull).

set -uo pipefail

# Resolve store.py from the plugin layout (same chain as zmem-precompact.sh:
# ZMEM_ROOT / plugin-root env, falling back to this script's own location —
# the launcher does NOT export a store.py path).
PLUGIN_ROOT="${ZMEM_ROOT:-${ZCODE_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}}"
if [ -z "$PLUGIN_ROOT" ]; then
    PLUGIN_ROOT="$(cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.." && pwd)"
fi
if command -v cygpath >/dev/null 2>&1 && [[ "$(uname -s 2>/dev/null)" == MINGW* || "$(uname -s 2>/dev/null)" == MSYS* || "$(uname -s 2>/dev/null)" == CYGWIN* ]]; then
    STORE_PY="$(cygpath -w "$PLUGIN_ROOT")\\skills\\memory\\scripts\\store.py"
else
    STORE_PY="$PLUGIN_ROOT/skills/memory/scripts/store.py"
fi

if [ -z "$STORE_PY" ] || [ ! -f "$STORE_PY" ]; then
    echo '{}'
    exit 0
fi

# Resolve python like the sibling hooks: python3 first on POSIX, python
# first on Windows (the Store python3 stub is a no-op).
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

# The payload (session_id + compact_summary) arrives on stdin — the launcher
# replays the host's event JSON verbatim. Pure stash, no context output.
"$PYTHON_BIN" -c '
import json, os, sys

store_py = sys.argv[1]
raw = ""
try:
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
except Exception:
    raw = ""
try:
    obj = json.loads(raw) if raw.strip() else {}
except ValueError:
    obj = {}
if not isinstance(obj, dict):
    obj = {}
summary = obj.get("compact_summary", "")
if not isinstance(summary, str):
    summary = ""
sid = obj.get("session_id", "")
if not isinstance(sid, str):
    sid = ""
if not sid:
    sid = (os.environ.get("ZMEM_SESSION", "")
           or os.environ.get("CLAUDE_SESSION_ID", "")
           or os.environ.get("ZCODE_SESSION_ID", ""))
if not (summary.strip() and sid):
    print("{}")
    sys.exit(0)
try:
    sys.path.insert(0, os.path.join(os.path.dirname(store_py), "storelib"))
    import delivery_ledger as dl
    # Same data-dir chain as the ledger readers (_data_dir() in the shared
    # hook body / zmem-session-start.sh): ZMEM_STORE > ZMEM_DATA >
    # CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA > ~/.zmem, expanduser on every
    # branch so a tilde-valued var resolves identically on both sides.
    dd = ""
    store_env = os.environ.get("ZMEM_STORE", "")
    if store_env:
        dd = os.path.expanduser(os.path.dirname(store_env))
    if not dd:
        dd = os.path.expanduser(os.environ.get("ZMEM_DATA", ""))
    if not dd:
        for var in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
            val = os.environ.get(var, "")
            if val:
                dd = os.path.expanduser(val)
                break
    if not dd:
        dd = os.path.join(os.path.expanduser("~"), ".zmem")
    dl.park_compact_summary(dd, sid, summary)
except Exception:
    pass  # fail-open: the post-compact moment degrades to the recency lane
print("{}")
' "$STORE_PY" 2>/dev/null || echo '{}'
exit 0
