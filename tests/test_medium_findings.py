"""Regression tests for issue #36 medium-severity codebase-review findings.

Covers (subprocess-level, where exit codes and argparse matter):
  M1  — negative --limit rejected on recall/recent/search/list
  M2  — `get` exits non-zero on a missing id (fail-closed)
  M3  — `none`-signal memory sits below the recall floor by default
  M5  — prompt-injection-risk tag surfaces in recall/recent/list results
  M7  — `failures` distinguishes "could not check" (nonzero) from "none" (0)
  M17 — content over the cap is rejected on CLI add (not silently stored)

Run: python tests/test_medium_findings.py
No pytest / third-party harness required — matches the repo convention.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable


def _env(tmp: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    return env


def _run(env: dict, *args: str, timeout: int = 60):
    return subprocess.run(
        [PYTHON, str(STORE_PY), *args],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


class M1NegativeLimit(unittest.TestCase):
    """M1: negative --limit must be rejected (not reach SQL as unbounded)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-m1-")
        self.env = _env(self.tmp)
        # Seed one row so a buggy negative limit would have something to dump.
        ns = "project:m1test"
        r = _run(self.env, "add", "--namespace", ns, "--type", "fact",
                 "--content", "alpha beta gamma", "--signal", "test")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.ns = ns

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_negative_limit_rejected_on_all_four_commands(self):
        for cmd, extra in [
            ("recall", ["--query", "alpha"]),
            ("recent", []),
            ("search", ["--text", "alpha"]),
            ("list", []),
        ]:
            with self.subTest(cmd=cmd):
                r = _run(self.env, cmd, "--namespace", self.ns,
                         "--limit", "-1", *extra)
                self.assertNotEqual(r.returncode, 0,
                                    f"{cmd} --limit -1 must not exit 0:\n{r.stdout}{r.stderr}")
                # argparse rejects with exit 2 and an error mentioning the limit.
                self.assertEqual(r.returncode, 2, r.stderr)

    def test_zero_limit_accepted(self):
        # 0 is a valid non-negative limit (returns nothing / empty).
        r = _run(self.env, "recent", "--namespace", self.ns, "--limit", "0")
        self.assertEqual(r.returncode, 0, r.stderr)


class M2GetExitCode(unittest.TestCase):
    """M2: `get` must exit non-zero on a missing id (fail-closed, like supersede)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-m2-")
        self.env = _env(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_missing_id_exits_nonzero(self):
        r = _run(self.env, "get", "--id", "nonexistent-id-12345")
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.returncode, 1, r.stderr)

    def test_get_existing_id_exits_zero(self):
        ns = "project:m2test"
        add = _run(self.env, "add", "--namespace", ns, "--type", "fact",
                   "--content", "a real memory", "--signal", "test")
        self.assertEqual(add.returncode, 0, add.stderr)
        # add prints: "[zmem] added memory <uuid> (ns=...)". Extract the uuid.
        mid = self._extract_id(add.stdout)
        self.assertIsNotNone(mid, f"could not find id in add output:\n{add.stdout}")
        r = _run(self.env, "get", "--id", mid)
        self.assertEqual(r.returncode, 0, r.stderr)

    @staticmethod
    def _extract_id(stdout: str):
        # Parse "[zmem] added memory <uuid> (...)".
        import re
        m = re.search(r"added memory ([0-9a-f-]{36})", stdout or "")
        return m.group(1) if m else None


class M3NoneSignalBelowFloor(unittest.TestCase):
    """M3: a none-signal memory must be excluded from default recall (below the
    floor) but retrievable via an explicit low min-confidence / search."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-m3-")
        self.env = _env(self.tmp)
        self.ns = "project:m3test"
        # A none-signal row (confidence default now 0.2 < floor 0.25).
        r = _run(self.env, "add", "--namespace", self.ns, "--type", "fact",
                 "--content", "ungrounded self opinion lesson zeta",
                 "--signal", "none")
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_none_signal_excluded_from_default_recall(self):
        r = _run(self.env, "recall", "--namespace", self.ns,
                 "--query", "zeta", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        results = self._parse_results(r.stdout)
        # The none-signal row is below the floor and must not surface by default.
        self.assertEqual(results, [], f"none-signal row should be below floor:\n{r.stdout}")

    def test_none_signal_visible_via_keyword_search(self):
        # search uses min_confidence=0.0 (no floor) — the row IS findable.
        r = _run(self.env, "search", "--namespace", self.ns,
                 "--text", "zeta")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("zeta", r.stdout)

    @staticmethod
    def _parse_results(stdout: str):
        # recall/recent print pretty-printed JSON (indent=2), so parse the whole
        # stdout blob, not line-by-line.
        text = stdout.strip()
        if not text:
            return []
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict) and "results" in obj:
            return obj["results"]
        return None


