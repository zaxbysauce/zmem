"""Regression tests for PR #379 review feedback (issue #282).

Non-frozen companion to tests/test_t03_ingest_trust.py / test_t03_peer_kinds.py.
Every case here corresponds to a blocker the swarm-pr-review critic called a
merge blocker, and each is written to survive the revert of its own fix:

- a peer memory_expiry must NOT retire a PRE-EXISTING LOCAL state row
  (authorship, not namespace, is the boundary) — both lanes;
- a peer row must NOT fold into a pre-existing local keeper (the merge
  reads and writes the keeper: max-confidence lift, tag union,
  retrieval_count bump);
- the provenance stamp must not be skippable by a row-declared `peer:`
  prefix;
- replaying a peer file must not re-quarantine an already-stored row
  (the ledger grew 1->2->3 in review's probe);
- superseded peer rows keep the tombstones_refused authority-probe
  signal that docs/CLOUD.md tells operators to watch.

Runs standalone: python tests/test_t03_peer_feedback.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"


class PeerTrustCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-t03-feedback-")
        self.store = os.path.join(self.tmp, "store.sqlite")
        self._saved = {
            k: os.environ.get(k) for k in (
                "ZMEM_STORE", "ZMEM_DATA", "ZMEM_CAPTURE_MODE",
                "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
            )
        }
        os.environ["ZMEM_STORE"] = self.store
        os.environ.pop("ZMEM_DATA", None)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(self.tmp, "no-models")

    def tearDown(self):
        import shutil

        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), *args],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=180)

    def write(self, rows, name="peer.jsonl"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return path

    def row(self, ns, **over):
        r = {
            "id": str(uuid.uuid4()),
            "namespace": ns,
            "type": "fact",
            "content": f"peer row {uuid.uuid4().hex[:8]}",
            "signal": "none",
            "taint": "untrusted_tool",
            "tags": "",
            "source_ref": "peer://fixture",
            "valid_from": "2026-01-01T00:00:00Z",
            "ingestion_ts": "2026-01-01T00:00:00Z",
        }
        r.update(over)
        return r

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.store)
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def ingest(self, rows, *extra, name="peer.jsonl"):
        path = self.write(rows, name=name)
        return self.cli("ingest-jsonl", "--in", path,
                        "--trust", "untrusted-peer",
                        "--allow-namespace", "project:t03/peer", *extra)

    def seed_local_state_row(self, ns="project:t03/peer"):
        r = self.row(ns, type="state", content=f"local state {uuid.uuid4().hex[:8]}")
        path = self.write([r], name="seed.jsonl")
        proc = self.cli("ingest-jsonl", "--in", path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return r["id"]


class PeerCannotRetireLocalRowTest(PeerTrustCase):
    def test_expiry_side_record_cannot_retire_a_local_row(self):
        local = self.seed_local_state_row()
        rec = {
            "kind": "memory_expiry", "memory_id": local,
            "inject_until": "2020-01-01T00:00:00Z", "expiry_class": "setting",
        }
        r = self.ingest([rec])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(
            self.query("SELECT inject_until FROM memory_expiry WHERE memory_id=?",
                       (local,)),
            "a peer must not retire a row it did not author")

    def test_expiry_for_a_peer_authored_row_still_applies(self):
        """The gate is AUTHORSHIP, not a blanket refusal."""
        peer_row = self.row("project:t03/peer", type="state",
                            content=f"peer state {uuid.uuid4().hex[:8]}")
        rec = {
            "kind": "memory_expiry", "memory_id": peer_row["id"],
            "inject_until": "2020-01-01T00:00:00Z", "expiry_class": "setting",
        }
        r = self.ingest([peer_row, rec])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNotNone(
            self.query("SELECT inject_until FROM memory_expiry WHERE memory_id=?",
                       (peer_row["id"],)),
            "a peer may set expiry on the row it authored")


class PeerCannotFoldIntoLocalKeeperTest(PeerTrustCase):
    def test_dedup_fold_does_not_mutate_a_local_row(self):
        local = self.seed_local_state_row("project:t03/peer")
        before = self.query(
            "SELECT confidence, tags, retrieval_count FROM memory WHERE id=?",
            (local,))
        # Same content, so a dedup hit is certain if dedup is not suppressed.
        twin = self.row("project:t03/peer", content=before and "" or "")
        row = dict(local and {} or {})
        twin["id"] = str(uuid.uuid4())
        twin["content"] = self._content_of(local)
        twin["confidence"] = 0.9
        twin["taint"] = "untrusted_tool"
        twin["tags"] = "attacker-tag"
        r = self.ingest([twin])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = self.query(
            "SELECT confidence, tags, retrieval_count FROM memory WHERE id=?",
            (local,))
        self.assertEqual(after, before,
                         "a peer row must not mutate the local keeper it "
                         "duplicates")

    def _content_of(self, mid):
        row = self.query("SELECT content FROM memory WHERE id=?", (mid,))
        return row[0]


class ProvenanceStampTest(PeerTrustCase):
    def test_row_declared_peer_prefix_cannot_forge_the_stamp(self):
        row = self.row("project:t03/peer",
                       source_ref="peer:someone-elses-box",
                       content=f"forge attempt {uuid.uuid4().hex[:8]}")
        r = self.ingest([row], "--attribute-host", "ingest-box-7")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        stored = self.query("SELECT source_ref FROM memory WHERE id=?",
                            (row["id"],))
        self.assertEqual(stored[0], "peer:ingest-box-7", stored)
        self.assertNotIn("someone-elses", stored[0] or "")


class ReplayQuarantineTest(PeerTrustCase):
    def test_replaying_a_stored_row_does_not_grow_the_ledger(self):
        """The row must EXIST locally for this to be a no-op.

        A row that never lands (refused every run) is legitimately
        re-quarantined each time, so seed the id authoritatively with safe
        content first, then replay it as a peer row carrying content the
        capture policy would refuse. Authoritative behavior for a present
        id is `skipped=1` with no ledger write (its local-existence
        short-circuit runs before capture) — peer mode must match.
        """
        safe = self.row("project:t03/peer",
                        content=f"already local {uuid.uuid4().hex[:8]}")
        seed = self.write([safe], name="seed.jsonl")
        proc = self.cli("ingest-jsonl", "--in", seed)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNotNone(
            self.query("SELECT id FROM memory WHERE id=?", (safe["id"],)))
        ledger = self._ledger_lines()
        self.assertEqual(ledger, 0, "the safe seed must not quarantine")
        dangerous = dict(safe)
        dangerous["content"] = (f"peer rewrite {uuid.uuid4().hex[:8]} "
                                f"sudo -S secret")
        second = self.ingest([dangerous], "--capture-mode", "auto",
                             name="b.jsonl")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("quarantined=0", second.stdout, second.stdout)
        self.assertEqual(self._ledger_lines(), ledger,
                         "a no-op re-ingest must not append to the ledger")

    def _ledger_lines(self):
        import glob

        total = 0
        for path in glob.glob(os.path.join(self.tmp, "**", "*.jsonl"),
                              recursive=True):
            if os.path.basename(os.path.dirname(path)) == "quarantine":
                with open(path, encoding="utf-8") as f:
                    total += sum(1 for line in f if line.strip())
        return total


class TombstoneSignalTest(PeerTrustCase):
    def test_superseded_peer_row_keeps_the_authority_probe_signal(self):
        local = self.seed_local_state_row()
        row = self.row("project:t03/peer",
                        superseded_at="2026-02-01T00:00:00Z",
                        content=f"superseded probe {uuid.uuid4().hex[:8]}")
        r = self.ingest([row])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("tombstones_refused=1", r.stdout, r.stdout)
        self.assertEqual(
            self.query("SELECT superseded_at FROM memory WHERE id=?",
                       (local,))[0],
            None,
            "the live local row must still be live (not tombstoned)")
        self.assertIn("tombstone", r.stderr.lower(), r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)