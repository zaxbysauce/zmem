"""Tests for store.py's `export-pack` subcommand (Tier 1: markdown memory
pack for a repo).

Covers:
  - ordering: confidence DESC, retrieval_count DESC, ingestion_ts DESC
  - --min-confidence filter excludes low-confidence rows from both sections
  - --max-bytes cap: bullets are omitted whole (never truncated) once the
    budget is exhausted, and a trailing note reports how many were omitted
  - empty pack (namespace AND user:global both empty) refuses with exit 2
  - "(none)" placeholder for an empty section
  - --out writes UTF-8 with LF line endings

Drives the REAL store.py CLI via subprocess against a throwaway temp store —
never the box store — following the isolation fixture pattern established in
tests/test_backup.py / tests/test_no_bump.py (ZMEM_STORE set inline on every
subprocess env dict; ZMEM_DATA and friends popped so no ambient env var can
redirect a run at the real ~/.zmem store).

Run: python -m pytest tests/test_export_pack.py -q
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable
NS = "project:packtest"


def _base_env(tmp: str) -> dict:
    """Env for a store.py subprocess pinned to a throwaway store, with the
    embedding model forced absent (fast + deterministic; repo convention from
    tests/test_model_fallback.py, tests/test_backup.py)."""
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env.pop("ZMEM_DATA", None)
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_BACKUP_INTERVAL_DAYS", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    return env


class _StoreCase(unittest.TestCase):
    """Common temp-store fixture: a fresh store dir per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-exportpack-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = _base_env(self.tmp)

    def run_store(self, *args, env: dict | None = None):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=env or self.env, capture_output=True, text=True, timeout=60,
        )

    def add(self, namespace: str, content: str, *, confidence: float,
            signal: str = "test", type_: str = "lesson") -> str:
        r = self.run_store("add", "--namespace", namespace, "--type", type_,
                           "--content", content, "--signal", signal,
                           "--confidence", str(confidence))
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT id FROM memory WHERE content=? AND namespace=? "
                "ORDER BY ingestion_ts DESC LIMIT 1",
                (content, namespace),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, f"could not find just-added row: {content!r}")
        return row[0]

    def bump_retrieval(self, mid: str, count: int) -> None:
        conn = sqlite3.connect(self.store)
        try:
            conn.execute("UPDATE memory SET retrieval_count=? WHERE id=?", (count, mid))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# ordering + min-confidence filter
