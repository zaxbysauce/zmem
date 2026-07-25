"""Tests for store.py's `promote` command (Phase 9: promotion quality fix +
dual-target write).

Covers:
  - the synthesized `description:` is a clean single trigger line: no
    triplication with the body, no mid-word truncation, never contains the
    literal "EDIT THIS DESCRIPTION" placeholder text that leaked into the
    old draft.
  - body sections ("When to use" / "The rule" / "Source") are not the same
    repeated sentence.
  - `--description` override lands verbatim.
  - dual-target write: promotion writes SKILL.md to every dir in
    ZMEM_SKILLS_DIRS, and collision detection fires per-target.

Drives the REAL store.py CLI via subprocess against a throwaway temp store
and throwaway temp skills dirs — NEVER the user's real ~/.claude/skills or
~/.zcode/skills.

Run: python tests/test_promote.py   (no pytest required - repo convention)
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable
NS = "project:promotetest"

LESSON_CONTENT = (
    "Always run pytest with the --tb=short flag before committing changes to "
    "the parser module. It surfaces regressions in the tokenizer that the "
    "default traceback format buries under noise."
)


class PromoteTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.skills_a = os.path.join(self.tmp, "skills_a")
        self.skills_b = os.path.join(self.tmp, "skills_b")
        self.env = {
            **os.environ,
            "ZMEM_STORE": self.store,
            "ZMEM_SKILLS_DIRS": os.pathsep.join([self.skills_a, self.skills_b]),
        }
        r = self._run(
            "add", "--namespace", NS, "--type", "lesson",
            "--content", LESSON_CONTENT,
            "--tags", "pytest, parser, tokenizer",
            "--signal", "test", "--confidence", "0.9",
        )
        self.assertEqual(r.returncode, 0, r.stderr)

        # Promotion requires retrieval_count > 3; add_memory always starts a
        # fresh row at 0, so bump it directly — the store schema, not a CLI
        # surface, is the fastest/most direct way to set up this fixture.
        conn = sqlite3.connect(self.store)
        try:
            self.memory_id = conn.execute(
                "SELECT id FROM memory WHERE superseded_at IS NULL"
            ).fetchone()[0]
            conn.execute(
                "UPDATE memory SET retrieval_count = 5 WHERE id = ?", (self.memory_id,)
            )
            conn.commit()
        finally:
            conn.close()

    def _run(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=30,
        )

    def _skill_files(self):
        return sorted(Path(self.tmp).glob("skills_*/*/SKILL.md"))

    def _supersede_all(self):
        """Leave the store with zero eligible promotion candidates."""
        conn = sqlite3.connect(self.store)
        conn.execute("UPDATE memory SET superseded_at = '2026-01-01T00:00:00Z'")
        conn.commit()
        conn.close()


class TestDryRun(PromoteTestBase):
    def test_dry_run_shows_candidate_and_both_targets(self):
        r = self._run("promote", "--dry-run", "--namespace", NS)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.memory_id[:8], r.stdout)
        self.assertIn(self.skills_a, r.stdout)
        self.assertIn(self.skills_b, r.stdout)
        self.assertNotIn("EDIT THIS DESCRIPTION", r.stdout)


class TestPromoteQuality(PromoteTestBase):
    def _promote(self, extra=()):
        # --confirm is the real write gate, so every write path here goes
        # through it — the same invocation the docs prescribe.
        r = self._run("promote", "--id", self.memory_id, "--confirm", *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def _frontmatter_description(self, text):
        m = re.search(r'^description:\s*(.*)$', text, re.MULTILINE)
        self.assertIsNotNone(m, "no description: line found in frontmatter")
        return m.group(1).strip()

    def test_writes_to_both_target_dirs(self):
        self._promote()
        files = self._skill_files()
        self.assertEqual(len(files), 2, files)

    def test_description_has_no_placeholder_text(self):
        self._promote()
        for f in self._skill_files():
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("EDIT THIS DESCRIPTION", text)

    def test_description_is_single_line_and_not_mid_word_truncated(self):
        self._promote()
        for f in self._skill_files():
            text = f.read_text(encoding="utf-8")
            desc = self._frontmatter_description(text)
            # Single line: the next raw line in the file is the closing '---'.
            frontmatter = text.split("---")[1]
            self.assertEqual(frontmatter.count("description:"), 1)
            # Guard against a multiline description value: the "description:"
            # line must be immediately followed by the frontmatter's closing
            # "---" marker (the shape store.py's promote_memory() emits:
            # name/description/---), not by more wrapped description text.
            all_lines = text.splitlines()
            desc_idx = next(
                i for i, line in enumerate(all_lines) if line.startswith("description:")
            )
            self.assertEqual(all_lines[desc_idx + 1].strip(), "---")
            # No truncation ellipsis mid-word: if present, the char before it
            # must not be inside an alphanumeric token abutting more text -
            # simplest robust check is that the description contains the
            # full first sentence of the lesson content verbatim.
            first_sentence = LESSON_CONTENT.split(". ")[0] + "."
            self.assertIn(first_sentence, desc)
            # And the raw content[:120]-style artifact ("...") must be gone.
            self.assertNotIn("...", desc)

    def test_description_not_triplicated_across_sections(self):
        self._promote()
        for f in self._skill_files():
            text = f.read_text(encoding="utf-8")
            desc = self._frontmatter_description(text)
            when_section = text.split("## When to use")[1].split("## The rule")[0]
            rule_section = text.split("## The rule")[1].split("## Source")[0]
            # "The rule" must carry the full lesson content.
            self.assertIn(LESSON_CONTENT, rule_section)
            # "When to use" must differ from "The rule" (not the same
            # sentence repeated) and must reference the trigger tags.
            self.assertNotEqual(when_section.strip(), rule_section.strip())
            self.assertIn("pytest", when_section)
            # description must differ from the raw "When to use" section too
            # (distinct content, not a third copy of the same sentence).
            self.assertNotEqual(desc.strip('"'), when_section.strip())

    def test_description_override_used_verbatim(self):
        custom = "Use when touching the parser tokenizer tests"
        self._promote(extra=["--description", custom])
        for f in self._skill_files():
            text = f.read_text(encoding="utf-8")
            desc = self._frontmatter_description(text)
            self.assertEqual(desc.strip('"'), custom)

    def test_documented_id_confirm_path_writes(self):
        # The documented write gate is `--id <uuid> --confirm`.
        r = self._run("promote", "--id", self.memory_id, "--confirm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self._skill_files()), 2)

    def test_id_without_confirm_refuses_and_writes_nothing(self):
        # --confirm is a REAL gate: promotion writes into every dir in
        # ZMEM_SKILLS_DIRS (both hosts' skills dirs by default), so bare --id
        # must refuse. It was previously accepted-and-ignored while three docs
        # described it as the gate — protection that did not exist.
        r = self._run("promote", "--id", self.memory_id)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--confirm", r.stderr)
        self.assertEqual(self._skill_files(), [], "refused promote must write nothing")

    def test_dry_run_needs_no_confirm(self):
        r = self._run("promote", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._skill_files(), [])

    def test_unknown_id_refuses_with_exit_2(self):
        # Same reasoning as the collision case: a refusal must not report
        # success. Previously this returned bare, exiting 0.
        r = self._run("promote", "--id", "00000000-0000-0000-0000-000000000000",
                      "--confirm")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self._skill_files(), [])

    def test_unknown_id_refuses_even_with_no_eligible_candidates(self):
        # Regression: the candidate-empty early return used to fire BEFORE the
        # --id lookup, so on a store with nothing promotable an unknown id
        # printed "no promotion candidates found" and exited 0. The sibling
        # test above could not catch it — its fixture always seeds an eligible
        # candidate, so the early return never fired.
        self._supersede_all()  # leave zero eligible candidates
        r = self._run("promote", "--id", "00000000-0000-0000-0000-000000000000",
                      "--confirm")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self._skill_files(), [])

    def test_survey_with_no_candidates_is_not_an_error(self):
        # The short-circuit still applies when surveying (no --id): nothing to
        # promote is a normal outcome, not a refusal.
        self._supersede_all()
        r = self._run("promote", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no promotion candidates", r.stdout + r.stderr)

    def test_collision_detection_fires_per_target(self):
        self._promote()
        before = {f: f.read_text(encoding="utf-8") for f in self._skill_files()}
        # Re-promoting the same lesson (still live, id known) should collide
        # in both dirs it already wrote to, and refuse to overwrite either.
        r = self._run("promote", "--id", self.memory_id, "--confirm")
        # Exit code, not just the message: a collision refusal that exits 0 is
        # indistinguishable from a successful promote to any caller checking $?
        # — and CUTOVER's re-promotion loop runs against ~24 existing skills.
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("ERROR", r.stderr)
        self.assertIn(self.skills_a, r.stderr)
        self.assertIn(self.skills_b, r.stderr)
        # Still exactly 2 files on disk (no partial/duplicate write), unchanged.
        after = self._skill_files()
        self.assertEqual(len(after), 2)
        for f in after:
            self.assertEqual(f.read_text(encoding="utf-8"), before[f])


if __name__ == "__main__":
    unittest.main(verbosity=2)
