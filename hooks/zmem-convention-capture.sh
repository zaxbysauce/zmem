#!/usr/bin/env bash
# zmem-convention-capture.sh — PostToolUse convention adapter.
#
# Every eligible tool event is offered to the operation ring through store.py.
# Only a non-amend git commit emits a convention prompt, once per session.

set -u

# Keep the artifact resolver in lockstep with SessionStart: explicit store,
# canonical data, Claude/ZCode plugin data, then the box-wide default.
. "$(dirname "$0")/lib/zmem-tilde-expand.sh"

emit_empty() {
  printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' '{}'
  exit 0
}

# The capture switch is deliberately before stdin and all state/subprocess work.
CAPTURE_VALUE=1
if CAPTURE_VALUE="$(printenv ZMEM_CAPTURE 2>/dev/null)"; then :; else CAPTURE_VALUE=1; fi
CAPTURE_VALUE="$(printf '%s' "$CAPTURE_VALUE" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
if [ "$CAPTURE_VALUE" = "0" ]; then emit_empty; fi

INPUT="$(cat)"

IS_WINDOWS=0
case "$(uname -s 2>/dev/null)" in
  MINGW*|CYGWIN*|MSYS*) IS_WINDOWS=1 ;;
esac
PYTHON_BIN=""
if [ "$IS_WINDOWS" -eq 1 ]; then
  if python --version >/dev/null 2>&1; then PYTHON_BIN="python"
  elif python3 --version >/dev/null 2>&1; then PYTHON_BIN="python3"; fi
else
  if python3 --version >/dev/null 2>&1; then PYTHON_BIN="python3"
  elif python --version >/dev/null 2>&1; then PYTHON_BIN="python"; fi
fi
[ -n "$PYTHON_BIN" ] || emit_empty

to_py_path() {
  if [ "$IS_WINDOWS" -eq 0 ]; then printf '%s' "$1"; return; fi
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}
to_shell_path() {
  if [ "$IS_WINDOWS" -eq 1 ] && command -v cygpath >/dev/null 2>&1; then
    cygpath -u "$1"
  else
    printf '%s' "$1"
  fi
}
join_path() {
  local base="$1"; shift
  local sep='/'; [ "$IS_WINDOWS" -eq 1 ] && sep='\'
  printf '%s' "$base"
  for part in "$@"; do printf '%s%s' "$sep" "$part"; done
}
join_shell_path() {
  local base="$1"; shift
  printf '%s' "$base"
  for part in "$@"; do printf '/%s' "$part"; done
}
env_value() { printenv "$1" 2>/dev/null || true; }

SESSION_ID="$(env_value ZMEM_SESSION)"
[ -n "$SESSION_ID" ] || SESSION_ID="$(env_value CLAUDE_SESSION_ID)"
[ -n "$SESSION_ID" ] || SESSION_ID="$(env_value ZCODE_SESSION_ID)"
[ -n "$SESSION_ID" ] || emit_empty
PROJECT="$(env_value ZMEM_PROJECT)"
[ -n "$PROJECT" ] || PROJECT="$(env_value ZCODE_PROJECT_DIR)"
[ -n "$PROJECT" ] || PROJECT="$(env_value CLAUDE_PROJECT_DIR)"
PLUGIN_ROOT="$(env_value ZMEM_ROOT)"
[ -n "$PLUGIN_ROOT" ] || PLUGIN_ROOT="$(env_value ZCODE_PLUGIN_ROOT)"
[ -n "$PLUGIN_ROOT" ] || PLUGIN_ROOT="$(env_value CLAUDE_PLUGIN_ROOT)"
DATA_DIR="$(env_value ZMEM_DATA)"
[ -n "$DATA_DIR" ] || DATA_DIR="$(env_value CLAUDE_PLUGIN_DATA)"
[ -n "$DATA_DIR" ] || DATA_DIR="$(env_value ZCODE_PLUGIN_DATA)"
STORE_PATH="$(env_value ZMEM_STORE)"
if [ -n "$STORE_PATH" ]; then
  if [ "$IS_WINDOWS" -eq 1 ]; then STORE_PATH="$(printf '%s' "$STORE_PATH" | tr '\134' '/')"; fi
  DATA_DIR="$(dirname "$STORE_PATH")"
