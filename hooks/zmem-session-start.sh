#!/usr/bin/env bash
# zmem-session-start.sh — shared SessionStart hook for ZMem (ZCode + Claude Code).
#
# Injects Tier 0 memory (core.md always; project AGENTS.md unless ZMEM_TIER0=
# native) and a bounded recall of Tier 2 semantic memories into the conversation
# as additionalContext at session start. Non-blocking: always exits 0.
#
# Tier-0 gating (P6, "replace native"): on ZCode (ZMEM_TIER0=zmem) AGENTS.md is
# injected as project-level Tier 0, same as always. On Claude Code
# (ZMEM_TIER0=native) AGENTS.md is skipped — CC's own project-level Tier 0 is
# CLAUDE.md, a separate always-on mechanism this hook must not double-inject.
# core.md (user-level Tier 0) and the Tier 2 recall still inject on both hosts.
#
# Native-memory nudge (Claude Code only): a one-time, read-only, best-effort
# check of ~/.claude/settings.json for autoMemoryEnabled — nudges the user to
# set it to false (a plugin can't set it itself) so CC's native memory and
# ZMem don't double-run. Guarded by a marker file in ZMEM_DATA; fail-open.
#
# Canonical env is supplied by the host adapter (zmem-launch.js): ZMEM_HOST,
# ZMEM_ROOT, ZMEM_DATA, ZMEM_PROJECT, ZMEM_NAMESPACE, ZMEM_TIER0,
# ZMEM_CTX_BUDGET. Legacy ZCODE_*/CLAUDE_* vars are the back-compat fallback
# for manual (non-launcher) invocation.
#
# First-run seeding: if core.md is absent in the data dir, copy from the template.
#
# Cross-platform: Windows Python cannot resolve Cygwin paths (/c/...). We convert
# with cygpath before passing to python. store.py uses os.path.expanduser and
# ZMEM_STORE/ZCODE_PLUGIN_DATA env internally.

set -u

# --- Cross-platform setup ---
IS_WINDOWS=0
if [[ "$(uname -s 2>/dev/null)" == MINGW* ]] || [[ "$(uname -s 2>/dev/null)" == CYGWIN* ]] || [[ "$(uname -s 2>/dev/null)" == MSYS* ]]; then
  IS_WINDOWS=1
fi

# Resolve python binary. On Windows, python3 is often a Microsoft Store stub
# that does nothing; prefer python. On POSIX, prefer python3, fall back to python.
# Verify the binary actually runs (--version) to avoid stubs.
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

# Convert a path for the local python. On Windows, python is a Windows build
# that cannot resolve Cygwin paths (/c/...). On POSIX, paths pass through.
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

# Build a sub-path with the correct separator for the platform.
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

# Canonical env is exported by the host adapter (zmem-launch.js). Prefer it;
# fall back to the legacy ZCODE_* vars for manual/back-compat installs.
PLUGIN_ROOT="${ZMEM_ROOT:-${ZCODE_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}}"
DATA_DIR="${ZMEM_DATA:-${ZCODE_PLUGIN_DATA:-}}"
PROJECT="${ZMEM_PROJECT:-${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}}"

# Resolve the data dir: canonical ZMEM_DATA / plugin data dir if set, else the
# box-neutral ~/.zmem default (the cutover target), else legacy ~/.zcode/memory.
if [ -n "$DATA_DIR" ]; then
  DATA_DIR_PY="$(to_py_path "$DATA_DIR")"
  mkdir -p "$DATA_DIR" 2>/dev/null || true
else
  DATA_DIR="$HOME/.zmem"
  DATA_DIR_PY="$(join_path "$(to_py_path "$HOME")" .zmem)"
  mkdir -p "$DATA_DIR" 2>/dev/null || true
fi

# Resolve plugin root for scripts + templates.
if [ -z "$PLUGIN_ROOT" ]; then
  # Manual install fallback: scripts live alongside this hook.
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

