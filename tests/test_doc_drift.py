"""Documentation-drift ratchet (issue #56, task 1.8).

Each test pins a documentation claim that had drifted from code and was
repaired by the 0.8.7 documentation-truth pass (issue #56). If a test here
fails, a doc or tracked file regressed — fix the doc/file; do not weaken the
test. The companion schema-version pin lives in tests/test_schema_version.py.

Covered regressions:
  - SKILL.md describing vector recall as "a future optional tier" (hybrid
    `--hybrid` RRF recall already shipped)
  - SKILL.md recall usage omitting `--hybrid` / `--no-bump`
  - PLAN.md claiming userConfig.storeDirectory is unwired (it is wired via
    zmem-launch.js / CLAUDE_PLUGIN_OPTION_STOREDIRECTORY, #38 I6)
  - any tracked file claiming a 3.8-era Python floor (the floor is 3.11)
  - any tracked file embedding an absolute user home path (leaks the
    operator's username/machine into the distributable; e.g. the graphify
    cache that shipped under graphify-out/ before 0.8.7 untracked it)

Needles that would match this file's own source are built by concatenation so
the scan cannot self-match (and a test asserts exactly that).

Run: python tests/test_doc_drift.py
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "skills" / "memory" / "SKILL.md"
PLAN_MD = REPO_ROOT / "PLAN.md"

_BS = chr(92)  # backslash, built indirectly so this file cannot self-match
# One-or-more path separator as a REGEX fragment: a character class holding an
# escaped backslash and a slash. The backslash is doubled (chr(92) twice)
# because inside a class a lone backslash before ] would read as an escaped
# closing bracket, silently changing the class to {], /, +} — no backslash.
_SEP = "[" + _BS * 2 + "/]+"

# An absolute user home path: a Windows drive form (drive-letter + separator
# + Users + separator + a real-looking name), or a POSIX/macOS home dir.
# Placeholders whose first segment character is outside [A-Za-z0-9_~-] (e.g.
# an angle-bracket form after the Users segment) deliberately do NOT match —
# that is the sanctioned way to show a path shape in docs/examples.
HOME_PATH_RE = re.compile(
    "(?:[A-Za-z]:{sep}Users{sep}|{sep}home{sep}|{sep}Users{sep})"
    "[A-Za-z0-9_~-]+".format(sep=_SEP)
)

# The Python-floor claim, built by concatenation so this file's own source
# cannot contain the contiguous needle it forbids.
PY38_NEEDLE = "3.8" + "+"


def _tracked_files():
    """Every git-tracked path. A git failure is a loud test failure, never a
    vacuous pass — the ratchet must not silently skip the scan."""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(REPO_ROOT), capture_output=True, timeout=60,
    )
    if out.returncode != 0:
        raise AssertionError(
            "git ls-files failed (rc=%d): %s" % (
                out.returncode, out.stderr.decode("utf-8", "replace")))
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def _read(rel):
    return (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")


class SkillDocDriftTest(unittest.TestCase):

    def test_no_future_optional_tier_claim(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertNotIn(
            "future optional tier", text,
            "SKILL.md must not describe vector/embedding recall as a future "
            "tier — `--hybrid` RRF recall shipped; document it instead")

    def test_recall_usage_lists_hybrid_and_no_bump(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("--hybrid", text,
                      "SKILL.md recall documentation must list --hybrid")
        self.assertIn("--no-bump", text,
                      "SKILL.md recall documentation must list --no-bump")


class PlanDocDriftTest(unittest.TestCase):

    def test_stale_storedirectory_phrase_is_gone(self):
        text = PLAN_MD.read_text(encoding="utf-8")
        self.assertNotIn(
            "aren't wired to `ZMEM_DATA` at runtime", text,
            "PLAN.md must not carry the stale claim that userConfig values "
            "are unwired (#38 I6): zmem-launch.js consumes "
            "CLAUDE_PLUGIN_OPTION_STOREDIRECTORY. Keep the superseded note "
            "paraphrased, never verbatim.")


class CloudDocDriftTest(unittest.TestCase):
    """docs/CLOUD.md is the Tier 1 contract that `export-pack --help` points
    at; before 0.8.7 it still taught the pre-#37-L1 false claim that
    structural text was exempt from the --max-bytes budget (final-critic
    finding on this very PR — the help was corrected but the doc it
    references was not)."""

    def test_cloud_md_states_the_whole_pack_budget(self):
        text = (REPO_ROOT / "docs" / "CLOUD.md").read_text(encoding="utf-8")
        self.assertIn("whole rendered pack", text,
                      "CLOUD.md must state that --max-bytes budgets the whole "
                      "rendered pack, matching _render_pack and --help")
        self.assertNotIn(
            "is exempt", text,
            "CLOUD.md must not claim structural text is exempt from the "
            "--max-bytes budget — _render_pack counts framing toward the cap; "
            "only post-walk framing (empty-section heading + omitted-count "
            "note) can exceed it")


class TrackedFileHygieneTest(unittest.TestCase):

    def test_no_tracked_file_claims_python_3_8_floor(self):
        offenders = [
            rel for rel in _tracked_files()
            if PY38_NEEDLE in _read(rel)
        ]
        self.assertEqual(
            [], offenders,
            "tracked files still claim a 3.8-era Python floor; the supported "
            "floor is 3.11 (README, SKILL.md, doctor, script docstrings)")

    def test_no_tracked_file_embeds_an_absolute_home_path(self):
        offenders = [
            f"{rel}:{m.start()}" for rel in _tracked_files()
            for m in HOME_PATH_RE.finditer(_read(rel))
        ]
        self.assertEqual(
            [], offenders,
            "tracked files embed absolute user home paths — replace with a "
            "placeholder (angle-bracket segment) or an env-var form and "
            "untrack generated caches")

    def test_home_path_ratchet_does_not_self_match(self):
        # If this file itself matched the scan, test_no_tracked_file_embeds_
        # an_absolute_home_path would fail confusingly on its own source.
        self.assertIsNone(
            HOME_PATH_RE.search(Path(__file__).read_text(encoding="utf-8")),
            "tests/test_doc_drift.py itself must not contain a matchable "
            "home-path literal — build fixtures by concatenation")

    def test_home_path_regex_positive_and_negative_controls(self):
        # Positive control (fixture assembled from parts, so the literal never
        # appears in this source): a real-shaped home path MUST match.
        sample = "C:" + _BS + "Users" + _BS + "someoperator" + _BS + ".zmem"
        self.assertIsNotNone(HOME_PATH_RE.search(sample))
        posix = "/home/" + "someoperator"
        self.assertIsNotNone(HOME_PATH_RE.search(posix))
        # Negative controls: sanctioned placeholder forms must NOT match.
        placeholder = "C:" + _BS + "Users" + _BS + "<user>" + _BS + ".zmem"
        self.assertIsNone(HOME_PATH_RE.search(placeholder))
        ellipsis = "C:" + _BS + "Users" + _BS + "..." + _BS + "plugins"
        self.assertIsNone(HOME_PATH_RE.search(ellipsis))


if __name__ == "__main__":
    unittest.main(verbosity=2)
