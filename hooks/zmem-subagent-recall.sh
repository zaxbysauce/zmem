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
# WHY `recent`, not `recall`: SubagentStart carried NO task/prompt text when
# this hook was written (payload keys: session_id, transcript_path, cwd,
# prompt_id, agent_id, agent_type, hook_event_name — Phase 7 empirical dump).
# Issue #90 / #85 D: hosts that DO include the delegated prompt in the event
# now get task-text recall (mode "subagent" probes prompt/task/description
# and falls back to this same query-less recent pull when none is present),
# so a "fix CI" subagent queries the ratchet lessons instead of whatever
# recently landed.
# (agent_type is passed through to the shared body, which renders it in
# the header — preserving the pre-#58 header contract.)
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

# Build the recall payload via the shared body (issue #58, 3.5 + 3.9).
# The body lives in hooks/lib/zmem-recall-body.py and is invoked as a
# subprocess exactly like zmem-recall.sh and zmem-precompact.sh do —
# the file is HYPHENATED, so it cannot be `import`ed; every consumer
# runs it as a script (final-critic round 2 fix). Mode "subagent" (issue
# #90 / #85 D) prefers the delegated task text when the host event carries
# it and otherwise falls back to the query-less recent pull; it emits
# the full {"additionalContext": ...} envelope with the fenced,
# provenance-tagged render and the selective-inject gate applied.
# Limits 5/3 preserve this hook's pre-existing pull width (up to 5
# namespace rows then up to 3 user:global rows).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECALL_BODY="$SCRIPT_DIR/lib/zmem-recall-body.py"
if [ ! -f "$RECALL_BODY" ]; then
    RECALL_BODY_MISSING=1
fi

if [ -n "${RECALL_BODY_MISSING:-}" ]; then
  CTX_JSON='{}'
else
  CTX_JSON="$("$PYTHON_BIN" "$RECALL_BODY" "$STORE_PY_PY" "$NS" "$BUDGET" "subagent" "5" "3" "$AGENT_TYPE" 2>/dev/null || echo '{}')"
fi

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
# I7 critic-fix (issue #58, 3.5): also neutralize the new fence markers
# so a stored memory containing the literal fence opener/closer text
# cannot break the host adapter's `<<<END>>>` extraction or render a
# nested fence.
CTX_JSON="${CTX_JSON//<<<ZMEM_JSON>>>/<<<ZMEM_JSON_NEUTRALIZED>>>}"
CTX_JSON="${CTX_JSON//<<<END>>>/<<<END_NEUTRALIZED>>>}"
CTX_JSON="${CTX_JSON//<<<ZMEM_UNTRUSTED_FENCE>>>/<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"
CTX_JSON="${CTX_JSON//<<<END_ZMEM_UNTRUSTED_FENCE>>>/<<<END_ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"

# Wrap the payload in the sentinel so the host adapter can extract + rewrap it.
printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$CTX_JSON"
exit 0
