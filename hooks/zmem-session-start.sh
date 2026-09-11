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

# Shared tilde expansion for the DATA_DIR resolvers (one implementation for
# every bash resolver of the lane — zmem-convention-capture.sh sources it
# too; zmem_tilde_expand mutates DATA_DIR / DATA_DIR_IS_NATIVE). Sourced
# before DATA_DIR is resolved so zmem_tilde_expand is defined at the call
# site.
. "$(dirname "$0")/lib/zmem-tilde-expand.sh"

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
PROJECT="${ZMEM_PROJECT:-${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}}"

# Resolve the data dir with the SAME chain the shared hook body's _data_dir()
# uses (PRR-101 review: every artifact this hook writes — core.md, markers,
# the bg log — must co-locate with the store and the ops ring in every
# environment, including non-launcher plugin-data-only ones):
#   ZMEM_STORE > ZMEM_DATA > CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA > ~/.zmem
# (expanduser on the plugin-data values happens python-side via host.py /
# _data_dir(); this bash chain keeps the values verbatim like the writer).
DATA_DIR=""
if [ -n "${ZMEM_STORE:-}" ]; then
  # Normalize Windows separators before dirname (MSYS/Git Bash coreutils
  # treats `\` as a separator; WSL's does not — see convention-capture.sh).
  if [ "$IS_WINDOWS" -eq 1 ]; then
    DATA_DIR="$(dirname "$(printf '%s' "$ZMEM_STORE" | tr '\134' '/')")"
  else
    DATA_DIR="$(dirname "$ZMEM_STORE")"
  fi
elif [ -n "${ZMEM_DATA:-}" ]; then
  DATA_DIR="$ZMEM_DATA"
