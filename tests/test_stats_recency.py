"""Tests for store.py's relative-recency formatting in `stats` (#39 E1).

Covers `_format_recency()`:
  - None / empty -> '(never)'
  - a recent ISO timestamp -> 'just now' / 'Nm ago' / 'Nh ago' / 'Nd ago'
  - an unparseable string -> returned verbatim (strict parse surfaces drift)
  - a future-dated timestamp -> returned verbatim (clock skew, no negative age)

And an integration check that `store.py stats` surfaces recency in its
operational-health section.

Run: python tests/test_stats_recency.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable


def _fmt_module():
    """Import store.py as a module to reach _format_recency directly."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("zmem_store", STORE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(dt: datetime) -> str:
    """Render a datetime the same way now_iso() does (UTC, second precision)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FormatRecencyTests(unittest.TestCase):
    def setUp(self):
        # store.py is heavy to import; do it once per TestCase, not per test.
        if not hasattr(self, "_store"):
            self.__class__._store = _fmt_module()
        self.store = self._store

    def test_none_is_never(self):
        self.assertEqual(self.store._format_recency(None), "(never)")

    def test_empty_is_never(self):
        self.assertEqual(self.store._format_recency(""), "(never)")

    def test_just_now(self):
        ts = _iso(datetime.now(timezone.utc) - timedelta(seconds=10))
        self.assertEqual(self.store._format_recency(ts), "just now")

    def test_minutes_ago(self):
        ts = _iso(datetime.now(timezone.utc) - timedelta(minutes=5, seconds=3))
        self.assertEqual(self.store._format_recency(ts), "5m ago")

    def test_hours_ago(self):
        ts = _iso(datetime.now(timezone.utc) - timedelta(hours=3, minutes=2))
        self.assertEqual(self.store._format_recency(ts), "3h ago")

    def test_days_ago(self):
        ts = _iso(datetime.now(timezone.utc) - timedelta(days=5, hours=2))
        self.assertEqual(self.store._format_recency(ts), "5d ago")

    def test_unparseable_returned_verbatim(self):
        self.assertEqual(self.store._format_recency("garbage"), "garbage")
        self.assertEqual(self.store._format_recency("2026-13-99T99:99:99Z"),
                         "2026-13-99T99:99:99Z")

    def test_future_dated_returned_verbatim(self):
        # A timestamp in the future (clock skew) must not yield a negative age.
        ts = _iso(datetime.now(timezone.utc) + timedelta(days=1))
        self.assertEqual(self.store._format_recency(ts), ts)


class StatsRecencyIntegrationTests(unittest.TestCase):
    """End-to-end: `store.py stats` surfaces recency in operational health."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-recency-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = {
            **os.environ,
            "ZMEM_STORE": self.store,
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "no-such-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        for k in ("ZMEM_DATA", "ZMEM_BACKUP_DIR", "ZMEM_BACKUP_INTERVAL_DAYS"):
            self.env.pop(k, None)

    def _run(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=60,
        )

    def test_stats_shows_never_on_fresh_store(self):
        r = self._run("init")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run("stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("operational health:", r.stdout)
        # A fresh store has never run backup/consolidation.
        self.assertIn("last_backup: (never)", r.stdout)
        self.assertIn("last_consolidation: (never)", r.stdout)

    def test_stats_shows_recency_after_consolidate(self):
        r = self._run("init")
        self.assertEqual(r.returncode, 0, r.stderr)
        # consolidate needs at least one row to be meaningful, but even on an
        # empty store it writes the last_consolidation meta timestamp.
        r = self._run("consolidate")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run("stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        # After consolidate, last_consolidation is recent -> 'just now' or a
        # 'Nh ago'/'Nd ago' form with the raw ISO timestamp in parentheses.
        # Match all unit suffixes so the test is robust against slow CI.
        self.assertRegex(
            r.stdout,
            r"last_consolidation: (just now|\d+[mhd] ago)\s+\(",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
