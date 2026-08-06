#!/usr/bin/env bash
# zmem-subagent-recall.sh — SubagentStart hook: inject box-wide memory into a
# freshly-dispatched subagent (shared, both hosts).
#
# When a subagent (coder / explorer / reviewer / general-purpose / …) starts, it
# begins with a clean context and would otherwise inherit none of the box's
# memory. This hook recalls the dispatching namespace's recent high-confidence
# memories (plus the user:global bridge) and injects them as additionalContext,
# so a delegated agent sees the same lessons/conventions a fresh top-level
# session would. This is the "recall into subagents" half of box-wide memory
# (PLAN.md §5).
#
# WHY `recent`, not `recall`: SubagentStart carries NO task/prompt text (payload
# keys: session_id, transcript_path, cwd, prompt_id, agent_id, agent_type,
# hook_event_name — Phase 7 empirical dump), so there is no query to FTS on. The
# spec's primary behavior is "scoped to the namespace (+ user:global)", which is
# exactly the query-less `recent` pull session-start already uses for Tier 2.
# agent_type only biases the header label (kept simple, no query machinery).
#
# PASSIVE (CRITIC 6 / issue #21): recall runs with `--no-bump` so a dispatch fan-out
# does NOT turn N subagents into N concurrent retrieval_count writers on the shared
# store; the surface event is still recorded on surfaced_count.
#
# Envelope: emits a bare {"additionalContext": …} wrapped in the
# <<<ZMEM_JSON>>>…<<<END>>> sentinel. The host adapter (zmem-launch.js) extracts
# it and rewraps per host (Claude Code: hookSpecificOutput.additionalContext for
# SubagentStart — empirically injected into the subagent, CC 2.1.218; ZCode:
# bare additionalContext) and enforces the encoded context budget.
#
# NON-BLOCKING / FAIL-OPEN: always exits 0; any error degrades to no injection.
#
# Canonical env (from zmem-launch.js): ZMEM_ROOT, ZMEM_DATA, ZMEM_PROJECT,
# ZMEM_NAMESPACE, ZMEM_AGENT_TYPE, ZMEM_CTX_BUDGET. Legacy fallbacks kept for
# manual/back-compat runs.

set -u

# Read + discard stdin (SubagentStart payload); fields already parsed by the
# launcher into canonical env. Draining stdin avoids an EPIPE on the writer.
INPUT="$(cat)"
: "${INPUT:=}"

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

# No python → cannot recall; fail open (no injection).
if [ -z "$PYTHON_BIN" ]; then
  printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' '{}'
  exit 0
fi

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

# Canonical env from the host adapter; legacy vars as fallback.
PLUGIN_ROOT="${ZMEM_ROOT:-${ZCODE_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}}"
DATA_DIR="${ZMEM_DATA:-${ZCODE_PLUGIN_DATA:-}}"
PROJECT="${ZMEM_PROJECT:-${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}}"
AGENT_TYPE="${ZMEM_AGENT_TYPE:-}"

# Resolve data dir.
if [ -n "$DATA_DIR" ]; then
  DATA_DIR_PY="$(to_py_path "$DATA_DIR")"
else
  DATA_DIR_PY="$(join_path "$(to_py_path "$HOME")" .zmem)"
fi

