"""Regression + guardrail tests for issue #21 (surfaced_count telemetry).

Issue #21: `retrieval_count` never increments for hook-driven recall, so promotion
ranking and prune decisions rest on a metric biased toward manual/explicit use. The fix
adds a `surfaced_count`/`last_surfaced` counter that passive (`--no-bump`) recall IS
allowed to bump, and blends it with `retrieval_count` everywhere the latter was the sole
"usefulness" signal.

This module covers:
  - adapter-scan guardrail: every PASSIVE recall/recent consumer (`hooks/*.sh`, Hermes
    `prefetch`) carries a literal `--no-bump`, and every EXPLICIT consumer (MCP
    `recall`/`search`, Hermes `_tool_search`) does NOT — codifying the single rule across
    all adapters so a future adapter cannot silently introduce a fourth rule.
  - `consolidate --prune` protection: a memory that has been SURFACED but never explicitly
    retrieved must NOT be pruned by the never-used rule.
  - `compute_score` popularity blends surfaced + retrieval (a surfaced-only row scores
    above an inert row; equivalent surface totals score equal regardless of which counter
    carried the events).

Run: python tests/test_surface_consistency.py   (no pytest required — repo convention)
"""

from __future__ import annotations

import os
import re
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

NS = "project:surfaceconsistencytest"

