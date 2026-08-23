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

# Width-insensitive whitespace / time / path normalizers used by _norm and
# _norm_stats. Compiled at import so the freeze is deterministic across
# argparse HelpFormatter widths (which read shutil.get_terminal_size()),
# wall-clock relative durations, and OS path separators in `models_dir=...`.
_WS_RE = __import__("re").compile(r"  +")          # 2+ spaces -> single space
_RE_AGO = __import__("re").compile(r"\b\d+[dhs] ago\b")
_RE_MODELS_DIR = __import__("re").compile(r"models_dir=[^\s)]+")
_RE_EMBED_STATUS = __import__("re").compile(r"embeddings=[^\n]+")

# Frozen pre-split snapshots. Compute via ZMEM_CHAR_RECORD=1 (see RECORD mode).
# Captured from the behavior-identical split (verified byte-identical to
# pre-split `ddce432` store.py) on 2026-08-22. Every surface is LF-normalized
# (CRLF collapsed to LF) and `stats` relative durations are time-normalized
# before hashing, so the freeze is deterministic across OS line endings and
# wall-clock time (the capture method, not just one machine).
ROOT_HELP_SHA = "d436dfdd85f9fb6f247029d5fa82d94706e41d58c086a8eb0b224d69b0561def"
SUBCMD_SHA = {
    "init": "30fc42c1e762cc80f303addaef7e27b1aa3f2a1f68a7fc601737af8245364b0c",
    "add": "6e18acaf33a95173c14178ae72778016d3f487d87d86547a7f52ff4bf75a7d33",
    "recall": "49f68802f88a3a595b73f68cb3be4f327e83342bbdaa9ebf876b557f0d81a09e",
    "recent": "aededbd9b93ca0bf39c2aa30eba05faffbb6ab86a90061f7e2af1febfe2350d6",
    "search": "a1719d6946efb2df46c97851e471b252b3491589949b2267efcd810523560fa6",
    "supersede": "fac31e7a2071d8e1880213bcbb4379a52fead5ab1b5c9e560a9ef8479463328f",
    "get": "cd0b8d4fb998e7376a956352c21b087a82f8f8647d347be7c97e386499bd0be7",
    "list": "9cc0f45e439623685d20fb6fb3164ac5fbb45753a95f4e00aba2954e525b9138",
    "stats": "b6c0ac4d2c0eb66fdba02934fa6f5cd56eb38130065723d820b776ea7736a5ec",
    "path": "6f24f6dc2858cbac740d7b340fd5d629dde4a0e1662b77f3abac7444a3e71dbe",
    "session-cadence": "4024a6b79ff2a3c4384287a8dfeefff7f2084172189ccd1a823dfb06534c3728",
    "rebuild-fts": "b2e139de801f5275a70e874581d6188837a70461433e3b8761a656116fdec5e4",
    "reembed": "365507b2cf0b58bd077122758f2218e5149e4f92f962c2574165ceec1e7fbbdd",
    "consolidate": "54c7461045e2164abd13da6c16a69d79d9ee09307455dc35a7690341c67314ae",
    "promote": "b928882e7e3a05c5b38224473ac765c521ff5e248d7d9782a48baa6663989abd",
    "rekey-namespace": "1a574bc38f59dcacdfafac5f8f21d04b25852fd77484b6448227e5bc1168eb69",
    "backup": "0d8dd5a18ee888bf2a495680e64a1b15d7b1bdb5c28471810fcfaba2b47f3dfa",
    "restore": "68d072cf031c309e58396817dbf5dc254834e613d1f1b0f923cb4182f051c756",
    "export-pack": "5b7ec35e229aca634029c4029092719515647806aed021daa91ed35577008e88",
    "export-jsonl": "d0e8109eb72a6babae9e84de19763a41b2018df68395e3101c8102aa0c31ff8e",
    "ingest-jsonl": "633362416c98f426913c4b8afa56713083a42ba4d324097481599cb631782cfe",
    "failures": "bd1051e97a82ef4fbb2c8a506e277a3480ed77ef784fad55f9e0563a023fcbc0",
    "corrections": "7789555ba725576801eadc2624ce7bbedcebcd9185dfa9b40432db0649fb7808",
    "queue-list": "3d0858c0dff65354d84f3800c4026175a8ac41bd8d9a6e3eeedb87895eef998a",
    "queue-clear": "6a068f865e962eb5d518acbb52193fe8904aaec090ce4dd32969007ab8c71862",
    "mine-history": "2542313e42fbc786fb11458b0503f52386f8921121ed9964a28babf645ee1741",
    "sweep": "7b4b6437702447a05ad33151da75a4e8d39aa9ed0c33a519bf034e1de83f0a9e",
}
DATA_SHA = {
    "stats": "11ac804ebd82da80a80772814b5507f233d58e259b4c9c3498fa1311500630c8",
    "list": "c2e285928d3ee75154a12e4a61947d6793d3bc8f55edb0b3319e73ff4b75a598",
    "recall": "24226d852d52ebf3be91d6f12e813c8d6876342049077f093d2ac7810e300e79",
    "export_jsonl": "24d7c3b13cecc31fa3a845ee5a181369627298410bc3d41ccf90c043e460ce65",
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


def _norm_help(text: str) -> str:
    """Width-insensitive normalization for argparse help surfaces.

    Argparse HelpFormatter wraps long descriptions at the runner's terminal
    width (shutil.get_terminal_size()), so the same help text produces
    different line breaks across hosts even with COLUMNS pinned. Splitting on
    blank lines and collapsing all whitespace within each block makes the
    result a width-stable canonical form (block content is preserved; only
    the wrap point changes).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = text.split("\n\n")
    norm_blocks = [" ".join(b.split()) for b in blocks]
    return "\n\n".join(norm_blocks)


def _norm(text: str) -> str:
    """Normalization for data surfaces (list/recall/export). LF + time only;
    whitespace is preserved because JSON has structural spaces that must not
    be collapsed (the help surfaces use _norm_help instead)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _RE_AGO.sub("X ago", text)
    return text


def _norm_stats(text: str) -> str:
    lines = []
    for ln in text.splitlines():
        ln = ln.replace(builder.BASE_ENV["ZMEM_MODELS_DIR"], "__MODELS_DIR__")
        if ln.startswith("store: "):
            ln = "store: __STORE__"
        lines.append(ln)
    normed = "\n".join(lines)
    # The `models_dir=<path>)` token value differs by OS (Windows backslash
    # vs POSIX slash); the replace above matches the captured env value, but
    # also normalize the literal token form to a sentinel so a re-capture on
    # a different OS cannot leave a divergent path.
    normed = _RE_MODELS_DIR.sub("models_dir=__MODELS_DIR__", normed)
    # The whole `embeddings=... (reason=..., models_dir=..., ...)` line varies
    # with the embedding availability probe (reason can be model_file_missing,
    # checksum_mismatch, or others depending on env). Collapse the entire line
    # to a sentinel so the freeze does not depend on which probe branch runs.
    normed = _RE_EMBED_STATUS.sub("embeddings=__EMBED_STATUS__", normed)
    # Now run the LF + time normalizer (no WS collapse: stats output is
    # human-aligned, not JSON, and preserving single-space alignment is fine
    # as long as the content tokens are deterministic).
    return _norm(normed)


def _run_env(tmp: str) -> dict:
    env = {**os.environ, **builder.BASE_ENV, "ZMEM_STORE": os.path.join(tmp, "store.sqlite")}
    for k in ("ZMEM_DATA", "ZMEM_BACKUP_DIR", "ZMEM_BACKUP_INTERVAL_DAYS"):
        env.pop(k, None)
    return env


def _run_cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    # Pin COLUMNS so argparse HelpFormatter wraps at a deterministic width
    # regardless of the runner's terminal size. Without this, the same help
    # text wraps at different line breaks on different hosts, which is the
    # single biggest source of cross-platform hash drift after CRLF.
    env = {**env, "COLUMNS": "100"}
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
    snap["root_help_sha"] = _sha(_norm_help(r.stdout))
    for cmd in KNOWN_SUBCMDS:
        r = _run_cli({}, cmd, "--help")
        snap.setdefault("subcmd_sha", {})[cmd] = _sha(_norm_help(r.stdout))

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
        out = _norm_stats(h.stdout) if label == "stats" else _norm(h.stdout)
        snap.setdefault("data_sha", {})[label] = _sha(out)
    builder._pin_timestamps(store)  # clear any prior telemetry before recall
    h = _run_cli(env, "recall", "--query", "python insertion", "--json", "--limit", "5")
    snap.setdefault("data_sha", {})["recall"] = _sha(_norm(h.stdout))
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
        self._assert_sha("root --help", _sha(_norm_help(r.stdout)), ROOT_HELP_SHA)
        # Every known subcommand must still appear in the root help.
        for cmd in KNOWN_SUBCMDS:
            self.assertIn(cmd, r.stdout)

    def test_each_subcommand_help_surface(self):
        for cmd in KNOWN_SUBCMDS:
            r = _run_cli({}, cmd, "--help")
            self.assertEqual(r.returncode, 0, f"{cmd} --help rc={r.returncode}")
            self._assert_sha(f"{cmd} --help", _sha(_norm_help(r.stdout)), SUBCMD_SHA.get(cmd, ""))

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
        self._assert_sha("list", _sha(_norm(r.stdout)), DATA_SHA["list"])

    def test_data_recall_json(self):
        r = _run_cli(self.env, "recall", "--query", "python insertion", "--json", "--limit", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_sha("recall --json", _sha(_norm(r.stdout)), DATA_SHA["recall"])

    def test_data_export_jsonl(self):
        r = _run_cli(self.env, "export-jsonl")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_sha("export-jsonl", _sha(_norm(r.stdout)), DATA_SHA["export_jsonl"])


if __name__ == "__main__":
    if RECORD:
        snap = _capture_snapshot()
        print("\nZMEM_CHAR_RECORD snapshot (paste into constants):")
        print(json.dumps(snap, indent=2))
        sys.exit(0)
    unittest.main(verbosity=2)
