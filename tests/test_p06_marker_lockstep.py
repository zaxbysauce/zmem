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

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

# Pin the store env before the first storelib import (storelib freezes
# STORE_PATH at first import from ambient env).
_TMP = tempfile.mkdtemp(prefix="zmem-p06-lock-")
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
    """The renderer/mirror/whitelist triple must agree on the marker."""

    def test_contested_marker_triple_in_lockstep(self):
        """RED under any single-site bare-marker mutation (renderer, inject
        mirror, or ledger whitelist alone); GREEN on the lockstep fix."""
        # Site 1 — renderer: the named marker, with the parent's id.
        text = recall_mod._format_fenced_recall(
            [_CHILD_ROW], "header", legacy_injection_wire=True)
        self.assertIn("[CONTESTED LINK: %s]" % PARENT, text)
        # Site 2 — admission mirror: charge covers the rendered marker on
        # both wires (a bare-marker mirror under-charges by the id).
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
