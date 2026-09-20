#!/usr/bin/env bash
# zmem-capture-failure.sh — PostToolUseFailure capture adapter.
#
# Policy and persistent state live behind the capture-quality module and the
# store.py command boundary. This adapter translates the host payload, applies
# the fail-open envelope contract, and renders the nudge.

set -u

emit_empty() {
  printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' '{}'
  exit 0
}

# This gate intentionally precedes stdin reads, parsing, marker access, and
# every store subprocess. Only a trimmed value of exactly "0" disables it.
CAPTURE_VALUE=1
if CAPTURE_VALUE="$(printenv ZMEM_CAPTURE 2>/dev/null)"; then :; else CAPTURE_VALUE=1; fi
CAPTURE_VALUE="$(printf '%s' "$CAPTURE_VALUE" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
if [ "$CAPTURE_VALUE" = "0" ]; then
  emit_empty
fi

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
PROJECT="$(env_value ZMEM_PROJECT)"
[ -n "$PROJECT" ] || PROJECT="$(env_value ZCODE_PROJECT_DIR)"
[ -n "$PROJECT" ] || PROJECT="$(env_value CLAUDE_PROJECT_DIR)"
DATA_DIR="$(env_value ZMEM_DATA)"
[ -n "$DATA_DIR" ] || DATA_DIR="$(env_value ZCODE_PLUGIN_DATA)"
PLUGIN_ROOT="$(env_value ZMEM_ROOT)"
[ -n "$PLUGIN_ROOT" ] || PLUGIN_ROOT="$(env_value ZCODE_PLUGIN_ROOT)"
[ -n "$PLUGIN_ROOT" ] || PLUGIN_ROOT="$(env_value CLAUDE_PLUGIN_ROOT)"
[ -n "$SESSION_ID" ] || emit_empty

if [ -z "$DATA_DIR" ]; then DATA_DIR="$(join_path "$(to_py_path "$HOME")" .zmem)"; fi
DATA_DIR_PY="$(to_py_path "$DATA_DIR")"
DATA_DIR_SH="$(to_shell_path "$DATA_DIR")"

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

# The filesystem key is never the raw session id. Hashing is over the exact
# UTF-8 session bytes and is shared by recurrence and prompt markers.
SESSION_HASH="$("$PYTHON_BIN" -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest()[:32])' "$SESSION_ID" 2>/dev/null)" || emit_empty
[ -n "$SESSION_HASH" ] || emit_empty
MARKER="$(join_shell_path "$DATA_DIR_SH" ".capture-prompted-$SESSION_HASH")"
RECURRENCE="$(join_path "$DATA_DIR_PY" ops "$SESSION_HASH.capture-failure.json")"
[ -e "$MARKER" ] && emit_empty

# Parse one failed call and atomically advance its recurrence record. The
# subprocess emits only bounded, normalized fields for the renderer.
META_JSON="$(printf '%s' "$INPUT" | "$PYTHON_BIN" -c '
import json, os, re, sys, tempfile

store_py = sys.argv[1]
session = sys.argv[2]
recurrence = sys.argv[3]
sys.path.insert(0, os.path.dirname(store_py))
try:
    from capture_quality import infer_signal, operation_descriptor
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
tool_input = payload.get("tool_input")
if not isinstance(tool_input, dict): tool_input = {}
command = tool_input.get("command", "")
command = command if isinstance(command, str) else ""
path = ""
for key in ("file_path", "notebook_path", "path"):
    value = tool_input.get(key, "")
    if isinstance(value, str) and value:
        path = value
        break
error = payload.get("error", "")
if isinstance(error, dict):
    error_text = error.get("message", "") or ""
    error_type = error.get("type", "") or ""
else:
    error_text = error if isinstance(error, str) else ""
    error_type = payload.get("error_type", "") or ""
if not isinstance(error_type, str): error_type = str(error_type)
error_text = re.sub(r"\s+", " ", str(error_text).replace("\r", " ").replace("\n", " ")).strip()[:240]
descriptor = operation_descriptor(tool, command, path, error_type)
signal = infer_signal(command, exit_code=payload.get("exit_code"))