# ---------------------------------------------------------------------------
class OrderingAndFilterTest(_StoreCase):
    def test_confidence_then_retrieval_then_recency_order_and_min_confidence(self):
        # Two rows tie on confidence (0.9); B has a higher retrieval_count so
        # it must win the tiebreak and sort ahead of A.
        mid_a = self.add(NS, "row A: tie on confidence, loses on retrieval_count",
                         confidence=0.9)
        mid_b = self.add(NS, "row B: tie on confidence, wins on retrieval_count",
                         confidence=0.9)
        self.bump_retrieval(mid_b, 5)
        self.add(NS, "row C: lower confidence, still above the floor", confidence=0.7)
        self.add(NS, "row D: below the default min-confidence floor", confidence=0.5)

        r = self.run_store("export-pack", "--namespace", NS)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout

        self.assertIn("row A", out)
        self.assertIn("row B", out)
        self.assertIn("row C", out)
        self.assertNotIn("row D", out, "row below --min-confidence must be excluded")

        pos_b = out.index("row B")
        pos_a = out.index("row A")
        pos_c = out.index("row C")
        self.assertLess(pos_b, pos_a, "higher retrieval_count must sort first on a confidence tie")
        self.assertLess(pos_a, pos_c, "higher confidence must sort before lower confidence")

    def test_explicit_min_confidence_override(self):
        self.add(NS, "low confidence row visible with a lowered floor", confidence=0.4)
        r = self.run_store("export-pack", "--namespace", NS, "--min-confidence", "0.3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("low confidence row visible with a lowered floor", r.stdout)

    def test_empty_global_section_renders_none_placeholder(self):
        self.add(NS, "only a project row exists in this store", confidence=0.9)
        r = self.run_store("export-pack", "--namespace", NS)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("## Cross-project lessons (user:global)", r.stdout)
        # The (none) placeholder must appear specifically under the global
        # section, which is the last section in the document.
        global_idx = r.stdout.index("## Cross-project lessons")
        self.assertIn("(none)", r.stdout[global_idx:])

    def test_header_is_auto_generated_notice_with_date_only_no_time(self):
        self.add(NS, "some project fact", confidence=0.9)
        r = self.run_store("export-pack", "--namespace", NS)
        self.assertEqual(r.returncode, 0, r.stderr)
        first_line = r.stdout.splitlines()[0]
        self.assertIn("Auto-generated", first_line)
        self.assertIn("zmem export-pack", first_line)
        self.assertIn("hand-edit", first_line.lower())
        # Date-only: no HH:MM:SS-shaped timestamp anywhere in the header line.
        self.assertIsNone(re.search(r"\d{2}:\d{2}:\d{2}", first_line))
        self.assertRegex(first_line, r"\d{4}-\d{2}-\d{2}")
        self.assertIn(f"# Memory pack: {NS}", r.stdout.splitlines())


# ---------------------------------------------------------------------------
# --max-bytes cap: whole bullets only, trailing omitted-count note
# ---------------------------------------------------------------------------
class MaxBytesCapTest(_StoreCase):
    @staticmethod
    def _row_content(i: int) -> str:
        return f"ROW{i}START " + ("filler word " * 15) + f"ROW{i}END"

    def test_cap_omits_whole_bullets_never_truncates_and_notes_count(self):
        contents = [self._row_content(i) for i in range(6)]
        for c in contents:
            self.add(NS, c, confidence=0.9)

        out_path = os.path.join(self.tmp, "pack.md")
        r = self.run_store("export-pack", "--namespace", NS,
                           "--max-bytes", "500", "--out", out_path)
        self.assertEqual(r.returncode, 0, r.stderr)

        raw = Path(out_path).read_bytes()
        text = raw.decode("utf-8")
        # The bullet-bearing prefix (through the last emitted bullet) is what
        # --max-bytes actually governs; the section headings, the "(none)"
        # placeholder for the untouched user:global section, and the trailing
        # omitted-count note are exempt structural framing (see _render_pack).
        # Assert on that governed prefix specifically, not the whole file.
        last_bullet_end = max(
            (m.end() for m in re.finditer(r"^- \*\*\[.*$", text, re.MULTILINE)),
            default=0,
        )
        self.assertLessEqual(
            len(text[:last_bullet_end].encode("utf-8")), 500,
            "the bullet region alone must respect --max-bytes")

        shown = 0
        for i, content in enumerate(contents):
            start_marker = f"ROW{i}START"
            end_marker = f"ROW{i}END"
            if start_marker in text:
                shown += 1
                self.assertIn(end_marker, text,
                              f"row {i} appears truncated: START present without END")
                self.assertIn(f"- **[lesson/test]** {content}", text,
                              f"row {i} bullet must be emitted whole or not at all")
            else:
                self.assertNotIn(end_marker, text,
                                 f"row {i} END marker leaked without its START (partial bullet)")

        self.assertLess(shown, len(contents), "max-bytes cap had no effect on this fixture")

        m = re.search(r"\((\d+) row\(s\) omitted", text)
        self.assertIsNotNone(m, "missing trailing omitted-rows note")
        self.assertEqual(int(m.group(1)), len(contents) - shown)

    def test_out_file_uses_lf_line_endings(self):
        self.add(NS, "a row for LF-ending verification", confidence=0.9)
        out_path = os.path.join(self.tmp, "pack.md")
        r = self.run_store("export-pack", "--namespace", NS, "--out", out_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        raw = Path(out_path).read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertIn(b"\n", raw)


# ---------------------------------------------------------------------------
# empty pack -> exit 2
# ---------------------------------------------------------------------------
class EmptyPackTest(_StoreCase):
    def test_zero_rows_in_namespace_and_global_exits_2(self):
        # init only -- no rows anywhere.
        r0 = self.run_store("init")
        self.assertEqual(r0.returncode, 0, r0.stderr)
        r = self.run_store("export-pack", "--namespace", NS)
        self.assertEqual(r.returncode, 2)
        self.assertIn("[zmem]", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_nonempty_global_alone_is_not_treated_as_empty(self):
        self.add("user:global", "a cross-project lesson", confidence=0.9)
        r = self.run_store("export-pack", "--namespace", NS)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("a cross-project lesson", r.stdout)
        self.assertIn("## Project knowledge", r.stdout)
        project_idx = r.stdout.index("## Project knowledge")
        global_idx = r.stdout.index("## Cross-project lessons")
        self.assertIn("(none)", r.stdout[project_idx:global_idx])


if __name__ == "__main__":
    unittest.main(verbosity=2)