fi
DATA_DIR_IS_NATIVE=0
if [ -z "$DATA_DIR" ]; then
  # Ask the canonical resolver for host-specific fallback selection. This is
  # especially important when a pre-migration legacy store already exists.
  if [ -n "$PLUGIN_ROOT" ]; then
    HOST_ROOT="$PLUGIN_ROOT"
  else
    HOST_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
  fi
  HOST_DIR_PY="$(join_path "$(to_py_path "$HOST_ROOT")" skills memory scripts)"
  RESOLVED_DATA="$($PYTHON_BIN -c 'import sys; sys.path.insert(0, sys.argv[1]); import host; print(host.resolve_store_path().parent)' "$HOST_DIR_PY" 2>/dev/null)"
  if [ -n "$RESOLVED_DATA" ]; then
    DATA_DIR="$RESOLVED_DATA"
    DATA_DIR_IS_NATIVE=1
  fi
fi
if [ -z "$DATA_DIR" ]; then DATA_DIR="$(join_path "$(to_py_path "$HOME")" .zmem)"; fi
zmem_tilde_expand
if [ "$DATA_DIR_IS_NATIVE" -eq 1 ]; then DATA_DIR_PY="$DATA_DIR"; else DATA_DIR_PY="$(to_py_path "$DATA_DIR")"; fi
DATA_DIR_SH="$(to_shell_path "$DATA_DIR_PY")"

if [ -n "$PLUGIN_ROOT" ]; then
  STORE_PY_PY="$(join_path "$(to_py_path "$PLUGIN_ROOT")" skills memory scripts store.py)"
else
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  STORE_PY_PY="$(join_path "$(to_py_path "$SCRIPT_DIR/..")" skills memory scripts store.py)"
fi
NS="$(env_value ZMEM_NAMESPACE)"
if [ -z "$NS" ]; then
  if [ -n "$PROJECT" ]; then NS="project:$(basename "$PROJECT")"; else NS="user:global"; fi
fi

SESSION_HASH="$("$PYTHON_BIN" -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest()[:32])' "$SESSION_ID" 2>/dev/null)" || emit_empty
MARKER="$(join_shell_path "$DATA_DIR_SH" ".convention-commit-prompted-$SESSION_HASH")"

# Parse the host payload and apply the shared descriptor/commit policy. The
# commit check is token-exact: only [git, commit] at the beginning qualifies.
META_JSON="$(printf '%s' "$INPUT" | "$PYTHON_BIN" -c '
import json, os, shlex, sys
store_py = sys.argv[1]
sys.path.insert(0, os.path.dirname(store_py))
try:
    from capture_quality import operation_descriptor
except Exception:
    print("{}")
    raise SystemExit(0)
try:
    payload = json.load(sys.stdin)
except Exception:
    print("{}")
    raise SystemExit(0)
if not isinstance(payload, dict):
    print("{}")
    raise SystemExit(0)
tool = payload.get("tool_name", "")
tool = tool if isinstance(tool, str) else ""
if tool not in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"):
    print("{}")
    raise SystemExit(0)
ti = payload.get("tool_input")
if not isinstance(ti, dict): ti = {}
command = ti.get("command", "")
command = command if isinstance(command, str) else ""
path = ""
for key in ("file_path", "notebook_path", "path"):
    value = ti.get(key, "")
    if isinstance(value, str) and value:
        path = value
        break
error_type = payload.get("error_type", "")
if not isinstance(error_type, str): error_type = str(error_type)
descriptor = operation_descriptor(tool, command, path, error_type)
try:
    tokens = shlex.split(command, posix=True)
except (TypeError, ValueError):
    tokens = []
is_commit = len(tokens) >= 2 and tokens[:2] == ["git", "commit"] and "--amend" not in tokens
op = descriptor["command"] or path
print(json.dumps({"tool": tool, "op": op, "is_commit": is_commit,
                  "descriptor": descriptor}, ensure_ascii=False,
                 separators=(",", ":")))