count = 0
try:
    with open(recurrence, "r", encoding="utf-8") as f:
        old = json.load(f)
    if isinstance(old, dict) and old.get("session") == session:
        count = int(old.get("count", 0) or 0)
except Exception:
    pass
count += 1
state = {"session": session, "count": count, "last_error": error_text}
try:
    os.makedirs(os.path.dirname(recurrence), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".capture-failure-", suffix=".tmp", dir=os.path.dirname(recurrence))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
        os.replace(tmp, recurrence)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        print("{}")
        raise SystemExit(0)
except Exception:
    print("{}")
    raise SystemExit(0)

tags = "tool:%s,verb:%s,basename:%s,error:%s" % (
    descriptor["tool"], descriptor["verb"], descriptor["basename"], descriptor["error"])
print(json.dumps({"ok": True, "session": session, "count": count,
                  "signal": signal, "descriptor": descriptor, "tags": tags,
                  "prompt": signal != "none" or count >= 2},
                 ensure_ascii=False, separators=(",", ":")))
' "$STORE_PY_PY" "$SESSION_ID" "$RECURRENCE" 2>/dev/null)" || emit_empty

PROMPT_ALLOWED="$("$PYTHON_BIN" -c '
import json,sys
try:
    obj=json.loads(sys.argv[1])
    print("1" if obj.get("ok") and obj.get("prompt") else "0")
except Exception:
    print("0")
' "$META_JSON" 2>/dev/null)" || emit_empty
[ "$PROMPT_ALLOWED" = "1" ] || emit_empty

# source-exists is deliberately after recurrence gating. It must not create a
# missing store and a malformed/nonzero result is fail-open.
SOURCE_EXISTS="$("$PYTHON_BIN" "$STORE_PY_PY" source-exists --namespace "$NS" --source-ref "session:$SESSION_ID" --json 2>/dev/null | "$PYTHON_BIN" -c '
import json,sys
try:
    obj=json.load(sys.stdin)
    print("true" if obj.get("exists") is True else "false" if obj.get("exists") is False else "invalid")
except Exception:
    print("invalid")
' 2>/dev/null)" || emit_empty
case "$SOURCE_EXISTS" in
  true|invalid) emit_empty ;;
  false) : ;;
  *) emit_empty ;;
esac

CTX_JSON="$("$PYTHON_BIN" -c '
import json, shlex, sys
obj = json.loads(sys.argv[1])
descriptor = obj["descriptor"]
store = shlex.quote(sys.argv[2])
namespace = shlex.quote(sys.argv[3])
source_ref = shlex.quote("session:" + obj["session"])
claim = "when X happens, do Y, because Z"
command = ("%s add --namespace %s --type lesson --content %s --tags %s "
           "--signal %s --source-ref %s" %
           (store, namespace, shlex.quote(claim), shlex.quote(obj["tags"]),
            obj["signal"], source_ref))
msg = ("ZMem auto-capture: a repeated or recognized tool failure was observed. "
       "Capture a generalizable lesson only when the claim is grounded in a "
       "test/compile/lint/reviewer/user signal. Operation descriptor: %s. "
       "Tags: %s. Use the claim shape %s and choose whether this is "
       "project-bound or box-wide before running: %s. If it is a one-off, "
       "do nothing.") % (
           json.dumps(descriptor, ensure_ascii=False, separators=(",", ":")),
           obj["tags"], claim, command)
print(json.dumps({"additionalContext": msg}, ensure_ascii=False, separators=(",", ":")))
' "$META_JSON" "$STORE_PY_PY" "$NS" 2>/dev/null)" || emit_empty
[ -n "$CTX_JSON" ] || emit_empty

# Only the successful render path writes the prompt marker.
MARKER_DIR="$(dirname "$MARKER")"
if [ ! -d "$MARKER_DIR" ] && ! mkdir -p "$MARKER_DIR" 2>/dev/null; then emit_empty; fi
if ! printf '1\n' > "$MARKER" 2>/dev/null; then emit_empty; fi

CTX_JSON="$(printf '%s' "$CTX_JSON" | sed 's/<<<ZMEM_JSON>>>/<<<ZMEM_JSON_NEUTRALIZED>>>/g; s/<<<END>>>/<<<END_NEUTRALIZED>>>/g')"
printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$CTX_JSON"
exit 0