# Resolve store.py.
if [ -z "$PLUGIN_ROOT" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
STORE_PY_PY="$(join_path "$(to_py_path "$PLUGIN_ROOT")" skills memory scripts store.py)"

# Export the store location so store.py resolves it (chain:
# ZMEM_STORE > ZMEM_DATA > CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA > ~/.zmem > ~/.zcode).
export ZMEM_DATA="${ZMEM_DATA:-$DATA_DIR}"
export ZCODE_PLUGIN_DATA="${ZCODE_PLUGIN_DATA:-}"

# Canonical namespace (single derived key) with legacy basename fallback.
NS="${ZMEM_NAMESPACE:-}"
if [ -z "$NS" ]; then
  if [ -n "$PROJECT" ]; then
    NS="project:$(basename "$PROJECT")"
  else
    NS="user:global"
  fi
fi
BUDGET="${ZMEM_CTX_BUDGET:-25000}"

# Build the recall payload. A single python process pulls recent high-confidence
# memories from the subagent's namespace AND the user:global tier in ONE
# `recent` call (--include-global), PASSIVE via --no-bump (surface counted, retrieval
# not bumped — issue #21). The store does the
# project-first merge and id dedup, so this is now one subprocess instead of the
# old two-pull shell bridge (issue #18). Result is byte-equivalent to the old
# bridge: up to 5 namespace rows then up to 3 user:global rows, deduped.
CTX_JSON="$("$PYTHON_BIN" -c '
import json, os, subprocess, sys

store_py = sys.argv[1]
ns = sys.argv[2]
agent_type = sys.argv[3]
try:
    budget = int(sys.argv[4])
except (IndexError, ValueError):
    budget = 25000

def emit(obj):
    print(json.dumps(obj) if obj else "{}")
    sys.exit(0)

if not store_py or not os.path.isfile(store_py):
    emit({})

def recent(namespace, limit, global_limit):
    """PASSIVE recent pull (records a surface, does not bump retrieval) for a namespace
    + the user:global tier (fail-open to []).

    --include-global folds the user:global tier into one call with a per-tier
    budget; the store merges project-first and dedups by id. When namespace IS
    user:global the store treats --include-global as a no-op (the project tier
    already is global), matching the old `if ns != user:global` bridge guard.
    """
    try:
        out = subprocess.check_output(
            [sys.executable, store_py, "recent",
             "--namespace", namespace, "--limit", str(limit),
             "--include-global", "--global-limit", str(global_limit),
             "--min-confidence", "0.5", "--no-bump", "--json"],  # passive: surface not retrieval
            stderr=subprocess.DEVNULL, timeout=10,
        ).decode("utf-8", "replace")
        return json.loads(out) if out.strip() else []
    except Exception:
        return []

rows = recent(ns, 5, 3)

if not rows:
    emit({})

header = "# Box-wide memory (zmem subagent recall, namespace %s" % ns
if agent_type:
    header += ", agent %s" % agent_type
header += "). Consider if relevant to your task; ignore if not."
lines = [header, ""]
total = len(header)
for r in rows:
    content = (r.get("content") or "")[:300]
    signal = r.get("signal", "?")
    # Same stale rendering as zmem-recall.sh: store.py returns a separate
    # "stale" boolean (content itself is never annotated), and the halved
    # confidence does not reliably drop the row under the floor, so stale rows
    # reach the injected context and must be marked for the agent.
    stale = " [STALE SOURCE]" if r.get("stale") else ""
    entry = "- [%s]%s %s" % (signal, stale, content)
    total += len(entry)
    if budget > 0 and total > budget:  # soft cap; launcher enforces hard encoded budget
        break
    lines.append(entry)

ctx = "\n".join(lines)
emit({"additionalContext": ctx})
' "$STORE_PY_PY" "$NS" "$AGENT_TYPE" "$BUDGET" 2>/dev/null || echo '{}')"

if [ -z "$CTX_JSON" ]; then
  CTX_JSON='{}'
fi

# Neutralize any sentinel token that a MEMORY'S OWN CONTENT happens to contain
# before wrapping (same defense as zmem-recall.sh). The launcher locates the
# payload by scanning stdout for the literal markers, so a stored memory
# containing "<<<ZMEM_JSON>>>" would move the extraction boundary into the
# middle of the JSON and the whole recall would silently degrade to {}.
# Both replacements are safe inside the serialized JSON string: neither
# introduces a quote or a backslash.
CTX_JSON="${CTX_JSON//<<<ZMEM_JSON>>>/<<<ZMEM_JSON_NEUTRALIZED>>>}"
CTX_JSON="${CTX_JSON//<<<END>>>/<<<END_NEUTRALIZED>>>}"

# Wrap the payload in the sentinel so the host adapter can extract + rewrap it.
printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$CTX_JSON"
exit 0
