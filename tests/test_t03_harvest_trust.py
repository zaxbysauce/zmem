"""Issue #373 acceptance freeze: scripts/ingest_harvest.py grows a trust mode
(AC5, AC6 of trace 381-378-373-peer-trust-hardening).

The harvest adapter today has no trust surface at all: it drives one
``store.py add --capture-mode auto --json`` child per row, so a harvest row
claiming signal=test lands as taint=trusted_internal / confidence=0.9, the
row's tags (host:...) land verbatim, the row's own source_ref overrides the
caller's uniform ref, and any valid namespace (user:global) is accepted.

- AC5: with --trust untrusted-peer (plus --allow-namespace and
  --attribute-host), rows claiming signal=test land clamped EXACTLY like
  ingest-jsonl peer mode: claimed_signal:test tag present, stored signal
  demoted (not "test"), taint not trusted_internal, confidence <= 0.6, and no
  host:/agent: token from the row survives (the caller's host:<box> is
  stamped instead).
- AC6: rows whose namespaces are outside --allow-namespace are refused with a
  namespace_refused count in the summary, while allow-listed rows still land.

The contract is the CLI surface of the REAL scripts/ingest_harvest.py
subprocess driving the REAL store.py child against a throwaway store pinned
via the canonical env fixture (tests/test_jsonl_sync.py _base_env pattern);
assertions pin STORED sqlite state plus the adapter's printed summary.

RED at base (the adapter rejects the trust flags outright), GREEN after
issue #373.

Run standalone (CI style): python tests/test_t03_harvest_trust.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import test_jsonl_sync as T  # noqa: E402  (canonical ingest harness: _TwoStoreCase + _base_env)

NS = "project:t03/harvest"
CALLER_HOST = "harvest-box-1"
PEER_CONFIDENCE_CEILING = 0.6
HARVEST_PY = T.REPO_ROOT / "scripts" / "ingest_harvest.py"


def _harvest_row(**overrides) -> dict:
    """One well-formed harvest JSON row (the closeout-remote array format:
    REQUIRED_KEYS are namespace/type/content/tags/signal/why), overridable
    field by field."""
    row = {
        "namespace": NS,
        "type": "fact",
        "content": "harvest row content",
        "tags": "",
        "signal": "test",
        "why": "closeout harvested observation",
    }
    row.update(overrides)
    return row


class HarvestPeerTrustTest(T._TwoStoreCase):
    """AC5/AC6 of issue #373: the harvest adapter runs under the SAME peer
    trust boundary as ingest-jsonl."""

    def _harvest(self, rows, *extra):
        """Write `rows` as a harvest JSON array and run the REAL adapter with
        the peer trust flags against store B's pinned subprocess env (the
        adapter's store.py children inherit it)."""
        path = os.path.join(self.b.tmp, "harvest.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(HARVEST_PY), path,
             "--store", str(T.STORE_PY),
             "--trust", "untrusted-peer",
             "--allow-namespace", NS,
             "--attribute-host", CALLER_HOST, *extra],
            capture_output=True, text=True, env=self.b.env,
            cwd=str(T.REPO_ROOT), timeout=300)

    def _stored(self, content):
        return self.b.query_one(
            "SELECT taint, signal, confidence, tags FROM memory "
            "WHERE namespace=? AND content=?", (NS, content))

    def test_peer_trust_flags_clamp_claimed_signal_and_attribution(self):
        """AC5: a harvest row claiming signal=test with forged host: tags
        must land clamped exactly like ingest-jsonl peer mode --
        claimed_signal:test tag present, stored signal demoted (not "test"),
        taint not trusted_internal, confidence <= 0.6, no host:/agent: token
        from the row surviving, and the caller's host:<box> stamped instead.
        RED at base (the adapter has no --trust surface), GREEN after #373."""
        row = _harvest_row(
            content="harvest row claiming a tested signal and host tags",
            signal="test",
            tags="host:harvest-lie,Host:upcase-lie,topic:h1")
        r = self._harvest([row])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        stored = self._stored(row["content"])
        self.assertIsNotNone(stored,
                             "the clamped harvest row must be stored, not "
                             "rejected: " + r.stdout + r.stderr)
        taint, signal, confidence, tags = stored
        self.assertIn("claimed_signal:test", tags,
                      "the claimed signal must be recorded as a "
                      "claimed_signal tag, ingest-jsonl peer parity")
        self.assertNotEqual(signal, "test",
                            "a harvested signal claim must be demoted, not "
                            "stored as the row signal")
        self.assertNotEqual(taint, "trusted_internal",
                            "a harvest row may not land with a trusted taint")
        self.assertLessEqual(
            confidence, PEER_CONFIDENCE_CEILING,
            "harvest peer confidence is clamped to the peer ceiling")
        tokens = [t for t in re.split(r"[,;\s]+", tags or "") if t]
        self.assertIn(f"host:{CALLER_HOST}", tokens,
                      "attribution must come from the caller --attribute-host "
                      "value")
        survivors = [t for t in tokens
                     if t.lower().startswith(("host:", "agent:"))
                     and t != f"host:{CALLER_HOST}"]
        self.assertEqual(
            survivors, [],
            f"host:/agent: tokens from the harvest row survived: "
            f"{survivors!r}")
        self.assertIn("topic:h1", tokens,
                      "non-attribution tags must survive the clamp")

    def test_namespace_allow_list_refuses_out_of_scope_rows(self):
        """AC6: in one harvest run, a row outside --allow-namespace
        (user:global) is refused and counted as namespace_refused in the
        summary while the allow-listed row still lands. RED at base (the
        adapter has no --trust surface), GREEN after #373."""
        allowed = _harvest_row(content="harvest row inside the allow list")
        refused = _harvest_row(
            namespace="user:global",
            content="harvest row outside the allow list")
        r = self._harvest([allowed, refused])
        self.assertEqual(
            r.returncode, 0,
            "a counted namespace refusal is not a run failure (ingest-jsonl "
            "peer-lane parity): " + r.stdout + r.stderr)
        self.assertIn(
            "namespace_refused=1", (r.stdout or "") + (r.stderr or ""),
            "the refused row must be counted as namespace_refused in the "
            f"summary: {r.stdout!r} {r.stderr!r}")
        self.assertIsNotNone(
            self._stored(allowed["content"]),
            "the allow-listed row must still land")
        self.assertEqual(
            self.b.query_one(
                "SELECT COUNT(*) FROM memory WHERE namespace=?",
                ("user:global",))[0], 0,
            "a namespace outside the allow list must be refused, not stored")


if __name__ == "__main__":
    unittest.main()
