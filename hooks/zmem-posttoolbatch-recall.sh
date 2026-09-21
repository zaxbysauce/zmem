#!/usr/bin/env bash
# zmem-posttoolbatch-recall.sh — Claude PostToolBatch hook for post-edit
# checkpoint recall (issue #120, Workstream D PR 5 of 8).
#
# After one COMPLETED batch of tool calls, this hook derives the recall query
# from the batch itself — every retained tool name plus its command / path
# fields, bounded (150-char fields, 500-char query) — and injects matching
# hazard lessons as additionalContext BEFORE the next turn acts on the batch's
# effects. The batch parser never forwards tool_response, result, or unknown
# keys. The internal mode is "posttoolbatch"; the runtime moment recorded in
# the decision log and the delivery ledger is the closed-set "pretool" — no
# posttoolbatch moment is emitted anywhere (issue #120 contract).
#
# Claude-only: hooks.claude.json is the ONLY manifest that registers
# PostToolBatch. Codex has no such upstream event and stays without one
# (hooks.codex.json deliberately unchanged); the launcher maps the verb for
# envelope translation but never synthesizes the event.
#
# Contract (issue #120):
#   - Reads stdin once, invokes hooks/lib/zmem-recall-body.py with mode
#     "posttoolbatch", and ALWAYS exits 0.
#   - Malformed JSON, missing Python, nonzero store exit, invalid store JSON,
#     and an empty payload all degrade to `{}` (fail-open, no injection).
#   - The wrapper never imports storelib, never opens SQLite, never reads the
#     delivery ledger, and never touches the correction queue — the shared
#     body owns every store-side decision (same single source of truth as
#     recall/precompact/subagent-recall/pretool).
#
# Non-blocking: a memory hiccup never blocks a session.

set -u

# --- Read stdin (one JSON payload) -------------------------------------------
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
# Mode "posttoolbatch" parses the batch event, derives the bounded query and
# the operation tokens, consults the #117 delivery ledger, and makes ONE
# store.py recall call (--for-injection, --no-bump) — then renders the fence,
# records the delivery, and writes the decision line there.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECALL_BODY="$SCRIPT_DIR/lib/zmem-recall-body.py"
if [ ! -f "$RECALL_BODY" ]; then
    echo '{}'
    exit 0
fi

OUT="$(printf '%s' "$INPUT" | "$PYTHON_BIN" "$RECALL_BODY" "$STORE_PY_PY" "$NS" "$BUDGET" "posttoolbatch" 2>/dev/null || echo '{}')"

# Issue #124: report the successful batch to the observational feedback loop
# after the selector decision and before the next delivery moment. The
# selector invocation above remains the sole delivery decision and keeps its
# required --namespace. Event/evidence ids are derived deterministically
# from the payload bytes (evidence_id/tool_use_id wins when present); the
# call is fail-open and inherits ZMEM_STORE/ZMEM_DATA.
"$PYTHON_BIN" -c '
import hashlib, json, subprocess, sys
store, raw = sys.argv[1], sys.argv[2]
try:
    payload = json.loads(raw)
except Exception:
    raise SystemExit(0)
if not isinstance(payload, dict):
    raise SystemExit(0)
session = payload.get("session_id")
if not isinstance(session, str) or not session:
    raise SystemExit(0)
tokens = []
for use in payload.get("tool_uses") or []:
    if not isinstance(use, dict):
        continue
    inp = use.get("input")
    if not isinstance(inp, dict):
        continue
    text = " ".join(str(v) for v in inp.values() if v).lower()
    tokens.extend(w for w in text.split() if w)
import re as _re
_SECRET = _re.compile(r"(ghp_|gho_|github_pat_|sk-[A-Za-z0-9]|AKIA|glpat_|xox[bap]-|AIza)", _re.I)
tokens = [w for w in tokens
          if not w.startswith("-") and not _SECRET.search(w)]
if not tokens:
    raise SystemExit(0)
seen = []
for tok in tokens:
    if tok not in seen:
        seen.append(tok)
event = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]
args = [sys.executable, store, "operation-feedback",
        "--session-id", session, "--event-id", event,
        "--outcome", "success"]
# Same evidence-id rule as the capture-failure hook: forward the evidence_id
# supplied by the payload when present, omit the flag otherwise (final-critic
# round 1).
ev = payload.get("evidence_id")
if isinstance(ev, str) and ev:
    args.extend(["--evidence-id", ev])
for tok in seen[:8]:
    args.extend(["--operation-token", tok])
subprocess.call(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
raise SystemExit(0)
' "$STORE_PY_PY" "$INPUT" 2>/dev/null || true

# Neutralize sentinel/fence tokens a memory's own content might contain
# (same defense as zmem-pretool-recall.sh — the launcher locates the payload
# by scanning for the literal markers).
OUT="${OUT//<<<ZMEM_JSON>>>/<<<ZMEM_JSON_NEUTRALIZED>>>}"
OUT="${OUT//<<<END>>>/<<<END_NEUTRALIZED>>>}"
OUT="${OUT//<<<ZMEM_UNTRUSTED_FENCE>>>/<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"
OUT="${OUT//<<<END_ZMEM_UNTRUSTED_FENCE>>>/<<<END_ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"

printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$OUT"
exit 0
