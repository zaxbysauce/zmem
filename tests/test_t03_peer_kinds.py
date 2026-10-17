"""Peer ingest record-kind and strict-lane coverage (issue #282).

Non-frozen companion to tests/test_t03_ingest_trust.py (the frozen file's
bytes are checkpoint-pinned; additive hardening lives here, per the
issue-tracer additive-hardening rule). Covers the record kinds and lanes the
plan critic's rounds mandated: episode namespace gating, side-record gating
by resolved target namespaces, evidence refusal, link-carriage hardening,
strict-lane fatal semantics, and the authoritative-combination floor.

Runs standalone: python tests/test_t03_peer_kinds.py
RED at base (flags absent), GREEN after issue #282.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import test_jsonl_sync as T  # noqa: E402

NS = "project:peer/kinds"
NS_B = "project:peer/other"


def _episode_row(namespace=NS):
    return {
        "kind": "episode",
        "id": str(uuid.uuid4()),
        "namespace": namespace,
        "started_at": "2026-10-17T00:00:00Z",
        "ended_at": "",
        "summary_memory_id": "",
        "token_count": 10,
    }


class PeerKindsTest(T._TwoStoreCase):

    def _ingest(self, rows, *extra):
        path = os.path.join(self.b.tmp, "peer.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return self.b.run("ingest-jsonl", "--in", path,
                          "--trust", "untrusted-peer",
                          "--allow-namespace", NS, *extra)

    def _episode_count(self, eid):
        return self.b.query_one(
            "SELECT COUNT(*) FROM episode WHERE id=?", (eid,))

    def test_episode_namespace_gated(self):
        eid_ok = str(uuid.uuid4())
        eid_bad = str(uuid.uuid4())
        r = self._ingest([
            _episode_row(),
            {**_episode_row(NS_B), "id": eid_bad},
        ])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("namespace_refused=1", r.stdout, r.stdout)
        self.assertEqual(
            self._episode_count(eid_bad), (0,),
            "episode outside the allow-list must not land")

    def test_expiry_side_record_gated_by_target(self):
        # Seed a live row OUTSIDE the allow-list, then aim a memory_expiry
        # record at it from a peer file whose allowed namespace differs.
        rid_foreign = self.a_add(NS_B, type_="state")
        rec = {
            "kind": "memory_expiry",
            "memory_id": rid_foreign,
            "inject_until": "2027-01-01T00:00:00Z",
            "expiry_class": "setting",
        }
        r = self._ingest([rec])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("namespace_refused=1", r.stdout, r.stdout)
        self.assertIsNone(
            self.b.query_one(
                "SELECT inject_until FROM memory_expiry WHERE memory_id=?",
                (rid_foreign,)),
            "expiry aimed at a refused-namespace row must not apply")

    def test_refused_row_links_dropped(self):
        # A row whose namespace is refused carries a _links carriage naming
        # an allowed row: the edge must die with the row.
        target = self.a_add(NS)
        row = T._sync_row(id=str(uuid.uuid4()),
                          content="refused row with hostile link",
                          tags="host:peer,topic:x")
        row["links"] = [{
            "dst": target, "relation": "related", "score": 0.9,
            "created_at": "2026-10-17T00:00:00Z",
        }]
        row["namespace"] = NS_B  # outside the allow-list
        r = self._ingest([row])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("namespace_refused=1", r.stdout, r.stdout)
        self.assertIsNone(
            self.b.query_one(
                "SELECT score FROM memory_link WHERE src_id=?", (row["id"],)),
            "a refused row's link carriage must be dropped")

    def test_link_dst_namespace_gated(self):
        # An ALLOWED row may not declare an edge into a refused namespace.
        foreign = self.a_add(NS_B)
        row = T._sync_row(id=str(uuid.uuid4()), content="allowed row with foreign edge")
        row["namespace"] = NS
        row["links"] = [{
            "dst": foreign, "relation": "related", "score": 0.5,
            "created_at": "2026-10-17T00:00:00Z",
        }]
        r = self._ingest([row])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("links_skipped=1", r.stdout, r.stdout)
        self.assertIsNone(
            self.b.query_one(
                "SELECT score FROM memory_link WHERE dst_id=?", (foreign,)))

    def test_link_carriage_id_collision_dropped(self):
        # Plan F14's named pin (final-critic F6): a peer row whose id
        # COLLIDES with an existing local row resolves "skipped" — its
        # _links carriage must not graft edges onto the local victim.
        victim = self.a_add(NS)
        row = T._sync_row(id=victim, content="collision row with links",
                          namespace=NS)
        row["links"] = [{
            "dst": victim, "relation": "related", "score": 0.9,
            "created_at": "2026-10-17T00:00:00Z",
        }]
        r = self._ingest([row])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("skipped=1", r.stdout, r.stdout)
        self.assertIsNone(
            self.b.query_one(
                "SELECT score FROM memory_link WHERE src_id=?", (victim,)),
            "an id-collision row must not graft edges onto the victim")

    def test_strict_link_score_clamped(self):
        # Final-critic F1 pin: the STRICT lane clamps edge scores too.
        target = self.a_add(NS)
        row = T._sync_row(id=str(uuid.uuid4()), content="strict edge row",
                          namespace=NS)
        row["links"] = [{
            "dst": target, "relation": "related", "score": 0.95,
            "created_at": "2026-10-17T00:00:00Z",
        }]
        r = self._ingest([row], "--strict")
        self.assertEqual(r.returncode, 0, r.stderr)
        score = self.b.query_one(
            "SELECT score FROM memory_link WHERE dst_id=?", (target,))
        self.assertIsNotNone(score, "the edge lands")
        self.assertLessEqual(score[0], 0.6 + 1e-9, score)

    def test_peer_update_of_foreign_target_cleared(self):
        # Final-critic F2 pin (plan F22): a lineage pointer to a row OUTSIDE
        # the allow-list is a claim, not authority — cleared on ingest.
        foreign = self.a_add(NS_B)
        row = T._sync_row(id=str(uuid.uuid4()),
                          content="row claiming foreign lineage", namespace=NS)
        row["update_of"] = foreign
        r = self._ingest([row])
        self.assertEqual(r.returncode, 0, r.stderr)
        stored = self.b.query_one(
            "SELECT update_of FROM memory WHERE id=?", (row["id"],))
        self.assertEqual(stored[0] or "", "",
                         "the foreign lineage pointer must be cleared")

    def test_link_score_clamped(self):
        target = self.a_add(NS)
        row = T._sync_row(id=str(uuid.uuid4()), content="allowed row with high-score edge")
        row["namespace"] = NS
        row["links"] = [{
            "dst": target, "relation": "related", "score": 0.95,
            "created_at": "2026-10-17T00:00:00Z",
        }]
        r = self._ingest([row])
        self.assertEqual(r.returncode, 0, r.stderr)
        score = self.b.query_one(
            "SELECT score FROM memory_link WHERE dst_id=?", (target,))
        self.assertIsNotNone(score, "the edge itself lands")
        self.assertLessEqual(score[0], 0.6 + 1e-9, score)

    def _valid_evidence_row(self):
        # A staging-valid evidence record: allowed lane, correct sha256
        # content hash (review round 1 F3: the previous fixture died in
        # BASE validation, never reaching the peer gate).
        ev = {
            "table": "evidence",
            "id": str(uuid.uuid4()),
            "session_id": "s1", "lane": "claude", "moment": "user_prompt",
            "kind": "edit", "ts": "2026-10-17T00:00:00Z",
            "excerpt": "peer excerpt", "ref_path": "x", "ref_offset": 0,
        }
        ev["hash"] = hashlib.sha256(
            f"{ev['kind']}|{ev['ts']}|{ev['excerpt']}".encode()).hexdigest()
        return ev

    def test_evidence_table_rows_refused(self):
        # table-format files force the strict lane (with or without
        # --strict); in peer mode evidence records are refused by the F13
        # gate with its stable string.
        mem = T._sync_row(id=str(uuid.uuid4()),
                          content="evidence carrier row", namespace=NS)
        ev = self._valid_evidence_row()
        for extra in ([], ["--strict"]):
            with self.subTest(extra=extra):
                r = self._ingest([mem, ev], *extra)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(
                    "peer files may not carry",
                    (r.stderr or "") + (r.stdout or ""))

    def test_membership_existing_foreign_episode_strict(self):
        # Review round 1 F2: the strict lane's membership gate must resolve
        # the EPISODE endpoint against the episode table — an existing
        # foreign-namespace episode must not accept memberships.
        ep = _episode_row(NS_B)
        # Seed the foreign episode AUTHORITATIVELY (no --trust): the pin
        # needs an EXISTING foreign episode, which the peer lane would
        # refuse (review round 2 F6: peer-lane seeding left the episode
        # absent and the test passed via the base unknown-episode check).
        seed_path = os.path.join(self.b.tmp, "seed_ep.jsonl")
        with open(seed_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(ep) + chr(10))
        r = self.b.run("ingest-jsonl", "--in", seed_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        mem = T._sync_row(id=str(uuid.uuid4()),
                          content="membership carrier", namespace=NS)
        m = {"kind": "episode_memory", "episode_id": ep["id"],
             "memory_id": mem["id"], "added_at": "2026-10-17T00:00:00Z"}
        r = self._ingest([mem, m], "--strict")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(
            "episode_memory endpoint",
            (r.stderr or "") + (r.stdout or ""),
            "the refusal must come from the namespace gate, not the "
            "unknown-episode check")
        self.assertIsNone(
            self.b.query_one(
                "SELECT * FROM episode_memory WHERE episode_id=?",
                (ep["id"],)),
            "membership of a foreign-namespace episode must not land")

    def test_strict_peer_refusal_is_fatal(self):
        row = T._sync_row(id=str(uuid.uuid4()), content="strict peer refusal row")
        row["namespace"] = NS_B
        r = self._ingest([row], "--strict")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIsNone(
            self.b.query_one(
                "SELECT id FROM memory WHERE id=?", (row["id"],)),
            "strict refusal rolls the whole import back")

    def test_authoritative_strict_skipped_row_links_kept(self):
        # Final-critic F9 pin: AUTHORITATIVE --strict keeps the collect-on-
        # every-outcome contract — a skipped (pre-existing) row's new edge
        # still lands.
        target = self.a_add(NS)
        existing = T._sync_row(id=str(uuid.uuid4()),
                               content="pre-existing strict row", namespace=NS)
        r = self._ingest_auth([existing])
        self.assertEqual(r.returncode, 0, r.stderr)
        collided = dict(existing)
        collided["links"] = [{
            "dst": target, "relation": "related", "score": 0.5,
            "created_at": "2026-10-17T00:00:00Z",
        }]
        r = self._ingest_auth([collided], "--strict")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(
            self.b.query_one(
                "SELECT score FROM memory_link WHERE dst_id=?", (target,)),
            "authoritative strict keeps skipped rows' link carriages")

    def test_peer_episode_summary_foreign_target_cleared(self):
        # Final-critic F10 pin (plan F22): the LEGACY peer lane clears an
        # episode summary pointer aimed outside the allow-list.
        foreign = self.a_add(NS_B)
        ep = _episode_row(NS)
        ep["summary_memory_id"] = foreign
        r = self._ingest([ep])
        self.assertEqual(r.returncode, 0, r.stderr)
        stored = self.b.query_one(
            "SELECT summary_memory_id FROM episode WHERE id=?", (ep["id"],))
        self.assertIsNotNone(stored, "the episode itself lands")
        self.assertEqual(stored[0] or "", "",
                         "the foreign summary pointer must be cleared")

    def _ingest_auth(self, rows, *extra):
        path = os.path.join(self.b.tmp, "auth_kinds.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + chr(10))
        return self.b.run("ingest-jsonl", "--in", path, *extra)

    def test_authoritative_strict_purged_row_links_dropped(self):
        # Final-critic round-3 pin: a PURGED row's carriage is dropped in
        # every mode (issue #255 contract) — authoritative strict keeps
        # every-outcome-EXCEPT-purged, so the import must still succeed.
        target = self.a_add(NS)
        purged = T._sync_row(id=str(uuid.uuid4()),
                             content="row destined for purge", namespace=NS)
        r = self._ingest_auth([purged])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.b.run("purge", "--id", purged["id"])
        r = self.b.run("get", "--id", purged["id"])
        self.assertNotEqual(r.returncode, 0, "row must be purged first")
        resurrected = dict(purged)
        resurrected["links"] = [{
            "dst": target, "relation": "related", "score": 0.5,
            "created_at": "2026-10-17T00:00:00Z",
        }]
        r = self._ingest_auth([resurrected], "--strict")
        self.assertEqual(
            r.returncode, 0,
            "a purged row's carriage must not fail the import: "
            + (r.stdout or "") + (r.stderr or ""))

    def test_authoritative_tombstones_still_legal(self):
        # F3 floor: the DEFAULT mode spelled out must accept the flag combo.
        row = T._sync_row(content="authoritative tombstone legal row")
        path = os.path.join(self.b.tmp, "auth.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        r = self.b.run("ingest-jsonl", "--in", path,
                       "--trust", "authoritative", "--allow-tombstones")
        self.assertEqual(r.returncode, 0, r.stderr)

    def a_add(self, ns, type_="fact"):
        row = T._sync_row(id=str(uuid.uuid4()), type=type_,
                          content=f"local target {uuid.uuid4().hex[:8]}")
        row["namespace"] = ns
        path = os.path.join(self.b.tmp, "seed.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        return row["id"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
