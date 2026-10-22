"""Issue #287 (Workstream T PR 8) — in-process MCP hub for the t08 node check.

Launches the REAL hub (hermes-plugin/server/mcp_server.py::build_server) on an
ephemeral loopback port against an ISOLATED scratch store, for
tests/test_t08_broker_remote.js's remote leg. stdlib + repo only (uvicorn
ships with the mcp package the hub already requires).

Contract (issue #287, frozen with tests/test_t08_broker_remote.js):

- Args: ``--store <sqlite path>`` (the hub's isolated store),
  ``--seed <jsonl>`` (optional; each line ``{"step": N, "argv": [...]}`` is
  replayed via ``<repo>/skills/memory/scripts/store.py`` with
  ``ZMEM_STORE=<store>`` pinned, in step order — the
  tests/fixtures/fleet/README.md replay protocol),
  ``--namespace <ns>`` (pinned as ZMEM_MCP_DEFAULT_NS, deterministic only).
- Env discipline: every ambient ZMEM_* var is stripped BEFORE anything is
  imported or spawned; ZMEM_STORE/ZMEM_DATA point at the scratch store so the
  real ~/.zmem / ~/.zcode stores are never touched.
- The hub sets ZMEM_MCP_TOKEN=t08-hub-token for its own auth and accepts
  exactly that token (the node test's remote leg sends it).
- Serves on 127.0.0.1 only. Prints ONE line ``T08_HUB <port>`` (flushed) once
  the socket is bound, then blocks until stdin closes (the parent kills the
  process when done; SIGINT also unwinds cleanly). The inherited stdin is
  QUARANTINED at startup (duplicated off fd 0, which is re-pointed at
  DEVNULL): store.py's CLI reads a private hook payload from stdin when it
  is a pipe, so a hub child inheriting the parent's never-closing stdin
  pipe would deadlock on that read (verified at base — the child sits at
  ~0 CPU until the 30 s subprocess budget kills it).

Fail-fast: any seed replay failure or bind failure prints the reason to
stderr and exits 1 WITHOUT the T08_HUB line, so the node test's "hub died
early" guard reports it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"

HUB_TOKEN = "t08-hub-token"

# Everything the hub (and its store.py subprocesses) could read from the
# operator's environment — stripped so the scratch store is the only store
# this process can ever touch.
_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_INJECT",
    "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    "ZMEM_NAMESPACE", "ZMEM_CROSS_PROJECT", "ZMEM_MCP_TOKEN",
    "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE", "ZMEM_MCP_DEFAULT_NS",
    "ZMEM_MCP_URL", "ZMEM_MCP_MAX_CONCURRENT", "ZMEM_MCP_MAX_CONCURRENT_READ",
    "ZMEM_MCP_MAX_CONCURRENT_WRITE", "ZMEM_MCP_QUEUE_TIMEOUT_S",
    "ZMEM_EMBED_PROFILE", "ZMEM_HOST", "ZMEM_SESSION", "ZMEM_REMOTE_URL",
    "ZMEM_HERMES_MODE", "ZMEM_HERMES_DEADLINE_S",
)

_STARTUP_TIMEOUT_S = 30.0


def _pin_env(store: str, namespace: str) -> None:
    """Strip ambient ZMEM_* and pin the isolated scratch store."""
    for key in _PIN_KEYS:
        os.environ.pop(key, None)
    store_dir = str(Path(store).resolve().parent)
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": str(Path(store).resolve()),
        "ZMEM_DATA": store_dir,
        "ZMEM_MODELS_DIR": os.path.join(store_dir, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZMEM_MCP_TOKEN": HUB_TOKEN,
        "ZMEM_MCP_DEFAULT_NS": namespace,
    })


def _seed(store: str, seed_jsonl: str) -> None:
    """Replay the seed recipe through store.py (README.md:24-29 protocol)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZMEM_")}
    env.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": str(Path(store).resolve()),
        "ZMEM_DATA": str(Path(store).resolve().parent),
        "ZMEM_MODELS_DIR": os.path.join(
            str(Path(store).resolve().parent), "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    recipes = []
    for line in Path(seed_jsonl).read_text(encoding="utf-8").splitlines():
        if line.strip():
            recipes.append(json.loads(line))
    recipes.sort(key=lambda r: r.get("step", 0))
    for recipe in recipes:
        argv = list(recipe.get("argv") or [])
        if not argv:
            raise SystemExit(f"t08-hub: seed step {recipe.get('step')!r} has no argv")
        # issue #287 harness note: the recipe's signal=none default lands at
        # confidence 0.2, below the store's CONFIDENCE_FLOOR (0.25) where no
        # recall lane can ever see the row; the replay pins 0.9 so the seeded
        # content is retrievable from BOTH legs (deviation recorded in the
        # check-author report; applied identically to every leg).
        if argv[0] == "add" and "--confidence" not in argv:
            argv += ["--confidence", "0.9"]
        proc = subprocess.run(
            [sys.executable, str(STORE_PY), *argv],
            capture_output=True, text=True, env=env,
            cwd=str(REPO_ROOT), timeout=120)
        if proc.returncode != 0:
            sys.stderr.write(
                f"t08-hub: seed step {recipe.get('step')} failed "
                f"({proc.returncode}): {(proc.stderr or '')[:400]}\n")
            raise SystemExit(1)


def _quarantine_stdin() -> "int | None":
    """Park the inherited stdin and put DEVNULL on descriptor 0.

    store.py's CLI best-effort reads a private hook payload from stdin when
    stdin is a pipe; a hub child that inherits this process's NEVER-CLOSING
    parent pipe deadlocks on that read (verified at base: the child sits at
    ~0 CPU until the 30 s subprocess budget kills it). Duplicating the real
    handle off fd 0 and re-pointing fd 0 at DEVNULL means every later child
    (seed replays, the hub's store.py tool subprocesses) inherits a stdin
    that answers EOF immediately, while the returned descriptor still
    delivers the parent-closed-stdin EOF the shutdown protocol needs.
    Returns the duplicated descriptor, or None when stdin is unusable (the
    caller then just waits to be killed).
    """
    try:
        dup = os.dup(0)
    except OSError:
        return None
    try:
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
    except OSError:
        return dup
    return dup


def _wait_stdin_eof(dup: "int | None") -> None:
    """Block until the ORIGINAL stdin reaches EOF (parent closed the pipe)."""
    if dup is None:
        # No usable stdin signal — park forever; the parent kills us.
        threading.Event().wait()
        return
    try:
        while True:
            chunk = os.read(dup, 4096)
            if not chunk:
                break
    except OSError:
        pass
    finally:
        try:
            os.close(dup)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="t08 in-process zmem MCP hub (issue #287)")
    parser.add_argument("--store", required=True,
                        help="hub store sqlite path (isolated scratch)")
    parser.add_argument("--seed", default=None,
                        help="optional seed.jsonl recipe to replay first")
    parser.add_argument("--namespace", default="project:fixture-280",
                        help="pinned ZMEM_MCP_DEFAULT_NS (deterministic only)")
    args = parser.parse_args()

    store = str(Path(args.store).resolve())
    Path(store).parent.mkdir(parents=True, exist_ok=True)
    stdin_dup = _quarantine_stdin()
    _pin_env(store, args.namespace)
    if args.seed:
        _seed(store, args.seed)

    try:
        import mcp  # noqa: F401  (the SDK import that gates the hub surface)
    except ImportError:
        sys.stderr.write("SKIP: mcp package absent — t08_hub requires the "
                         "'mcp' package (hermes-plugin/server/requirements."
                         "txt)\n")
        return 0

    sys.path.insert(0, str(SERVER_DIR))
    import mcp_server  # noqa: E402  (imported under the pinned env, like t06)
    try:
        import uvicorn
    except ImportError:  # pragma: no cover - uvicorn ships with mcp
        sys.stderr.write("t08-hub: the uvicorn package is required\n")
        return 1

    hub = mcp_server.build_server(host="127.0.0.1", port=0, use_tls=False)
    app = hub.streamable_http_app()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning",
        access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True,
                              name="t08-hub-uvicorn")
    thread.start()

    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        sys.stderr.write("t08-hub: uvicorn did not start within "
                         f"{_STARTUP_TIMEOUT_S:.0f}s\n")
        return 1
    sockets = getattr(server, "servers", None) or []
    if not sockets:
        sys.stderr.write("t08-hub: uvicorn started without a bound socket\n")
        return 1
    port = int(sockets[0].sockets[0].getsockname()[1])

    # The ONE handshake line; the node test blocks on it.
    print(f"T08_HUB {port}", flush=True)

    try:
        # Block until the parent closes our stdin (its finally block) or the
        # process is killed. The REAL stdin handle lives on stdin_dup (fd 0
        # is DEVNULL now so spawned children never inherit the open pipe).
        _wait_stdin_eof(stdin_dup)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
