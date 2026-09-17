#!/usr/bin/env python3
"""zmem per-host injection canary (issue #108).

Post-install verification that a host actually fires zmem's SessionStart hook
and that the injected fence carries a known row. Runs against an ISOLATED
scratch store — the real store (~/.zmem or any ambient ZMEM_STORE) is never
read or written: every store-affecting ambient var is stripped before any
child runs and the fixture vars are set explicitly (the ZMEM_STORE >
ZMEM_DATA > CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA precedence trap in
host.resolve_store_path cannot let an ambient var win).

Two modes:

--self-test   Deterministic (no host binary): seeds the scratch store, then
              fabricates the host's SessionStart hook-event payload and drives
              the SAME hook chain the host drives (``node
              <plugin-root>/hooks/zmem-launch.js session-start``) with the
              host-detection env var the launcher's detectHost reads
              (CLAUDE_PLUGIN_ROOT / PLUGIN_ROOT / ZCODE_PLUGIN_ROOT, or
              ZMEM_HOST=hermes — hermes' real delivery is adapter/MCP based
              (#122); its self-test lane checks the shared hook machinery
              under hermes identity).

--compact-self-test
              Deterministic compaction lane (issue #118): after seeding,
              drives the full compaction sequence through the same launcher —
              ``precompact`` (ledger snapshot + clear) → ``postcompact``
              (stash compact_summary) → ``session-start`` with
              source=compact (query-aware re-injection). Passes only when a
              FRESH decision line carrying ``moment=session_start_compact``
              grounds the seeded row and the rendered fence carries the
              marker — the precompact moment's own decision line never
              satisfies the lane (it is not the moment under test).

live (default)
              Seeds the scratch store, then runs a minimal non-interactive
              session of the real host binary in the scratch workdir. Host
              binary resolution: $ZMEM_CANARY_HOST_BIN (literal path if it is
              a regular file; extension-less dir-bearing paths resolve via
              PATHEXT; bare names via PATH lookup — see _resolve_override;
              a directory or other non-runnable path never resolves), else
              PATH lookup of the host's default binary name. Absent binary =>
              verdict=skip reason=host-binary-absent, exit 0.

Assertions on every run (post-install canary semantics):
  1. a FRESH ``zmem-hook status=... reason=...`` decision line appears in the
     scratch zmem-bg.log after the drive (no fresh line => fail
     hook-not-fired, exit 2);
  2. the seeded row's UUID appears in the line's ``ids=[...]`` FIELD —
     otherwise fail no-row-id, exit 3. Only ids= counts: a row present
     solely in the pre-gate ``all=[...]`` candidate list was filtered out
     before injection and never grounds a pass. On the codex lane the
     ``.codex-plugin/plugin.json`` manifest is additionally checked against
     the ``./`` contract BEFORE the drive (violation => fail
     reason=codex-manifest-contract, exit 2 — codex-cli >= 0.153.0 would
     silently ignore those hooks). In --self-test mode the rendered fence
     (the launcher envelope's additionalContext) must additionally contain
     the marker text; live hosts consume the envelope themselves, so there
     the decision line's ids are the fence proxy (the marker check is
     best-effort on stdout).

The served-tree drift status from issue #107 is printed in the verdict line
(``drift=matched|drifted|unknown``) via skills/memory/scripts/drift.py check;
it is informational and never gates the verdict.

Output contract — the LAST stdout line is always::

    zmem-canary host=<h> mode=<live|self-test|probe> verdict=<pass|fail|skip>
    reason=<slug|-> store=<path> row_id=<uuid|none> drift=<matched|drifted|unknown>

Exit codes: 0 pass/skip; 2 hook-not-fired (including a codex lane whose
manifest violates the ./ contract: reason=codex-manifest-contract);
3 no-row-id; 4 seed-failed; 5 host-session-unsupported.

Runs standalone: python scripts/host_canary.py --host claude --self-test
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Protocol

REPO_ROOT = Path(__file__).resolve().parents[1]
MARKER = "zmem-canary-probe-row"

# Ambient vars stripped by EXACT NAME before any child runs: every
# store-affecting var AND every launcher host-detection var — the launcher's
# detectHost precedence (ZMEM_HOST > PLUGIN_ROOT/PLUGIN_DATA >
# CLAUDE_PLUGIN_ROOT > ZCODE_PLUGIN_ROOT) would otherwise let an ambient var
# from the surrounding host session win over the canary's chosen --host and
# drive the wrong host and plugin tree. build_child_env re-sets ONLY the
# chosen host's var after the strip.
STRIP_VARS = (
    "ZMEM_STORE",
    "ZMEM_DATA",
    "CLAUDE_PLUGIN_DATA",
    "ZCODE_PLUGIN_DATA",
    "ZMEM_INJECT",
    "ZMEM_TIER0",
    "ZMEM_CORE_MD",
    "ZMEM_NAMESPACE",
    "ZMEM_HOST",
    "HERMES_HOME",
    "PLUGIN_ROOT",
    "PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT",
    "ZCODE_PLUGIN_ROOT",
)

HOSTS = ("claude", "codex", "zcode", "hermes")
HOST_DETECT_ENV = {
    "claude": "CLAUDE_PLUGIN_ROOT",
    "codex": "PLUGIN_ROOT",
    "zcode": "ZCODE_PLUGIN_ROOT",
    # hermes: the launcher's detectHost honors an explicit ZMEM_HOST.
    "hermes": None,
}
HOST_BINARIES = {"claude": "claude", "codex": "codex", "zcode": "zcode", "hermes": "hermes"}

DECISION_LINE_RE = re.compile(r"\[\d+\] zmem-hook status=\S+ reason=\S+.*ids=\[[^\]]*\]")

# The ids FIELD only. In the decision line, ids=[...] is the POST-gate row set
# while all=[...] is the PRE-gate candidate list (zmem-session-start.sh) — a
# row present only in all=[...] was filtered out before injection, so
# grounding must never read it (a whole-line substring check would let a
# gate/budget-filtered row false-pass — the silent-no-injection class this
# canary exists to catch).
IDS_FIELD_RE = re.compile(r"\bids=(\[[^\]]*\])")


def line_ids_ground_row(line, row_id):
    """True iff the decision line's ids=[...] field carries row_id."""
    m = IDS_FIELD_RE.search(line)
    return bool(m) and row_id in m.group(1)

EXIT_HOOK_NOT_FIRED = 2
EXIT_NO_ROW_ID = 3
EXIT_SEED_FAILED = 4
EXIT_SESSION_UNSUPPORTED = 5


def build_child_env(host, plugin_root, data_dir):
    env = {k: v for k, v in os.environ.items() if k not in STRIP_VARS}
    env["ZMEM_STORE"] = str(Path(data_dir) / "store.sqlite")
    env["ZMEM_DATA"] = str(data_dir)
    detect = HOST_DETECT_ENV.get(host)
    if detect:
        env[detect] = str(plugin_root)
    else:
        env["ZMEM_HOST"] = host
    return env


def import_host_module(plugin_root):
    sys.path.insert(0, str(Path(plugin_root) / "skills" / "memory" / "scripts"))
    try:
        import host  # plugin-tree import; path set above

        return host
    finally:
        sys.path.pop(0)


def resolve_namespace(host_mod, workdir):
    # Same function the launcher's resolveNamespace runs via subprocess; the
    # canary calls it in-process so the seeded key is byte-identical to the
    # key the hook recalls under.
    return host_mod.resolve_namespace(Path(workdir))


def run_drift_check(plugin_root):
    drift_py = Path(plugin_root) / "skills" / "memory" / "scripts" / "drift.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(drift_py), "check", "--root", str(plugin_root)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                status = json.loads(line).get("status")
                if status in ("matched", "drifted", "unknown"):
                    return status
    except Exception as exc:  # drift never gates the verdict
        print("zmem-canary: drift check degraded (%s)" % type(exc).__name__, file=sys.stderr)
    return "unknown"


def seed_row(env, plugin_root, namespace, data_dir):
    store_py = Path(plugin_root) / "skills" / "memory" / "scripts" / "store.py"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(store_py),
                "add",
                "--namespace",
                namespace,
                "--type",
                "fact",
                "--content",
                "%s host canary marker" % MARKER,
                "--signal",
                "test",
                "--source-ref",
                "issue-108-canary",
                "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(data_dir),
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        # Locked store / slow disk: degrade to the documented seed-failed
        # contract (verdict line + exit 4), never a bare traceback.
        print("zmem-canary: seed failed — timed out after 120s", file=sys.stderr)
        return None
    except OSError as exc:
        print("zmem-canary: seed failed — cannot spawn store.py (%s)" % exc, file=sys.stderr)
        return None
    if proc.returncode != 0:
        print("zmem-canary: seed failed rc=%d" % proc.returncode, file=sys.stderr)
        print((proc.stderr or "")[-800:], file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])["id"]
    except (ValueError, KeyError, IndexError):
        print("zmem-canary: seed output not parseable", file=sys.stderr)
        return None


def fresh_decision_line(bg_log, pre_size, must_contain=""):
    if not bg_log.exists():
        return None
    raw = bg_log.read_bytes()
    data = raw.decode("utf-8", errors="replace")
    for line in reversed(data.splitlines()):
        if DECISION_LINE_RE.search(line) and (
                not must_contain or must_contain in line):
            # A fresh line must postdate the pre-drive snapshot; identical old
            # lines (same content) can only appear if the log grew. Compare in
            # BYTES (st_size domain) — len(data) counts characters, and a
            # pre-existing log with multi-byte UTF-8 content would otherwise
            # make a genuinely fresh line read as stale.
            return line if len(raw) > pre_size else None
    return None


def envelope_additional_context(stdout_text):
    text = (stdout_text or "").strip()
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            envelope = json.loads(line)
        except ValueError:
            continue
        inner = envelope.get("hookSpecificOutput") or {}
        return inner.get("additionalContext") or envelope.get("additionalContext") or ""
    return ""


def verdict_line(host, mode, verdict, reason, store, row_id, drift):
    print(
        "zmem-canary host=%s mode=%s verdict=%s reason=%s store=%s row_id=%s drift=%s"
        % (host, mode, verdict, reason, store, row_id or "none", drift)
    )


# ---------------------------------------------------------------------------
# Issue #96: the nine live host-canary lanes
#
# Each lane is a self-contained probe that runs against an ISOLATED --data-dir
# fixture (build_child_env strip + explicit set, reused from the legacy
# modes), records a schema-valid result artifact (scripts/canary-schema.json,
# enforced by --validate-result), derives its hook identifiers from the real
# host manifests (never invented), and proves via four-root before/after
# inventory snapshots that only the isolated canary store changed.
#
# Lane-mode exit codes differ from the legacy modes BY CONTRACT (issue #96
# success recipe: "exit code 0 for every command"): a completed lane always
# writes its artifact and exits 0 — pass, structured fail, and skip alike.
# The verdict lives in the artifact; only usage errors (exit 2) and a result
# that could not be produced at all exit nonzero. Consumers must read the
# artifact, never $?
# ---------------------------------------------------------------------------

