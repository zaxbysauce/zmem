#!/usr/bin/env bash
# zmem-session-start.sh — ZCode SessionStart hook for ZMem (plugin version).
#
# Injects Tier 0 memory (core.md + project AGENTS.md) and a bounded recall of
# Tier 2 semantic memories into the conversation as additionalContext at session
# start. Non-blocking: always exits 0.
#
# Plugin paths: ZCODE_PLUGIN_ROOT (scripts, templates) and ZCODE_PLUGIN_DATA
# (store.sqlite, core.md — writable per-plugin data dir) are injected by the
# ZCode runner for plugin hooks. Falls back to ~/.zcode/memory for manual installs.
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
    sep='\\'
  else
    sep='/'
  fi
  printf '%s' "$base"
  for part in "$@"; do
    printf '%s%s' "$sep" "$part"
  done
}

PLUGIN_ROOT="${ZCODE_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
DATA_DIR="${ZCODE_PLUGIN_DATA:-}"
PROJECT="${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}"

# Resolve the data dir: plugin data dir if running as plugin, else ~/.zcode/memory.
if [ -n "$DATA_DIR" ]; then
  DATA_DIR_PY="$(to_py_path "$DATA_DIR")"
else
  DATA_DIR="$HOME/.zcode/memory"
  DATA_DIR_PY="$(join_path "$(to_py_path "$HOME")" .zcode memory)"
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

# Resolve project AGENTS.md.
AGENTS_FILE_PY=""
if [ -n "$PROJECT" ]; then
  AGENTS_FILE_PY="$(join_path "$(to_py_path "$PROJECT")" AGENTS.md)"
fi

# Export env so store.py finds its store via ZCODE_PLUGIN_DATA.
export ZCODE_PLUGIN_DATA="${ZCODE_PLUGIN_DATA:-$DATA_DIR}"

# Build the additionalContext payload using python for guaranteed-valid JSON.
CTX_JSON="$("$PYTHON_BIN" -c '
import json, os, sys, subprocess

core = sys.argv[1]
agents = sys.argv[2]
store_py = sys.argv[3]
home_win = sys.argv[4]
project = sys.argv[5]
data_dir = sys.argv[6]

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

# Tier 2: bounded recall — cheap admin pull of recent high-confidence live memories.
if store_py and os.path.isfile(store_py):
    ns = "user:global"
    if project:
        ns = "project:" + os.path.basename(project)
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

ctx = "\n\n".join(parts) if parts else ""
print(json.dumps({"additionalContext": ctx}) if ctx else "{}")
' "$CORE_FILE_PY" "$AGENTS_FILE_PY" "$STORE_PY_PY" "$DATA_DIR_PY" "$PROJECT" "$DATA_DIR" 2>/dev/null || echo '{}')"

printf '%s\n' "$CTX_JSON"
exit 0
