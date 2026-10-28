"""Issue #381 acceptance freeze: the peer attribution strip must match the
CONSUMER's tokenization (AC1 of trace 381-378-373-peer-trust-hardening).

sync.py's untrusted-peer tag strip splits on commas only and matches the
host:/agent: prefixes case-sensitively, while the entity tokenizer that
CONSUMES the stored tags splits on comma/semicolon/whitespace and folds the
prefix case-insensitively. A peer row carrying ``Host:evil``, ``AGENT:ghost``
or ``x;host:evil`` therefore keeps its forged attribution AND mints host
entities from the claims the strip was supposed to remove.

AC1: WHEN a peer row carries attribution tags in case-varied (Host:, AGENT:)
or non-comma (x;host:evil, whitespace) forms, THE SYSTEM SHALL strip every
host:/agent: attribution token under the CONSUMER's tokenization (split on
comma/semicolon/whitespace, case-insensitive prefix), stamp only caller
attribution, and mint no host entity from stripped claims.

The contract is the CLI surface of the REAL store.py subprocess, exactly like
tests/test_t03_ingest_trust.py (whose bytes are checkpoint-pinned): rows are
hand-built with the canonical _sync_row builder, ingested through
ingest-jsonl with the peer trust flags, and the assertions pin STORED sqlite
state (memory.tags, entity rows) plus the printed summary line.

RED at base (case-varied and non-comma tokens survive verbatim), GREEN after
issue #381.

Run standalone (CI style): python tests/test_t03_attribution_strip.py
Run one frozen leg:
  python -X utf8 -m pytest tests/test_t03_attribution_strip.py::PeerAttributionStripTest::test_case_varied_and_non_comma_attribution_tokens_are_stripped -q -p no:randomly -p no:cacheprovider
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import os
import re
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import test_jsonl_sync as T  # noqa: E402  (canonical ingest harness: _TwoStoreCase + _sync_row)

NS = "project:t03/strip"
HOSTILE_ROW_ID = "00000000-0a01-4000-8000-0000000000a1"
CALLER_HOST = "strip-box-1"
# One row carrying every mismatch form from the issue: case-varied prefixes
# (Host:, AGENT:), a semicolon-joined token (x;host:evil2), and a
# whitespace-separated token (host:evil3) -- plus one honest non-attribution
# tag that must survive.
HOSTILE_TAGS = "Host:evil,AGENT:ghost,x;host:evil2,topic:y host:evil3"
STRIPPED_NAMES = ("evil", "evil2", "evil3")


class PeerAttributionStripTest(T._TwoStoreCase):
    """AC1 of issue #381: the peer strip runs under the consumer's
    tokenization (comma/semicolon/whitespace split, case-insensitive prefix),
    so no host:/agent: claim survives in any form."""

    def test_case_varied_and_non_comma_attribution_tokens_are_stripped(self):
        """AC1: every host:/agent: attribution token -- including the
        case-varied (Host:, AGENT:) and non-comma (semicolon, whitespace)
        forms the consumer tokenizer splits on -- must be stripped from a
        peer row's tags; only the caller's --attribute-host stamp may carry a
        host: token, and no host entity may be minted from the stripped
        claims. RED at base, GREEN after issue #381."""
        # Consumer ground truth (pure function, green at base): the entity
        # tokenizer demonstrably reads the hostile forms as host: claims --
        # this is why the strip MUST share the consumer's tokenization.
        sys.path.insert(0, str(T.SCRIPTS_DIR))
        from storelib.entity import extract_entities  # noqa: E402
        minted = extract_entities("", HOSTILE_TAGS, NS)
        for name in STRIPPED_NAMES:
            self.assertIn(("host", name), minted,
                          "probe drift: the entity tokenizer no longer reads "
                          f"{name!r} out of the hostile tag fixture -- update "
                          "the fixture, the threat model stands")

        row = T._sync_row(
            id=HOSTILE_ROW_ID, namespace=NS,
            content="peer row carrying case-varied and non-comma attribution",
            tags=HOSTILE_TAGS)
        path = self._write_jsonl(self.b, "peer_strip.jsonl", [row])
        r = self.b.run("ingest-jsonl", "--trust", "untrusted-peer",
                       "--allow-namespace", NS,
                       "--attribute-host", CALLER_HOST, "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1, r.stdout)

        stored = self.b.query_one(
            "SELECT tags FROM memory WHERE id=?", (HOSTILE_ROW_ID,))
        self.assertIsNotNone(stored, "the peer row must be stored")
        # The CONSUMER's tokenization, applied to what actually landed: the
        # only permitted host:/agent: token is the caller's own stamp.
        tokens = [t for t in re.split(r"[,;\s]+", stored[0] or "") if t]
        self.assertIn(f"host:{CALLER_HOST}", tokens,
                      "the caller's --attribute-host stamp must be present")
        survivors = [t for t in tokens
                     if t.lower().startswith(("host:", "agent:"))
                     and t != f"host:{CALLER_HOST}"]
        self.assertEqual(
            survivors, [],
            "host:/agent: attribution tokens survived the peer strip under "
            "the consumer's tokenization (comma/semicolon/whitespace split, "
            f"case-insensitive prefix): {survivors!r}")
        self.assertIn("topic:y", tokens,
                      "non-attribution tags must survive the strip")
        self.assertNotIn("Host:evil", tokens)
        self.assertNotIn("AGENT:ghost", tokens)
        self.assertNotIn("host:evil2", tokens)
        self.assertNotIn("host:evil3", tokens)

        # No host entity may be minted from the stripped claims.
        placeholders = ",".join("?" * len(STRIPPED_NAMES))
        minted_rows = self.b.query_all(
            f"SELECT kind, canonical_name FROM entity "
            f"WHERE canonical_name IN ({placeholders})",
            STRIPPED_NAMES)
        self.assertEqual(
            minted_rows, [],
            "host entities were minted from attribution claims the peer "
            "strip was supposed to remove")


if __name__ == "__main__":
    unittest.main()