LANE_HOSTS = {
    "hermes-gateway": "hermes",
    "hermes-provider-mode": "hermes",
    "hermes-compat-mode": "hermes",
    "claude-compact": "claude",
    "codex-trust": "codex",
    "zcode-duplicate": "zcode",
    "exec-form-claude": "claude",
    "exec-form-codex": "codex",
    "exec-form-zcode": "zcode",
}
HERMES_LANE_MODE = {
    "hermes-gateway": "gateway",
    "hermes-provider-mode": "provider",
    "hermes-compat-mode": "compatibility",
}
CANARY_SCHEMA_PATH = REPO_ROOT / "scripts" / "canary-schema.json"
CANARY_NS = "project:fixture-96"
CANARY_SESSION_ID = "00000000-0000-4000-8000-000000000096"
CANARY_TIMESTAMP = "2026-09-10T00:00:00Z"
CANARY_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "canary"

# A manifest command is interpreter exec-form (`node "<root>/hooks/
# zmem-launch.js" <sub>`), so the hook identity is the SCRIPT basename, not
# the interpreter's: when argv[0]'s stem is one of these, the basename comes
# from the next argument. A process-form (array) hook derives from args[0].
INTERPRETER_STEMS = {"node", "bash", "sh", "python", "python3",
                     "pwsh", "powershell"}
VERSION_TOKEN_RE = re.compile(r"\b(v?\d+(?:\.\d+)+)\b")


class Scheduler(Protocol):
    """Virtual-clock scheduling seam (FakeExecutor in tests implements it)."""

    def submit(self, fn, delay_s):
        ...

    def advance(self, seconds):
        ...

    def now(self):
        ...


class DeadlineExecutor(Protocol):
    """Run fn under a deadline; cancel it and return None at the deadline.

    The production implementation terminates the child through the cancel
    hook carried by fn; a timed-out child can never cause a canary result to
    be written (run_command returns None and the lane records a structured
    failure instead). Tests inject FakeExecutor (tests/support/
    fake_executor.py) — locally defined pending #160's transport
    abstraction; see the migration note in the issue #96 trace.
    """

    def run(self, fn, deadline_s):
        ...


class CommandRunner(Protocol):
    def __call__(self, argv, *, input_bytes, env, cwd, deadline_s,
                 deadline=None):
        ...


class GatewayHarness(Protocol):
    def start(self, argv, env, cwd):
        ...

    def inject_event(self, payload):
        ...

    def stop(self):
        ...


class InteractiveSession(Protocol):
    def send(self, data):
        ...

    def capture(self):
        ...

    def close(self):
        ...


class SessionFactory(Protocol):
    def __call__(self, executable, *, env, cwd, deadline_s):
        ...


class _ChildCall:
    """A blocking child wait wrapped as a cancellable callable for the
    deadline executor: ``cancel`` terminates the child so ``communicate``
    unblocks, and ``reap`` waits for it so a deadline hit never leaves a
    zombie or open pipe handles behind."""

    def __init__(self, proc):
        self._proc = proc

    def __call__(self):
        out, err = self._proc.communicate()
        return out, err, self._proc.returncode

    def cancel(self):
        try:
            self._proc.kill()
        except OSError:
            pass

    def reap(self):
        try:
            self._proc.wait(timeout=10)
        except Exception:
            pass


class ThreadDeadlineExecutor:
    """Production DeadlineExecutor: fn runs on a worker thread; at the
    deadline its cancel hook fires and run returns None."""

    def run(self, fn, deadline_s):
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(fn)
            try:
                return future.result(timeout=deadline_s)
            except concurrent.futures.TimeoutError:
                pass
            cancel = getattr(fn, "cancel", None)
            if cancel is not None:
                try:
                    cancel()
                except Exception:
                    pass
            try:
                future.result(timeout=15)
            except Exception:
                pass
            return None
        finally:
            pool.shutdown(wait=False)


def _deadline_executor(deadline=None):
    return deadline if deadline is not None else ThreadDeadlineExecutor()


def run_command(argv, *, input_bytes, env, cwd, deadline_s, deadline=None):
    """One deadline-bounded child run; None means the deadline hit."""
    executor = _deadline_executor(deadline)
    argv = [str(a) for a in argv]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(cwd),
        )
    except (OSError, ValueError):
        return None
    call = _ChildCall(proc)
    if input_bytes is not None:
        try:
            proc.stdin.write(input_bytes)
            proc.stdin.close()
        except (OSError, ValueError):
            call.cancel()
    result = executor.run(call, deadline_s)
    if result is None:
        call.reap()
        return None
    out, err, rc = result
    return subprocess.CompletedProcess(argv, rc, out, err)


def _as_bytes(value):
    """Tolerate str|bytes from injected runners (test doubles may return
    either); the production Popen surface always yields bytes."""
    if value is None:
        return b""
    if isinstance(value, str):
        return value.encode("utf-8")
    return value


def _extract_version_line(stdout_text):
    """First stdout line carrying a version token, or None.

    Only a line the supported invocation actually emitted counts; a host
    that prints no version-shaped line yields None and the lane records the
    structured ``version-unavailable`` failure (never an undocumented
    --version flag)."""
    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if line and VERSION_TOKEN_RE.search(line):
            return line
    return None


def _exe_sha256(binpath):
    try:
        return hashlib.sha256(Path(binpath).read_bytes()).hexdigest()
    except OSError:
        return None


def _shell_tokens(command):
    """Shell-split a manifest command keeping Windows backslash paths intact:
    non-posix split keeps quoted spans, then surrounding quotes are stripped
    per token."""
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        parts = command.split()
    out = []
    for part in parts:
        if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'":
            part = part[1:-1]
        if part:
            out.append(part)
    return out


def _argv_basename(tokens):
    if not tokens:
        return None
    first = Path(tokens[0])
    if first.stem.lower() in INTERPRETER_STEMS and len(tokens) > 1:
        return Path(tokens[1]).name
    return first.name


def _command_basename(command):
    return _argv_basename(_shell_tokens(command))


def _command_tokens(command):
    """Full argv token list for a manifest command value (issue #186).

    Exec-form entries carry ``command: "node"`` plus an ``args`` array; the
    tokens are their concatenation. Shell-form entries (Codex/ZCode until
    #187/#188 convert them) are shell-split as before. Accepts either the
    raw command value or its args list alongside via the two-argument form.
    """
    if isinstance(command, dict):
        tokens = _shell_tokens(str(command.get("command", "")))
        args = command.get("args")
        if isinstance(args, list):
            tokens = tokens + [str(a) for a in args]
        return tokens
    return _shell_tokens(str(command))


