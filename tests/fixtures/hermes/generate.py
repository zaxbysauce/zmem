"""Deterministic fixture generator for issue #160 (provider transport).

Reuses the issue #159 parity-store builder (``tests/fixtures/prefetch/
generate.py``) so both fixture families share one source of truth for the
seeded ``project:parity`` store: two signal=test rows with pinned sentinel
ids ``e0000000-0000-4000-8000-000000000001``/``...0002`` and the fixed store
clock ``2026-06-01T00:00:00Z`` (the repo's ``ZMEM_TEST_NOW`` seam).

The generator runs the CLI ``store.py prefetch`` ONCE with the issue #160
wire shape (``--lane hermes-provider``, session id ``hermes-fixture-session``,
moment ``user_prompt``, query ``stash pop``) against a fresh empty delivery
ledger and writes:

- ``mcp-prefetch.json`` — the exact UTF-8 envelope bytes the #159 command
  emitted (what the MCP ``prefetch`` tool returns to ``McpHttp``), and
- ``expected-envelope.json`` — the same envelope with sorted keys, indent 2,
  and one trailing LF (the repo's expected-fixture convention).

No network call happens anywhere in this generator.  Usage (repo root)::

    python tests/fixtures/hermes/generate.py \
        [--store <scratch>/store.sqlite] [--out tests/fixtures/hermes/mcp-prefetch.json]

Both flags are optional; every run builds a brand-new scratch store, so
re-running reproduces identical fixture bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parent
_PREFETCH_GENERATOR = REPO_ROOT / "tests" / "fixtures" / "prefetch" / "generate.py"

SESSION_ID = "hermes-fixture-session"
MOMENT = "user_prompt"
LANE = "hermes-provider"
RAW_FIXTURE = FIXTURE_DIR / "mcp-prefetch.json"
EXPECTED_FIXTURE = FIXTURE_DIR / "expected-envelope.json"

REQUIRED_KEYS = (
    "results", "count", "omitted", "reason", "excluded", "candidate_ids",
    "tokens_used", "tokens_budget", "budget_dropped", "budget_admission",
    "budget_truncated", "budget_dropped_protected", "arms", "rendered",
)


def _load_prefetch_generator():
    spec = importlib.util.spec_from_file_location(
        "zmem_prefetch_fixture_generator", _PREFETCH_GENERATOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_prefetch_fixture_generator"] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="generate the issue #160 hermes transport fixtures")
    parser.add_argument("--store", default=None,
                        help="scratch store path (default: fresh temp dir)")
    parser.add_argument("--out", default=str(RAW_FIXTURE),
                        help="path of the mcp-prefetch.json fixture")
    args = parser.parse_args(argv)

    own_scratch = False
    if args.store:
        store_path = str(Path(args.store).expanduser().resolve())
    else:
        own_scratch = True
        scratch = tempfile.mkdtemp(prefix="zmem-160-fixture-")
        store_path = os.path.join(scratch, "store.sqlite")

    prefetch_gen = _load_prefetch_generator()
    env = prefetch_gen.isolation_env(store_path)
    prefetch_gen.build_parity_store(store_path, env)
    prefetch_gen.seed_empty_ledger(store_path, SESSION_ID)

    r = prefetch_gen._run(
        env, "prefetch", "--query", prefetch_gen.QUERY,
        "--namespace", prefetch_gen.NAMESPACE,
        "--session-id", SESSION_ID, "--moment", MOMENT, "--lane", LANE)
    if r.returncode != 0:
        raise RuntimeError(
            "store.py prefetch failed: {}\n{}\n{}".format(
                r.returncode, r.stdout, r.stderr))
    try:
        envelope = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("prefetch stdout is not JSON: {}".format(exc))
    missing = [key for key in REQUIRED_KEYS if key not in envelope]
    if missing or not envelope.get("rendered"):
        raise RuntimeError(
            "incomplete prefetch envelope (missing {}): {}".format(
                missing, json.dumps(envelope, sort_keys=True)[:500]))

    raw_path = Path(args.out).expanduser().resolve()
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(r.stdout.rstrip("\n") + "\n",
                        encoding="utf-8", newline="")
    expected_path = EXPECTED_FIXTURE
    expected_path.write_text(
        json.dumps(envelope, sort_keys=True, ensure_ascii=False, indent=2)
        + "\n", encoding="utf-8", newline="")

    print("mcp-prefetch.json sha256=" +
          hashlib.sha256(raw_path.read_bytes()).hexdigest())
    print("expected-envelope.json sha256=" +
          hashlib.sha256(expected_path.read_bytes()).hexdigest())

    if own_scratch:
        import shutil
        shutil.rmtree(str(Path(store_path).parent), ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
