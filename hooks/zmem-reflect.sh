#!/usr/bin/env bash
# zmem-reflect.sh — ZCode Stop hook for ZMem reflection-on-failure (plugin version).
#
# When the agent stops, checks for failure signals in the active session episodic
# memory (db.sqlite tool_usage with status=error or exit_code!=0 on non-read-only
# tools) and, if found AND no lesson was captured for this session, emits an
# additionalContext prompt to capture a grounded lesson. NON-BLOCKING (exit 0).
#
# Plugin paths: ZCODE_PLUGIN_DATA (store.sqlite) injected by the runner.
# Falls back to ~/.zcode/memory/store.sqlite for manual installs.
#
# Cross-platform: Windows Python cannot resolve Cygwin paths (/c/...). Convert.

set -u

SESSION_ID="${CLAUDE_SESSION_ID:-}"
PROJECT="${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}"
DATA_DIR="${ZCODE_PLUGIN_DATA:-}"

if [ -z "$SESSION_ID" ]; then
  exit 0
fi

to_win_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  else
    local p="$1"
    if [[ "$p" =~ ^/([a-zA-Z])/(.*)$ ]]; then
      local drive="${BASH_REMATCH[1]}"
      local rest="${BASH_REMATCH[2]}"
      printf '%s:%s' "$drive" "${rest//\//\\}"
    else
      printf '%s' "$p"
    fi
  fi
}

# Resolve data dir + store path (Windows format for python).
if [ -n "$DATA_DIR" ]; then
  DATA_DIR_WIN="$(to_win_path "$DATA_DIR")"
else
  DATA_DIR_WIN="$(to_win_path "$HOME")\\.zcode\\memory"
fi
STORE_DB_WIN="$DATA_DIR_WIN\\store.sqlite"

# The episodic db is always at ~/.zcode/cli/db/db.sqlite (user-level, not plugin).
DB_PATH_WIN="$(to_win_path "$HOME")\\.zcode\\cli\\db\\db.sqlite"

# Resolve the store.py path for the prompt message (plugin root or fallback).
PLUGIN_ROOT="${ZCODE_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
if [ -n "$PLUGIN_ROOT" ]; then
  STORE_PY_WIN="$(to_win_path "$PLUGIN_ROOT")\\skills\\memory\\scripts\\store.py"
else
  STORE_PY_WIN="$(to_win_path "$HOME")\\.zcode\\skills\\memory\\scripts\\store.py"
fi

NS="user:global"
if [ -n "$PROJECT" ]; then
  NS="project:$(basename "$PROJECT")"
fi

# Verify the episodic db and store exist (fail-open otherwise).
python -c "import os,sys; sys.exit(0 if os.path.isfile(sys.argv[1]) else 1)" "$DB_PATH_WIN" 2>/dev/null || exit 0
python -c "import os,sys; sys.exit(0 if os.path.isfile(sys.argv[2]) else 1)" "$DB_PATH_WIN" "$STORE_DB_WIN" 2>/dev/null || exit 0

python -c '
import json, os, sys, sqlite3

db_path = sys.argv[1]
store_py = sys.argv[2]
session_id = sys.argv[3]
ns = sys.argv[4]
data_dir_win = sys.argv[5]

# 1a. Detect failures using ONLY the columns the original count query used
#     (session_id, read_only, status, exit_code). This is the load-bearing query —
#     if it fails, reflection is correctly disabled. It must NOT depend on columns
#     we added for enrichment (error_message, retry_count, etc.) because those are
#     on the ZCode internal schema and could change across versions.
fail_count = 0
conn = None
try:
    conn = sqlite3.connect(db_path)
    row = conn.execute("""
        SELECT count(*) FROM tool_usage
        WHERE session_id = ?
          AND COALESCE(read_only, 0) = 0
          AND (
            status = '"'"'error'"'"'
            OR (exit_code IS NOT NULL AND exit_code != 0)
          )
    """, (session_id,)).fetchone()
    fail_count = row[0] if row else 0
