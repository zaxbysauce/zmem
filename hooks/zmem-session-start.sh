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
  # Background maintenance log (#37 L22): consolidate/backup/sweep previously
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
    fi
  fi
  # Batch the three cadence ops into ONE detached python process (#39 E9):
  # consolidate + backup --if-due + sweep. Each keeps its own cadence gate /
  # single-flight lock inside session-cadence, so this is behavior-equivalent
  # to the former three-line spawn but starts one interpreter instead of three.
  "$PYTHON_BIN" "$STORE_PY_PY" session-cadence \
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
# The recent floor reads ZMEM_INJECT_FLOOR_RECENT (default 0.5) — the SAME
# env var the shared body reads — so operator tuning applies uniformly
# (final-critic round-3 fix; was a hardcoded "0.5" literal).
if store_py and os.path.isfile(store_py):
    try:
        _rf_raw = os.environ.get("ZMEM_INJECT_FLOOR_RECENT", "")
        try:
            _recent_floor = float(_rf_raw) if _rf_raw else 0.5
        except ValueError:
            _recent_floor = 0.5
        out = subprocess.check_output(
            [sys.executable, store_py, "recent", "--namespace", ns, "--limit", "3", "--min-confidence", str(_recent_floor), "--include-global", "--global-limit", "2", "--no-bump", "--json"],
            stderr=subprocess.DEVNULL, timeout=8,
        ).decode("utf-8", "replace")
        rows = json.loads(out) if out.strip() else []
        if rows:
            # Issue #58, 3.5: wrap Tier 2 in the same non-executable
            # fence + provenance render that zmem-recall uses. The
            # gate + fence helpers live in storelib. The gate is a
            # local mirror of hooks/lib/zmem-recall-body.py without
            # the log-write side effect (hook-only). Floors read the
            # SAME env vars with the SAME defaults as the shared body
            # so operator tuning applies uniformly (final-critic
            # round-2 fix).
            try:
                sys.path.insert(0, os.path.dirname(store_py))
                sys.path.insert(0, os.path.join(os.path.dirname(store_py), "storelib"))
                from storelib import _format_fenced_recall

                def _env_floor(name, default):
                    raw = os.environ.get(name, "")
                    if not raw:
                        return default
                    try:
                        return float(raw)
                    except ValueError:
                        return default

                _floor_prompt = _env_floor("ZMEM_INJECT_FLOOR_PROMPT", 0.25)
                _floor_gate_none = _env_floor("ZMEM_INJECT_FLOOR_GATE_NONE", 0.4)
                # Grounded signals (test/compile/lint/reviewer/user) inject
                # at the prompt floor; ONLY signal=none is tightened to the
                # gate-none floor (issue #58 3.8 — mirror of the shared
                # body gate; dropping `user` here broke the launcher
                # sentinel round-trip canary).
                _grounded = {"test", "compile", "lint", "reviewer", "user"}
                _selected = []
                for r in rows:
                    try:
                        _conf = float(r.get("confidence", 0) or 0)
                    except (TypeError, ValueError):
                        _conf = 0.0
                    _sig = (r.get("signal") or "none").lower()
                    if _sig == "none":
                        if _conf >= _floor_gate_none:
                            _selected.append(r)
                    elif _sig in _grounded and _conf >= _floor_prompt:
                        _selected.append(r)
                rows = _selected
                if rows:
                    block = _format_fenced_recall(
                        rows,
                        header=(
                            f"Recent memories (Tier 2 — namespace {ns}). "
                            f"High-confidence admin pull. Consider if relevant; ignore if not."
                        ),
                    )
                    parts.append(block)
            except Exception:
                # Storage helper import failed — fall back to the
                # legacy unfenced render. Fail-open: session-start is
                # non-blocking.
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

# Pending live-capture correction candidates (issue #47): surface a COUNT only
# (budget — never the contents) so the agent knows the queue has candidates for
# the closeout skill to review. The queue is a fail-open sidecar; a read error
# must never block session start.
if store_py and os.path.isfile(store_py):
    try:
        sys.path.insert(0, os.path.dirname(store_py))
        import correction_queue as _cq
        # Surface a COUNT of PENDING (non-stale) candidates. Stale items are
        # already past decay and are pruned by closeout `--drop-stale`, so they
        # are not "pending review" - do not nudge the agent about them.
        _pending = sum(1 for _it in _cq.load_queue(ns) if not _it.get("stale"))
        if _pending:
            # PREPEND (not append): the payload is truncated with ctx[:budget],
            # which keeps the FRONT and drops the tail, so an appended note would
            # vanish whenever Tier 0 + recall fill the budget. Leading placement
            # guarantees the agent sees the closeout-work nudge.
            parts.insert(
                0,
                "zmem: %d captured correction candidate(s) pending review — "
                "run the closeout skill to process." % _pending
            )
    except Exception:
        pass  # fail-open: queue read errors never block session start

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
# Final-critic round-2 fix: a naive slice can cut mid-Tier-2-block and drop
# the <<<END_ZMEM_UNTRUSTED_FENCE>>> closer, leaving a dangling fence opener
# in the injected context. If a truncation would split a fence, cut at the
# fence closer instead (the block is simply shorter, never unclosed).
if budget > 0 and len(ctx) > budget:
    _closer = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
    _cut = ctx[:budget]
    _last_open = _cut.rfind("<<<ZMEM_UNTRUSTED_FENCE>>>")
    _last_close_in_cut = _cut.rfind(_closer)
    if _last_open > _last_close_in_cut:
        # The cut lands inside a fence: KEEP the partial body (up to
        # the cut) and append the closer so the fence is complete —
        # never an unclosed or orphaned marker (final-critic round-3
        # fix: the previous branch dropped the opener and emitted a
        # dangling closer).
        ctx = _cut.rstrip() + "\n" + _closer + "\n[recall truncated]"
    else:
        ctx = _cut + "\n[recall truncated]"
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
# I7 critic-fix (issue #58, 3.5): also neutralize the new fence markers.
CTX_JSON="${CTX_JSON//<<<ZMEM_UNTRUSTED_FENCE>>>/<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"
CTX_JSON="${CTX_JSON//<<<END_ZMEM_UNTRUSTED_FENCE>>>/<<<END_ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>}"

# Wrap the payload in the <<<ZMEM_JSON>>>…<<<END>>> sentinel so the host adapter
# (zmem-launch.js) can extract it even if other stdout noise is present. The
# payload stays a bare {"additionalContext":…}; the launcher does host-envelope
# translation. Emitting the sentinel on its own line keeps extraction robust.
printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$CTX_JSON"
exit 0
