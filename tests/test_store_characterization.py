"""CLI characterization suite for the store.py → storelib split (issue #57, 2.1).

Freezes two kinds of pre-split behavior so the refactor MUST stay green:

1. Public CLI surface — the root `store.py --help` output and each subcommand's
   `--help` output are hashed. If a subparser disappears, gets renamed, or a
   flag/required-arg changes, the hash breaks. The root help must list exactly
   the frozen set of subcommands.
2. Deterministic data output — on a fixed, seeded fixture store (rebuilt by
   `tests/fixtures/store_builder.py`, model-absent), the stdout of `stats`,
   `list --json`, `recall --json` and `export-jsonl` is hashed. `stats` embeds
   the resolved store path and models dir (both location-specific), which are
   normalized to sentinels before hashing; `list/recall/export` carry only
   stored data, which the builder pins to fixed timestamps.

Run: `python tests/test_store_characterization.py` (no pytest required).
Model-absent by construction.

Rebasing (only when behaviour intentionally changes): set ZMEM_CHAR_RECORD=1
and the test prints the current snapshot JSON to stderr instead of asserting.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
import store_builder as builder  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

RECORD = os.environ.get("ZMEM_CHAR_RECORD") == "1"

# Frozen pre-split snapshots. Compute via ZMEM_CHAR_RECORD=1 (see RECORD mode).
# Captured from `ddce432` (pre-split store.py) on 2026-08-22 — deterministic
# across runs (verified: build-twice hashes identical).
ROOT_HELP_SHA = "421bb48e35ed1a0680bb7092181e344d29f910bcf6290e55963f21a05e8130e5"
SUBCMD_SHA = {
    "init": "4fdbcd3dcadf19c560ca99b39dac2697f092737a9f46a4510f367954968ba225",
    "add": "eb4305c79535114ec9013a0d5373c5ee072031a794b7fd6f02527cf479c530fa",
    "recall": "5b7903ecc356527a7f0082178cd598bcc7cd45c4728651f47dc17092374aaedd",
    "recent": "dcd566553c516e9a228719b586d0e7510230eb7dc2bfa8a0a25904c05c80d06e",
    "search": "26e223db97b3d663d714cf984a90b79cfd78c88db18e21c81e3cd5c555cb416f",
    "supersede": "3ea4ca935bb5702918d02009d470cc0e83197698c93d9267f005b9cc971f2b90",
    "get": "5f5242c92fa699673d04114fa80b4f077232c96f9582e8f49978dfc772b32caa",
    "list": "93b69486e65495856b682ec5393b750206e2cf026fee94744bc6f248daa572e2",
    "stats": "4b3871de31bfeba3c0f2e8f0c1826651676d652930045b4a256a2403cf2345b7",
    "path": "d9d2d5e45d7fbfe6de017e727f0b4f4a3818e3af861cfbd6259cf1d23f1f571a",
    "session-cadence": "90059a3c99208a8700ab45ee39e8639f48d79b1c69a1dfbb3bddb2e1d78d6aa1",
    "rebuild-fts": "66b57c6a964f653087b97d1e6108cab82fe05cd7a777f5c74825abf310873562",
    "reembed": "87f2c1488cc77db41add8a28f1b8e8206349af6dd26706543daa2e01bd74e15a",
    "consolidate": "d76e6d88469f5cefc03e646e626197d06c11a5e3c7b60cfcbb2bcfa4789de0c6",
    "promote": "064699be820d08fd4bc001fafd1e0a71a9f2bfcb855bfcda916f3dd40dd84e64",
    "rekey-namespace": "08d5fb2c4b00d4bc7b4889686336dd71c1165692cc3af1176516ab38d4b6820d",
    "backup": "aee393da7c6c3f507fa8319588b26fb27a66e3082596e20086e68963bc63e092",
    "restore": "060c1d35a0b6206b0d5a77678df4fa334aa7320790c3a9c1d72fc39b87863c10",
    "export-pack": "651e5cc779e3b3dc87d0177843e8c07b7ea9655e5540ab7cc9236339c5ac4e6c",
    "export-jsonl": "1ba54507d26f0f563a84cb3e9074a0f61f0e5afc41b70744af5b6896d35ee90e",
    "ingest-jsonl": "7e1ed3c2c9dc745f301e5fec27db779d3c741de9ff3f98d8cd260ab4d2436159",
    "failures": "9be60158a2be8bb0d7697a4d6284b6eecbbb893e5b611d9808182e63c31dac5b",
    "corrections": "009deb75d922750efe2f9a9f8b17d6a89cb26f4eb9aae27484f7ffb359e9a396",
    "queue-list": "e97276152716ccc12db6869912f806fed470f073cd7261dae95c9f4c830e7e87",
    "queue-clear": "2046822f7094a9369c3ff2c1c2a841b3ed5389bfe60633d454de5a50772629f2",
    "mine-history": "374e023c814bf515098777c77213f700dd263a3dcec97936f33ad9d42df98aaa",
    "sweep": "c3dde0db83c036e8066f5d7259c085a37b39d44788dbdf68ab13b1aad6c30c91",
}
DATA_SHA = {
    "stats": "5e0047663b98ceac4fa0c510c059d9610ac5cbbfd008917543d310956e8aa788",
    "list": "dc17f07711c260a67882f22898e22adda2b9a771f8ea1c74cdfdcdc10dd5e2d2",
    "recall": "99410cf89a456ce111eaf9af2c92f7d36fc0aa8fab1b64eeeec1059ac7529b42",
    "export_jsonl": "6392c1c33c4344e3c9454f207791da73d21c17933371e8855a2d2d6f72e9ed74",
}
KNOWN_SUBCMDS = [
    "init", "add", "recall", "recent", "search", "supersede", "get", "list",
    "stats", "path", "session-cadence", "rebuild-fts", "reembed", "consolidate",
    "promote", "rekey-namespace", "backup", "restore", "export-pack",
    "export-jsonl", "ingest-jsonl", "failures", "corrections", "queue-list",
    "queue-clear", "mine-history", "sweep",
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm_stats(text: str) -> str:
    lines = []
    for ln in text.splitlines():
        ln = ln.replace(builder.BASE_ENV["ZMEM_MODELS_DIR"], "__MODELS_DIR__")
        if ln.startswith("store: "):
            ln = "store: __STORE__"
        lines.append(ln)
    return "\n".join(lines)


def _run_env(tmp: str) -> dict:
    env = {**os.environ, **builder.BASE_ENV, "ZMEM_STORE": os.path.join(tmp, "store.sqlite")}
    for k in ("ZMEM_DATA", "ZMEM_BACKUP_DIR", "ZMEM_BACKUP_INTERVAL_DAYS"):
        env.pop(k, None)
    return env


def _run_cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    r = subprocess.run([PYTHON, str(STORE_PY), *args], env=env,
                       capture_output=True, timeout=90)
    # Child runs with PYTHONIOENCODING=utf-8 (see builder.BASE_ENV), so bytes
    # decode deterministically. errors="replace" guards an edge case but should
    # never trigger under the forced encoding.
    r.stdout = r.stdout.decode("utf-8", errors="replace")
    r.stderr = r.stderr.decode("utf-8", errors="replace")
    return r


def _capture_snapshot() -> dict:
    snap: dict = {}
    r = _run_cli({}, "--help")
    snap["root_help_sha"] = _sha(r.stdout)
    for cmd in KNOWN_SUBCMDS:
        r = _run_cli({}, cmd, "--help")
        snap.setdefault("subcmd_sha", {})[cmd] = _sha(r.stdout)

    tmp = tempfile.mkdtemp(prefix="zmem-char-snap-")
    store = builder.build_store(tmp)
    env = _run_env(tmp)
    # Read-only surfaces first. `recall` bumps telemetry (writes a fresh now
    # to last_retrieved/last_surfaced), so it runs LAST so its now-write can't
    # leak into an earlier target. recall's own printed rows are read at fetch
    # time (pinned sentinels) so its hash stays stable.
    for label, argv in [
        ("stats", ("stats",)),
        ("list", ("list",)),
        ("export_jsonl", ("export-jsonl",)),
    ]:
        h = _run_cli(env, *argv)
        out = _norm_stats(h.stdout) if label == "stats" else h.stdout
        snap.setdefault("data_sha", {})[label] = _sha(out)
    builder._pin_timestamps(store)  # clear any prior telemetry before recall
    h = _run_cli(env, "recall", "--query", "python insertion", "--json", "--limit", "5")
    snap.setdefault("data_sha", {})["recall"] = _sha(h.stdout)
    return snap


class CharacterizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="zmem-char-")
        cls.store = builder.build_store(cls.tmp)
        cls.env = _run_env(cls.tmp)

    def _assert_sha(self, label: str, actual: str, expected: str):
        self.assertEqual(
            actual, expected,
            f"\n\n### {label} hash drifted ###\n  actual   = {actual}\n  expected = {expected}\n"
            "The storelib split must be behavior-identical. If this change is "
            "intentional, rebase via ZMEM_CHAR_RECORD=1.\n",
        )

    def test_root_help_surface(self):
        r = _run_cli({}, "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_sha("root --help", _sha(r.stdout), ROOT_HELP_SHA)
        # Every known subcommand must still appear in the root help.
        for cmd in KNOWN_SUBCMDS:
            self.assertIn(cmd, r.stdout)

    def test_each_subcommand_help_surface(self):
        for cmd in KNOWN_SUBCMDS:
            r = _run_cli({}, cmd, "--help")
            self.assertEqual(r.returncode, 0, f"{cmd} --help rc={r.returncode}")
            self._assert_sha(f"{cmd} --help", _sha(r.stdout), SUBCMD_SHA.get(cmd, ""))

    def test_data_stats(self):
        r = _run_cli(self.env, "stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_sha("stats", _sha(_norm_stats(r.stdout)), DATA_SHA["stats"])

    def test_data_list(self):
        r = _run_cli(self.env, "list")
        self.assertEqual(r.returncode, 0, r.stderr)
        # NOTE: `list` has no `--json` flag today (issue #57 said "list --json",
        # but the real CLI emits a human format). Characterize the real surface.
        self.assertTrue(r.stdout.strip(), "list emitted no output")
        self._assert_sha("list", _sha(r.stdout), DATA_SHA["list"])

    def test_data_recall_json(self):
        r = _run_cli(self.env, "recall", "--query", "python insertion", "--json", "--limit", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_sha("recall --json", _sha(r.stdout), DATA_SHA["recall"])

    def test_data_export_jsonl(self):
        r = _run_cli(self.env, "export-jsonl")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_sha("export-jsonl", _sha(r.stdout), DATA_SHA["export_jsonl"])


if __name__ == "__main__":
    if RECORD:
        snap = _capture_snapshot()
        print("\nZMEM_CHAR_RECORD snapshot (paste into constants):")
        print(json.dumps(snap, indent=2))
        sys.exit(0)
    unittest.main(verbosity=2)
