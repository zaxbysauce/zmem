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

PLUGIN_ROOT="${ZCODE_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
DATA_DIR="${ZCODE_PLUGIN_DATA:-}"
PROJECT="${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}"

# --- Path conversion helper (same as session-start/reflect hooks) -----------
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

# --- Resolve data dir (Windows format for python) ---------------------------
if [ -n "$DATA_DIR" ]; then
  DATA_DIR_WIN="$(to_win_path "$DATA_DIR" 2>/dev/null || cygpath -w "$DATA_DIR" 2>/dev/null || echo "$DATA_DIR")"
else
  DATA_DIR_WIN="$(to_win_path "$HOME" 2>/dev/null || cygpath -w "$HOME" 2>/dev/null || echo "$HOME")\\.zcode\\memory"
fi

# --- Resolve store.py path --------------------------------------------------
if [ -n "$PLUGIN_ROOT" ]; then
  STORE_PY_WIN="$(to_win_path "$PLUGIN_ROOT" 2>/dev/null || cygpath -w "$PLUGIN_ROOT" 2>/dev/null || echo "$PLUGIN_ROOT")\\skills\\memory\\scripts\\store.py"
else
  # Manual install fallback: scripts live alongside this hook.
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
  STORE_PY_WIN="$(to_win_path "$PLUGIN_ROOT" 2>/dev/null || cygpath -w "$PLUGIN_ROOT" 2>/dev/null || echo "$PLUGIN_ROOT")\\skills\\memory\\scripts\\store.py"
fi

# Export env so store.py finds its store via ZCODE_PLUGIN_DATA.
export ZCODE_PLUGIN_DATA="${ZCODE_PLUGIN_DATA:-$DATA_DIR}"

# --- Determine namespace from project ---------------------------------------
NS="user:global"
if [ -n "$PROJECT" ]; then
  NS="project:$(basename "$PROJECT")"
fi

# --- Build the recall payload via python (guaranteed-valid JSON) ------------
# We pass: the stdin JSON, store.py path, namespace, data dir.
printf '%s' "$INPUT" | python -c '
import json, os, sys, subprocess

raw_stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
prompt = ""
try:
    obj = json.loads(raw_stdin)
    prompt = obj.get("prompt", "")
except Exception:
    prompt = ""

# Bail on empty/trivial prompts — recall adds latency for no value on one-word prompts.
if not prompt or not prompt.strip() or len(prompt.strip()) < 5:
    print("{}")
    sys.exit(0)

store_py = sys.argv[1]
ns = sys.argv[2]

if not store_py or not os.path.isfile(store_py):
    print("{}")
    sys.exit(0)

# Run store.py recall against the prompt text.
# Limit to 5 results to stay within the 32KB additionalContext budget.
# Use the default confidence floor (do NOT pass --min-confidence so the
# configured floor applies).
try:
    out = subprocess.check_output(
        [sys.executable, store_py, "recall",
         "--query", prompt[:500],  # cap query length
         "--namespace", ns,
         "--limit", "5",
         "--json"],
        stderr=subprocess.DEVNULL, timeout=10,
    ).decode("utf-8", "replace")
    rows = json.loads(out) if out.strip() else []
except Exception:
    rows = []  # fail-open: recall errors never block the prompt

if not rows:
    print("{}")
    sys.exit(0)

# Build a compact, bounded additionalContext block.
# Each memory: [signal] content (truncated to 300 chars for budget).
lines = [
    "# Relevant memories (zmem recall, namespace %s). Consider if they apply to this task; ignore if not." % ns,
    "",
]
total_chars = 0
for r in rows:
    content = (r.get("content") or "")[:300]
    signal = r.get("signal", "?")
    entry = "- [%s] %s" % (signal, content)
    total_chars += len(entry)
    if total_chars > 25000:  # stay well under the 32KB budget
        break
    lines.append(entry)

ctx = "\n".join(lines)
print(json.dumps({"additionalContext": ctx}))
' "$STORE_PY_WIN" "$NS" 2>/dev/null || echo '{}'

exit 0