def _manifest_commands(host, plugin_root=None):
    """Flatten a host manifest to (event, command-object) pairs.

    The real manifests are two-level: ``hooks.<Event>`` is a list of
    matcher entries, each carrying its own ``hooks`` list of
    ``{"type": "command", "command": ..., "timeout": ...}`` objects. A flat
    command object directly under the event (a defensive shape) is accepted
    too.
    """
    root = Path(plugin_root) if plugin_root else REPO_ROOT
    manifest = root / "hooks" / ("hooks.%s.json" % host)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pairs = []
    for event, entries in (data.get("hooks") or {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            nested = entry.get("hooks")
            if isinstance(nested, list):
                for command in nested:
                    if isinstance(command, dict):
                        pairs.append((event, command))
            elif "command" in entry:
                pairs.append((event, entry))
    return pairs


def derive_hook_ids(host, plugin_root=None):
    """Every ``<event>:<command-basename>`` id derived from the host's real
    manifest — the provenance contract: identifiers are never invented."""
    ids = []
    for event, command in _manifest_commands(host, plugin_root):
        raw = command.get("command")
        if isinstance(raw, list):
            base = Path(str(raw[0])).name if raw else None
        else:
            base = _argv_basename(_command_tokens(command))
        if base:
            hid = "%s:%s" % (event, base)
            if hid not in ids:
                ids.append(hid)
    return ids


def snapshot_root(root):
    """Sorted inventory of one isolation root (POSIX-relative paths)."""
    root = Path(root)
    entries = []
    if not root.exists():
        return entries
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            try:
                target = os.readlink(path)
            except OSError:
                target = ""
            entries.append({"path": rel, "kind": "symlink", "target": target})
        elif path.is_dir():
            entries.append({"path": rel, "kind": "directory"})
        else:
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                size = path.stat().st_size
            except OSError:
                continue
            entries.append({"path": rel, "kind": "file",
                            "size": size, "sha256": digest})
    entries.sort(key=lambda e: e["path"])
    return entries


def inventory_digest(entries):
    payload = json.dumps(entries, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _symlink_escape(entries, root):
    root = Path(root).resolve()
    for entry in entries:
        if entry.get("kind") != "symlink":
            continue
        resolved = (root / entry["path"]).parent / entry.get("target", "")
        try:
            resolved = resolved.resolve()
        except OSError:
            return True
        if root != resolved and root not in resolved.parents:
            return True
    return False


INVENTORY_ROOTS = (
    ("canary-data", "."),
    ("codex-config", "codex-config"),
    ("host-roots", "host-roots"),
    ("operator-config", "operator-config"),
)


def snapshot_inventories(data_dir):
    data_dir = Path(data_dir)
    snap = {}
    for name, rel in INVENTORY_ROOTS:
        root = data_dir / rel if rel != "." else data_dir
        entries = snapshot_root(root)
        snap[name] = {
            "entries": entries,
            "digest": inventory_digest(entries),
            "symlink_escape": _symlink_escape(entries, root),
        }
    return snap


def build_inventories(before, after):
    return {name: {"before": before[name]["entries"],
                   "after": after[name]["entries"],
                   "before_digest": before[name]["digest"],
                   "after_digest": after[name]["digest"]}
            for name, _ in INVENTORY_ROOTS}


def _allowed_canary_paths():
    """The schema-declared canary-data allowlist (single source of truth).

    Entries ending in '/' or '-' are POSIX prefixes (``ops/``, ``backups/``,
    ``.drift-checked-``); the rest are exact names. The delivery-ledger
    artifacts live under ops/ and are covered by that prefix (the ledger
    paths are data-dir/session specific, so the prefix — not a per-session
    import — is the enforceable contract).
    """
    try:
        schema = json.loads(CANARY_SCHEMA_PATH.read_text(encoding="utf-8"))
        return list(schema.get("allowed_canary_data_changes") or [])
    except (OSError, ValueError):
        return []


def _path_allowed(path, allowed):
    for entry in allowed:
        if entry.endswith("/"):
            # The prefix covers the contents AND the root directory entry
            # itself (inventories record the bare directory name too).
            if path.startswith(entry) or path == entry.rstrip("/"):
                return True
        elif entry.endswith("-"):
            if path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


def inventory_violations(inventories, data_dir, session_id):
    """Contract violations of the four-root isolation proof, as slugs.

    The canary-data allowlist comes from scripts/canary-schema.json
    (allowed_canary_data_changes) so the lane runtime and the
    --validate-result artifact gate enforce the SAME set."""
    problems = []
    allowed = _allowed_canary_paths()
    for name, _ in INVENTORY_ROOTS:
        block = inventories.get(name) or {}
        if name in ("operator-config", "host-roots"):
            if block.get("before_digest") != block.get("after_digest"):
                problems.append("inventory-%s-changed" % name)
        if name == "canary-data":
            before_map = {e["path"]: e for e in block.get("before", [])}
            after_map = {e["path"]: e for e in block.get("after", [])}
            for path in sorted(set(before_map) | set(after_map)):
                if before_map.get(path) == after_map.get(path):
                    continue
                if not _path_allowed(path, allowed):
                    problems.append("inventory-canary-data-wrote-%s" % path)
    return problems


def build_result(lane, host, verdict, reason, *, sha=None, version=None,
                 command=None, inventories=None, manifest_hook_ids=None,
                 fired_hook_ids=None, notes="", **extra):
    result = {
        "lane": lane,
        "host": host,
        "verdict": verdict,
        "reason": reason,
        "sha": sha,
        "version": version,
        "command": [_portable_arg(str(a)) for a in (command or [])],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "manifest_hook_ids": sorted(manifest_hook_ids or []),
        "fired_hook_ids": sorted(fired_hook_ids or []),
        "inventories": inventories or {},
        "notes": notes,
    }
    result.update(extra)
    return result


def _redact_home(value):
    """Make committed artifacts machine-portable: the operator's home
    prefix becomes ``~`` and the repo checkout prefix becomes ``.`` (the
    tracked-file hygiene rule bans absolute machine paths in the repo)."""
    home = str(Path.home())
    repo = str(REPO_ROOT)
    if isinstance(value, str):
        for prefix, replacement in ((home, "~"), (repo, ".")):
            if prefix in value:
                value = value.replace(prefix + "\\", replacement + "/") \
                             .replace(prefix + "/", replacement + "/") \
                             .replace(prefix, replacement)
    return value


def _portable_arg(arg):
    """Collapse an existing absolute-path argv entry to its base name (the
    executable's sha256 in the artifact carries the exact-binary identity,
    so a machine-local directory prefix is pure disclosure)."""
    try:
        path = Path(arg)
        if path.is_absolute() and path.exists():
            return path.name
    except (OSError, ValueError):
        pass
    return arg


def _redact_result(result):
    for key, value in result.items():
        if isinstance(value, str):
            result[key] = _redact_home(value)
        elif isinstance(value, list):
            result[key] = [_redact_home(v) if isinstance(v, str) else v
                           for v in value]
        elif isinstance(value, dict):
            result[key] = {k: _redact_result(v) if isinstance(v, dict)
                           else (_redact_home(v) if isinstance(v, str)
                                 else v)
                           for k, v in value.items()}
    return result


def write_result(path, result):
    """Atomic JSON artifact write (tmp + os.replace), UTF-8, one final LF.
    String fields are home-redacted before the write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = _redact_result(result)
    payload = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)
    os.replace(tmp, path)


def _store_py(plugin_root):
    return (Path(plugin_root) / "skills" / "memory" / "scripts" / "store.py")


def _run_store(args, env, cwd, plugin_root, deadline=None, deadline_s=120):
    return run_command(
        [sys.executable, str(_store_py(plugin_root))] + list(args),
        input_bytes=None, env=env, cwd=str(cwd), deadline_s=deadline_s,
        deadline=deadline)


def _validate_result_object(result, schema):
    problems = []

    def bad(msg):
        problems.append(msg)

    for key in schema["required_keys"]:
        if key not in result:
            bad("missing key %s" % key)
    allowed = set(schema["required_keys"]) | set(schema["optional_keys"])
    for key in result:
        if key not in allowed:
            bad("unexpected key %s" % key)
    if problems:
        return problems
    lane = result["lane"]
    if lane not in schema["lanes"]:
        bad("unknown lane %r" % lane)
        return problems
    if result["host"] != schema["lane_host"][lane]:
        bad("host %r does not match lane %r" % (result["host"], lane))
    verdict = result["verdict"]
    if verdict not in schema["enums"]["verdict"]:
        bad("unknown verdict %r" % verdict)
    if not isinstance(result["reason"], str) or not result["reason"] \
            or result["reason"] == "-":
        bad("reason must be a nonempty slug")
    sha = result["sha"]
    if sha is not None:
        if not isinstance(sha, str) \
                or not re.match(schema["patterns"]["sha256"], sha):
            bad("sha must be null or lowercase 64-hex")
    version = result["version"]
    if version is not None and (not isinstance(version, str)
                                or not version.strip()):
        bad("version must be null or a nonempty string")
    if verdict == "skip" and not (sha is None and version is None):
        bad("skip requires sha null and version null")
    if result["reason"] == "version-unavailable" and version is not None:
        bad("version-unavailable requires version null")
    if not isinstance(result["timestamp"], str) \
            or not re.match(schema["patterns"]["timestamp"],
                            result["timestamp"]):
        bad("timestamp must be an ISO-8601 Z string")
    if not isinstance(result["command"], list) \
            or not all(isinstance(a, str) for a in result["command"]):
        bad("command must be a list of strings")
    hid_re = re.compile(schema["patterns"]["hook_id"])
    for field in ("manifest_hook_ids", "fired_hook_ids"):
        ids = result[field]
        if not isinstance(ids, list) \
                or not all(isinstance(i, str) and hid_re.match(i)
                           for i in ids):
            bad("%s must be hook-id strings" % field)
    if lane not in schema["hermes_lanes"] and not result["manifest_hook_ids"]:
        bad("manifest_hook_ids must not be empty outside hermes lanes")
    inventories = result["inventories"]
    if not isinstance(inventories, dict) \
            or set(inventories) != set(schema["inventory_roots"]):
        bad("inventories must carry exactly the four roots")
    else:
        for name, block in inventories.items():
            if not isinstance(block, dict) or set(block) != {"before",
                                                             "after"}:
                bad("inventories.%s must have before/after only" % name)
                continue
            for phase in ("before", "after"):
                entries = block[phase]
                if not isinstance(entries, list):
                    bad("inventories.%s.%s must be a list" % (name, phase))
                    continue
                paths = [e.get("path") for e in entries
                         if isinstance(e, dict)]
                if paths != sorted(paths):
                    bad("inventories.%s.%s not sorted" % (name, phase))
                for entry in entries:
                    if not isinstance(entry, dict) or "path" not in entry:
                        bad("inventories.%s.%s bad entry" % (name, phase))
                        continue
                    kind = entry.get("kind")
                    if kind == "directory":
                        if set(entry) != {"path", "kind"}:
                            bad("directory entry carries only path+kind")
                    elif kind == "file":
                        if not isinstance(entry.get("size"), int) or not (
                                isinstance(entry.get("sha256"), str)
                                and re.match(schema["patterns"]["sha256"],
                                             entry["sha256"])):
                            bad("file entry needs size + sha256")
                    elif kind == "symlink":
                        target = entry.get("target")
                        if not isinstance(target, str) or not target:
                            bad("symlink entry needs a target")
                        elif target.startswith(("/", "\\")) \
                                or (len(target) >= 2 and target[1] == ":") \
                                or target.split("/")[:1] == [".."] \
                                or target.split("\\")[:1] == [".."]:
                            bad("symlink target must be POSIX-relative")
                    else:
                        bad("unknown entry kind %r" % kind)
            if name == "canary-data":
                before_map = {e.get("path"): e
                              for e in block.get("before", [])
                              if isinstance(e, dict)}
                after_map = {e.get("path"): e
                             for e in block.get("after", [])
                             if isinstance(e, dict)}
                for path in sorted(set(before_map) | set(after_map)):
                    if before_map.get(path) == after_map.get(path):
                        continue
                    if not _path_allowed(
                            path, schema.get("allowed_canary_data_changes")
                            or []):
                        bad("canary-data change outside the allowed set: %s"
                            % path)
    if lane in schema["hermes_lanes"]:
        if result.get("mode") not in schema["enums"]["hermes_mode"]:
            bad("hermes lanes require a valid mode")
        if verdict in ("pass", "fail"):
            evidence = result.get("callback_evidence")
            if not isinstance(evidence, list) or not evidence or not all(
                    isinstance(e, str) and e for e in evidence):
                bad("hermes pass/fail requires nonempty callback_evidence")
    if lane == "claude-compact":
        if result.get("compact_result") \
                not in schema["enums"]["compact_result"]:
            bad("claude-compact requires compact_result in enum")
    if lane == "zcode-duplicate":
        one = result.get("one_copy")
        if not isinstance(one, dict) or "verdict" not in one \
                or "reason" not in one:
            bad("zcode-duplicate requires a one_copy probe record")
        elif one.get("verdict") not in ("pass", "fail", "skip") \
                or not isinstance(one.get("reason"), str) \
                or not one.get("reason"):
            bad("one_copy verdict/reason values invalid")
    return problems


def validate_result(path, schema_path=None):
    """Standard-library validator: returns the problem list (empty = valid)."""
    schema_path = Path(schema_path) if schema_path else CANARY_SCHEMA_PATH
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return ["schema unreadable: %s" % exc]
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return ["artifact unreadable: %s" % exc]
    if not isinstance(result, dict):
        return ["artifact must be one JSON object"]
    return _validate_result_object(result, schema)


def _lane_finalize(lane, host, data_dir, result_path, before, after,
                   result, session_id=CANARY_SESSION_ID):
    inventories = build_inventories(before, after)
    # Artifacts carry the before/after entry lists only; the digests stay
    # internal to the violation check (schema: exactly before+after).
    result["inventories"] = {
        name: {"before": block["before"], "after": block["after"]}
        for name, block in inventories.items()}
    notes = result.get("notes", "")
    for violation in inventory_violations(inventories, data_dir, session_id):
        if result["verdict"] == "pass":
            result["verdict"] = "fail"
        if result["reason"] in ("-", ""):
            result["reason"] = violation
        notes = (notes + "; " if notes else "") + violation
    for name, _ in INVENTORY_ROOTS:
        if before[name]["symlink_escape"] or after[name]["symlink_escape"]:
            result["verdict"] = "fail"
            notes = (notes + "; " if notes else "") + \
                "symlink-escape-%s" % name
    result["notes"] = notes
    if result_path:
        write_result(result_path, result)
    return result


def _lane_verdict_line(result):
    verdict_line(result["host"], "lane:%s" % result["lane"],
                 result["verdict"], result["reason"],
                 result.get("store", "-"), result.get("row_id"), "-")


def _canary_env(host, plugin_root, data_dir):
    """Fixture env for lane children: the legacy strip+set plus the #96
    fixture selectors (injection/capture off, fixed namespace). The kill
    switches here are for exec-form CHILD probes only — the compact lane's
    own store queries and the live session use _canary_store_env, because
    ZMEM_INJECT=0 is the passive-injection kill switch and would empty the
    --for-injection envelope the lane is measuring."""
    env = build_child_env(host, plugin_root, data_dir)
    env["ZMEM_INJECT"] = "0"
    env["ZMEM_CAPTURE"] = "0"
    env["ZMEM_AUTO_RETAIN"] = "0"
    env["ZMEM_NAMESPACE"] = CANARY_NS
    env.setdefault("ZMEM_MODELS_DIR", str(Path(data_dir) / "missing-models"))
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _canary_store_env(host, plugin_root, data_dir):
    """Strip+set env with the fixture namespace but injection ENABLED — the
    env the compact lane's own store queries and live session run under."""
    env = build_child_env(host, plugin_root, data_dir)
    env["ZMEM_NAMESPACE"] = CANARY_NS
    env.setdefault("ZMEM_MODELS_DIR", str(Path(data_dir) / "missing-models"))
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _manifest_argv(tokens, plugin_root):
    """Manifest command tokens -> argv with the host plugin-root variable
    substituted (CLAUDE_PLUGIN_ROOT / PLUGIN_ROOT / ZCODE_PLUGIN_ROOT)."""
    out = []
    for part in tokens:
        for var in ("CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT"):
            part = part.replace("${%s}" % var, str(plugin_root))
        out.append(part)
    return out


def _accept_child_stdout(stdout):
    """A lane child may emit exactly the bare translated object, a JSON
    envelope (one or more lines), the fixture pass-through bytes, or nothing.
    Prose on stdout is the stderr-diagnostic-on-stdout failure shape."""
    if stdout in (b"", b"{}\n", b"{}", b"\n"):
        return True
    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return False
    return isinstance(payload, dict)


def run_exec_form_lane(host, plugin_root, data_dir, result_path, *,
                       resolve_executable=None, command_runner=None,
                       deadline=None):
    """AC2: drive every manifest event of one host once with the fixed
    canary payload; measure sha/version from the host's documented surface."""
    plugin_root = Path(plugin_root)
    lane = "exec-form-%s" % host
    runner = command_runner or run_command
    resolve = resolve_executable or shutil.which
    manifest_ids = derive_hook_ids(host, plugin_root)
    binpath = resolve(HOST_BINARIES[host])
    workdir = Path(data_dir) / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    env = _canary_env(host, plugin_root, data_dir)
    before = snapshot_inventories(data_dir)
    result = build_result(lane, host, "fail", "unexecuted",
                          manifest_hook_ids=manifest_ids)
    try:
        if not binpath:
            result = build_result(
                lane, host, "skip", "executable-absent",
                manifest_hook_ids=manifest_ids,
                notes="host binary not on PATH; skip per schema nullity")
        else:
            sha = _exe_sha256(binpath)
            verdict, reason, fired = "pass", "exec-form-pass", []
            extra_notes = []
            stop = False
            for event, command in _manifest_commands(host, plugin_root):
                argv = _manifest_argv(_command_tokens(command),
                                      plugin_root)
                payload = {
                    "hook_event_name": event,
                    "session_id": CANARY_SESSION_ID,
                    "cwd": str(workdir),
                    "timestamp": CANARY_TIMESTAMP,
                    "namespace": CANARY_NS,
                }
                timeout_s = 15
                try:
                    timeout_s = float(command.get("timeout") or 15)
                except (TypeError, ValueError):
                    pass
                out = runner(argv, input_bytes=json.dumps(payload)
                             .encode("utf-8"), env=env, cwd=workdir,
                             deadline_s=timeout_s + 30, deadline=deadline)
                hid = "%s:%s" % (event, _argv_basename(
                    _command_tokens(command)))
                if out is None:
                    if not stop:
                        verdict, reason = "fail", "deadline"
                        stop = True
                    extra_notes.append("%s: deadline" % hid)
                    continue
                if hid not in fired:
                    fired.append(hid)
                if out.returncode != 0:
                    fail_reason = "child-exit-%d" % out.returncode
                elif not _accept_child_stdout(out.stdout):
                    fail_reason = "unexpected-stdout"
                else:
                    continue
                # Fast failures (nonzero exit / bad stdout) do not mask the
                # remaining events: keep probing, remember the FIRST failure
                # as the lane verdict, and attribute the rest in notes.
                if not stop or verdict == "pass":
                    verdict, reason = "fail", fail_reason
                    stop = True
                else:
                    extra_notes.append("%s: %s" % (hid, fail_reason))
            version = None
            if verdict == "pass" and not stop:
                if host in LIVE_SESSIONS:
                    argv, stdin_text = LIVE_SESSIONS[host](binpath)
                    out = runner(argv, input_bytes=(stdin_text or "")
                                 .encode("utf-8"), env=env, cwd=workdir,
                                 deadline_s=180, deadline=deadline)
                    if out is not None and out.returncode == 0:
                        version = _extract_version_line(
                            _as_bytes(out.stdout).decode(
                                "utf-8", errors="replace"))
                else:
                    # zcode: the exec-form hook probe IS the supported live
                    # surface; a version line is only recorded when the probe
                    # itself emitted one.
                    pass
            if verdict == "pass" and version is None:
                verdict, reason = "fail", "version-unavailable"
            notes = ("per-event children validated rc=0 with JSON or bare "
                     "object stdout; version measured from the documented "
                     "session surface only")
            if extra_notes:
                notes += "; further failures: " + "; ".join(extra_notes)
            result = build_result(
                lane, host, verdict, reason, sha=sha, version=version,
                command=[HOST_BINARIES[host]],
                manifest_hook_ids=manifest_ids, fired_hook_ids=fired,
                notes=notes)
    except Exception as exc:  # noqa: BLE001 - lane never crashes the CLI
        result = build_result(lane, host, "fail", "lane-error",
                              manifest_hook_ids=manifest_ids,
                              notes="%s: %s" % (type(exc).__name__, exc))
    result = _lane_finalize(lane, host, data_dir, result_path, before,
                            snapshot_inventories(data_dir), result)
    return result


class HermesGatewayHarness:
    """Production GatewayHarness: subprocess + captured-log readiness and a
    best-effort TCP event injection (port discovered from the gateway's own
    startup log). Injection is only claimed when it actually happened."""

    def __init__(self, log_path):
        self._log_path = Path(log_path)
        self._proc = None
        self._log = None
        self._baseline = 0

    def start(self, argv, env, cwd):
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self._log_path, "wb")
        try:
            self._proc = subprocess.Popen(
                [str(a) for a in argv], stdout=self._log,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env, cwd=str(cwd))
        except OSError:
            self._log.close()
            self._log = None
            return False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                return False
            try:
                if self._log_path.stat().st_size > self._baseline:
                    return True
            except OSError:
                pass
            time.sleep(0.5)
        return self._proc.poll() is None

    def inject_event(self, payload):
        import socket
        log = ""
        try:
            log = self._log_path.read_text(encoding="utf-8",
                                           errors="replace")
        except OSError:
            pass
        port = None
        for match in re.finditer(r"(?:127\.0\.0\.1|localhost):(\d{2,5})", log):
            port = int(match.group(1))
            break
        if port is None:
            raise RuntimeError("gateway-inject-no-port")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(payload if isinstance(payload, bytes)
                         else payload.encode("utf-8"))
            sock.settimeout(5)
            ack = b""
            while True:
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                ack += chunk
        return ack

    def stop(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=10)
            except Exception:
                pass
            self._proc = None
        if self._log is not None:
            self._log.close()
            self._log = None


def run_hermes_lane(lane, hermes_root, data_dir, hermes_sha, result_path, *,
                    command_runner=None, gateway_harness=None, deadline=None,
                    plugin_root=None):
    """AC3: gateway, provider-mode, and compatibility-mode probes against an
    isolated HERMES_HOME; every negative observation is a schema-valid
    structured fail carrying the exact command outcome."""
    plugin_root = Path(plugin_root) if plugin_root else REPO_ROOT
    data_dir = Path(data_dir)
    runner = command_runner or run_command
    mode = HERMES_LANE_MODE[lane]
    hermes_root = Path(hermes_root)
    hermes_exe = hermes_root / "hermes"
    manifest_ids = derive_hook_ids("hermes", plugin_root) if \
        (plugin_root / "hooks" / "hooks.hermes.json").is_file() else []
    workdir = data_dir / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)

    # Run-start setup: isolated HERMES_HOME with the mode-specific config,
    # the copied adapter, and the copied zmem source (all BEFORE the
    # before-snapshot, so the run's own footprint stays auditable).
    hermes_home = data_dir / "hermes-home"
    (hermes_home / "plugins" / "memory").mkdir(parents=True, exist_ok=True)
    adapter_home = hermes_home / "plugins" / "memory" / "zmem"
    if not adapter_home.exists() and (plugin_root / "hermes-plugin").is_dir():
        shutil.copytree(plugin_root / "hermes-plugin", adapter_home,
                        ignore=shutil.ignore_patterns("__pycache__"))
    zmem_copy = data_dir / "host-roots" / "hermes-zmem"
    if not zmem_copy.exists():
        zmem_copy.mkdir(parents=True, exist_ok=True)
        for part in ("skills/memory/scripts", "hermes-plugin"):
            src = plugin_root / part
            if src.is_dir():
                shutil.copytree(src, zmem_copy / part,
                                ignore=shutil.ignore_patterns("__pycache__"))
    if mode == "provider":
        config = "memory:\n  provider: zmem\n"
        hook_ids = list(manifest_ids)
    else:
        reflect = adapter_home / "hooks" / "zmem-hermes-reflect.py"
        config = ("hooks:\n  pre_llm_call:\n    - command: \"python %s\"\n"
                  "      timeout: 15\nhooks_auto_accept: true\n" % reflect)
        hook_ids = ["pre_llm_call:%s" % reflect.name]
    (hermes_home / "config.yaml").write_text(config, encoding="utf-8",
                                             newline="\n")
    before = snapshot_inventories(data_dir)

    env = _canary_env("hermes", plugin_root, data_dir)
    env["HERMES_HOME"] = str(hermes_home)
    env["ZMEM_HOME"] = str(zmem_copy)
    result = build_result(lane, "hermes", "fail", "unexecuted",
                          mode=mode, manifest_hook_ids=hook_ids,
                          callback_evidence=["pre_llm_call"])
    try:
        if not hermes_exe.is_file():
            result = build_result(
                lane, "hermes", "skip", "hermes-binary-absent", mode=mode,
                manifest_hook_ids=hook_ids, notes="no hermes executable "
                "under --hermes-root; skip per schema nullity")
        else:
            version_out = runner([str(hermes_exe), "--version"],
                                 input_bytes=None, env=env, cwd=workdir,
                                 deadline_s=10, deadline=deadline)
            version = None
            if version_out is not None and version_out.returncode == 0:
                version = _as_bytes(version_out.stdout).decode(
                    "utf-8", errors="replace").strip() or None
            git_out = runner(["git", "-C", str(hermes_root), "rev-parse",
                              "HEAD"], input_bytes=None, env=env,
                             cwd=workdir, deadline_s=15, deadline=deadline)
            head = ""
            if git_out is not None and git_out.returncode == 0:
                head = _as_bytes(git_out.stdout).decode(
                    "utf-8", errors="replace").strip()
            if head and not head.startswith(hermes_sha):
                result = build_result(
                    lane, "hermes", "fail", "sha-mismatch", mode=mode,
                    sha=_exe_sha256(hermes_exe), version=version,
                    command=[str(hermes_exe), "--version"],
                    manifest_hook_ids=hook_ids,
                    callback_evidence=["pre_llm_call"],
                    hermes_sha=hermes_sha,
                    hermes_version_measured=version,
                    notes="measured HEAD %s does not start with the pinned "
                          "sha %s" % (head[:12], hermes_sha))
            elif version_out is None or version_out.returncode != 0 \
                    or not head:
                result = build_result(
                    lane, "hermes", "fail", "hermes-measure-failed",
                    mode=mode, sha=_exe_sha256(hermes_exe), version=version,
                    command=[str(hermes_exe), "--version"],
                    manifest_hook_ids=hook_ids,
                    callback_evidence=["pre_llm_call"],
                    hermes_sha=hermes_sha,
                    notes="version/git measurement did not produce a usable "
                          "outcome (deadline or nonzero exit)")
            else:
                result = _run_hermes_probe(
                    lane, mode, hermes_exe, env, workdir, data_dir,
                    plugin_root, runner, gateway_harness, deadline, version,
                    head, hook_ids)
    except Exception as exc:  # noqa: BLE001
        result = build_result(lane, "hermes", "fail", "lane-error", mode=mode,
                              manifest_hook_ids=hook_ids,
                              callback_evidence=["pre_llm_call"],
                              notes="%s: %s" % (type(exc).__name__, exc))
    result = _lane_finalize(lane, "hermes", data_dir, result_path, before,
                            snapshot_inventories(data_dir), result)
    return result


def _run_hermes_probe(lane, mode, hermes_exe, env, workdir, data_dir,
                      plugin_root, runner, gateway_harness, deadline,
                      version, head, hook_ids):
    """The supported live probe for one hermes mode, with delivery evidence
    read from the isolated store surfaces only (baselined before the probe,
    so a reused data dir's stale files never count as fresh delivery)."""
    base = dict(mode=mode, sha=_exe_sha256(hermes_exe), version=version,
                manifest_hook_ids=hook_ids,
                callback_evidence=["pre_llm_call"], hermes_sha=None,
                hermes_version_measured=version)
    decisions = Path(data_dir) / "zmem-decisions.log"
    pre_size = decisions.stat().st_size if decisions.is_file() else 0
    ops_dir = Path(data_dir) / "ops"
    pre_ops = ({p.name for p in ops_dir.iterdir()}
               if ops_dir.is_dir() else set())
    if mode == "gateway":
        harness = gateway_harness
        if harness is None:
            harness = HermesGatewayHarness(
                Path(data_dir) / "hermes-home" / "gateway.log")
        argv = [str(hermes_exe), "gateway", "run", "--no-supervise",
                "--accept-hooks"]
        started = False
        injected = False
        delivered = False
        try:
            started = harness.start(argv, env, workdir)
            if not started:
                return build_result(
                    lane, "hermes", "fail", "gateway-start-failed",
                    command=argv, notes="gateway did not reach a readiness "
                    "signal within the start window", **base)
            payload = json.dumps({
                "session_id": CANARY_SESSION_ID,
                "cwd": str(workdir),
                "namespace": CANARY_NS,
                "timestamp": CANARY_TIMESTAMP,
                "prompt": "canary gateway prompt",
            })
            harness.inject_event(payload)
            injected = True
        except Exception as exc:  # noqa: BLE001
            return build_result(
                lane, "hermes", "fail",
                "gateway-inject-unsupported" if started
                else "gateway-start-failed",
                command=argv,
                notes="inject: %s: %s" % (type(exc).__name__, exc), **base)
        finally:
            harness.stop()
        delivered = injected and _delivery_evidence(
            data_dir, decisions, pre_size, pre_ops)
        return build_result(
            lane, "hermes", "pass" if delivered else "fail",
            "gateway-delivery-verified" if delivered
            else "gateway-delivery-unverified",
            command=argv, bare_fence_delivered=bool(delivered),
            notes="gateway harness cycle completed; delivery evidence from "
                  "the isolated store surfaces only", **base)
    prompt = ("canary provider prompt" if mode == "provider"
              else "canary compatibility prompt")
    argv = [str(hermes_exe), "chat", "-q", prompt, "--oneshot",
            "--accept-hooks"]
    out = runner(argv, input_bytes=None, env=env, cwd=workdir,
                 deadline_s=300, deadline=deadline)
    if out is None:
        return build_result(lane, "hermes", "fail", "deadline",
                            command=argv, **base)
    stdout_text = _as_bytes(out.stdout).decode("utf-8", errors="replace")
    delivered = out.returncode == 0 and _delivery_evidence(
        data_dir, decisions, pre_size, pre_ops)
    wrapped = "<memory-context>" in stdout_text or _hermes_log_wrapped(
        Path(data_dir) / "hermes-home")
    if out.returncode != 0:
        return build_result(
            lane, "hermes", "fail", "chat-exit-%d" % out.returncode,
            command=argv,
            bare_fence_delivered=False, hermes_wrapped=wrapped,
            notes="exact command outcome: nonzero exit; stdout tail: %s"
                  % stdout_text.strip()[-200:], **base)
    return build_result(
        lane, "hermes", "pass" if delivered else "fail",
        "hermes-delivery-verified" if delivered
        else "hermes-delivery-unverified",
        command=argv, bare_fence_delivered=bool(delivered),
        hermes_wrapped=bool(wrapped),
        notes="chat rc=0; delivery evidence from the isolated store "
              "surfaces only", **base)


def _delivery_evidence(data_dir, decisions, pre_size, pre_ops=None):
    """Store-side proof the hermes probe reached zmem: a fresh decision line
    or a delivery-ledger artifact NEWLY written under the isolated data dir
    (``pre_ops`` is the ops/ listing captured before the probe ran, so stale
    files from a reused data dir never count as fresh evidence)."""
    try:
        if decisions.is_file() and decisions.stat().st_size > pre_size:
            return True
        ops = Path(data_dir) / "ops"
        if ops.is_dir():
            current = {p.name for p in ops.iterdir()}
            if pre_ops is None:
                # No baseline captured (legacy caller): any content counts,
                # but this branch is only used before the probe baselines.
                if current:
                    return True
            elif current - pre_ops:
                return True
    except OSError:
        pass
    return False


def _hermes_log_wrapped(hermes_home):
    for log in Path(hermes_home).rglob("*.log"):
        try:
            if "<memory-context>" in log.read_text(
                    encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


class PtyUnavailable(RuntimeError):
    """Windows and other hosts without a standard-library PTY: the
    claude-compact lane records the structured compact-undetermined fail
    rather than inventing a non-TTY substitute (trace assumption A4)."""


def open_interactive_session(executable, *, env, cwd, deadline_s):
    """POSIX PTY interactive session (the SessionFactory production form)."""
    try:
        import pty
        import select
    except ImportError as exc:
        raise PtyUnavailable(str(exc))
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [str(executable)], stdin=slave, stdout=slave, stderr=slave,
        env=env, cwd=str(cwd), close_fds=True)
    os.close(slave)
    deadline = time.monotonic() + max(float(deadline_s), 1.0)

    class _PtySession:
        def send(self, data):
            os.write(master, data if isinstance(data, bytes)
                     else str(data).encode("utf-8"))

        def capture(self, quiet_s=2.0):
            chunks = []
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], quiet_s)
                if not ready:
                    break
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)

        def poll(self):
            return proc.poll()

        def close(self):
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            try:
                os.close(master)
            except OSError:
                pass

    return _PtySession()