except Exception:
    fail_count = 0

if fail_count == 0:
    if conn:
        conn.close()
    sys.exit(0)

# 1b. Enrichment: fetch per-failure detail in a SEPARATE query with its own
#     try/except. If these columns do not exist (schema varies across ZCode
#     versions), fail_details stays empty — the prompt falls back to the count
#     with no per-error detail. The detection above still fires.
fail_details = []  # list of (tool_name, error_message, error_type, retry_count, destructive)
try:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT tool_name, error_message, error_type, retry_count,
               COALESCE(destructive, 0) AS destructive
        FROM tool_usage
        WHERE session_id = ?
          AND COALESCE(read_only, 0) = 0
          AND (
            status = '"'"'error'"'"'
            OR (exit_code IS NOT NULL AND exit_code != 0)
          )
        ORDER BY completed_at DESC
        LIMIT 10
    """, (session_id,)).fetchall()
    for r in rows:
        fail_details.append((
            r["tool_name"] or "?",
            (r["error_message"] or "")[:200],
            r["error_type"] or "",
            r["retry_count"] or 0,
            r["destructive"],
        ))
except Exception:
    pass  # enrichment is optional; detection already succeeded
finally:
    try:
        conn.close()
    except Exception:
        pass

# 2. Check whether a lesson was already captured for this session (avoid nagging).
# Direct column probe on source_ref (NOT FTS — source_ref is not FTS-indexed).
lesson_exists = False
store_db = os.path.join(data_dir_win, "store.sqlite")
if os.path.isfile(store_db):
    try:
        sconn = sqlite3.connect(store_db)
        row_l = sconn.execute(
            "SELECT 1 FROM memory WHERE source_ref=? AND superseded_at IS NULL LIMIT 1",
            ("session:" + session_id,),
        ).fetchone()
        lesson_exists = row_l is not None
        sconn.close()
    except Exception:
        pass

if lesson_exists:
    sys.exit(0)

# 3. Build enriched failure summary for the prompt.
# Group by tool_name for a compact overview, then list the most recent errors.
from collections import Counter
tool_counts = Counter(d[0] for d in fail_details)
summary_parts = ["%d=%s" % (c, t) for t, c in tool_counts.most_common()]
tool_summary = ", ".join(summary_parts)

# Build per-error detail lines (up to 5, most recent first).
detail_lines = []
for tool_name, err_msg, err_type, retries, destructive in fail_details[:5]:
    parts = [tool_name]
    if err_type:
        parts.append("(%s)" % err_type)
    if retries > 0:
        parts.append("[retried %dx]" % retries)
    if destructive:
        parts.append("[destructive]")
    if err_msg:
        parts.append(": %s" % err_msg)
    detail_lines.append("  - " + " ".join(parts))

detail_block = "\n".join(detail_lines) if detail_lines else ""

# 4. Emit non-blocking reflection prompt. Strict-schema JSON, additionalContext only.
msg = (
    "ZMem reflection prompt: %d failed tool call(s) detected in this session (%s). "
    "Most recent failures:\n%s\n"
    "If a generalizable lesson can be derived from a failure (grounded in a "
    "test/compile/lint/reviewer/user signal — not self-opinion), capture it with "
    "the memory skill: `%s add --namespace \"%s\" --type lesson --content \"...\" "
    "--signal <test|compile|lint|reviewer|user|none> --source-ref \"session:%s\"`. "
    "If no generalizable lesson applies, do nothing. "
    "Only capture lessons that would help a future session facing a similar situation."
) % (fail_count, tool_summary, detail_block, store_py, ns, session_id)

print(json.dumps({"additionalContext": msg}))
' "$DB_PATH_WIN" "$STORE_PY_WIN" "$SESSION_ID" "$NS" "$DATA_DIR_WIN" 2>/dev/null || true

exit 0
