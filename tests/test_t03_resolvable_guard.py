"""Behavioral guardrail for the #378 resolvable-set discrimination
(additive, UNFROZEN companion to tests/test_t03_lane_parity.py; written in
the implementation-review round after the frozen files were checkpointed).

The frozen lane-parity tests pin that same-file lineage pointers SURVIVE;
these tests pin the security-critical other side — which ids must NOT become
resolvable. The implementation review proved (by probe) that the
all-staged-union mutant — the exact F22 reopening the plan critic's round-1
blocker forbade — passes every frozen test, so the discrimination needs a
behavioral net of its own:

- test_all_staged_union_decoy_cannot_make_a_foreign_id_resolvable: a peer
  decoy staged row re-using a LOCAL row's id while CLAIMING an allow-listed
  namespace must not make that (foreign-namespace) id resolvable — the
  pointer stays cleared. Kills the all-staged-union mutant in BOTH lanes.
- test_purged_target_pointer_is_cleared: a staged id on the purged deny-list
  never lands (purged_denied) — a pointer to it is cleared, not kept. Kills
  the missing-purged-subtraction mutant in the strict lane (legacy never
  accumulated purged ids).

Run standalone (CI style): python tests/test_t03_resolvable_guard.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import test_jsonl_sync as T  # noqa: E402  (canonical ingest harness)

NS = "project:t03/rguard"          # the peer allow-list
NS_FOREIGN = "project:t03/foreign"  # deliberately OUTSIDE the allow-list
DECOY_TARGET_ID = "00000000-0c01-4000-8000-0000000000c1"
PURGED_TARGET_ID = "00000000-0c02-4000-8000-0000000000c2"
CHILD_ID = "00000000-0c03-4000-8000-0000000000c3"


class ResolvableSetGuardTest(T._TwoStoreCase):
    """The resolvable set is authored-and-landing rows, never staged claims
    over foreign ids and never purged ids."""

    def _peer_ingest(self, store, rows, *extra):
        path = self._write_jsonl(store, "peer_guard.jsonl", rows)
        return store.run("ingest-jsonl", "--trust", "untrusted-peer",
                         "--allow-namespace", NS, *extra, "--in", path)

    def test_all_staged_union_decoy_cannot_make_a_foreign_id_resolvable(self):
        """A LOCAL row sits in a namespace OUTSIDE the allow-list. A peer
        file stages a decoy row re-using that row's id while CLAIMING the
        allow-listed namespace (it can only land as `skipped` — the id is
        taken). A third row's update_of pointing at that id must stay
        CLEARED: staged claims confer no resolvability over foreign rows
        (the F22 boundary). RED under the all-staged-union mutant, GREEN on
        the shipped authored-union code."""
        for store, extra in ((self.a, ("--strict",)), (self.b, ())):
            with self.subTest(lane="strict" if extra else "legacy"):
                # Seed the local foreign-namespace row (authoritative lane).
                local_row = T._sync_row(
                    id=DECOY_TARGET_ID, namespace=NS_FOREIGN,
                    content="local row in a namespace outside the allow-list")
                seed = self._write_jsonl(
                    store, f"seed_local_{DECOY_TARGET_ID[:8]}.jsonl",
                    [local_row])
                r = store.run("ingest-jsonl", "--in", seed)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIsNotNone(
                    store.query_one(
                        "SELECT id FROM memory WHERE id=?",
                        (DECOY_TARGET_ID,)),
                    "the local foreign-namespace row must be seeded")

                # Peer file: decoy re-using the id (claiming the ALLOWED ns),
                # plus a child whose update_of points at that id.
                decoy = T._sync_row(
                    id=DECOY_TARGET_ID, namespace=NS,
                    content="peer decoy re-using the local row's id")
                child = T._sync_row(
                    id=CHILD_ID, namespace=NS,
                    content="peer child pointing at the decoy id",
                    update_of=DECOY_TARGET_ID)
                r = self._peer_ingest(store, [decoy, child], *extra)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                kept = store.query_one(
                    "SELECT update_of FROM memory WHERE id=?", (CHILD_ID,))
                self.assertIsNotNone(kept, "the child row must be stored")
                self.assertEqual(
                    kept[0] or "",
                    "",
                    "a staged decoy claiming an allowed namespace must not "
                    "make a FOREIGN-namespace local id resolvable — the "
                    "pointer must stay cleared (F22 boundary)")

    def test_purged_target_pointer_is_cleared(self):
        """A staged id on the purged deny-list never lands at apply time
        (purged_denied). A peer row's update_of pointing at it must stay
        CLEARED — no kept pointers to rows that were never created. RED
        under the missing-purged-subtraction mutant in the strict lane."""
        for store, extra in ((self.a, ("--strict",)), (self.b, ())):
            with self.subTest(lane="strict" if extra else "legacy"):
                conn = sqlite3.connect(store.path)
                try:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS purged_id ("
                        "id TEXT PRIMARY KEY, purged_at TEXT NOT NULL)")
                    conn.execute(
                        "INSERT OR REPLACE INTO purged_id VALUES (?, ?)",
                        (PURGED_TARGET_ID, "2026-10-21T00:00:00Z"))
                    conn.commit()
                finally:
                    conn.close()

                target = T._sync_row(
                    id=PURGED_TARGET_ID, namespace=NS,
                    content="peer row whose id is on the purged deny-list")
                child = T._sync_row(
                    id=CHILD_ID, namespace=NS,
                    content="peer child pointing at the purged id",
                    update_of=PURGED_TARGET_ID)
                r = self._peer_ingest(store, [target, child], *extra)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIsNone(
                    store.query_one(
                        "SELECT id FROM memory WHERE id=?",
                        (PURGED_TARGET_ID,)),
                    "a purged id must never land")
                kept = store.query_one(
                    "SELECT update_of FROM memory WHERE id=?", (CHILD_ID,))
                self.assertIsNotNone(kept, "the child row must be stored")
                self.assertEqual(
                    kept[0] or "",
                    "",
                    "a lineage pointer to a PURGED id (never landed) must "
                    "stay cleared")


if __name__ == "__main__":
    unittest.main()
