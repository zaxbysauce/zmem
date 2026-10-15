"""Phase 4.2 recurrence guardrail for issue #254: marker-text lockstep.

The contested-link marker text is deliberately duplicated across three
sites — the fence renderer (storelib.recall._format_fenced_recall), the
admission mirror (storelib.inject._row_markers via fence_row_cost), and the
delivery whitelist (storelib.delivery_ledger.rows_present_in). Changing one
site without the others desyncs render/charge/accounting: the fence
overruns its token budget, or a delivered contested row silently drops out
of ledger accounting. This test fails on any single-site desync of the
contested marker, RED under each single-site bare-marker mutation and GREEN
on the lockstep implementation.

Run: python tests/test_p06_marker_lockstep.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

# Pin the store env before the first storelib import (storelib freezes
# STORE_PATH at first import from ambient env).
_TMP = tempfile.mkdtemp(prefix="zmem-p06-lock-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["ZMEM_STORE"] = str(Path(_TMP) / "store.sqlite")
os.environ["ZMEM_DATA"] = _TMP
os.environ["ZMEM_MODELS_DIR"] = str(Path(_TMP) / "no-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
for _k in ("ZMEM_BACKUP_DIR", "ZMEM_MMR_LAMBDA", "ZMEM_LINK_THRESHOLD"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(SCRIPTS_DIR))

from storelib import delivery_ledger as dl  # noqa: E402
from storelib import inject as inject_mod  # noqa: E402
from storelib import recall as recall_mod  # noqa: E402

PARENT = "11111111-aaaa-4bbb-8ccc-000000000001"
CHILD = "22222222-aaaa-4bbb-8ccc-000000000002"
_CHILD_ROW = {
    "id": CHILD,
    "type": "fact",
    "signal": "test",
    "confidence": 0.9,
    "_score": 0.9,
    "content": "the cache ttl setting should never be ten minutes "
    "for the rate limiter service",
    "namespace": "project:links",
    "link_relation": "contradicts",
    "link_of": PARENT,
    "link_score": 0.9,
    "contested_link": True,
}


class MarkerLockstepTest(unittest.TestCase):
    """The renderer/mirror/whitelist triple must agree on the marker.

    Scope note (review N-1): this guards the marker STRING agreement between
    the three sites — it does not claim to catch every possible single-site
    desync (e.g. a change that edits row selection or the budget admission
    policy). The byte-exact assertions below are what make the agreement
    literal rather than inferred from token counts.
    """

    def test_contested_marker_triple_in_lockstep(self):
        """RED under any single-site bare-marker mutation (renderer, inject
        mirror, or ledger whitelist alone); GREEN on the lockstep fix."""
        # Site 1 — renderer: the named marker, with the parent's id.
        text = recall_mod._format_fenced_recall(
            [_CHILD_ROW], "header", legacy_injection_wire=True)
        self.assertIn("[CONTESTED LINK: %s]" % PARENT, text)
        # Site 2 — admission mirror: the mirror's marker is BYTE-IDENTICAL to
        # the rendered one (a token-count comparison is blind to a mirror
        # that names the wrong id — both ids cost the same tokens).
        self.assertEqual(
            inject_mod._row_markers(_CHILD_ROW),
            ["[CONTESTED LINK: %s]" % PARENT],
            "the admission mirror must name the same id the renderer names")
        # ...and the charge covers the rendered marker on both wires (a
        # bare-marker mirror under-charges by the id).
        for wire in (False, True):
            shell = inject_mod.estimate_tokens(recall_mod._format_fenced_recall(
                [], "header", legacy_injection_wire=wire))
            contribution = inject_mod.estimate_tokens(
                recall_mod._format_fenced_recall(
                    [_CHILD_ROW], "header", legacy_injection_wire=wire)) - shell
            self.assertGreaterEqual(
                inject_mod.fence_row_cost(_CHILD_ROW,
                                          legacy_injection_wire=wire),
                contribution,
                "admission must charge the named marker (wire=%s)" % wire)
        # Site 3 — delivery whitelist: the rendered contested row counts as
        # delivered (a bare-only whitelist drops it from ledger accounting).
        self.assertEqual(
            [r["id"] for r in dl.rows_present_in([_CHILD_ROW], text)],
            [CHILD])
        # The whitelist stays a CLOSED shape: prose posing as a named marker
        # (spaces inside the brackets) is still rejected.
        forged = (" [CONTESTED LINK: not one token]- [%s]\n" % CHILD)
        self.assertEqual(
            dl.rows_present_in([_CHILD_ROW], forged), [])

    def test_bare_fallback_is_consistent_across_all_three_sites(self):
        """A contested row WITHOUT `link_of` renders the bare marker at the
        renderer and the mirror, and the whitelist still credits it — the
        fallback cannot silently drop the row from ledger accounting."""
        row = {k: v for k, v in _CHILD_ROW.items() if k != "link_of"}
        self.assertTrue(row.get("contested_link"))
        rendered = recall_mod._format_fenced_recall(
            [row], "header", legacy_injection_wire=True)
        self.assertIn(" [CONTESTED LINK]- [%s]" % CHILD, rendered)
        self.assertEqual(inject_mod._row_markers(row), ["[CONTESTED LINK]"])
        self.assertEqual(
            [r["id"] for r in dl.rows_present_in([row], rendered)], [CHILD])

    def test_non_token_link_of_renders_bare_and_stays_credited(self):
        """A `link_of` that is not a single safe token (space, bracket,
        newline, non-string, empty) renders the bare marker at both sites,
        so the whitelist still credits the row — no silent ledger drop."""
        for bad in ("two words", "a]b", "a\nb", ["a", "b"], "", None, 0):
            row = dict(_CHILD_ROW, link_of=bad)
            marker = inject_mod.contested_marker(row)
            self.assertEqual(marker, "[CONTESTED LINK]",
                             "link_of=%r must render the bare marker" % (bad,))
            self.assertEqual(inject_mod._row_markers(row)[0], marker)
            rendered = recall_mod._format_fenced_recall(
                [row], "header", legacy_injection_wire=True)
            self.assertIn(" [CONTESTED LINK]- [%s]" % CHILD, rendered)
            self.assertEqual(
                [r["id"] for r in dl.rows_present_in([row], rendered)],
                [CHILD],
                "a bare-marker row must still count as delivered")


if __name__ == "__main__":
    unittest.main(verbosity=2)
