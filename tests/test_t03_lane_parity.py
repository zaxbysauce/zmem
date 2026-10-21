"""Issues #378 acceptance freeze: strict/legacy ingest lane parity for peer
lineage pointers, source-ref secret scanning, and link collection (AC2, AC3,
AC4 of trace 381-378-373-peer-trust-hardening).

- AC2 (#378 ADV1/2): a peer file's update_of/merged_from pointing at a row
  that lands EARLIER IN THE SAME FILE inside the allow-listed namespace is a
  RESOLVABLE lineage pointer, not a foreign claim -- both lanes must keep it.
  Today the resolvable-id set is a pre-batch store snapshot in both lanes, so
  same-file pointers are wrongly cleared.
- AC3 (#378 ADV5): under --source-ref the legacy lane scans the OVERRIDDEN
  ref for secret-like shapes while the strict lane scans (and fatally refuses
  on) the ORIGINAL row-carried ref. The lanes must agree: the original ref is
  what gets scanned.
- AC4 (lane parity ratchet): both peer link-collection sites (the strict
  lane's and the legacy lane's pending_links.append) must route through ONE
  shared outcome-admission predicate instead of two divergent inline
  conditions. Pinned STRUCTURALLY on the sync.py source, precedent
  tests/test_entity.py:885-905 (ClassifierScopeTest source ratchet).

The behavioral contract is the CLI surface of the REAL store.py subprocess,
exactly like tests/test_t03_ingest_trust.py: rows are hand-built with the
canonical _sync_row builder, ingested through ingest-jsonl with the peer
trust flags, and the assertions pin STORED sqlite state plus the printed
summary line.

RED at base (same-file pointers cleared; overridden-ref secret stored), GREEN
after issue #378.

Run standalone (CI style): python tests/test_t03_lane_parity.py
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import test_jsonl_sync as T  # noqa: E402  (canonical ingest harness: _TwoStoreCase + _sync_row)

NS = "project:t03/parity"
ANCESTOR_ID = "00000000-0b01-4000-8000-0000000000b1"
UPDATE_ROW_ID = "00000000-0b02-4000-8000-0000000000b2"
MERGE_ROW_ID = "00000000-0b03-4000-8000-0000000000b3"
SECRET_REF_ROW_ID = "00000000-0b04-4000-8000-0000000000b4"
# The secret-like ORIGINAL row-carried ref (AC3's fixed shape): a credential
# command line that capture-mode auto must refuse to attribute a row to.
SECRET_LIKE_REF = "sshpass -p pw999 probe ref"
CALLER_REF = "session:t03-parity-override"
SYNC_PY = (T.REPO_ROOT / "skills" / "memory" / "scripts"
           / "storelib" / "sync.py")


class SameFileLineageParityTest(T._TwoStoreCase):
    """AC2 of issue #378: same-file lineage pointers survive BOTH lanes."""

    def _peer_file(self, store):
        """A three-row peer file: the ancestor lands FIRST, then a row whose
        update_of points at it, then a row whose merged_from points at it."""
        rows = [
            T._sync_row(id=ANCESTOR_ID, namespace=NS,
                        content="same-file lineage ancestor row"),
            T._sync_row(id=UPDATE_ROW_ID, namespace=NS,
                        content="row updating its same-file ancestor",
                        update_of=ANCESTOR_ID),
            T._sync_row(id=MERGE_ROW_ID, namespace=NS,
                        content="row merged from its same-file ancestor",
                        merged_from=ANCESTOR_ID),
        ]
        return self._write_jsonl(store, "peer_lineage.jsonl", rows)

    def test_same_file_lineage_pointer_survives_both_lanes(self):
        """AC2: update_of and merged_from pointing at a row landing EARLIER
        IN THE SAME FILE (inside the allow-listed namespace) must be KEPT by
        the legacy lane and by the strict lane alike -- the pointer resolves
        against the file itself, not only against a pre-batch store snapshot.
        RED at base (both lanes clear the pointer), GREEN after #378."""
        # Legacy lane: per-row refusals are counted, file-level exit stays 0.
        path = self._peer_file(self.b)
        r = self.b.run("ingest-jsonl", "--trust", "untrusted-peer",
                       "--allow-namespace", NS, "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 3, r.stdout)
        kept_update = self.b.query_one(
            "SELECT update_of FROM memory WHERE id=?", (UPDATE_ROW_ID,))
        self.assertIsNotNone(kept_update, "the update row must be stored")
        self.assertEqual(kept_update[0] or "", ANCESTOR_ID,
                         "legacy lane: a same-file lineage pointer inside "
                         "the allow-list must be kept, not cleared")
        kept_merge = self.b.query_one(
            "SELECT merged_from FROM memory WHERE id=?", (MERGE_ROW_ID,))
        self.assertIsNotNone(kept_merge, "the merged row must be stored")
        self.assertEqual(kept_merge[0] or "", ANCESTOR_ID,
                         "legacy lane: a same-file merged_from pointer "
                         "inside the allow-list must be kept, not cleared")

        # Strict lane: same file, same expectation (all-or-nothing run).
        path = self._peer_file(self.a)
        r = self.a.run("ingest-jsonl", "--trust", "untrusted-peer",
                       "--allow-namespace", NS, "--strict", "--in", path)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        kept_update = self.a.query_one(
            "SELECT update_of FROM memory WHERE id=?", (UPDATE_ROW_ID,))
        self.assertIsNotNone(kept_update, "the update row must be stored")
        self.assertEqual(kept_update[0] or "", ANCESTOR_ID,
                         "strict lane: a same-file lineage pointer inside "
                         "the allow-list must be kept, not cleared")
        kept_merge = self.a.query_one(
            "SELECT merged_from FROM memory WHERE id=?", (MERGE_ROW_ID,))
        self.assertIsNotNone(kept_merge, "the merged row must be stored")
        self.assertEqual(kept_merge[0] or "", ANCESTOR_ID,
                         "strict lane: a same-file merged_from pointer "
                         "inside the allow-list must be kept, not cleared")


class SourceRefOverrideParityTest(T._TwoStoreCase):
    """AC3 of issue #378: the ORIGINAL row-carried ref is what gets scanned
    for secret-like shapes, in both lanes, even under --source-ref."""

    def _secret_row(self):
        return T._sync_row(
            id=SECRET_REF_ROW_ID, namespace=NS,
            content="peer row whose original ref looks like a credential",
            source_ref=SECRET_LIKE_REF)

    def test_legacy_lane_scans_original_ref_under_override(self):
        """AC3: with --source-ref supplied and --capture-mode auto, a row
        whose ORIGINAL source_ref is secret-like must NOT be stored by the
        legacy lane -- the refusal/quarantine is counted and the file-level
        exit stays 0 -- matching the strict lane, which already refuses the
        same row fatally. RED at base (the legacy lane stores the row because
        it only ever scans the clean overridden ref), GREEN after #378."""
        row = self._secret_row()
        path = self._write_jsonl(self.b, "peer_secret_ref.jsonl", [row])
        r = self.b.run("ingest-jsonl", "--trust", "untrusted-peer",
                       "--allow-namespace", NS,
                       "--source-ref", CALLER_REF,
                       "--capture-mode", "auto", "--in", path)
        self.assertEqual(r.returncode, 0,
                         "a counted peer refusal must not fail the legacy "
                         "file: " + r.stdout + r.stderr)
        self.assertIsNone(
            self.b.query_one("SELECT id FROM memory WHERE id=?",
                             (SECRET_REF_ROW_ID,)),
            "legacy lane: a secret-like ORIGINAL source_ref must not be "
            "masked by the caller's --source-ref override -- the row must "
            "not be stored")
        counts = self._summary_counts(r.stdout)
        self.assertGreaterEqual(
            counts["capture_refused"] + counts["quarantined"], 1,
            "the secret-like original ref must be counted as a "
            f"refusal/quarantine: {r.stdout!r}")

    def test_strict_lane_still_refuses_original_ref_under_override(self):
        """AC3 parity anchor (GREEN at base): the strict lane already scans
        the ORIGINAL row-carried ref and refuses the whole import fatally --
        this is the behavior the legacy lane must match, pinned here so the
        fix cannot regress the strict side while repairing the legacy side."""
        row = self._secret_row()
        path = self._write_jsonl(self.a, "peer_secret_ref.jsonl", [row])
        r = self.a.run("ingest-jsonl", "--trust", "untrusted-peer",
                       "--allow-namespace", NS,
                       "--source-ref", CALLER_REF,
                       "--capture-mode", "auto", "--strict", "--in", path)
        self.assertEqual(r.returncode, 2,
                         "strict lane: a secret-like ORIGINAL source_ref is "
                         "a fatal peer-policy violation: "
                         + r.stdout + r.stderr)
        self.assertIn("[zmem] ingest-jsonl: strict import rejected:",
                      r.stderr, r.stderr)
        self.assertIsNone(
            self.a.query_one("SELECT id FROM memory WHERE id=?",
                             (SECRET_REF_ROW_ID,)),
            "strict lane: the refused row must not land")


class PeerLinkOutcomeParityTest(unittest.TestCase):
    """AC4 of issue #378: ONE shared peer link-collection admission rule.

    CONTRACT (stated here, pinned structurally on the source -- precedent:
    the ClassifierScopeTest source ratchet in tests/test_entity.py:885-905):
    storelib/sync.py defines a module-level helper named
    ``_peer_link_outcome_admitted`` -- the SINGLE predicate deciding which
    ingest outcomes a PEER row's _links carriage may be collected from --
    and BOTH peer link-collection sites (the strict lane's and the legacy
    lane's ``pending_links.append``) reference that helper in the guard
    immediately above the append. Two divergent inline conditions (today the
    strict lane admits every outcome except skipped/deduped/purged_denied
    while the legacy lane admits only `added`) are the latent parity defect
    this ratchet forbids.
    """

    HELPER = "_peer_link_outcome_admitted"

    def test_peer_link_collection_routes_through_one_shared_predicate(self):
        """AC4: the shared predicate exists exactly once and is referenced at
        both pending_links collection sites. RED at base (no such helper; the
        two lanes carry divergent inline outcome conditions), GREEN after
        #378."""
        src = Path(SYNC_PY).read_text(encoding="utf-8")
        self.assertTrue(Path(SYNC_PY).is_file(), "sync.py source must exist")

        # The helper is defined exactly once.
        defs = re.findall(rf"^def {self.HELPER}\(", src, re.M)
        self.assertEqual(
            len(defs), 1,
            f"sync.py must define {self.HELPER} exactly once as the single "
            f"peer link-collection outcome-admission rule; found {len(defs)}")

        # Every collection site is a pending_links.append; BOTH peer
        # collection sites must sit under a guard CALLING the shared
        # predicate (the CALL form `helper(`, not a bare mention — a
        # comment naming the predicate satisfies a bare-name window check
        # while the guard re-inlines its own condition, which is the mutant
        # this ratchet exists to kill; found by the Phase 4.5 M4 mutation
        # probe) in the window of the guard immediately above each append.
        append_at = [m.start() for m in re.finditer(
            r"pending_links\.append\(", src)]
        self.assertGreaterEqual(
            len(append_at), 2,
            "expected a pending_links collection site in each lane "
            f"(strict + legacy); found {len(append_at)}")
        window_chars = 1000
        routed = sum(
            1 for idx in append_at
            if (self.HELPER + "(") in src[max(0, idx - window_chars):idx])
        self.assertGreaterEqual(
            routed, 2,
            "both peer link-collection sites (strict and legacy "
            "pending_links.append) must route their outcome admission "
            f"through the shared {self.HELPER} predicate instead of "
            "divergent inline conditions")


if __name__ == "__main__":
    unittest.main()