' "$STORE_PY_PY" 2>/dev/null)" || emit_empty

META_OK="$("$PYTHON_BIN" -c '
import json,sys
try:
    obj=json.loads(sys.argv[1])
    print("1" if obj.get("tool") else "0")
except Exception:
    print("0")
' "$META_JSON" 2>/dev/null)" || emit_empty
[ "$META_OK" = "1" ] || emit_empty

TOOL="$("$PYTHON_BIN" -c 'import json,sys; print(json.loads(sys.argv[1])["tool"])' "$META_JSON" 2>/dev/null)" || emit_empty
OP="$("$PYTHON_BIN" -c 'import json,sys; print(json.loads(sys.argv[1])["op"])' "$META_JSON" 2>/dev/null)" || emit_empty

# Ring collection is independent from prompt eligibility. The existing query
# context switch gates collection only; it does not disable commit prompts.
QUERY_CONTEXT="$(env_value ZMEM_QUERY_CONTEXT)"
[ -n "$QUERY_CONTEXT" ] || QUERY_CONTEXT=1
QUERY_CONTEXT="$(printf '%s' "$QUERY_CONTEXT" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
if [ "$QUERY_CONTEXT" != "0" ]; then
  [ -n "$OP" ] || emit_empty
  OPS_RESULT="$("$PYTHON_BIN" "$STORE_PY_PY" ops-append --session "$SESSION_ID" --tool "$TOOL" --op "$OP" --json 2>/dev/null)" || emit_empty
  "$PYTHON_BIN" -c 'import json,sys; o=json.loads(sys.argv[1]); raise SystemExit(0 if o.get("ok") is True else 1)' "$OPS_RESULT" 2>/dev/null || emit_empty
fi

IS_COMMIT="$("$PYTHON_BIN" -c 'import json,sys; print("1" if json.loads(sys.argv[1]).get("is_commit") else "0")' "$META_JSON" 2>/dev/null)" || emit_empty
[ "$IS_COMMIT" = "1" ] || emit_empty
[ -e "$MARKER" ] && emit_empty

CTX_JSON="$("$PYTHON_BIN" -c '
import json, shlex, sys
obj = json.loads(sys.argv[1])
d = obj["descriptor"]
store = shlex.quote(sys.argv[2])
namespace = shlex.quote(sys.argv[3])
source_ref = shlex.quote("session:" + sys.argv[4])
claim = "when X happens, do Y, because Z"
tags = "tool:%s,verb:%s,basename:%s,error:%s" % (
    d["tool"], d["verb"], d["basename"], d["error"])
add = ("%s add --namespace %s --type convention --content %s --tags %s "
       "--signal none --source-ref %s" %
       (store, namespace, shlex.quote(claim), shlex.quote(tags), source_ref))
msg = ("ZMem convention capture: a non-amend git commit completed. Operation "
       "descriptor: %s. Tags: %s. If this is a reusable convention, use the "
       "claim shape %s and choose whether it is project-bound or box-wide, "
       "then run: %s. If not, do nothing.") % (
           json.dumps(d, ensure_ascii=False, separators=(",", ":")),
           tags, claim, add)
print(json.dumps({"additionalContext": msg}, ensure_ascii=False, separators=(",", ":")))
' "$META_JSON" "$STORE_PY_PY" "$NS" "$SESSION_ID" 2>/dev/null)" || emit_empty
[ -n "$CTX_JSON" ] || emit_empty

# The marker follows successful prompt rendering. It is a best-effort cooldown.
if ! mkdir -p "$(dirname "$MARKER")" 2>/dev/null; then emit_empty; fi
if ! printf '1\n' > "$MARKER" 2>/dev/null; then emit_empty; fi

CTX_JSON="$(printf '%s' "$CTX_JSON" | sed 's/<<<ZMEM_JSON>>>/<<<ZMEM_JSON_NEUTRALIZED>>>/g; s/<<<END>>>/<<<END_NEUTRALIZED>>>/g')"
printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$CTX_JSON"
exit 0