CORE_FILE_PY="$(join_path "$DATA_DIR_PY" core.md)"
STORE_PY_PY="$(join_path "$(to_py_path "$PLUGIN_ROOT")" skills memory scripts store.py)"

# First-run seeding: if core.md absent and template exists, copy it.
if [ -n "$PLUGIN_ROOT" ] && [ ! -f "$DATA_DIR/core.md" ] && [ -f "$PLUGIN_ROOT/templates/core.md.template" ]; then
  mkdir -p "$DATA_DIR" 2>/dev/null || true
  cp "$PLUGIN_ROOT/templates/core.md.template" "$DATA_DIR/core.md" 2>/dev/null || true
fi

# Tier-0 gating (P6 — replace native): ZMEM_TIER0=native means Claude Code, and
# CC's own project-level Tier 0 is CLAUDE.md (separate, always-on mechanism —
# see plugins-reference.md). Injecting AGENTS.md too would double-inject
# project-level Tier 0, exactly the duplication "replace native" exists to
# avoid. ZCode has no CLAUDE.md equivalent, so ZMEM_TIER0=zmem keeps injecting
# AGENTS.md as project-level Tier 0 (today's behavior, unchanged).
TIER0="${ZMEM_TIER0:-zmem}"

# Resolve project AGENTS.md (skipped entirely when TIER0=native).
AGENTS_FILE_PY=""
if [ "$TIER0" != "native" ] && [ -n "$PROJECT" ]; then
  AGENTS_FILE_PY="$(join_path "$(to_py_path "$PROJECT")" AGENTS.md)"
fi

# Native-memory nudge (CC only): best-effort read of ~/.claude/settings.json
# (+ settings.local.json) to see if the user has already flipped
# autoMemoryEnabled:false. Fires at most once, guarded by a marker file in
# ZMEM_DATA — never touches settings.json, read-only.
HOST="${ZMEM_HOST:-zcode}"
SETTINGS_DIR_PY="$(join_path "$(to_py_path "$HOME")" .claude)"
NUDGE_MARKER_PY="$(join_path "$DATA_DIR_PY" .native-nudge-shown)"

# Export the store location so store.py resolves it. Prefer canonical ZMEM_DATA
# (host adapter sets this to the box-wide ~/.zmem at cutover); keep the legacy
# ZCODE_PLUGIN_DATA export for back-compat. store.py's chain is
# ZMEM_STORE > ZMEM_DATA > CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA > ~/.zmem > ~/.zcode.
export ZMEM_DATA="${ZMEM_DATA:-$DATA_DIR}"
export ZCODE_PLUGIN_DATA="${ZCODE_PLUGIN_DATA:-}"

# Background consolidation: fully detached, fire-and-forget. stdio is redirected
# to /dev/null so it (a) can't pollute the launcher's piped stdout buffer and
# (b) doesn't hold the launcher's read pipe open — the launcher gets EOF the
# moment THIS script exits. No wait/kill loop: blocking up to 5s here is exactly
# the ~5s session-start stall Phase 3 removes; consolidate has its own internal
# growth-threshold + interval guard, so an orphaned run is safe.
#
# Auto-snapshot (P11) rides the exact same detachment discipline for the exact
# same reasons: fully redirected stdio so nothing can leak into the
# <<<ZMEM_JSON>>>…<<<END>>> payload the launcher parses (it reads stdout to
# EOF), and no wait/kill loop so session start never gains latency. `--if-due`
# makes it a cheap no-op almost every session — it only snapshots once per
# $ZMEM_BACKUP_INTERVAL_DAYS (default 1). Both commands take their own
# single-flight lock, so several sessions starting at once produce one
# consolidation and one snapshot, not N of each.
if [ -n "$STORE_PY_PY" ] && [ -f "$STORE_PY_PY" ]; then
  "$PYTHON_BIN" "$STORE_PY_PY" consolidate >/dev/null 2>&1 &
  "$PYTHON_BIN" "$STORE_PY_PY" backup --if-due --retention "${ZMEM_BACKUP_RETENTION:-7}" >/dev/null 2>&1 &