# --- Point the import-time STORE_PATH at a throwaway location before importing store.py ---
_IMPORT_TMP = tempfile.mkdtemp(prefix="zmem-surface-import-")
os.environ["ZMEM_STORE"] = os.path.join(_IMPORT_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")

sys.path.insert(0, str(SCRIPTS_DIR))
import store  # noqa: E402


class AdapterScanTest(unittest.TestCase):
    """Guardrail: one rule across all adapters (the issue-comment ask)."""

    # Passive consumers: automatic / hook / prefetch recall paths. Each MUST
    # carry the literal --no-bump flag so only surfaced_count (+last_surfaced),
    # never retrieval_count, advances on the passive path.
    PASSIVE = {
        "hooks/zmem-recall.sh",
        "hooks/zmem-subagent-recall.sh",
        "hooks/zmem-session-start.sh",
    }

    def _method_body(self, text: str, method: str) -> str:
        """Return the body of `def method(...)` up to the next `def ` at col 0."""
        m = re.search(rf"^\s*def {method}\(.*$", text, re.MULTILINE)
        self.assertIsNotNone(m, f"def {method} not found")
        rest = text[m.end():]
        nxt = re.search(r"^\s*def ", rest, re.MULTILINE)
        return rest if nxt is None else rest[:nxt.start()]

    def test_passive_hooks_carry_no_bump(self):
        for rel in sorted(self.PASSIVE):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn(
                "--no-bump", text,
                f"{rel} is a passive recall path and MUST pass --no-bump so it records "
                "a surface event, not a retrieval")

    def test_passive_hermes_prefetch_carries_no_bump(self):
        text = (REPO_ROOT / "hermes-plugin" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("--no-bump", self._method_body(text, "prefetch"),
                      "Hermes prefetch is passive and MUST pass --no-bump")

    def test_explicit_hermes_tool_search_omits_no_bump(self):
        text = (REPO_ROOT / "hermes-plugin" / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("--no-bump", self._method_body(text, "_tool_search"),
                         "Hermes _tool_search is EXPLICIT and must NOT pass --no-bump")

    def test_explicit_mcp_recall_omits_no_bump(self):
        text = (REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py").read_text(
            encoding="utf-8")
        self.assertNotIn("--no-bump", text,
                         "MCP recall/search are EXPLICIT and must NOT pass --no-bump")

    def test_readonly_invariant_docstring_lists_all_hooks(self):
        # The passive-recall read-only contract (the recall() docstring) must name
        # ALL THREE automatic hook sources — UserPromptSubmit, SubagentStart, and
        # SessionStart — so the read-only invariant stays checkable and a comment
        # cannot silently drop one hook again (issue #23). SessionStart was the
        # one omitted here after it was made --no-bump by PR #29.
        text = (SCRIPTS_DIR / "store.py").read_text(encoding="utf-8")
        start = text.index("def recall_memory(")
        end = text.index("def ", start + 1)
        doc = text[start:end]
        self.assertIn("UserPromptSubmit", doc)
        self.assertIn("SubagentStart", doc)
        self.assertIn("SessionStart", doc)


class SurfaceTempStoreTest(unittest.TestCase):
    """Subprocess tests driving the REAL store.py CLI against a temp store."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = {**os.environ, "ZMEM_STORE": self.store,
                    "ZMEM_MODEL_AUTODOWNLOAD": "0"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run([PYTHON, str(STORE_PY), *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def _conn(self):
        return sqlite3.connect(self.store)

    def _counts(self, content):
        c = self._conn()
        try:
            return c.execute(
                "SELECT retrieval_count, surfaced_count, superseded_at FROM memory "
                "WHERE content LIKE ?", (f"%{content}%",)
            ).fetchone()
        finally:
            c.close()

    def test_prune_does_not_prune_surfaced_but_unretrieved(self):
        # Two old, low-confidence, signal=none rows: one really never surfaced, one
        # surfaced only by hooks. Only the never-surfaced one may be pruned.
        inert = "definitely never surfaced nor retrieved row alpha"
        surfaced = "surfaced many times by hooks but never fetched row beta"
        for content, sc in ((inert, 0), (surfaced, 3)):
            r = self._run("add", "--namespace", NS, "--type", "fact",
                          "--content", content, "--signal", "none", "--confidence", "0.2")
            self.assertEqual(r.returncode, 0, r.stderr)
        c = self._conn()
        try:
            for content, sc in ((inert, 0), (surfaced, 3)):
                c.execute(
                    "UPDATE memory SET surfaced_count=?, confidence=0.2, "
                    "ingestion_ts=datetime('now','-60 days') WHERE content LIKE ?",
                    (sc, f"%{content}%"))
            c.commit()
        finally:
            c.close()

        r = self._run("consolidate", "--prune")
        # consolidate prints a summary regardless; embeddings may be absent (lexical
        # fallback) but the run must still succeed or at least not abort the prune.
        self.assertEqual(r.returncode, 0, r.stderr)

        inert_counts = self._counts(inert)
        surfaced_counts = self._counts(surfaced)
        self.assertIsNotNone(inert_counts, "prune target row should exist")
        self.assertIsNotNone(surfaced_counts, "surfaced row should exist")
        self.assertIsNotNone(
            inert_counts[2],
            f"never-surfaced, never-retrieved row MUST be pruned (superseded); got {inert_counts}")
        self.assertIsNone(
            surfaced_counts[2],
            f"surfaced-but-unretrieved row must NOT be pruned; got {surfaced_counts}")


class ComputeScorePopularityBlendTest(unittest.TestCase):
    """compute_score popularity must blend surfaced + retrieval (defect-class fix)."""

    def _row(self, retrieval: int, surfaced: int) -> dict:
        return {
            "retrieval_count": retrieval,
            "surfaced_count": surfaced,
            "confidence": 0.5,
            "ingestion_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def test_surfaced_only_outranks_inert(self):
        now = time.time()
        surfaced = store.compute_score(self._row(0, 5), None, now, vec_sim=0.5)
        inert = store.compute_score(self._row(0, 0), None, now, vec_sim=0.5)
        self.assertGreater(surfaced, inert,
                           "a surfaced-only memory should outrank a never-surfaced one")

    def test_equivalent_totals_score_equal(self):
        now = time.time()
        via_surfaced = store.compute_score(self._row(0, 5), None, now, vec_sim=0.5)
        via_retrieval = store.compute_score(self._row(5, 0), None, now, vec_sim=0.5)
        self.assertAlmostEqual(
            via_surfaced, via_retrieval, places=12,
            msg="popularity counts total surface events regardless of which counter carried them")


if __name__ == "__main__":
    unittest.main(verbosity=2)
