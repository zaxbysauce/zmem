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

# Frozen pre-split snapshots. Compute via ZMEM_CHAR_RECORD=1 (see RECORD mode).
# Captured from the behavior-identical split (verified byte-identical to
# pre-split `ddce432` store.py) on 2026-08-22. Every surface is LF-normalized
# (CRLF collapsed to LF) and `stats` relative durations are time-normalized
# before hashing, so the freeze is deterministic across OS line endings and
# wall-clock time (the capture method, not just one machine).
ROOT_HELP_SHA = "9f9b0d3824810e64268c9e41a4b9ba57b7abec9399aff4262c0f7ccf9358dcf2"
SUBCMD_SHA = {
    "init": "4a5e07e603d6204196f2a41effe4913b5ed0d7ee57315916bf107f63fb827c72",
    "add": "837c5e6249992e88a5827540c1cce5ad38e671fedbe14f589160339d6f19e8db",
    "recall": "95b03a32b923aaec9cb026b5ee6572318e4e755e417963ce019edfece27e16f3",
    "recent": "b4c03f9a85b29fa2a49dc4014ce63e6e9f8f919c9f17d646643454db67741382",
    "search": "e4d48834bc8a0ed7532da6c5f251649673b7584162386725bc2fb54e66d3f77b",
    "supersede": "a3dc88b6534285b78c83ded09b24fc27c042001f04a7d4b05ddcc4702b4a1cab",
    "get": "fbb05d585a857af7a225dc48da8c20355449a3138e2db39cda55f16a7150f6f7",
    "list": "ccaff6e58329e784b49944101aa52b81a517ba5f72771a3e27f2df7fedd471f9",
    "stats": "1114b0e55b8e4c114ecdeed54f0450a5e4428aaffe9200bf029f78b319b18acb",
    "path": "a42f2f7d925ef7e38798e1d2119bfa4e37dce1fe735b16e915e93948ad0c3377",
    "session-cadence": "c5564e1c3421870b1bfca127b56aca1a1e0272959d61ac89983a919b3d10ae7c",
    "rebuild-fts": "f334a4f49b51ba386f203ba26fcafbf012c666feb261a93fe328a69762afcda9",
    "reembed": "ab523cebea15cc8cbc30f89bd950ce564d6dfc032fbfcf58764d3d492a83cd13",
    "consolidate": "ec7d75e67fe96a043f0a7b4cdd7337cc15e236b43c21aa560c04ca5fdb2b61c9",
    "promote": "563fc9b899ae1768eba2d88cc81329f4eb2e35af4ccb1c915f8f3dafb040381d",
    "rekey-namespace": "b562cc3cc6c1d17bb3213c107a0a0a73e4706e6798d09cd01b372e5759ab33b1",
    "backup": "25b253eac3eaacd8718dab22fc66fbf80351324a67ce6b28317b3b47f542df0f",
    "restore": "790a2b7e46e4c0d551e4a3e42bca6d15555d3221a53cdd646c739d88e19a6d6c",
    "export-pack": "fd4314fbd7acba972b0ddbb3106eb5964d5b487147d148c3fcb2f14864297614",
    "export-jsonl": "5063721b595fe9ceb25c1effcf5cb2901ee9d04a9a96b79bff873d6b668fcb9c",
    "ingest-jsonl": "00663d3d9f55acb732325b670c7cba8684556f9aaeadef8f357ea82e992a9ec9",
    "failures": "dc4453c0479b2025d66f67fb35bcf599023d12beea1c211bfe7a1a4db1b89d60",
    "corrections": "81339f793fe900689fabbcfb4dea12e31222c16eb7907ebf647b3a2b6abeaa71",
    "queue-list": "4fadd3550777991421446d6c6e292f0c1693d5de0abccea962462f2abacead0e",
    "queue-clear": "6331cca34ba06810dfa88b5536a68187c1ce29847d4617303e7bc6d423617de9",
    "mine-history": "395d7e86cc9f3e48f88209dccb8156ca940736f330ad01ca0584deb86f0ed9ac",
    "sweep": "2a84ed3a01faa2c1e0f6187d778506c726b43586022f49d3fa1c3f8015e1b962",
}
DATA_SHA = {
    "stats": "01d4d5cfd861585517eb9818828556a25ae7bcd10cc2e66b142a51d617799328",
    "list": "c2e285928d3ee75154a12e4a61947d6793d3bc8f55edb0b3319e73ff4b75a598",
    "recall": "d9dfc946c923c8c38d76cd7bdf828e09926881d1a33a3d86b031e9074d3d17a0",
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


def _norm(text: str) -> str:
    """Normalize platform-dependent text before hashing so the freeze is
    deterministic across OS line endings, argparse wrap width, and wall-clock
    time."""
    # CRLF vs LF: Windows-touched checkouts write \r\n to stdout; Linux CI \n.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Per-line: argparse HelpFormatter pads columns based on terminal width
    # (shutil.get_terminal_size()), so the same help text wraps/pads
    # differently across runners. Strip trailing whitespace and collapse runs
    # of 2+ spaces (inter-column padding) to a single space, preserving
    # newlines. This is content-preserving (argparse uses single spaces as
    # real separators) but width-insensitive.
    lines = [_WS_RE.sub(" ", ln).rstrip() for ln in text.split("\n")]
    text = "\n".join(lines)
    # Wall-clock relative durations ("201d ago") drift daily from the pinned
    # fixture timestamps; collapse to a sentinel. The absolute ISO timestamps
    # beside them are pinned/static, so only the `Nd ago` prefix moves.
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
    snap["root_help_sha"] = _sha(_norm(r.stdout))
    for cmd in KNOWN_SUBCMDS:
        r = _run_cli({}, cmd, "--help")
        snap.setdefault("subcmd_sha", {})[cmd] = _sha(_norm(r.stdout))

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
        self._assert_sha("root --help", _sha(_norm(r.stdout)), ROOT_HELP_SHA)
        # Every known subcommand must still appear in the root help.
        for cmd in KNOWN_SUBCMDS:
            self.assertIn(cmd, r.stdout)

    def test_each_subcommand_help_surface(self):
        for cmd in KNOWN_SUBCMDS:
            r = _run_cli({}, cmd, "--help")
            self.assertEqual(r.returncode, 0, f"{cmd} --help rc={r.returncode}")
            self._assert_sha(f"{cmd} --help", _sha(_norm(r.stdout)), SUBCMD_SHA.get(cmd, ""))

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
