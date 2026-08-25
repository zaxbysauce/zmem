"""CLI characterization suite for the store.py → storelib split (issue #57, 2.1).

Freezes two kinds of pre-split behavior so the refactor MUST stay green:

1. Public CLI surface — structural assertions on `store.py --help` and each
   subcommand's `--help`: KNOWN_SUBCMDS must all appear in root help; each
   subcommand's --help must mention its name, include `usage:` and
   `options:` sections, and expose at least one flag. If a subparser
   disappears, gets renamed, or a flag/required-arg changes, the assertion
   fires. (We do NOT hash rendered help text: argparse HelpFormatter's
   wrap/blank-line placement differs across Windows/Linux Python 3.11
   runners even at a fixed COLUMNS=100 and cannot be normalized into a
   stable cross-platform hash. The structural surface IS the CLI
   contract; the rendered text is presentation.)

2. Deterministic data output — on a fixed, seeded fixture store (rebuilt by
   `tests/fixtures/store_builder.py`, model-absent), the stdout of `stats`,
   `list`, `recall --json` and `export-jsonl` is hashed. `stats` embeds the
   resolved store path, models dir, and embedding-availability reason (all
   env-dependent), which are normalized to sentinels before hashing;
   `list/recall/export` carry only stored data, which the builder pins to
   fixed timestamps.

Run: `python tests/test_store_characterization.py` (no pytest required).
Model-absent by construction.

Rebasing (only when behaviour intentionally changes): set ZMEM_CHAR_RECORD=1
and the test prints the current data_sha snapshot JSON to stderr instead of
asserting. Help-surface hashes are no longer frozen (see note above).
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
# pre-split `ddce432` store.py) on 2026-08-22.
#
# "recall" and "export_jsonl" were RE-CAPTURED on 2026-08-24 for issue #59
# (schema v9) — twice: once when the shipped surfaces gained the v9 lineage
# fields (recall --json result dicts and export-jsonl rows now carry
# `valid_until` / `update_of` / `taint`, plan S3), and again after the
# swarm-pr-review round fixed the fixture (PRR-Q: `_pin_timestamps` used to
# stamp a past `valid_until` onto LIVE rows, whose "never expires" marker is
# the empty string; live rows now keep `''` per the live <=> empty-valid_until
# invariant). `stats` and `list` are byte-identical to their pre-v9 freeze.
#
# "recall" was RE-CAPTURED AGAIN on 2026-08-24 for issue #60 (schema v10):
# every recall row now carries the `entities` card list, and the pinned query
# ("python insertion") legitimately fires the new third RRF lane + MMR
# re-order on the fixture's tagged rows (the fixture builder adds
# `python,language`-tagged rows, so alias `python` matches). Fixture
# invariants re-checked before this freeze: live rows keep valid_until=''
# (the PRR-Q invariant), and the fixture rows/timestamps are unchanged —
# only the shipped recall surface changed. `stats`, `list`, and
# `export_jsonl` are byte-identical to their pre-v10 freeze (entities are
# deliberately NOT part of the export contract).
#
# NOTE on help surfaces: the argparse HelpFormatter renders wrapping, blank-line
# placement, and indentation from a combination of terminal width and
# Python-platform internals that we cannot fully normalize across Windows /
# Linux runners (multiple normalization strategies — CRLF collapse, COLUMNS
# pinning, block-collapse, line-collapse, whitespace runs — still produce
# platform-divergent bytes for `argparse --help`). The CLI contract is the
# *structural* surface (which subcommands exist, which flags each takes, that
# `--help` exits 0 and mentions the subcommand), not the rendered text. The
# help tests below therefore use structural assertions on KNOWN_SUBCMDS +
# per-subcommand flag presence, NOT a text hash. Data surfaces (stats/list/
# recall/export-jsonl) ARE stable across platforms with the normalizers in
# this file and keep their hash freeze.
DATA_SHA = {
    "stats": "11ac804ebd82da80a80772814b5507f233d58e259b4c9c3498fa1311500630c8",
    "list": "c2e285928d3ee75154a12e4a61947d6793d3bc8f55edb0b3319e73ff4b75a598",
    # RE-CAPTURED for v10 (issue #60): recall rows gained the `entities`
    # card key (see freeze note above). stats/list/export_jsonl unchanged.
    "recall": "c3719ff860497cd86da713217c1613e50fe33cae4adaf3d0118d1befaf448529",
    "export_jsonl": "8552767c0f9148e2b3c5d5d8759148837186386cdc78d309457d98ffa2d8ac73",
}
KNOWN_SUBCMDS = [
    "init", "add", "invalidate", "recall", "recent", "search", "supersede",
    "update", "get", "list",
    "stats", "path", "session-cadence", "rebuild-fts", "reembed", "consolidate",
    "promote", "rekey-namespace", "backup", "restore", "export-pack",
    "export-jsonl", "ingest-jsonl", "failures", "corrections", "queue-list",
    "queue-clear", "mine-history", "sweep",
    # v10 (issue #60): the entity identity inspection/reconciliation surface.
    "entity-list", "entity-merge",
]

# Subcommands whose argparse parser exposes ONLY the universal -h/--help
# option (no custom flags). These pass the "exposes at least one flag" check
# by definition; they are tracked here so the per-subcommand assertion is
# NON-vacuous (a regression that drops a real flag must trip the test).
# Adding a subcommand here without removing its real flags from the CLI is
# the only way this allowlist grows, so it forces a conscious decision.
FLAGLESS_SUBCMDS = frozenset({"init", "stats", "path", "rebuild-fts", "reembed"})


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
    """Re-capture the data-surface hashes (help surfaces use structural
    assertions, not hashes — see the freeze note above)."""
    snap: dict = {}
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
        # Every known subcommand must still appear in the root help.
        for cmd in KNOWN_SUBCMDS:
            self.assertIn(cmd, r.stdout,
                f"root --help no longer lists subcommand {cmd!r} — argparse "
                f"subparser wiring changed and the CLI surface drifted")
        # Structural sanity: argparse emits 'usage:' and 'options:' sections.
        self.assertIn("usage:", r.stdout)
        self.assertIn("options:", r.stdout)
        # Top-level 'store.py' invocation appears in the usage line.
        self.assertIn("store.py", r.stdout)

    def test_each_subcommand_help_surface(self):
        for cmd in KNOWN_SUBCMDS:
            r = _run_cli({}, cmd, "--help")
            self.assertEqual(r.returncode, 0, f"{cmd} --help rc={r.returncode}")
            out = r.stdout
            # Structural assertions (CLI contract, not rendered text — argparse
            # HelpFormatter's wrap/blanks differ across Windows/Linux even at
            # fixed COLUMNS=100 and cannot be normalized into a stable hash).
            self.assertIn(cmd, out,
                f"{cmd} --help must reference the subcommand name in its "
                f"usage line")
            self.assertIn("usage:", out)
            self.assertIn("options:", out)
            # Every subcommand in the `options:` section exposes at least one
            # CUSTOM flag (argparse always renders `-h, --help` too — exclude
            # those, otherwise the assertion is vacuous on flag-less
            # subcommands like `init` or `path` and cannot detect a regression
            # that drops a real flag). Subcommands that legitimately have only
            # the universal help option must be in FLAGLESS_SUBCMDS.
            in_options = False
            custom_flags = []
            for ln in out.splitlines():
                if ln.startswith("options:"):
                    in_options = True
                    continue
                if not in_options or not ln.strip():
                    continue
                stripped = ln.lstrip()
                if not stripped.startswith("-"):
                    continue
                token = stripped.split()[0].rstrip(",")
                if token in ("-h", "--help"):
                    continue
                custom_flags.append(token)
            if cmd in FLAGLESS_SUBCMDS:
                self.assertEqual(
                    custom_flags, [],
                    f"{cmd} is in FLAGLESS_SUBCMDS but its --help now exposes "
                    f"custom flags {custom_flags!r} — remove it from the "
                    f"allowlist (or the flag was added, update the freeze).",
                )
            else:
                self.assertGreater(
                    len(custom_flags), 0,
                    f"{cmd} --help exposes no custom flags (only -h/--help) — "
                    f"either this subcommand lost its real flags (regression), "
                    f"or it has always been flag-less and was missed when "
                    f"FLAGLESS_SUBCMDS was last reviewed.",
                )

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