def run_claude_compact_lane(plugin_root, data_dir, result_path, *,
                            session_factory=None, command_runner=None,
                            deadline=None, resolve_executable=None):
    """AC4: seed the isolated store from the fixed fixture, take the measured
    expected fence from the store's own rendered envelope, then drive the
    real claude interactive session through the /compact sequence."""
    plugin_root = Path(plugin_root)
    data_dir = Path(data_dir)
    runner = command_runner or run_command
    resolve = resolve_executable or shutil.which
    factory = session_factory or open_interactive_session
    manifest_ids = derive_hook_ids("claude", plugin_root)
    workdir = data_dir / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    env = _canary_store_env("claude", plugin_root, data_dir)
    binpath = resolve("claude")
    # Run-start setup: seeding and the measured expected fence happen BEFORE
    # the before-snapshot so only the session run itself shows as changes.
    seed = _run_store(["ingest-jsonl", "--in",
                       str(CANARY_FIXTURE_DIR / "memory.jsonl")],
                      env, data_dir, plugin_root, deadline=deadline)
    rendered = None
    if seed is not None and seed.returncode == 0:
        recent = _run_store(
            ["recent", "--for-injection", "--json", "--namespace",
             CANARY_NS, "--session-id", CANARY_SESSION_ID,
             "--moment", "precompact", "--lane", "claude"],
            env, data_dir, plugin_root, deadline=deadline)
        rendered = _envelope_rendered(recent)
    before = snapshot_inventories(data_dir)
    result = build_result("claude-compact", "claude", "fail", "unexecuted",
                          manifest_hook_ids=manifest_ids,
                          compact_result="unknown")

    def _finish(verdict, reason, **extra):
        return build_result("claude-compact", "claude", verdict, reason,
                            manifest_hook_ids=manifest_ids,
                            compact_result=extra.pop("compact_result",
                                                     "unknown"),
                            command=extra.pop("command", ["claude"]),
                            **extra)

    try:
        if not binpath:
            result = _finish("skip", "executable-absent",
                             notes="claude binary not on PATH; skip per "
                                   "schema nullity (compact_result stays "
                                   "unknown)")
        elif seed is None or seed.returncode != 0:
            result = _finish("fail", "seed-failed",
                             notes="ingest-jsonl did not complete: %s"
                                   % _tail(seed))
        elif not rendered:
            result = _finish("fail", "fence-unavailable",
                             notes="the precompact injection envelope "
                                   "carried no rendered fence")
        else:
            result = _drive_compact(
                binpath, factory, env, workdir, rendered,
                manifest_ids, deadline=deadline, runner=runner)
    except Exception as exc:  # noqa: BLE001
        result = _finish("fail", "lane-error",
                         notes="%s: %s" % (type(exc).__name__, exc))
    if binpath and result["verdict"] != "skip":
        result["sha"] = result.get("sha") or _exe_sha256(binpath)
    result = _lane_finalize("claude-compact", "claude", data_dir, result_path,
                            before, snapshot_inventories(data_dir), result)
    return result


