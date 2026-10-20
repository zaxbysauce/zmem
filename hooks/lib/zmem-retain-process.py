#!/usr/bin/env python3
"""zmem retain subprocess seam (issue #177, Workstream K PR 6).

Two roles:

1. ``run_retain`` — the shared deadline/kill sequence for EVERY retain
   subprocess spawned by a hook: popen with piped stdio, ``communicate``
   with a timeout, ``wait`` on normal completion; on ``TimeoutExpired``
   the exact kill -> communicate -> wait drain, returning exit code 124.
   Tests drive it with ``FakeRetainProcess`` (tests/test_auto_retain.py).

2. ``main`` — the one-spawn stage driver the Stop and SessionEnd scripts
   invoke.  It runs ``store.py retain-adapt`` (raw host payload on stdin)
   and pipes the normalized rows into ``store.py retain``, collapsing any
   child failure to a single ``[zmem] retain: child warning`` stderr line
   and exiting 0 on every path (fail-open — retention must never break a
   hook).  The stage prints the retain child's ``{"count":N}`` on success.

Boundary: this module never imports storelib, never opens SQLite, and never
touches the correction queue directly (tests/test_auto_retain.py::
ProcessBoundaryTest pins the import surface).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

RETAIN_SUBPROCESS_TIMEOUT_S = 10.0
# The post-kill drain must be bounded too: after kill() a surviving descendant
# that inherited the stdout pipe would hold the read open forever, which is the
# one place the deadline could silently stop being a deadline.
RETAIN_DRAIN_TIMEOUT_S = 2.0


def run_retain(
    argv: Sequence[str],
    input_text: str | None,
    *,
    timeout_s: float = RETAIN_SUBPROCESS_TIMEOUT_S,
    popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> tuple[int, str, str]:
    """Run one retain subprocess under a deadline; never raises.

    Returns ``(exit_code, stdout, stderr)``.  Timeout expiry kills the
    child, drains its pipes with its own bounded timeout, waits for the
    corpse, and answers 124 — the caller never sees a live child or a raise.

    ``errors="replace"`` on the pipes is deliberate: without it a single
    non-UTF-8 byte anywhere in the child's stderr raised
    UnicodeDecodeError *inside* ``communicate``, which is neither
    ``TimeoutExpired`` nor caught here, so it escaped a function documented
    as never raising.  The sibling Hermes seam already passes it.
    """
    try:
        proc = popen_factory(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        # A spawn failure must also be contained: this function promises the
        # caller never sees an exception.
        return (1, "", "")
    try:
        stdout, stderr = proc.communicate(input_text, timeout=timeout_s)
        code = proc.wait()
        return (code, stdout or "", stderr or "")
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=RETAIN_DRAIN_TIMEOUT_S)
        except Exception:
            stdout, stderr = "", ""
        try:
            proc.wait(timeout=RETAIN_DRAIN_TIMEOUT_S)
        except Exception:
            pass
        return (124, stdout or "", stderr or "")
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return (1, "", "")


def _resolve_store_py() -> Path:
    """store.py from this plugin layout (hooks/lib/../skills/...)."""
    root = Path(__file__).resolve().parents[2]
    return root / "skills" / "memory" / "scripts" / "store.py"


def _read_stdin_utf8() -> str:
    """Read stdin as UTF-8.

    Every producer of this payload emits UTF-8, but a bare ``sys.stdin.read()``
    decodes with the process locale codec — cp1252 on a default Windows
    install — so non-ASCII transcript text was corrupted once here and then
    corrupted again in the child.
    """
    buf = getattr(sys.stdin, "buffer", None)
    if buf is None:
        return sys.stdin.read()
    return buf.read().decode("utf-8", errors="replace")


def _retention_enabled() -> bool:
    """Cheap pre-spawn gate check.

    The feature is default off, but every lane read and re-serialized the whole
    transcript — two interpreter starts plus a full parse — before the store
    child ever consulted the gate.  Checking the env override and the config
    file here means a default install pays nothing.

    The store-side reader remains authoritative; this only refuses to start
    work it can already prove is unwanted.  An unresolvable store path returns
    True (do the work) rather than silently skipping.
    """
    raw = os.environ.get("ZMEM_AUTO_RETAIN", "")
    if raw == "0":
        return False
    if raw == "1":
        return True
    store = os.environ.get("ZMEM_STORE") or ""
    data = os.environ.get("ZMEM_DATA") or ""
    if store:
        base = Path(store).expanduser().resolve().parent
    elif data:
        base = Path(data).expanduser().resolve()
    else:
        return True
    try:
        with open(base / "config.json", "rb") as handle:
            config = json.loads(handle.read().decode("utf-8-sig"))
    except (OSError, ValueError):
        return False
    if not isinstance(config, dict):
        return False
    value = config.get("auto_retain", False)
    if not isinstance(value, bool):
        sys.stderr.buffer.write(
            b"[zmem] retain: ignoring non-boolean auto_retain in config.json\n")
        sys.stderr.buffer.flush()
        return False
    return value


def _retention_log() -> Path | None:
    """The retention diagnostic sidecar, in the store's data directory.

    Hook stderr is not a usable channel here: the Claude/Codex/ZCode Stop hook
    runs its whole python block with ``2>/dev/null`` so nothing it prints can
    corrupt the envelope, which means a diagnostic forwarded to stderr on that
    lane is discarded at the source.  The sidecar is the same fail-open,
    append-only shape the recall hook uses for its decision log.
    """
    store = os.environ.get("ZMEM_STORE") or ""
    data = os.environ.get("ZMEM_DATA") or ""
    if store:
        base = Path(store).expanduser().resolve().parent
    elif data:
        base = Path(data).expanduser().resolve()
    else:
        return None
    return base / "zmem-retain.log"


def _log_diagnostic(line: str) -> None:
    """Append one diagnostic to the sidecar; never raise, never print a path."""
    target = _retention_log()
    if target is None:
        return
    try:
        with open(target, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(line.rstrip("\n") + "\n")
    except Exception:
        pass


def _forward(code: int, stderr: str, stage: str) -> None:
    """Record the child's own diagnostic, whether or not it exited non-zero.

    Both retention verbs exit 0 on every designed failure (fail-open, so a
    retention problem can never break a host hook), which means a
    nonzero-exit gate here would record nothing for exactly the diagnostics
    that matter — "skipped malformed row", "queue write failed", "invalid
    ZMEM_AUTO_RETAIN". Record on ANY stderr output, and add the exit-code
    line only when the child also failed outright.
    """
    detail = [line.strip() for line in (stderr or "").splitlines()
              if line.strip()]
    if not detail and code == 0:
        return
    if detail:
        message = "[zmem] retain: %s: %s" % (stage, detail[0][:200])
    else:
        message = "[zmem] retain: %s failed (exit %d)" % (stage, code)
    _log_diagnostic(message)
    sys.stderr.buffer.write((message + "\n").encode("utf-8"))
    sys.stderr.buffer.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="zmem retention stage driver (issue #177)")
    parser.add_argument("--host", dest="host", required=True,
                        help="host lane for provenance")
    parser.add_argument("--session", dest="session", required=True,
                        help="session id for provenance")
    parser.add_argument("--namespace", dest="namespace", required=True,
                        help="queue namespace")
    parser.add_argument("--transcript", dest="transcript", default="-",
                        help="raw payload path; - reads stdin")
    args = parser.parse_args(argv)

    try:
        if not _retention_enabled():
            # Default off: no transcript read, no child, no queue, no store.
            sys.stdout.buffer.write(b'{"count":0,"ok":true}\n')
            sys.stdout.buffer.flush()
            return 0
        store_py = str(_resolve_store_py())
        payload = (
            _read_stdin_utf8()
            if args.transcript == "-"
            else open(args.transcript, "r", encoding="utf-8",
                      errors="replace").read()
        )
        adapt_argv = [
            sys.executable, store_py, "retain-adapt", "--input", "-",
            "--namespace", args.namespace,
            "--session", args.session,
            "--host", args.host,
        ]
        code_a, out_a, err_a = run_retain(
            adapt_argv, payload, timeout_s=RETAIN_SUBPROCESS_TIMEOUT_S)
        _forward(code_a, err_a, "adapter")
        if code_a != 0:
            return 0
        retain_argv = [
            sys.executable, store_py, "retain", "--input", "-",
            "--namespace", args.namespace,
            "--session", args.session,
            "--host", args.host,
        ]
        code_r, out_r, err_r = run_retain(
            retain_argv, out_a, timeout_s=RETAIN_SUBPROCESS_TIMEOUT_S)
        _forward(code_r, err_r, "classify")
        if code_r != 0:
            return 0
        sys.stdout.buffer.write(
            (out_r if out_r.endswith("\n") or not out_r else out_r + "\n")
            .encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    except Exception as exc:  # fail open — retention must never break a hook
        message = ("[zmem] retain: stage warning (%s)"
                   % exc.__class__.__name__)
        _log_diagnostic(message)
        sys.stderr.buffer.write((message + "\n").encode("utf-8"))
        sys.stderr.buffer.flush()
        return 0


if __name__ == "__main__":
    sys.exit(main())
