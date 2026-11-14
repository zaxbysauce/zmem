"""Issue #469 AC1: the add-path dedup fold must disclose the INCOMING
content's disposition (frozen acceptance check).

Contract this check pins (storelib/write.py add_memory fold branch): when
an add folds into an existing row (similarity >= threshold), the stdout
must carry, on ONE line, a "[zmem] dedup:" disclosure that names the
existing row's id, the incoming content's disposition (the literal
substrings "incoming" and "not stored"), and the fold similarity with
three decimals (similarity=d.ddd). The pre-existing bare refresh notice
("existing memory <id> refreshed (similarity=...)") does NOT name the
incoming content's fate and cannot satisfy the contract; that notice
must also SURVIVE the fix (the disclosure is additive, not a
replacement).

Discriminating signature: RED at base (the disclosure does not exist),
GREEN after the fix lands in storelib/write.py.

Drives the REAL skills/memory/scripts/store.py CLI via subprocess
against a hermetic throwaway store (fake embed profile; the boilerplate
below is proven to fold at similarity ~0.984 under that profile).

Run: python -X utf8 tests/test_store_dedup_disclosure.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable

# Boilerplate proven to fold at similarity ~0.984 under the fake embed
# profile (trace probe repro for #469): ~70 shared tokens, distinct tail.
BOILER = (
    "zmem ship record for workstream followup pr review session with plan critic rounds "
    "implementation reviewer and final critic gates all approve at head tree identity "
    "release version bump with eight manifests changelog newest first section dated "
    "canary report regenerated support report stamped after canary five lanes fixture "
    "regenerated release gate emit manifest after git add verification replayed frozen "
    "checks red to green mutation probes killed scan deferred clean full test loop green "
    "anchor receipt published on the target issue with semantics digest stable"
)
CONTENT_A = BOILER + " pr 467"
CONTENT_B = BOILER + " pr 468 zetafold"  # "zetafold" appears ONLY in B
NS = "project:dedup469"


def _env(tmp: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env.pop("ZMEM_DATA", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["ZMEM_EMBED_PROFILE"] = "fake"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class DedupDisclosureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-dedup469-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = _env(self.tmp)

    def _run(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=180,
        )

    def test_fold_discloses_incoming_content_disposition(self):
        """A fold must print, on one line, the incoming content's
        disposition: folded into the existing row and NOT stored as its
        own row. RED at base (the disclosure does not exist), GREEN after
        the fix."""
        self._run("init")
        ra = self._run("add", "--namespace", NS, "--type", "fact",
                       "--content", CONTENT_A, "--signal", "test")
        self.assertEqual(ra.returncode, 0, ra.stderr)
        m = re.search(r"\[zmem\] added memory ([0-9a-f-]{36})", ra.stdout)
        self.assertIsNotNone(
            m, f"add A must report its memory id; stdout:\n{ra.stdout}")
        mem_a_id = m.group(1)

        rb = self._run("add", "--namespace", NS, "--type", "fact",
                       "--content", CONTENT_B, "--signal", "test")
        self.assertEqual(rb.returncode, 0, rb.stderr)

        # THE DISCLOSURE: one "[zmem] dedup:" line carrying, together,
        # the existing row id, the disposition substrings "incoming" and
        # "not stored", and a three-decimal similarity value. The bare
        # refresh notice carries the id and similarity but neither
        # disposition substring, so it can never satisfy this alone.
        dedup_lines = [ln.strip() for ln in rb.stdout.splitlines()
                       if ln.strip().startswith("[zmem] dedup:")]
        disclosures = [
            ln for ln in dedup_lines
            if mem_a_id in ln
            and "incoming" in ln
            and "not stored" in ln
            and re.search(r"similarity=\d+\.\d{3}", ln)
        ]
        if not dedup_lines:
            detail = ("add B printed no '[zmem] dedup:' line at all; the "
                      "fold did not fire under the fake embed profile")
        else:
            detail = ("the '[zmem] dedup:' line(s) printed do not disclose "
                      "the incoming content's disposition; the bare refresh "
                      "notice is not a disclosure")
        self.assertTrue(
            disclosures,
            f"{detail}. The fold must print ONE line starting with "
            f"'[zmem] dedup:' that carries the existing row id {mem_a_id}, "
            f"the disposition substrings 'incoming' and 'not stored', and "
            f"a similarity value with three decimals (similarity=d.ddd). "
            f"add-B stdout:\n{rb.stdout}")

        # The disclosed disposition is characterized honestly: the folded
        # (not stored) content's distinctive token must be absent from the
        # store (row A's id MAY appear; only the incoming content's
        # absence is asserted).
        rs = self._run("search", "--text", "zetafold", "--namespace", NS)
        self.assertEqual(rs.returncode, 0, rs.stderr)
        self.assertIn(
            "[zmem] no matching memories found.", rs.stdout,
            "the distinctive-token search must return NO rows (the AC's "
            "'shall return no match' leg), not merely rows that lack the "
            f"token; search stdout:\n{rs.stdout}")
        self.assertNotIn(
            "zetafold", rs.stdout,
            "the folded content's distinctive token must not appear in "
            f"any returned row; search stdout:\n{rs.stdout}")

        # PRESERVING: the pre-existing refresh notice must survive the
        # fix; the disclosure is additive, not a replacement.
        self.assertIn(
            "refreshed (similarity=", rb.stdout,
            "the existing dedup refresh notice must still be printed "
            "alongside the new disclosure; add-B stdout:\n" + rb.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