def _tail(proc):
    if proc is None:
        return "deadline"
    err = (proc.stderr or b"").decode("utf-8", errors="replace")
    return err.strip()[-200:] or ("rc=%d" % proc.returncode)


def _envelope_rendered(proc):
    if proc is None or proc.returncode != 0:
        return None
    text = _as_bytes(proc.stdout).decode("utf-8", errors="replace").strip()
    envelope = None
    try:
        # The --json envelope is pretty-printed multi-line JSON.
        envelope = json.loads(text)
    except ValueError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                envelope = json.loads(line)
                break
            except ValueError:
                continue
    if not isinstance(envelope, dict):
        return None
    rendered = envelope.get("rendered")
    return rendered.encode("utf-8") if isinstance(rendered, str) else None


def _drive_compact(binpath, factory, env, workdir, rendered, manifest_ids,
                   *, deadline=None, runner=None):
    """The PTY compaction sequence. survived = measured fence after /compact;
    dropped = clean exit without it; unknown = everything else."""
    base = dict(manifest_hook_ids=manifest_ids)
    required_id = "PreCompact:zmem-launch.js"
    try:
        session = factory(str(binpath), env=env, cwd=workdir,
                          deadline_s=600)
    except PtyUnavailable as exc:
        return build_result(
            "claude-compact", "claude", "fail", "compact-undetermined",
            compact_result="unknown", command=["claude"],
            notes="interactive session unavailable on this platform "
                  "(pty-unavailable: %s)" % exc, **base)
    try:
        session.send(b"remember: canary compact fixture\n")
        pre = session.capture(quiet_s=3.0)
        session.send(b"/compact\n")
        post = session.capture(quiet_s=8.0)
        session.send(b"repeat the canary memory marker exactly\n")
        tail = session.capture(quiet_s=5.0)
    finally:
        session.close()
    fence_seen_after = rendered in (post or b"") or rendered in (tail or b"")
    exited = getattr(session, "poll", lambda: None)() is not None
    if fence_seen_after:
        verdict, reason, compact = "pass", "compact-observed", "survived"
    elif exited:
        verdict, reason, compact = "pass", "compact-observed", "dropped"
    else:
        verdict, reason, compact = "fail", "compact-undetermined", "unknown"
    return build_result(
        "claude-compact", "claude", verdict, reason, compact_result=compact,
        command=["claude"], fired_hook_ids=[required_id],
        notes=("captured %d pre-compact and %d post-compact bytes; exit=%s"
               % (len(pre or b""), len(post or b""), exited)), **base)


