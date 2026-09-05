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

live (default)
              Seeds the scratch store, then runs a minimal non-interactive
              session of the real host binary in the scratch workdir. Host
              binary resolution: $ZMEM_CANARY_HOST_BIN (literal path if it
              exists; extension-less dir-bearing paths resolve via PATHEXT;
              bare names via PATH lookup — see _resolve_override), else PATH
              lookup of the host's default binary name. Absent binary =>
              verdict=skip reason=host-binary-absent, exit 0.

Assertions on every run (post-install canary semantics):
  1. a FRESH ``zmem-hook status=... reason=...`` decision line appears in the
     scratch zmem-bg.log after the drive (no fresh line => fail
     hook-not-fired, exit 2);
  2. the seeded row's UUID is in the line's ``ids=[...]`` — otherwise fail
     no-row-id, exit 3. In --self-test mode the rendered fence (the launcher
     envelope's additionalContext) must additionally contain the marker text;
     live hosts consume the envelope themselves, so there the decision line's
     ids are the fence proxy (the marker check is best-effort on stdout).

The served-tree drift status from issue #107 is printed in the verdict line
(``drift=matched|drifted|unknown``) via skills/memory/scripts/drift.py check;
it is informational and never gates the verdict.

Output contract — the LAST stdout line is always::

    zmem-canary host=<h> mode=<live|self-test> verdict=<pass|fail|skip>
    reason=<slug|-> store=<path> row_id=<uuid|none> drift=<matched|drifted|unknown>

Exit codes: 0 pass/skip; 2 hook-not-fired; 3 no-row-id; 4 seed-failed;
5 host-session-unsupported.

Runs standalone: python scripts/host_canary.py --host claude --self-test
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MARKER = "zmem-canary-probe-row"

# Store-affecting (or lane-altering) ambient vars, stripped by EXACT NAME before
# any child runs. Host-detection vars (CLAUDE_PLUGIN_ROOT / PLUGIN_ROOT /
# ZCODE_PLUGIN_ROOT / ZMEM_HOST) are SET after the strip and never stripped.
STRIP_VARS = (
    "ZMEM_STORE",
    "ZMEM_DATA",
    "CLAUDE_PLUGIN_DATA",
    "ZCODE_PLUGIN_DATA",
    "ZMEM_INJECT",
    "ZMEM_TIER0",
    "ZMEM_CORE_MD",
    "ZMEM_NAMESPACE",
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
    if proc.returncode != 0:
        print("zmem-canary: seed failed rc=%d" % proc.returncode, file=sys.stderr)
        print((proc.stderr or "")[-800:], file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])["id"]
    except (ValueError, KeyError, IndexError):
        print("zmem-canary: seed output not parseable", file=sys.stderr)
        return None


def fresh_decision_line(bg_log, pre_size):
    if not bg_log.exists():
        return None
    data = bg_log.read_text(encoding="utf-8", errors="replace")
    for line in reversed(data.splitlines()):
        if DECISION_LINE_RE.search(line):
            # A fresh line must postdate the pre-drive snapshot; identical old
            # lines (same content) can only appear if the log grew.
            return line if len(data) > pre_size else None
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


def probe_store_path(args):
    """AC4: prove the strip+set construction resolves to the isolated fixture
    even with every ambient store var set."""
    for var in STRIP_VARS:
        os.environ.pop(var, None)
    os.environ["ZMEM_STORE"] = str(Path(args.data_dir) / "store.sqlite")
    os.environ["ZMEM_DATA"] = str(args.data_dir)
    host_mod = import_host_module(args.plugin_root)
    resolved = host_mod.resolve_store_path().resolve()
    fixture = (Path(args.data_dir) / "store.sqlite").resolve()
    print("zmem-canary probe store=%s" % resolved)
    if resolved != fixture:
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
    payload = json.dumps(
        {
            "hook_event_name": "SessionStart",
            "session_id": "zmem-canary-selftest",
            "cwd": str(workdir),
            "meta": {
                "session_id": "zmem-canary-selftest",
                "cwd": str(workdir),
                "hook_event_name": "SessionStart",
            },
        }
    )
    try:
        proc = subprocess.run(
            ["node", str(launcher), "session-start"],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(workdir),
            timeout=180,
        )
    except FileNotFoundError as exc:
        print("zmem-canary: hook not fired — cannot spawn node (%s)" % exc, file=sys.stderr)
        return EXIT_HOOK_NOT_FIRED, ""
    except subprocess.TimeoutExpired:
        print("zmem-canary: hook not fired — launcher timed out", file=sys.stderr)
        return EXIT_HOOK_NOT_FIRED, ""
    if proc.returncode != 0:
        print(
            "zmem-canary: hook not fired — launcher exited %d" % proc.returncode,
            file=sys.stderr,
        )
        print((proc.stderr or "")[-800:], file=sys.stderr)
        return EXIT_HOOK_NOT_FIRED, ""
    return 0, proc.stdout


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


def _resolve_override(override):
    """Resolve ZMEM_CANARY_HOST_BIN to a runnable path, or None.

    Precedence: the literal path if it exists; then PATHEXT probing for
    dir-bearing extension-less paths (shutil.which does NOT do this — it
    short-circuits any command with a directory component, so
    'C:/x/python' never finds python.exe); then shutil.which for bare
    names (PATH + PATHEXT). No resolution => None (skip, never fail).
    """
    if Path(override).exists():
        return override
    pathext = os.environ.get("PATHEXT", "")
    for ext in pathext.split(os.pathsep):
        if not ext:
            continue
        candidate = override + ext
        if Path(candidate).exists() and os.access(candidate, os.X_OK):
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
    return 0, proc.stdout


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="host_canary.py",
        description="zmem per-host injection canary (issue #108)",
    )
    parser.add_argument("--host", required=True, choices=HOSTS)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="deterministic mode: drive the hook chain directly, no host binary",
    )
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
    bg_log = data_dir / "zmem-bg.log"

    if args.probe_store_path:
        return probe_store_path(args)

    plugin_root = Path(args.plugin_root).resolve()
    env = build_child_env(args.host, plugin_root, data_dir)
    mode = "self-test" if args.self_test else "live"

    # Self-test drives the launcher ourselves, so a missing launcher (or a
    # broken plugin tree without skills/memory/scripts/host.py) means the hook
    # chain cannot run at all — classify before seeding or spawning.
    if args.self_test:
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

    pre_size = bg_log.stat().st_size if bg_log.exists() else 0
    if args.self_test:
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

    line = fresh_decision_line(bg_log, pre_size)
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
    if row_id not in line:
        print(
            "zmem-canary: fired but empty — seeded row %s not in the decision ids" % row_id,
            file=sys.stderr,
        )
        verdict_line(args.host, mode, "fail", "no-row-id", env["ZMEM_STORE"], row_id, drift)
        return EXIT_NO_ROW_ID
    if args.self_test and MARKER not in envelope_additional_context(stdout_text):
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
