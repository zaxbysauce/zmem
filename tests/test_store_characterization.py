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
# Captured from the behavior-identical split (verified byte-identical to
# pre-split `ddce432` store.py) on 2026-08-22. Every surface is LF-normalized
# (CRLF collapsed to LF) and `stats` relative durations are time-normalized
# before hashing, so the freeze is deterministic across OS line endings and
# wall-clock time (the capture method, not just one machine).
ROOT_HELP_SHA = "7522d1c79f7232fb4ad1dc72ad79f0658cd37da9c73a37ef4bc3bbb9056f9f59"
SUBCMD_SHA = {
    "init": "c6248e4bf68fcb949e1af8bf62a73c8169f1bef672f7e56cf6e52b3961a252f0",
    "add": "ea3811a4d2212bef6e36f13dd9ba16362aea4ce53bed529eae5e80b501a74ad0",
    "recall": "4b1852913678bd550d40691e4166354e28cdec43a43123c42ba6b3963a3359d2",
    "recent": "317a60ef86d11c72afba69b92d12c69b026aa57275b70bc4df4c4a0acf13ca8b",
    "search": "938f20685ffc79cd09178bcab1af1d28d3758340fb35489455ed43fbc2b93c7a",
    "supersede": "0b9e5755e77671c1fa7785857c63477003dff54a94d524660274eb2073f0a578",
    "get": "1735bf227e590c7c2fb5b0b9171d173fddd0c2ee4a9b0db8e5c593fddc116510",
    "list": "71e3a288e92ab6a3ddf287e8854782e9988b704c70e532564b852b25b6e3df10",
    "stats": "b52bd98a6495f0f4648be512a7ac5cc09670141f2d93fddcd7a2830c6a844c77",
    "path": "52d7dbd661ed9b94a86e23c3aa4ab6ea6ceeb67057dae0b6c56d1905d7804190",
    "session-cadence": "f8ef0eecaa355c560943cbefaa10926017afa9cb7768573f2d1384b867614bf8",
    "rebuild-fts": "80e8c88d05adba518aab2778e4a8084654043a0e81b44691cbc9f53f33d4917a",
    "reembed": "64ecd65f18aa4a37b9738b5d4db973b4e32ed63bf4ea373c8771a4891072193c",
    "consolidate": "bff160270d072deaf35ed860e795f6051cc0cbb518d32a918c1e1c77d6fededf",
    "promote": "4bd6ac0be86583b09cb35a3b8d605280c182b88afaef633cb71d91837d3a0d84",
    "rekey-namespace": "aeb2f4f5502972b35a9a6f2da4fc605b6ba92767feccdb6e4e53072b89f80e2e",
    "backup": "c688129c5cc2af61112db9ec4891bc0c7d544e244bee57acfb3d1777e78bc929",
    "restore": "6b2302a2dffb9766f29bcb31cd71224b990d608e7dfd3b72f21950927775ea8e",
    "export-pack": "93e83153cb1a79aa98ed0d8583957cc974bdad65342500fd06ba41636b6e07fa",
    "export-jsonl": "d6d3fb3c8f8e653da7635df70152b7e7f226409e71d85a34467a618f21ba7b56",
    "ingest-jsonl": "45b4a299c658f7588bd468cfcc28eaea97c5dae487a4d3dd0a5463e465c0d232",
    "failures": "3c6924a8cb34ffce47554c4d1ea7aa1ef57457b911eed1df3bbc1c18538061da",
    "corrections": "dbc812034ec316cbbde8d32c16d80f81bf0abf6a9da3cb7c81d8c2ab22b10400",
    "queue-list": "00ce96c369a3b136c441db89ffce3de574e1d4a8de508da12c16f72a308a485f",
    "queue-clear": "8db9216aff1b5f201c8c97d8190498899e06237caad162f474c71be6302c60cc",
    "mine-history": "9d75be80028c6209b83afa189d31f23044d9243f48655b6f9e32a5fc6e31e671",
    "sweep": "a4470aad9d62bfa4daf1d6fe61386218b8fcf2c6b13e0b34ab6c101161e4c6f0",
}
DATA_SHA = {
    "stats": "a57af2ea9cc6979991b5a326ad2b00a4a66c8b7df78acf3f4315cc27b01b42e7",
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


def _norm(text: str) -> str:
    """Normalize platform-dependent text before hashing so the freeze is
    deterministic across OS line endings and wall-clock time."""
    # CRLF vs LF: Windows-touched checkouts write \r\n to stdout; Linux CI \n.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Wall-clock relative durations ("201d ago") drift daily from the pinned
    # fixture timestamps; collapse to a sentinel. The absolute ISO timestamps
    # beside them are pinned/static, so only the `Nd ago` prefix moves.
    import re as _re
    text = _re.sub(r"\b\d+[dhs] ago\b", "X ago", text)
    return text


def _norm_stats(text: str) -> str:
    lines = []
    for ln in text.splitlines():
        ln = ln.replace(builder.BASE_ENV["ZMEM_MODELS_DIR"], "__MODELS_DIR__")
        if ln.startswith("store: "):
            ln = "store: __STORE__"
        lines.append(ln)
    return _norm("\n".join(lines))


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