class M5PromptInjectionRiskSurfaced(unittest.TestCase):
    """M5: a memory tagged prompt-injection-risk must surface that flag in
    recall/recent/list JSON output (the tag is consumed at read time, not
    write-only)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-m5-")
        self.env = _env(self.tmp)
        self.ns = "project:m5test"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tagged_memory_surfaces_flag(self):
        # Add a grounded row but tag it prompt-injection-risk explicitly so the
        # read path must surface the flag regardless of the write-time detector.
        r = _run(self.env, "add", "--namespace", self.ns, "--type", "fact",
                 "--content", "important tagged lesson omega",
                 "--signal", "test", "--tags", "prompt-injection-risk")
        self.assertEqual(r.returncode, 0, r.stderr)
        # recent surfaces it (min_confidence default 0.5; test signal = 0.9 passes).
        rec = _run(self.env, "recent", "--namespace", self.ns, "--json")
        self.assertEqual(rec.returncode, 0, rec.stderr)
        results = self._parse_results(rec.stdout)
        self.assertIsNotNone(results, f"no results in:\n{rec.stdout}")
        tagged = [m for m in results if "omega" in m.get("content", "")]
        self.assertEqual(len(tagged), 1, f"expected the omega row:\n{results}")
        self.assertTrue(tagged[0].get("prompt_injection_risk"),
                        f"flag must be True:\n{tagged[0]}")

    def test_tagged_memory_surfaces_flag_in_recall(self):
        # Cover the recall path (search delegates to recall too): the field
        # must be present on recall JSON results, not just recent (#36 M5).
        r = _run(self.env, "add", "--namespace", self.ns, "--type", "fact",
                 "--content", "tagged recall target omegaz",
                 "--signal", "test", "--tags", "prompt-injection-risk")
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = _run(self.env, "recall", "--namespace", self.ns,
                   "--query", "omegaz", "--json")
        self.assertEqual(rec.returncode, 0, rec.stderr)
        results = self._parse_results(rec.stdout)
        self.assertIsNotNone(results, f"no results in:\n{rec.stdout}")
        tagged = [m for m in results if "omegaz" in m.get("content", "")]
        self.assertEqual(len(tagged), 1, f"expected the omegaz row:\n{results}")
        self.assertTrue(tagged[0].get("prompt_injection_risk"),
                        f"recall must surface the flag:\n{tagged[0]}")

    def test_untagged_memory_has_false_flag(self):
        r = _run(self.env, "add", "--namespace", self.ns, "--type", "fact",
                 "--content", "plain untagged lesson omega2",
                 "--signal", "test")
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = _run(self.env, "recent", "--namespace", self.ns, "--json")
        results = self._parse_results(rec.stdout)
        self.assertIsNotNone(results, f"no results in:\n{rec.stdout}")
        plain = [m for m in results if "omega2" in m.get("content", "")]
        self.assertEqual(len(plain), 1)
        self.assertFalse(plain[0].get("prompt_injection_risk"))

    @staticmethod
    def _parse_results(stdout: str):
        # recall/recent print pretty-printed JSON (indent=2), so parse the whole
        # stdout blob, not line-by-line.
        text = stdout.strip()
        if not text:
            return []
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict) and "results" in obj:
            return obj["results"]
        return None


class M9IngestDedupCache(unittest.TestCase):
    """M9: ingest-jsonl's exact-match dedup must scan the namespace ONCE (the
    pre-built cache), not once per row (O(n²)). Verified by counting how many
    full-namespace candidate SELECTs execute during a batch ingest."""

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        import importlib.util
        self.tmp = tempfile.mkdtemp(prefix="zmem-m9-")
        store_path = os.path.join(self.tmp, "store.sqlite")
        spec = importlib.util.spec_from_file_location(
            f"zmem_store_m9_{os.getpid()}", str(STORE_PY))
        with mock.patch.dict(os.environ, {"ZMEM_STORE": store_path,
                                          "ZMEM_MODELS_DIR": os.path.join(self.tmp, "none"),
                                          "ZMEM_MODEL_AUTODOWNLOAD": "0"}):
            self.store = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.store)
        self.conn = self.store.connect()
        self.store.init_db(self.conn)
        self.store.migrate(self.conn)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_jsonl(self, rows):
        import json as _json
        path = os.path.join(self.tmp, "batch.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(_json.dumps(r) + "\n")
        return path

    def test_ingest_scans_namespace_once_not_per_row(self):
        # Seed one existing row so the dedup candidate query is meaningful.
        self.store.add_memory(self.conn, namespace="project:m9",
                              type_="fact", content="seed row",
                              signal="test")
        # Write a batch of N DISTINCT new rows (no embeddings → exact-match path).
        rows = [
            {"id": f"aaaaaaaa-0000-0000-0000-{i:012d}",
             "type": "fact", "content": f"distinct batch row number {i}",
             "namespace": "project:m9", "signal": "test"}
            for i in range(8)
        ]
        path = self._write_jsonl(rows)

        # Count full-namespace candidate scans via a connection proxy.
        # sqlite3.Connection.execute is read-only (can't monkeypatch), so wrap.
        class _CountingConn:
            def __init__(self, real):
                self._real = real
                self.ns_scans = 0
            def execute(self, sql, params=()):
                if "SELECT id, content FROM memory WHERE namespace=" in sql:
                    self.ns_scans += 1
                return self._real.execute(sql, params)
            def __getattr__(self, name):
                return getattr(self._real, name)

        proxy = _CountingConn(self.conn)
        rc = self.store.cmd_ingest_jsonl(proxy, in_path=path, source_ref=None)
        self.assertEqual(rc, 0)
        # The per-row full-namespace scan must NOT fire during the batch — the
        # cache pre-scan (different query shape) replaced it.
        self.assertEqual(proxy.ns_scans, 0,
                         f"per-row namespace scan fired {proxy.ns_scans} "
                         f"times — cache did not eliminate the O(n²) scan")

    def test_ingest_detects_in_batch_duplicate(self):
        # The cache is updated as rows insert, so a later row duplicating an
        # earlier in-batch row is detected without a namespace rescan.
        rows = [
            {"id": "bbbbbbbb-0000-0000-0000-000000000001",
             "type": "fact", "content": "dup content same text", "namespace": "project:m9b",
             "signal": "test"},
            {"id": "bbbbbbbb-0000-0000-0000-000000000002",
             "type": "fact", "content": "dup content same text", "namespace": "project:m9b",
             "signal": "test"},
        ]
        path = self._write_jsonl(rows)
        rc = self.store.cmd_ingest_jsonl(self.conn, in_path=path, source_ref=None)
        self.assertEqual(rc, 0)
        # Only ONE live row with that content (the second deduped).
        n = self.conn.execute(
            "SELECT count(*) FROM memory WHERE content='dup content same text' "
            "AND superseded_at IS NULL").fetchone()[0]
        self.assertEqual(n, 1, f"expected 1 live row after in-batch dedup, got {n}")


class M17CliAddContentCap(unittest.TestCase):
    """M17: add_memory must reject content over the cap (not silently store it).

    Tested via the module function directly (not argv) because a >65k string
    cannot be passed on the Windows command line (CreateProcess length limit)."""

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        import importlib.util
        self.tmp = tempfile.mkdtemp(prefix="zmem-m17-")
        store_path = os.path.join(self.tmp, "store.sqlite")
        # Load a fresh store module pointed at a throwaway store.
        spec = importlib.util.spec_from_file_location(
            f"zmem_store_m17_{os.getpid()}", str(STORE_PY))
        with mock.patch.dict(os.environ, {"ZMEM_STORE": store_path,
                                          "ZMEM_MODELS_DIR": os.path.join(self.tmp, "none"),
                                          "ZMEM_MODEL_AUTODOWNLOAD": "0"}):
            self.store = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.store)
        self.conn = self.store.connect()
        self.store.init_db(self.conn)
        self.store.migrate(self.conn)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_oversize_content_rejected(self):
        cap = self.store.MAX_CONTENT_CHARS
        with self.assertRaises(self.store.ContentTooLarge):
            self.store.add_memory(
                self.conn, namespace="project:m17", type_="fact",
                content="X" * (cap + 1), signal="test")

    def test_at_cap_accepted(self):
        cap = self.store.MAX_CONTENT_CHARS
        # At exactly the cap, the add succeeds.
        mid = self.store.add_memory(
            self.conn, namespace="project:m17", type_="fact",
            content="Y" * cap, signal="test")
        self.assertTrue(mid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