elif [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
  DATA_DIR="$CLAUDE_PLUGIN_DATA"
elif [ -n "${ZCODE_PLUGIN_DATA:-}" ]; then
  DATA_DIR="$ZCODE_PLUGIN_DATA"
fi
# Tilde-valued dirs (degenerate operator input — hosts send absolute paths):
# expand here exactly like convention-capture.sh does — the SHARED helper
# (sourced at the top of this script; one implementation for every bash
# resolver) — so core.md, markers, the bg log, and the ZMEM_DATA exported to
# children all land under the same expanded directory the python readers
# (host.py, _data_dir) use. python's output is native to the interpreter
# that consumes it.
DATA_DIR_IS_NATIVE=0
zmem_tilde_expand

if [ -n "$DATA_DIR" ]; then
  if [ "$DATA_DIR_IS_NATIVE" -eq 1 ]; then
    DATA_DIR_PY="$DATA_DIR"
  else
    DATA_DIR_PY="$(to_py_path "$DATA_DIR")"
  fi
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

# Tier-0 core.md: honor the canonical ZMEM_CORE_MD override exactly like
# host.resolve_core_md_path() does, so doctor's tier0-size check measures the
# same file this hook injects (PR feedback PRR-003 — the hook previously
# hardcoded <data dir>/core.md and silently ignored the documented override).
# Default is <store data dir>/core.md either way.
if [ -n "${ZMEM_CORE_MD:-}" ]; then
  CORE_FILE_PY="$(to_py_path "$ZMEM_CORE_MD")"
else
  CORE_FILE_PY="$(join_path "$DATA_DIR_PY" core.md)"
fi
STORE_PY_PY="$(join_path "$(to_py_path "$PLUGIN_ROOT")" skills memory scripts store.py)"

# First-run seeding: if core.md absent and template exists, copy it. Skipped
# when ZMEM_CORE_MD is set — the user manages an explicit override path, so
# seeding the default location would write a file this hook never injects and
# doctor never measures (final-critic round on PR feedback PRR-003).
if [ -z "${ZMEM_CORE_MD:-}" ] && [ -n "$PLUGIN_ROOT" ] && [ ! -f "$DATA_DIR/core.md" ] && [ -f "$PLUGIN_ROOT/templates/core.md.template" ]; then
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
# Session id for the bg-log decision line (issue #94): the env chain the
# other capture hooks use (launcher exports ZMEM_SESSION; the legacy vars
# cover manual/back-compat invocation). Empty when no host supplied it —
# the python block logs sid=unknown then.
SESSION_ID="${ZMEM_SESSION:-${CLAUDE_SESSION_ID:-${ZCODE_SESSION_ID:-}}}"
# Issue #118 (D-2 scope 1): the launcher exports the SessionStart payload's
# `source` field verbatim (startup | resume | clear | compact). The only
# branch is source == "compact" (post-compaction re-injection); every other
# value — and a host that sends no source at all — takes the cold-start lane
# unchanged. No resume handling (2026-09-10 amendment).
SOURCE="${ZMEM_SESSION_SOURCE:-}"
SETTINGS_DIR_PY="$(join_path "$(to_py_path "$HOME")" .claude)"
NUDGE_MARKER_PY="$(join_path "$DATA_DIR_PY" .native-nudge-shown)"

# Export the store location so store.py resolves it. Prefer canonical ZMEM_DATA
# (host adapter sets this to the box-wide ~/.zmem at cutover); keep the legacy
# ZCODE_PLUGIN_DATA export for back-compat. store.py's chain is
# ZMEM_STORE > ZMEM_DATA > CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA > ~/.zmem > ~/.zcode.
export ZMEM_DATA="${ZMEM_DATA:-$DATA_DIR}"
export ZCODE_PLUGIN_DATA="${ZCODE_PLUGIN_DATA:-}"

# Issue #107 (Workstream A PR 2): served-code drift check, BEFORE the payload
# block so it runs under ZMEM_INJECT=0 too (the disabled decision is still the
# session first logged decision). drift.py log-once is marker-guarded (at most
# one zmem-drift bg-log line per session id, exit 0 always — never blocking),
# and its JSON rides argv 13 into the payload block below so a drifted tree
# surfaces to the OPERATOR via systemMessage, never in additionalContext. The
# -f guard keeps a pre-0.17 tree (no drift.py) behaving exactly as before.
DRIFT_JSON=""
if [ -n "$PLUGIN_ROOT" ] && [ -f "$PLUGIN_ROOT/skills/memory/scripts/drift.py" ]; then
  # Python-native path (same to_py_path treatment as STORE_PY_PY): a raw
  # MSYS /c/... PLUGIN_ROOT is invisible to Windows Python and would
  # silently disable drift detection on Git Bash manual installs.
  DRIFT_PY="$(join_path "$(to_py_path "$PLUGIN_ROOT")" skills memory scripts drift.py)"
  # Bounded like the recall-body fallback (10s vs its 5s: the session path
  # gets slightly more headroom). `timeout` is GNU coreutils — present in
  # Git Bash, absent on stock macOS, where the call runs unguarded exactly
  # as before (fail-open).
  if command -v timeout >/dev/null 2>&1; then
    DRIFT_JSON="$(timeout 10 "$PYTHON_BIN" "$DRIFT_PY" log-once \
      --data-dir "$DATA_DIR_PY" --sid "$SESSION_ID" 2>/dev/null || true)"
  else
    DRIFT_JSON="$("$PYTHON_BIN" "$DRIFT_PY" log-once \
      --data-dir "$DATA_DIR_PY" --sid "$SESSION_ID" 2>/dev/null || true)"
  fi
fi

# Background sleep-time organization: fully detached, fire-and-forget. stdio is
# redirected to the $BG_SINK maintenance log below (a best-effort log file that
# falls back to /dev/null when the data dir is unwritable) so it (a) can't
# pollute the launcher's piped stdout buffer and (b) doesn't hold the launcher's
# read pipe open — the launcher gets EOF the moment THIS script exits. No
# wait/kill loop: blocking up to 5s here is exactly the ~5s session-start stall
# Phase 3 removes; organize shares consolidate's growth-threshold + interval
# gate, so an orphaned run is safe.
#
# Auto-snapshot (P11) rides the exact same detachment discipline for the exact
# same reasons: fully redirected stdio so nothing can leak into the
# <<<ZMEM_JSON>>>…<<<END>>> payload the launcher parses (it reads stdout to
# EOF), and no wait/kill loop so session start never gains latency. `--if-due`
# makes it a cheap no-op almost every session — it only snapshots once per
# $ZMEM_BACKUP_INTERVAL_DAYS (default 1). Both commands take their own
# single-flight lock, so several sessions starting at once produce one
# organization run and one snapshot, not N of each.
#
# `sweep` (issue #23) prunes the per-session cooldown markers the capture/
# convention hooks leave behind, so they cannot accumulate unboundedly in the
# data dirs. Its stdio is deliberately redirected exactly like its siblings so
# nothing can leak into the <<<ZMEM_JSON>>>…<<<END>>> payload the launcher
# parses. It takes NO advisory lock — the sweep is store-independent and
# idempotent (listdir + per-file unlink), so concurrent sweeps from two sessions
# starting at once are safe by construction; do not "harden" it with the
# consolidate/backup single-flight unless a real race is demonstrated.
if [ -n "$STORE_PY_PY" ] && [ -f "$STORE_PY_PY" ]; then
  # Background maintenance log (#37 L22): organize/backup/sweep previously
  # redirected to /dev/null, which hid cadence skips and errors completely —
  # an operator had no way to tell maintenance had silently stopped running.
  # Now the three detached jobs append to a log file under the data dir (shell
  # `>>` opens with O_APPEND, so concurrent appends from the three jobs never
  # corrupt — lines may interleave, never tear). `ZMEM_BG_LOG=0` restores the
  # old silent (/dev/null) behavior. Best-effort: if the data dir is missing
  # or the path is unwritable, the redirect target falls back to /dev/null so
  # the hook never wedges session-start on a logging failure. The log is
  # unbounded by design — operators truncate or rotate it manually (it only
  # grows when a maintenance job actually runs, which is cadence-gated, so in
  # steady state it gains a handful of lines per day). Note: the log captures
  # the maintenance commands' stdout/stderr, which may include absolute store
  # paths and snapshot filenames — it is a plaintext file under the (typically
  # owner-only) data dir, and ZMEM_BG_LOG=0 disables it entirely if the info
  # surface is undesirable on a shared/co-located box (PRR-011).
  # Issue #129: rotation helper for the sink, defined ABOVE the assignment
  # block on purpose — source-text extractors (the L22 behavioral test) cut
  # that block at the first python-interpreter invocation after the sink
  # assignment, so this literal must not appear between the two.
  zmem_rotate_maintenance_sink() {
    "$PYTHON_BIN" -c 'import sys; sys.path.insert(0, sys.argv[1]); from storelib.log_rotate import rotate_on_append; rotate_on_append(sys.argv[2])' "$(dirname "$STORE_PY_PY")" "$1" 2>/dev/null || true
  }
  BG_SINK="/dev/null"
  if [ "${ZMEM_BG_LOG:-1}" != "0" ] && [ -n "$DATA_DIR" ]; then
    BG_LOG_PATH="$DATA_DIR/zmem-bg.log"
    # Ensure the dir exists, is writable, AND the log file itself is appendable
    # before redirecting into it. The `{ : 2>/dev/null >>file ; }` probe opens
    # the file for append (creating it if absent) with stderr silenced FIRST —
    # if an EXISTING log file is read-only or locked, the probe fails quietly
    # and we fall through to /dev/null rather than leaking a "Permission denied"
    # to the hook's stderr or letting the later `>>"$BG_SINK"` redirect fail
    # silently and drop all maintenance output (PRR-004). Strict conjunction
    # (no `||`) so any failure falls through to /dev/null.
    if mkdir -p "$DATA_DIR" 2>/dev/null && [ -w "$DATA_DIR" ] && { : 2>/dev/null >>"$BG_LOG_PATH"; }; then
      BG_SINK="$BG_LOG_PATH"
      # Issue #129: rotate the maintenance sink before the detached worker
      # redirects into it — bounded segments instead of unbounded growth.
      # Size-gated so steady state pays only a wc -c. Fail-open: if the
      # helper call fails the worker appends to the existing file.
      if [ -f "$BG_LOG_PATH" ] && [ "$(wc -c < "$BG_LOG_PATH" 2>/dev/null || echo 0)" -gt "${ZMEM_BG_LOG_MAX_BYTES:-262144}" ]; then
        zmem_rotate_maintenance_sink "$BG_LOG_PATH"
      fi
    fi
  fi
  # Batch the three cadence ops into ONE detached python process (#39 E9):
  # organize (issue #62 7.7 — SessionStart invokes `session-cadence`, whose
  # sleep-time maintenance op is now ORGANIZE, not consolidate) + backup
  # --if-due + sweep. Each keeps its own cadence gate / single-flight lock
  # inside session-cadence, so this is behavior-equivalent to the former
  # three-line spawn but starts one interpreter instead of three.
  #
  # The 15s startup delay is a RACE GUARD, not cosmetic (PRR-101 CI
  # evidence): the Tier-2 recall below reads the SAME store, and this
  # worker — fired fire-and-forget — organizes/backs up/consolidates it
  # concurrently. On slow runners the worker grabbed the database while the
  # recent read was in flight, and the fail-open handling silently dropped
  # the whole Tier-2 block (inject AND its bg-log decision line) on roughly
  # every other CI run. Deferring the start gives the bounded recall a
  # guaranteed head start; maintenance is background housekeeping, so the
  # shift is invisible.
  # NOTE: the wrapper must exec store.py THROUGH the interpreter
  # ([sys.executable] + argv) — a bare subprocess.call(argv) would exec the
  # .py file directly, which fails with ENOEXEC/WinError 193 everywhere
  # (store.py has no shebang) and, being fire-and-forget with output in
  # BG_SINK, would silently kill maintenance on every box (reviewer gate
  # caught this pre-merge; CI was green because no test inspected the
  # worker's output — the test below now does).
  "$PYTHON_BIN" -c 'import time, subprocess, sys; time.sleep(15); sys.exit(subprocess.call([sys.executable] + sys.argv[1:]))' \
    "$STORE_PY_PY" session-cadence \
    --backup-retention "${ZMEM_BACKUP_RETENTION:-7}" >>"$BG_SINK" 2>&1 &
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
# The payload block lives in hooks/lib/zmem-session-start-payload.py —
# PR #190 review: the inline `python -c` form grew past the Windows
# CreateProcess ~32K command-line limit and silently degraded to `{}`
# (the spawn failure was swallowed by the || echo fallback). A real
# file removes that ceiling; the argv contract is unchanged.
HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD_PY="$(to_py_path "$(join_path "$HOOKS_DIR" lib zmem-session-start-payload.py)")"
CTX_JSON="$("$PYTHON_BIN" "$PAYLOAD_PY" "$CORE_FILE_PY" "$AGENTS_FILE_PY" "$STORE_PY_PY" "$DATA_DIR_PY" "$PROJECT" "$DATA_DIR" "$NS" "$BUDGET" "$HOST" "$SETTINGS_DIR_PY" "$NUDGE_MARKER_PY" "$SESSION_ID" "$DRIFT_JSON" "$SOURCE" 2>/dev/null || echo '{}')"

# Neutralize any sentinel token a MEMORY'S OWN CONTENT happens to contain
# before wrapping. The launcher locates the payload by scanning stdout for the
# literal markers, so a stored memory containing "<<<ZMEM_JSON>>>" would move
# the extraction boundary into the middle of the JSON, the parse would fail,
# and the whole injection would silently degrade to {} (a self-DoS of this
# turn — fail-open, not an injection vector). Both replacements are safe
# inside the serialized JSON string: neither introduces a quote or a backslash.
CTX_JSON="${CTX_JSON//<<<ZMEM_JSON>>>/<<<ZMEM_JSON_NEUTRALIZED>>>}"
CTX_JSON="${CTX_JSON//<<<END>>>/<<<END_NEUTRALIZED>>>}"
# I7 critic-fix (issue #58, 3.5): also neutralize the new fence markers.
CTX_JSON="${CTX_JSON//<<<ZMEM_UNTRUSTED_FENCE>>>/<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"
CTX_JSON="${CTX_JSON//<<<END_ZMEM_UNTRUSTED_FENCE>>>/<<<END_ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"

# Wrap the payload in the <<<ZMEM_JSON>>>…<<<END>>> sentinel so the host adapter
# (zmem-launch.js) can extract it even if other stdout noise is present. The
# payload stays a bare {"additionalContext":…}; the launcher does host-envelope
# translation. Emitting the sentinel on its own line keeps extraction robust.
printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$CTX_JSON"
exit 0