fi

# Canonical namespace from the host adapter (single derived key, closes the
# basename/remote split). Fall back to the legacy basename key when the adapter
# did not run (manual/back-compat invocation).
NS="${ZMEM_NAMESPACE:-}"
if [ -z "$NS" ]; then
  if [ -n "$PROJECT" ]; then
    NS="project:$(basename "$PROJECT")"
  else
    NS="user:global"
  fi
fi
BUDGET="${ZMEM_CTX_BUDGET:-25000}"

# Build the additionalContext payload using python for guaranteed-valid JSON.
CTX_JSON="$("$PYTHON_BIN" -c '
import json, os, sys, subprocess

core = sys.argv[1]
agents = sys.argv[2]
store_py = sys.argv[3]
home_win = sys.argv[4]
project = sys.argv[5]
data_dir = sys.argv[6]
ns = sys.argv[7]
try:
    budget = int(sys.argv[8])
except (IndexError, ValueError):
    budget = 25000
try:
    host = sys.argv[9]
except IndexError:
    host = ""
try:
    settings_dir = sys.argv[10]
except IndexError:
    settings_dir = ""
try:
    nudge_marker = sys.argv[11]
except IndexError:
    nudge_marker = ""

parts = []

# Tier 0: core.md (user-level). errors="replace" so one bad byte does not nuke
# the entire payload — a single corrupt file degrades to that file only.
if core and os.path.isfile(core):
    try:
        with open(core, encoding="utf-8", errors="replace") as f:
            parts.append("# Loaded from memory (Tier 0 — core.md, user-level):\n\n" + f.read())
    except OSError:
        pass

# Tier 0: AGENTS.md (project-level)
if agents and os.path.isfile(agents):
    try:
        with open(agents, encoding="utf-8", errors="replace") as f:
            parts.append("# Loaded from memory (Tier 0 — AGENTS.md, project-level):\n\n" + f.read())
    except OSError:
        pass

# Tier 2: bounded recall — cheap admin pull of recent high-confidence live
# memories. Namespace is the canonical key passed in (ns), NOT basename(project).
if store_py and os.path.isfile(store_py):
    try:
        out = subprocess.check_output(
            [sys.executable, store_py, "recent", "--namespace", ns, "--limit", "3", "--min-confidence", "0.5", "--json"],
            stderr=subprocess.DEVNULL, timeout=8,
        ).decode("utf-8", "replace")
        rows = json.loads(out) if out.strip() else []
        if rows:
            lines = ["# Recent memories (Tier 2 — namespace %s). Consider if relevant; ignore if not." % ns, ""]
            for r in rows:
                lines.append("- [%s] %s" % (r.get("signal","?"), r.get("content","")))
            parts.append("\n".join(lines))
    except Exception:
        pass  # fail-open: recall errors never block session start