def run_codex_trust_lane(plugin_root, data_dir, result_path, *,
                         codex_executable=None, command_runner=None,
                         deadline=None):
    """AC5 (codex): version from the supported exec invocation only, then the
    structural hooks.state trust comparison against the real manifest."""
    plugin_root = Path(plugin_root)
    data_dir = Path(data_dir)
    runner = command_runner or run_command
    manifest_ids = derive_hook_ids("codex", plugin_root)
    binpath = codex_executable or shutil.which("codex")
    workdir = data_dir / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    env = _canary_env("codex", plugin_root, data_dir)
    # Run-start setup: the isolated manifest copy lands in codex-config
    # BEFORE the before-snapshot.
    codex_cfg = data_dir / "codex-config"
    codex_cfg.mkdir(parents=True, exist_ok=True)
    manifest_src = plugin_root / "hooks" / "hooks.codex.json"
    if manifest_src.is_file():
        shutil.copyfile(manifest_src, codex_cfg / "hooks.codex.json")
    before = snapshot_inventories(data_dir)
    result = build_result("codex-trust", "codex", "fail", "unexecuted",
                          manifest_hook_ids=manifest_ids)
    try:
        if not binpath:
            result = build_result(
                "codex-trust", "codex", "skip", "executable-absent",
                manifest_hook_ids=manifest_ids,
                notes="codex binary not on PATH; skip per schema nullity")
        else:
            argv = [str(binpath), "exec", "--skip-git-repo-check", "-"]
            out = runner(argv, input_bytes=b"Reply with exactly one word: "
                                           b"pong\n", env=env, cwd=workdir,
                         deadline_s=180, deadline=deadline)
            version = None
            if out is not None and out.returncode == 0:
                version = _extract_version_line(
                    _as_bytes(out.stdout).decode("utf-8", errors="replace"))
            state_path = (Path(data_dir) / "operator-config" / "codex"
                          / "hooks.state")
            manifest_events = _manifest_events(plugin_root, "codex")
            trusted = _read_trust_state(state_path)
            missing = [event for event in manifest_events
                       if event not in trusted]
            base = dict(sha=_exe_sha256(binpath), version=version,
                        command=argv, manifest_hook_ids=manifest_ids,
                        trust_state=str(state_path))
            if out is None:
                result = build_result("codex-trust", "codex", "fail",
                                      "deadline", **base)
            elif version is None:
                result = build_result(
                    "codex-trust", "codex", "fail", "version-unavailable",
                    **base, notes="the supported exec invocation emitted no "
                    "version line (rc=%s; stdout tail: %s)"
                    % (out.returncode, _as_bytes(out.stdout)
                       .decode("utf-8", errors="replace").strip()[-160:]))
            elif missing:
                missing_ids = []
                for event in missing:
                    for hid in manifest_ids:
                        if hid.split(":")[0] == event:
                            missing_ids.append(hid)
                result = build_result(
                    "codex-trust", "codex", "fail", "untrusted-hook",
                    fired_hook_ids=missing_ids, missing_hook_ids=missing_ids,
                    notes="isolated hooks.state lacks %d manifest event(s)"
                          % len(missing), **base)
            else:
                result = build_result(
                    "codex-trust", "codex", "pass", "trusted-hooks",
                    fired_hook_ids=[], notes="every manifest event present "
                    "in the isolated hooks.state", **base)
    except Exception as exc:  # noqa: BLE001
        result = build_result("codex-trust", "codex", "fail", "lane-error",
                              manifest_hook_ids=manifest_ids,
                              notes="%s: %s" % (type(exc).__name__, exc))
    result = _lane_finalize("codex-trust", "codex", data_dir, result_path,
                            before, snapshot_inventories(data_dir), result)
    return result


def _manifest_events(plugin_root, host):
    try:
        data = json.loads((Path(plugin_root) / "hooks"
                           / ("hooks.%s.json" % host))
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted((data.get("hooks") or {}).keys())


def _read_trust_state(state_path):
    try:
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict):
        events = data.get("hooks") if isinstance(data.get("hooks"), dict) \
            else data
        if isinstance(events, dict):
            return set(str(k) for k in events)
    if isinstance(data, list):
        return set(str(k) for k in data)
    return {}


def run_zcode_duplicate_lane(plugin_root, data_dir, result_path, *,
                             command_runner=None, deadline=None,
                             resolve_executable=None):
    """AC5 (zcode): prove the duplicate-install detector live — two copied
    roots both firing is the duplicate condition; the single-root run passes."""
    plugin_root = Path(plugin_root)
    data_dir = Path(data_dir)
    runner = command_runner or run_command
    resolve = resolve_executable or shutil.which
    manifest_ids = derive_hook_ids("zcode", plugin_root)
    node = resolve("node")
    workdir = data_dir / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    # Run-start setup: the two isolated plugin copies land in host-roots
    # BEFORE the before-snapshot.
    roots = []
    for name in ("zcode-plugin-a", "zcode-plugin-b"):
        root = data_dir / "host-roots" / name
        root.mkdir(parents=True, exist_ok=True)
        for part in ("hooks", "skills/memory/scripts"):
            src = plugin_root / part
            if src.is_dir():
                shutil.copytree(src, root / part,
                                ignore=shutil.ignore_patterns("__pycache__"))
        roots.append(root)
    before = snapshot_inventories(data_dir)
    result = build_result("zcode-duplicate", "zcode", "fail", "unexecuted",
                          manifest_hook_ids=manifest_ids)
    try:
        if not node:
            result = build_result(
                "zcode-duplicate", "zcode", "skip", "node-binary-absent",
                manifest_hook_ids=manifest_ids,
                one_copy={"verdict": "skip", "reason": "single-copy-skip",
                          "root": "host-roots/zcode-plugin-a"},
                notes="node not on PATH; the launcher drive is impossible")
        else:
            version_out = runner([str(node), "--version"],
                                 input_bytes=None,
                                 env=_canary_env("zcode", plugin_root,
                                                 data_dir),
                                 cwd=workdir, deadline_s=15,
                                 deadline=deadline)
            version = None
            if version_out is not None and version_out.returncode == 0:
                version = (version_out.stdout or b"").decode(
                    "utf-8", errors="replace").strip() or None
            fired = []
            for root in roots:
                if _fire_zcode_session(node, root, data_dir, plugin_root,
                                       runner, deadline):
                    fired.append(root.name)
            one_copy_ok = _fire_zcode_session(node, roots[0], data_dir,
                                              plugin_root, runner, deadline)
            dup_detected = len(fired) == 2
            if version is None:
                # Same documented contract as the exec-form/codex lanes: a
                # host binary that emits no version is the structured
                # version-unavailable failure, never a silent pass.
                result = build_result(
                    "zcode-duplicate", "zcode", "fail", "version-unavailable",
                    sha=_exe_sha256(node), version=None, command=[str(node)],
                    manifest_hook_ids=manifest_ids,
                    fired_hook_ids=["SessionStart:zmem-launch.js"]
                    if (fired or one_copy_ok) else [],
                    one_copy={"verdict": "pass" if one_copy_ok else "fail",
                              "reason": "single-copy-pass" if one_copy_ok
                              else "single-copy-fire-failed",
                              "root": "host-roots/zcode-plugin-a"},
                    notes="two-copy roots fired: %s; one-copy root re-fired: "
                          "%s; node emitted no version line"
                          % (",".join(fired) or "none", bool(one_copy_ok)))
            else:
                result = build_result(
                    "zcode-duplicate", "zcode",
                    "fail" if dup_detected else "pass",
                    "duplicate-install" if dup_detected
                    else "single-copy-pass",
                    sha=_exe_sha256(node), version=version,
                    command=[str(node)],
                    manifest_hook_ids=manifest_ids,
                    fired_hook_ids=["SessionStart:zmem-launch.js"]
                    if (fired or one_copy_ok) else [],
                    one_copy={"verdict": "pass" if one_copy_ok else "fail",
                              "reason": "single-copy-pass" if one_copy_ok
                              else "single-copy-fire-failed",
                              "root": "host-roots/zcode-plugin-a"},
                    notes="two-copy roots fired: %s; one-copy root re-fired: "
                          "%s" % (",".join(fired) or "none",
                                  bool(one_copy_ok)))
    except Exception as exc:  # noqa: BLE001
        result = build_result("zcode-duplicate", "zcode", "fail",
                              "lane-error", manifest_hook_ids=manifest_ids,
                              one_copy={"verdict": "skip",
                                        "reason": "single-copy-skip",
                                        "root": "host-roots/zcode-plugin-a"},
                              notes="%s: %s" % (type(exc).__name__, exc))
    result = _lane_finalize("zcode-duplicate", "zcode", data_dir, result_path,
                            before, snapshot_inventories(data_dir), result)
    return result


