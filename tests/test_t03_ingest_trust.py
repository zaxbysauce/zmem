"""Issue #282 acceptance freeze (Workstream T PR 3 of 12): untrusted-peer
ingest mode for store.py ingest-jsonl.

Three CONTRACT tests, one per machine-checkable acceptance criterion:
  AC1  test_untrusted_peer_cannot_claim_trusted_taint
  AC2  test_untrusted_peer_namespace_allow_list
  AC3  test_peer_attribution_tags_are_server_derived

The contract is the CLI surface of the REAL store.py subprocess, exactly like
tests/test_jsonl_sync.py: rows are hand-built with that harness's _sync_row
builder, ingested through ingest-jsonl with the new trust flags, and the
assertions pin STORED sqlite state plus the printed summary line. Trust
fields are clamped (never rejected); only namespace is refused.

RED at base (flags absent, trust fields pass through verbatim), GREEN after
issue #282.

Run standalone (CI style): python tests/test_t03_ingest_trust.py
Run one frozen leg:
  python -X utf8 -m pytest tests/test_t03_ingest_trust.py::UntrustedPeerIngestTest::test_untrusted_peer_cannot_claim_trusted_taint -q -p no:randomly -p no:cacheprovider
"""

from __future__ import annotations

import os
import re
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import test_jsonl_sync as T  # noqa: E402  (canonical ingest harness: _TwoStoreCase + _sync_row)

# The peer confidence ceiling from the issue (default 0.6). A claimed
# confidence above it must be clamped down to at most this value.
PEER_CONFIDENCE_CEILING = 0.6


class UntrustedPeerIngestTest(T._TwoStoreCase):
    """AC1-AC3 of issue #282: untrusted-peer ingest clamps trust, refuses
    namespaces outside the allow list, and derives attribution from the
    ingesting caller instead of the peer."""

    @staticmethod
    def _summary_field(stdout: str, name: str) -> int:
        """One integer field of the ingest-jsonl summary line by name (the
        issue adds namespace_refused and trust_clamped to it)."""
        m = re.search(name + r"=(\d+)", stdout)
        assert m, "summary field " + name + " missing from the ingest-jsonl summary line"
        return int(m.group(1))

    def test_untrusted_peer_cannot_claim_trusted_taint(self):
        """AC1: an untrusted-peer row CLAIMING signal test with taint
        trusted_internal and confidence 0.95 must still be STORED, but with
        taint no stronger than untrusted_tool, the claimed signal recorded
        only as a claimed_signal tag, and confidence clamped to the peer
        ceiling. Clamp, never reject, on trust fields.
        RED at base (flags absent, trust fields pass through verbatim),
        GREEN after issue #282."""
        row = T._sync_row(
            id="11111111-2222-4333-8444-555555555555",
            namespace="project:github.com/peer/claims",
            content="peer row claiming a trusted taint it cannot have",
            signal="test", taint="trusted_internal", confidence=0.95)
        path = self._write_jsonl(self.b, "peer_claim.jsonl", [row])
        r = self.b.run("ingest-jsonl", "--trust", "untrusted-peer",
                       "--allow-namespace", row["namespace"], "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1, r.stdout)
        self.assertIn("trust_clamped=", r.stdout,
                      "the summary line must carry a trust_clamped field")
        stored = self.b.query_one(
            "SELECT taint, signal, confidence, tags FROM memory WHERE id=?",
            (row["id"],))
        self.assertIsNotNone(stored,
                             "a clamped peer row must be stored not rejected")
        taint, signal, confidence, tags = stored
        self.assertIn(taint, ("untrusted_tool", "untrusted_web"),
                      "untrusted-peer ingest may not store a taint stronger than untrusted_tool")
        self.assertIn("claimed_signal:test", tags,
                      "the claimed signal must be recorded as a claimed_signal tag")
        self.assertNotEqual(signal, "test",
                            "the grounded claimed signal must not be stored as the row signal")
        self.assertLessEqual(confidence, PEER_CONFIDENCE_CEILING,
                             "untrusted-peer confidence is clamped to the peer ceiling")
        # Final clause: untrusted-peer trust mode refuses to compose with
        # tombstone authority -- a usage error, exit code exactly 2.
        r2 = self.b.run("ingest-jsonl", "--trust", "untrusted-peer",
                        "--allow-tombstones", "--in", path)
        self.assertEqual(r2.returncode, 2,
                         "untrusted-peer ingest combined with allow-tombstones is a usage error")

    def test_untrusted_peer_namespace_allow_list(self):
        """AC2: with --allow-namespace project:github.com/a/b, a row for that
        namespace ingests while a user:global row is refused and counted in a
        new namespace_refused summary field (trust_clamped also present);
        the file-level exit stays 0 because a refusal is counted, not fatal.
        RED at base (flags absent, trust fields pass through verbatim),
        GREEN after issue #282."""
        allowed = T._sync_row(
            id="22222222-3333-4444-8555-666666666666",
            namespace="project:github.com/a/b",
            content="peer row inside the namespace allow list")
        refused = T._sync_row(
            id="22222222-3333-4444-8555-777777777777",
            namespace="user:global",
            content="peer row outside the namespace allow list")
        path = self._write_jsonl(self.b, "peer_namespaces.jsonl",
                                 [allowed, refused])
        r = self.b.run("ingest-jsonl", "--trust", "untrusted-peer",
                       "--allow-namespace", "project:github.com/a/b",
                       "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1, r.stdout)
        self.assertEqual(self._summary_field(r.stdout, "namespace_refused"), 1,
                         "the refused user:global row must be counted as namespace_refused")
        self.assertIn("trust_clamped=", r.stdout,
                      "the summary line must carry a trust_clamped field")
        self.assertIsNotNone(
            self.b.query_one("SELECT id FROM memory WHERE id=?",
                             (allowed["id"],)),
            "the allow-listed namespace row must be stored")
        self.assertEqual(
            self.b.query_one(
                "SELECT COUNT(*) FROM memory WHERE namespace=?",
                ("user:global",))[0], 0,
            "a namespace outside the allow list must be refused not stored")

    def test_peer_attribution_tags_are_server_derived(self):
        """AC3: host and agent tags on an untrusted-peer row are dropped and
        the host attribution is applied from the --attribute-host value the
        INGESTING CALLER supplies; non-attribution tags survive untouched.
        RED at base (flags absent, trust fields pass through verbatim),
        GREEN after issue #282."""
        row = T._sync_row(
            id="33333333-4444-4555-8666-777777777777",
            namespace="project:github.com/peer/attribution",
            content="peer row with forged attribution tags",
            tags="host:peer-lie,agent:peer-lie,topic:x")
        path = self._write_jsonl(self.b, "peer_attribution.jsonl", [row])
        r = self.b.run("ingest-jsonl", "--trust", "untrusted-peer",
                       "--allow-namespace", row["namespace"],
                       "--attribute-host", "ingest-box-7", "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1, r.stdout)
        stored = self.b.query_one(
            "SELECT tags FROM memory WHERE id=?", (row["id"],))
        self.assertIsNotNone(stored, "the peer row must be stored")
        tags = stored[0]
        self.assertIn("host:ingest-box-7", tags,
                      "attribution must come from the caller attribute-host value")
        self.assertNotIn("host:peer-lie", tags,
                         "peer-supplied host tags must be dropped")
        self.assertNotIn("agent:", tags,
                         "peer-supplied agent tags must be dropped")
        self.assertIn("topic:x", tags,
                      "non-attribution tags must survive untrusted-peer ingest")


if __name__ == "__main__":
    unittest.main()