# Inject the store.py path so the agent knows how to invoke the memory skill.
if store_py and os.path.isfile(store_py):
    parts.append("# Memory skill: invoke `%s <subcommand>` to recall/add/search memories." % store_py)
    # Check for promotion candidates (non-blocking, one-line suggestion).
    try:
        promote_out = subprocess.check_output(
            [sys.executable, store_py, "promote", "--dry-run"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8", "replace")
        # Extract the count from the first line.
        for line in promote_out.strip().split("\n"):
            if "promotion candidate" in line.lower():
                parts.append(line.strip())
                break
    except Exception:
        pass  # fail-open: promotion check errors never block session start

# Native-memory nudge (CC only, P6): one-time, best-effort, fail-open. Never
# raises - a missing/malformed settings.json just means the nudge stays quiet.
if host == "claude" and nudge_marker:
    try:
        already_shown = os.path.isfile(nudge_marker)
        native_disabled = bool(os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"))
        if not native_disabled and settings_dir:
            for fname in ("settings.json", "settings.local.json"):
                fpath = os.path.join(settings_dir, fname)
                try:
                    with open(fpath, encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict) and data.get("autoMemoryEnabled") is False:
                        native_disabled = True
                        break
                except (OSError, ValueError):
                    continue  # absent or malformed — treat as "not set here"
        if not already_shown and not native_disabled:
            # Attempt the exclusive marker create FIRST, and only append the
            # notice text in the process that actually won the create. This
            # is the order that matters: appending-then-racing-the-create (the
            # old order) let two concurrent sessions both queue the notice
            # before either one touched the marker, so both showed it and only
            # one "won" a create that, by then, was pointless. Deciding who
            # gets to show the notice before building any output closes that
            # race.
            show_notice = False
            try:
                os.makedirs(os.path.dirname(nudge_marker), exist_ok=True)
                # Atomic exclusive create (matches host.py _try_create_lock
                # pattern): two sessions starting concurrently could both see
                # the marker absent under a plain overwrite-open and both
                # show the nudge. O_EXCL makes only one winner.
                fd = os.open(nudge_marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, b"shown\n")
                finally:
                    os.close(fd)
                show_notice = True  # we won the race - we are the one to show it
            except FileExistsError:
                show_notice = False  # another process already showed it — no-op
            except OSError:
                # Marker could not be created for an unrelated reason (e.g. the
                # data dir is unwritable/read-only, or missing and could not be
                # made). Deliberate choice: show the notice anyway rather than
                # silently wedging the user out of it forever. Worst case here
                # is the notice repeats on a later session (annoying, visible,
                # self-correcting) instead of the alternative — a
                # never-writable marker path permanently suppressing a notice
                # about a real double-run risk. Fail-open toward showing.
                show_notice = True
            if show_notice:
                parts.append(
                    "# ZMem notice (one-time): Claude Code native memory still looks "
                    "enabled. ZMem is replacing it as your sole memory system - add "
                    "\"autoMemoryEnabled\": false to ~/.claude/settings.json so the two "
                    "systems do not double-run (a plugin cannot set this for you)."
                )
    except Exception:
        pass  # fail-open: nudge logic never blocks session start

ctx = "\n\n".join(parts) if parts else ""
# Soft budget cap (belt-and-suspenders; the launcher enforces the hard encoded
# budget). Trim raw content here so the payload is roughly bounded before the
# launcher re-measures the JSON-encoded envelope.
if budget > 0 and len(ctx) > budget:
    ctx = ctx[:budget] + "\n[recall truncated]"
print(json.dumps({"additionalContext": ctx}) if ctx else "{}")
' "$CORE_FILE_PY" "$AGENTS_FILE_PY" "$STORE_PY_PY" "$DATA_DIR_PY" "$PROJECT" "$DATA_DIR" "$NS" "$BUDGET" "$HOST" "$SETTINGS_DIR_PY" "$NUDGE_MARKER_PY" 2>/dev/null || echo '{}')"

# Neutralize any sentinel token a MEMORY'S OWN CONTENT happens to contain
# before wrapping. The launcher locates the payload by scanning stdout for the
# literal markers, so a stored memory containing "<<<ZMEM_JSON>>>" would move
# the extraction boundary into the middle of the JSON, the parse would fail,
# and the whole injection would silently degrade to {} (a self-DoS of this
# turn — fail-open, not an injection vector). Both replacements are safe
# inside the serialized JSON string: neither introduces a quote or a backslash.
CTX_JSON="${CTX_JSON//<<<ZMEM_JSON>>>/<<<ZMEM_JSON_NEUTRALIZED>>>}"
CTX_JSON="${CTX_JSON//<<<END>>>/<<<END_NEUTRALIZED>>>}"

# Wrap the payload in the <<<ZMEM_JSON>>>…<<<END>>> sentinel so the host adapter
# (zmem-launch.js) can extract it even if other stdout noise is present. The
# payload stays a bare {"additionalContext":…}; the launcher does host-envelope
# translation. Emitting the sentinel on its own line keeps extraction robust.
printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$CTX_JSON"
exit 0