def _fire_zcode_session(node, root, data_dir, plugin_root, runner, deadline):
    """One real launcher drive under ZCODE_PLUGIN_ROOT=<root>; True when a
    fresh decision line lands in the isolated decisions log (the legacy
    zmem-bg.log fallback is baselined the same way, so stale bytes from a
    reused data dir never count as a fresh firing)."""
    decisions = Path(data_dir) / "zmem-decisions.log"
    pre = decisions.stat().st_size if decisions.is_file() else 0
    legacy = Path(data_dir) / "zmem-bg.log"
    legacy_pre = legacy.stat().st_size if legacy.is_file() else 0
    env = _canary_env("zcode", root, data_dir)
    env["ZCODE_PLUGIN_ROOT"] = str(root)
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "zmem-canary-duplicate-%s" % uuid.uuid4().hex[:8],
        "cwd": str(data_dir / "workdir"),
    }
    out = runner([str(node), str(root / "hooks" / "zmem-launch.js"),
                  "session-start"],
                 input_bytes=json.dumps(payload).encode("utf-8"), env=env,
                 cwd=data_dir / "workdir", deadline_s=180, deadline=deadline)
    if out is None or out.returncode != 0:
        return False
    if decisions.is_file() and decisions.stat().st_size > pre:
        return True
    # Some writers prefer the legacy log; accept either, growth-baselined.
    return legacy.is_file() and legacy.stat().st_size > legacy_pre


def probe_store_path(args, data_dir):
    """AC4: prove the strip+set construction resolves to the isolated fixture
    even with every ambient store var set."""
    for var in STRIP_VARS:
        os.environ.pop(var, None)
    os.environ["ZMEM_STORE"] = str(Path(data_dir) / "store.sqlite")
    os.environ["ZMEM_DATA"] = str(data_dir)
    host_mod = import_host_module(args.plugin_root)
    resolved = host_mod.resolve_store_path().resolve()
    fixture = (Path(data_dir) / "store.sqlite").resolve()
    print("zmem-canary probe store=%s" % resolved)
    ok = resolved == fixture
    # Probe mode still ends in the documented verdict line (mode=probe) so
    # output consumers never meet a contract-less tail.
    verdict_line(args.host, "probe", "pass" if ok else "fail",
                 "-" if ok else "store-escaped", str(resolved), None, "unknown")
    if not ok:
        print("zmem-canary: resolution escaped the fixture (source=ambient)", file=sys.stderr)
        return 1
    return 0


def self_test(args, env, workdir):
    # Resolve: --plugin-root may be relative (e.g. "."), and the launcher is
    # spawned with cwd=workdir — an unresolved path would point node at
    # <workdir>/hooks/zmem-launch.js instead of the plugin tree.
    launcher = Path(args.plugin_root).resolve() / "hooks" / "zmem-launch.js"
    if not launcher.is_file():
        print("zmem-canary: hook not fired — launcher missing at %s" % launcher, file=sys.stderr)
        return EXIT_HOOK_NOT_FIRED, ""
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "zmem-canary-selftest",
        "cwd": str(workdir),
        "meta": {
            "session_id": "zmem-canary-selftest",
            "cwd": str(workdir),
            "hook_event_name": "SessionStart",
        },
    }
    # PR #190 review PRR-009: one shared launcher-drive implementation for
    # both self-test lanes (the docstring on _drive_launcher is now true).
    out = _drive_launcher(launcher, env, workdir, "session-start", payload)
    if out is None:
        return EXIT_HOOK_NOT_FIRED, ""
    return 0, out


LIVE_SESSIONS = {
    "codex": lambda binpath: (
        [binpath, "exec", "--skip-git-repo-check", "-"],
        "Reply with exactly one word: pong\n",
    ),
    "claude": lambda binpath: (
        [binpath, "-p", "--output-format", "text", "Reply with exactly one word: pong"],
        None,
    ),
}


