"""Tests for scripts/eval_self_corpus.py — the self-corpus probe (issue #82).

Pins:
- missing --store -> exit 2 (argparse), never a default-store fallback;
- a nonexistent store path is refused BEFORE any file is created;
- the home-store refusal: pointing --store at the path the host would resolve
  (computed with the override env vars stripped) exits 2 with the exact
  remediation (store.py backup snapshot);
- a run is fully passive: the probed store's bytes are identical after;
- --json-out writes the report and the aggregate shape is sane.

Run: python tests/test_eval_self_corpus.py   (no pytest — repo convention)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
RUNNER = REPO_ROOT / "scripts" / "eval_self_corpus.py"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
PYTHON = sys.executable


def _base_env(store: str) -> dict:
    return {
        **os.environ,
        "ZMEM_STORE": store,
        "ZMEM_EMBED_PROFILE": "fake",
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_MODELS_DIR": "/nonexistent-zmem-models-dir",
        "PYTHONUTF8": "1",
    }


class SelfCorpusRefusalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args, env=None):
        return subprocess.run(
            [PYTHON, str(RUNNER), *args],
            env=env or {**os.environ, "PYTHONUTF8": "1"},
            capture_output=True, text=True, timeout=300,
        )

    def test_missing_store_flag_is_a_usage_error(self):
        r = self._run()
        self.assertEqual(r.returncode, 2)
        self.assertIn("--store", r.stderr)

    def test_nonexistent_store_refused_before_creation(self):
        missing = str(Path(self.tmp) / "not-here" / "store.sqlite")
        r = self._run("--store", missing)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("REFUSED", r.stderr)
        self.assertFalse(Path(missing).exists(),
                         "the runner must never create a store at the "
                         "probed path")

    def test_home_store_path_is_refused_with_remediation(self):
        # Point --store at the path host.resolve_store_path() would return
        # with the override env vars stripped — exactly the path the script
        # is required to refuse. The refusal fires BEFORE any connect, so
        # this cannot touch the real store.
        script = (
            "import os, sys\n"
            "for k in ('ZMEM_STORE','ZMEM_DATA','CLAUDE_PLUGIN_DATA',"
            "'ZCODE_PLUGIN_DATA'):\n"
            "    os.environ.pop(k, None)\n"
            "sys.path.insert(0, r'%s')\n"
            "import host\n"
            "print(host.resolve_store_path())\n" % str(SCRIPTS_DIR)
        )
        probe = subprocess.run([PYTHON, "-c", script], capture_output=True,
                               text=True, timeout=60)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        home_path = probe.stdout.strip()
        r = self._run("--store", home_path, env=_base_env(str(
            Path(self.tmp) / "unused.sqlite")))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("backup", r.stderr,
                      "the remediation must name the store.py backup path")

    def test_copy_of_a_store_runs_and_passivates(self):
        # Build the deterministic eval fixture, copy it, probe the copy.
        store = str(Path(self.tmp) / "fixture" / "store.sqlite")
        r = subprocess.run(
            [PYTHON, str(FIXTURES_DIR / "eval_store.py"), store],
            env={**os.environ, "ZMEM_EMBED_PROFILE": "fake",
                 "ZMEM_MODEL_AUTODOWNLOAD": "0", "PYTHONUTF8": "1"},
            capture_output=True, text=True, timeout=600,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        copy = str(Path(self.tmp) / "copy.sqlite")
        shutil.copyfile(store, copy)
        before = Path(copy).read_bytes()
        out_path = Path(self.tmp) / "report.json"
        r = self._run("--store", copy, "--limit", "5",
                      "--json-out", str(out_path),
                      env=_base_env(str(Path(self.tmp) / "unused.sqlite")))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(copy).read_bytes(), before,
                         "probing a store must be byte-passive")
        report = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(report["runner"], "scripts/eval_self_corpus.py")
        self.assertEqual(report["probes"], 5)
        self.assertTrue(report["per_probe"])
        for probe in report["per_probe"]:
            self.assertIn("5", probe["hits"])
            self.assertIn("20", probe["hits"])
        for k, rate in report["aggregate_hit_rate"].items():
            self.assertGreaterEqual(rate, 0.0)
            self.assertLessEqual(rate, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