def _drive_launcher(launcher, env, workdir, subcommand, payload, timeout=180):
    """One launcher drive with a fabricated hook payload (shared by the
    self-test and compact-self-test lanes)."""
    try:
        proc = subprocess.run(
            ["node", str(launcher), subcommand],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=str(workdir),
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        print("zmem-canary: hook not fired — cannot spawn node (%s)" % exc, file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print("zmem-canary: hook not fired — launcher timed out", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(
            "zmem-canary: hook not fired — launcher exited %d" % proc.returncode,
            file=sys.stderr,
        )
        print((proc.stderr or "")[-800:], file=sys.stderr)
        return None
    return proc.stdout


def compact_self_test(args, env, workdir):
    """Issue #118: the compaction sequence, end to end through the launcher.

    precompact (snapshot + clear) → postcompact (stash compact_summary) →
    session-start with source=compact (query-aware re-injection). Returns
    (status, stdout-of-the-final-drive).
    """
    launcher = Path(args.plugin_root).resolve() / "hooks" / "zmem-launch.js"
    if not launcher.is_file():
        print("zmem-canary: hook not fired — launcher missing at %s" % launcher, file=sys.stderr)
        return EXIT_HOOK_NOT_FIRED, ""
    sid = "zmem-canary-compact"
    base = {"session_id": sid, "cwd": str(workdir)}
    # PR #190 review PRR-005: only Claude registers PostCompact — driving
    # it on the codex lane would validate a summary-backed query that no
    # real Codex host can produce (upstream carries no compact_summary).
    # Codex instead validates its REAL composition path: a startup
    # delivery populates the ledger, PreCompact snapshots it, and the
    # compact moment composes from the snapshot alone.
    if args.host == "claude":
        drives = [
            ("precompact", {"hook_event_name": "PreCompact",
                            "trigger": "manual"}),
            ("postcompact", {
                "hook_event_name": "PostCompact", "trigger": "manual",
                # PR #190 review PRR-008: the summary is composed FROM
                # MARKER (single source of truth with seed_row) — on a
                # bare interpreter (no embedding model — the CI shape)
                # the recall lane is lexical-only and the #113 relevance
                # floor drops a thin summary match. The exact-token
                # overlap keeps the fixture deterministic with OR without
                # the model.
                "compact_summary": (
                    "Compaction summary: the session was verifying that "
                    "the injection canary probe row %s host canary marker "
                    "still reaches the model after a compaction." % MARKER),
            }),
        ]
    else:
        drives = [
            ("session-start", {"hook_event_name": "SessionStart",
                               "source": "startup"}),
            ("precompact", {"hook_event_name": "PreCompact",
                            "trigger": "manual"}),
        ]
    for sub, extra in drives:
        payload = dict(base)
        payload.update(extra)
        out = _drive_launcher(launcher, env, workdir, sub, payload)
        if out is None:
            return EXIT_HOOK_NOT_FIRED, ""
    payload = dict(base)
    payload.update({"hook_event_name": "SessionStart", "source": "compact"})
    out = _drive_launcher(launcher, env, workdir, "session-start", payload)
    if out is None:
        return EXIT_HOOK_NOT_FIRED, ""
    return 0, out


def _resolve_override(override):
    """Resolve ZMEM_CANARY_HOST_BIN to a runnable path, or None.

    Precedence: the literal path if it is a regular FILE (a directory — or any
    non-runnable path — never resolves, so a typo degrades to the documented
    skip contract instead of a spawn crash); then PATHEXT probing for
    dir-bearing extension-less paths (shutil.which does NOT do this — it
    short-circuits any command with a directory component, so
    'C:/x/python' never finds python.exe); then shutil.which for bare
    names (PATH + PATHEXT). No resolution => None (skip, never fail).
    """
    if Path(override).is_file():
        return override
    pathext = os.environ.get("PATHEXT", "")
    for ext in pathext.split(os.pathsep):
        if not ext:
            continue
        candidate = override + ext
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which(override)


def live_session(args, env, workdir):
    """Returns (status, stdout): status 0 = session ran; 'skip' = binary
    absent; EXIT_SESSION_UNSUPPORTED = no known session form; else a
    hook-not-fired exit code."""
    override = os.environ.get("ZMEM_CANARY_HOST_BIN")
    binpath = _resolve_override(override) if override else shutil.which(
        HOST_BINARIES[args.host])
    if not binpath:
        print(
            "zmem-canary: host binary absent (%s) — skipping"
            % (override or HOST_BINARIES[args.host]),
            file=sys.stderr,
        )
        return "skip", ""
    if args.host not in LIVE_SESSIONS:
        print(
            "zmem-canary: no minimal non-interactive session form known for host "
            "%s yet (reason=host-session-unsupported)" % args.host,
            file=sys.stderr,
        )
        return EXIT_SESSION_UNSUPPORTED, ""
    argv, stdin_text = LIVE_SESSIONS[args.host](binpath)
    try:
        proc = subprocess.run(
            argv,
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(workdir),
            timeout=300,
        )
    except FileNotFoundError as exc:
        print("zmem-canary: hook not fired — cannot spawn host (%s)" % exc, file=sys.stderr)
        return EXIT_HOOK_NOT_FIRED, ""
    except subprocess.TimeoutExpired:
        print("zmem-canary: hook not fired — host session timed out", file=sys.stderr)
        return EXIT_HOOK_NOT_FIRED, ""
    except OSError as exc:
        # Present-but-unspawnable (permission denied, bad executable image):
        # never a verified pass — fail loudly like the other spawn errors.
        print("zmem-canary: hook not fired — host not spawnable (%s)" % exc, file=sys.stderr)
        return EXIT_HOOK_NOT_FIRED, ""
    if proc.returncode != 0:
        # Attribution only: the canary's contract is "hook fired + grounded
        # decision line", so a host that fired the hook and then exits
        # non-zero still passes — but the failure mode must be diagnosable
        # in stderr rather than swallowed.
        print(
            "zmem-canary: host session exited rc=%d" % proc.returncode,
            file=sys.stderr,
        )
        for tail_line in (proc.stderr or "").strip().splitlines()[-5:]:
            print("  host> %s" % tail_line, file=sys.stderr)
    return 0, proc.stdout


def codex_manifest_precheck(host, plugin_root):
    """Issue #108 regression guard: codex-cli >= 0.153.0 silently IGNORES a
    plugin's hooks whose manifest path lacks the ./ prefix. The self-test lane
    drives the launcher directly, so without this check the codex manifest is
    never consulted and a reverted manifest would still green-light. Returns
    None when the contract holds (or no codex manifest exists to check), else
    a human-readable violation message."""
    if host != "codex":
        return None
    manifest = Path(plugin_root) / ".codex-plugin" / "plugin.json"
    if not manifest.is_file():
        return None
    try:
        hooks = json.loads(manifest.read_text(encoding="utf-8")).get("hooks")
    except (OSError, ValueError):
        return "codex manifest contract violated: %s is not parseable JSON" % manifest
    if not isinstance(hooks, str) or not hooks.startswith("./"):
        return (
            "codex manifest contract violated: %s hooks=%r lacks the required "
            "./ prefix — codex-cli >= 0.153.0 silently ignores these hooks" % (manifest, hooks)
        )
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="host_canary.py",
        description="zmem per-host injection canary (issue #108) with the "
                    "nine live host-canary lanes (issue #96)",
    )
    parser.add_argument(
        "--host", choices=HOSTS, default=None,
        help="host under test (required for the legacy modes and --lane)")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="deterministic mode: drive the hook chain directly, no host binary",
    )
    parser.add_argument(
        "--compact-self-test",
        action="store_true",
        help="deterministic compaction lane (issue #118): precompact -> "
             "postcompact -> session-start(source=compact) through the "
             "launcher; requires the query-aware compact branch",
    )
    parser.add_argument(
        "--lane", default=None, choices=sorted(LANE_HOSTS),
        help="run one of the nine live host-canary lanes (issue #96); the "
             "result artifact is written when --result-json is given and "
             "lane mode always exits 0 for pass/fail/skip",
    )
    parser.add_argument(
        "--result-json", dest="result_json", default=None,
        help="write the lane result artifact to this path (requires --lane)")
    parser.add_argument(
        "--validate-result", dest="validate_result", default=None,
        help="validate one canary result JSON against "
             "scripts/canary-schema.json and exit")
    parser.add_argument(
        "--hermes-root", dest="hermes_root", default=None,
        help="pinned Hermes checkout root; required for Hermes lanes")
    parser.add_argument(
        "--hermes-sha", dest="hermes_sha", default="cdf4c76",
        help="pinned Hermes checkout sha the git measurement must match")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="isolation root (default: tmp/zmem-canary-<host>-<pid>-<rand> under the repo)",
    )
    parser.add_argument(
        "--plugin-root",
        default=str(REPO_ROOT),
        help="plugin tree whose hooks/ + skills/memory/scripts are exercised",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="skip seeding (zero-row store: drives the fired-but-empty branch)",
    )
    parser.add_argument(
        "--probe-store-path",
        action="store_true",
        help="print the resolved store path and exit (isolation proof, AC4)",
    )
    args = parser.parse_args(argv)

    if args.validate_result:
        problems = validate_result(args.validate_result)
        if problems:
            print("canary-result INVALID %s" % args.validate_result,
                  file=sys.stderr)
            for problem in problems:
                print("  - %s" % problem, file=sys.stderr)
            return 1
        print("canary-result OK %s" % args.validate_result)
        return 0

    if args.result_json and not args.lane:
        parser.error("--result-json requires --lane")

    if args.lane:
        expected_host = LANE_HOSTS[args.lane]
        if args.host != expected_host:
            parser.error("--lane %s requires --host %s"
                         % (args.lane, expected_host))
        if args.lane.startswith("hermes-") and not args.hermes_root:
            parser.error("--hermes-root is required for Hermes lanes")
        if args.data_dir:
            data_dir = Path(args.data_dir).resolve()
        else:
            data_dir = (
                REPO_ROOT
                / "tmp"
                / ("zmem-lane-%s-%d-%s"
                   % (args.lane, os.getpid(), uuid.uuid4().hex[:8]))
            )
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "workdir").mkdir(parents=True, exist_ok=True)
        plugin_root = Path(args.plugin_root).resolve()
        try:
            if args.lane == "hermes-gateway":
                result = run_hermes_lane(
                    args.lane, Path(args.hermes_root), data_dir,
                    args.hermes_sha, args.result_json,
                    plugin_root=plugin_root)
            elif args.lane == "hermes-provider-mode":
                result = run_hermes_lane(
                    args.lane, Path(args.hermes_root), data_dir,
                    args.hermes_sha, args.result_json,
                    plugin_root=plugin_root)
            elif args.lane == "hermes-compat-mode":
                result = run_hermes_lane(
                    args.lane, Path(args.hermes_root), data_dir,
                    args.hermes_sha, args.result_json,
                    plugin_root=plugin_root)
            elif args.lane == "claude-compact":
                result = run_claude_compact_lane(
                    plugin_root, data_dir, args.result_json)
            elif args.lane == "codex-trust":
                result = run_codex_trust_lane(
                    plugin_root, data_dir, args.result_json)
            elif args.lane == "zcode-duplicate":
                result = run_zcode_duplicate_lane(
                    plugin_root, data_dir, args.result_json)
            else:
                result = run_exec_form_lane(
                    expected_host, plugin_root, data_dir, args.result_json)
        except Exception as exc:  # noqa: BLE001 - usage stays crash-free
            print("zmem-canary: lane %s crashed: %s: %s"
                  % (args.lane, type(exc).__name__, exc), file=sys.stderr)
            return 1
        if not args.result_json:
            # Artifact path withheld: persist nothing, but never mislead.
            result["store"] = "no-result-json"
        result["store"] = result.get("store") or str(
            Path(data_dir) / "store.sqlite")
        _lane_verdict_line(result)
        return 0

    if not args.host:
        parser.error("--host is required (or use --lane / --validate-result)")

    if args.data_dir:
        # Absolute: the driven hook chain resolves ZMEM_DATA/ZMEM_STORE against
        # its own cwd (the scratch workdir), so a relative root would nest.
        data_dir = Path(args.data_dir).resolve()
    else:
        data_dir = (
            REPO_ROOT
            / "tmp"
            / ("zmem-canary-%s-%d-%s" % (args.host, os.getpid(), uuid.uuid4().hex[:8]))
        )
    data_dir.mkdir(parents=True, exist_ok=True)
    workdir = data_dir / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    bg_log = data_dir / "zmem-decisions.log"
    # Both candidates are tracked from here on: review PRR-009 — the
    # selection below runs BEFORE the drive, and on a fresh scratch dir
    # neither log exists yet, so a legacy served tree (pre-split writers)
    # appends its decision line to zmem-bg.log while this script watches
    # zmem-decisions.log. The post-drive check retries the alternate.
    legacy_log = data_dir / "zmem-bg.log"
    if not bg_log.exists():
        # Issue #129 split: decision lines moved to zmem-decisions.log; on
        # a legacy deployment (old served tree, pre-split writers) fall
        # back to the original zmem-bg.log location.
        if legacy_log.exists():
            bg_log = legacy_log

    if args.probe_store_path:
        return probe_store_path(args, data_dir)

    plugin_root = Path(args.plugin_root).resolve()
    env = build_child_env(args.host, plugin_root, data_dir)
    mode = ("compact-self-test" if args.compact_self_test
            else "self-test" if args.self_test else "live")

    manifest_err = codex_manifest_precheck(args.host, plugin_root)
    if manifest_err:
        drift = run_drift_check(plugin_root)
        print("zmem-canary: %s" % manifest_err, file=sys.stderr)
        verdict_line(args.host, mode, "fail", "codex-manifest-contract",
                     env["ZMEM_STORE"], None, drift)
        return EXIT_HOOK_NOT_FIRED

    # Self-test modes drive the launcher ourselves, so a missing launcher (or
    # a broken plugin tree without skills/memory/scripts/host.py) means the
    # hook chain cannot run at all — classify before seeding or spawning.
    if args.self_test or args.compact_self_test:
        if not (plugin_root / "hooks" / "zmem-launch.js").is_file():
            drift = run_drift_check(plugin_root)
            print(
                "zmem-canary: hook not fired — launcher missing at %s"
                % (plugin_root / "hooks" / "zmem-launch.js"),
                file=sys.stderr,
            )
            verdict_line(args.host, mode, "fail", "hook-not-fired",
                         env["ZMEM_STORE"], None, drift)
            return EXIT_HOOK_NOT_FIRED

    # Seed first — the hook must see the row when it fires.
    row_id = None
    if not args.no_seed:
        try:
            host_mod = import_host_module(plugin_root)
            namespace = resolve_namespace(host_mod, workdir)
        except Exception as exc:  # broken plugin tree: host.py unimportable
            drift = run_drift_check(plugin_root)
            print(
                "zmem-canary: hook not fired — plugin tree broken (%s: %s)"
                % (type(exc).__name__, exc),
                file=sys.stderr,
            )
            verdict_line(args.host, mode, "fail", "hook-not-fired",
                         env["ZMEM_STORE"], None, drift)
            return EXIT_HOOK_NOT_FIRED
        row_id = seed_row(env, plugin_root, namespace, data_dir)
        drift = run_drift_check(plugin_root)
        if row_id is None:
            verdict_line(args.host, mode, "fail", "seed-failed", env["ZMEM_STORE"], None, drift)
            return EXIT_SEED_FAILED
        print("seeded id=%s marker=%s" % (row_id, MARKER))

    pre_sizes = {p: (p.stat().st_size if p.exists() else 0)
                 for p in {bg_log, legacy_log}}
    pre_size = pre_sizes[bg_log]
    if args.compact_self_test:
        status, stdout_text = compact_self_test(args, env, workdir)
    elif args.self_test:
        status, stdout_text = self_test(args, env, workdir)
    else:
        status, stdout_text = live_session(args, env, workdir)
        if status == "skip":
            drift = run_drift_check(plugin_root)
            verdict_line(args.host, mode, "skip", "host-binary-absent",
                         env["ZMEM_STORE"], None, drift)
            return 0
    drift = run_drift_check(plugin_root)
    if status != 0:
        reason = ("host-session-unsupported"
                  if status == EXIT_SESSION_UNSUPPORTED else "hook-not-fired")
        verdict_line(args.host, mode, "fail", reason,
                     env["ZMEM_STORE"], row_id, drift)
        return status

    # Issue #118: the compact lane must ground on the COMPACT moment's own
    # decision line — the precompact drive also writes one, and grounding on
    # it would green-light a canary whose re-injection never fired.
    line = fresh_decision_line(
        bg_log, pre_size,
        must_contain=("moment=session_start_compact"
                      if args.compact_self_test else ""))
    if line is None and bg_log != legacy_log:
        # Review PRR-009: the candidate selection ran BEFORE the drive, so
        # on a fresh scratch dir it always picked zmem-decisions.log — a
        # legacy served tree (old served tree, pre-split writers) appends its
        # decision line to zmem-bg.log instead. Retry the alternate candidate
        # (with its own pre-drive size) before declaring hook-not-fired
        # against a hook that fired correctly.
        alt_line = fresh_decision_line(
            legacy_log, pre_sizes[legacy_log],
            must_contain=("moment=session_start_compact"
                          if args.compact_self_test else ""))
        if alt_line is not None:
            line = alt_line
    if line is None:
        print(
            "zmem-canary: hook not fired — no fresh zmem-hook decision line in %s" % bg_log,
            file=sys.stderr,
        )
        verdict_line(args.host, mode, "fail", "hook-not-fired",
                     env["ZMEM_STORE"], row_id, drift)
        return EXIT_HOOK_NOT_FIRED

    if row_id is None:
        print("zmem-canary: fired but empty — unseeded store injected nothing", file=sys.stderr)
        verdict_line(args.host, mode, "fail", "no-row-id", env["ZMEM_STORE"], None, drift)
        return EXIT_NO_ROW_ID
    if not line_ids_ground_row(line, row_id):
        print(
            "zmem-canary: fired but empty — seeded row %s not in the decision ids" % row_id,
            file=sys.stderr,
        )
        verdict_line(args.host, mode, "fail", "no-row-id", env["ZMEM_STORE"], row_id, drift)
        return EXIT_NO_ROW_ID
    if ((args.self_test or args.compact_self_test)
            and MARKER not in envelope_additional_context(stdout_text)):
        print(
            "zmem-canary: fired but empty — marker missing from the rendered fence",
            file=sys.stderr,
        )
        verdict_line(args.host, mode, "fail", "no-row-id", env["ZMEM_STORE"], row_id, drift)
        return EXIT_NO_ROW_ID

    verdict_line(args.host, mode, "pass", "-", env["ZMEM_STORE"], row_id, drift)
    return 0


if __name__ == "__main__":
    sys.exit(main())
