"""Episode storage tests (issue #65, 10.7).

Covers:
- Fresh-store creation at v13 with both tables
- v12 → v13 migration from a HAND-PLANTED legacy store (lossless, idempotent)
- episode-open / episode-add / episode-close --summary / episode-list flows
- Refusals: tombstoned member, closed episode re-close, unknown ids
- `episode` is NOT an ALLOWED_TYPES member
- get --json episode linkage
- doctor episode-tables counts
- JSONL round-trip (export → ingest → identical episodes; double ingest is
  a zero-delta no-op; membership with a missing episode is malformed)

Runs standalone: python tests/test_episodes.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from schema_meta import ALLOWED_TYPES, SUPPORTED_SCHEMA_VERSION  # noqa: E402

STORE_PY = str(SCRIPTS / "store.py")
DOCTOR_PY = str(SCRIPTS / "doctor.py")


def _run(args, env=None, timeout=120):
    return subprocess.run(
        [sys.executable, STORE_PY, *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


class EpisodeIsolationTest(unittest.TestCase):
    """Each test gets a fresh isolated store (never the box store)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-episodes-")
        self._saved = {k: os.environ.get(k) for k in ("ZMEM_STORE", "ZMEM_DATA")}
        self.store = os.path.join(self._tmp, "store.sqlite")
        os.environ["ZMEM_STORE"] = self.store
        os.environ["ZMEM_DATA"] = self._tmp
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _init(self):
        r = _run(["init"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def _add(self, content, **kw):
        args = ["add", "--namespace", kw.pop("namespace", "project:eps"),
                "--type", kw.pop("type_", "fact"), "--content", content,
                "--signal", kw.pop("signal", "test"), "--json"]
        r = _run(args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["id"]

    def _schema_version(self):
        conn = sqlite3.connect(self.store)
        try:
            return conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
        finally:
            conn.close()

    # -- schema ---------------------------------------------------------------

    def test_fresh_store_is_v13_with_episode_tables(self):
        self._init()
        self.assertEqual(self._schema_version(), str(SUPPORTED_SCHEMA_VERSION))
        self.assertEqual(SUPPORTED_SCHEMA_VERSION, 13)
        conn = sqlite3.connect(self.store)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("episode", names)
            self.assertIn("episode_memory", names)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(episode)")}
            self.assertEqual(
                cols, {"id", "namespace", "started_at", "ended_at",
                       "summary_memory_id", "token_count"})
        finally:
            conn.close()

    def test_v12_to_v13_migration_lossless_and_idempotent(self):
        # Build a faithful v12 store: initialize with current code (the full
        # real schema), then REMOVE the v13 artifacts and pin the version back
        # — exactly what a pre-upgrade operator store looks like.
        self._init()
        m = self._add("legacy v12 row. one sentence.")
        conn = sqlite3.connect(self.store)
        conn.executescript(
            """
            DROP TABLE IF EXISTS episode_memory;
            DROP TABLE IF EXISTS episode;
            DROP INDEX IF EXISTS idx_episode_ns;
            UPDATE meta SET value='12' WHERE key='schema_version';
            """
        )
        conn.commit()
        conn.close()
        # First writable command migrates (additive: two CREATE TABLE IF NOT
        # EXIST + one index; no memory column changes, no data rewrite).
        r = _run(["stats"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._schema_version(), "13")
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT content FROM memory WHERE id=?", (m,)).fetchone()
            self.assertIn("legacy v12 row", row[0], "legacy data preserved")
            names = {r2[0] for r2 in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("episode", names)
            self.assertIn("episode_memory", names)
        finally:
            conn.close()
        # Idempotent re-run: same version, no error.
        r = _run(["stats"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._schema_version(), "13")

    def test_episode_not_in_allowed_types(self):
        self.assertNotIn("episode", ALLOWED_TYPES)
        # And add refuses it like any other unknown type.
        self._init()
        r = _run(["add", "--namespace", "project:eps", "--type", "episode",
                  "--content", "x"])
        self.assertNotEqual(r.returncode, 0)

    # -- CLI flows ------------------------------------------------------------

    def test_open_add_close_list_flow(self):
        self._init()
        m1 = self._add("episode member one. standalone sentence.")
        m2 = self._add("episode member two. standalone sentence.")
        r = _run(["episode-open", "--namespace", "project:eps", "--json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        ep = json.loads(r.stdout)
        self.assertEqual(ep["namespace"], "project:eps")
        self.assertEqual(ep["ended_at"], "")
        eid = ep["id"]

        r = _run(["episode-add", "--episode", eid, "--memory", m1, "--json"])
        self.assertEqual(json.loads(r.stdout)["added"], True)
        # Idempotent duplicate attach.
        r = _run(["episode-add", "--episode", eid, "--memory", m1, "--json"])
        self.assertEqual(json.loads(r.stdout)["added"], False)

        r = _run(["episode-close", "--episode", eid, "--json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        closed = json.loads(r.stdout)
        self.assertTrue(closed["ended_at"])
        self.assertEqual(closed["member_count"], 1)
        self.assertGreater(closed["token_count"], 0)

        r = _run(["episode-list", "--namespace", "project:eps", "--json"])
        rows = json.loads(r.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], eid)

    def test_close_with_summary_pins_summary_row(self):
        self._init()
        m1 = self._add("summary member one. first sentence.")
        r = _run(["episode-open", "--namespace", "project:eps", "--json"])
        eid = json.loads(r.stdout)["id"]
        _run(["episode-add", "--episode", eid, "--memory", m1])
        r = _run(["episode-close", "--episode", eid, "--summary", "--json"])
        closed = json.loads(r.stdout)
        self.assertTrue(closed["summary_memory_id"])
        # The summary row exists, is a memory row, and links back on get.
        g = _run(["get", "--id", closed["summary_memory_id"]])
        row = json.loads(g.stdout)
        self.assertIn("summary", row["tags"])
        # Extractive: the FIRST sentence of each member content, one bullet.
        self.assertIn("- summary member one.", row["content"])
        # The linkage is episode -> summary (the summary row itself is NOT an
        # episode member), so get on the summary carries an empty episode list
        # while episode-list points back at the summary row.
        self.assertEqual(row["episodes"], [])
        listing = json.loads(
            _run(["episode-list", "--namespace", "project:eps", "--json"]).stdout)
        self.assertEqual(listing[0]["summary_memory_id"],
                         closed["summary_memory_id"])

    def test_get_json_carries_episode_linkage(self):
        self._init()
        m1 = self._add("linkage probe row. one sentence.")
        r = _run(["episode-open", "--namespace", "project:eps", "--json"])
        eid = json.loads(r.stdout)["id"]
        _run(["episode-add", "--episode", eid, "--memory", m1])
        g = _run(["get", "--id", m1])
        row = json.loads(g.stdout)
        self.assertEqual(row["episodes"][0]["id"], eid)
        # A row outside any episode still has the key (empty list).
        m2 = self._add("no episode membership row. one sentence.")
        g2 = _run(["get", "--id", m2])
        self.assertEqual(json.loads(g2.stdout)["episodes"], [])

    def test_refusals(self):
        self._init()
        m1 = self._add("tombstone target row. one sentence.")
        r = _run(["episode-open", "--namespace", "project:eps", "--json"])
        eid = json.loads(r.stdout)["id"]
        _run(["episode-add", "--episode", eid, "--memory", m1])
        _run(["episode-close", "--episode", eid])

        # Closed episode: no more adds, no re-close.
        r = _run(["episode-add", "--episode", eid, "--memory", m1])
        self.assertEqual(r.returncode, 2)
        self.assertIn("already closed", r.stderr)
        r = _run(["episode-close", "--episode", eid])
        self.assertEqual(r.returncode, 2)
        self.assertIn("append-only", r.stderr)

        # Tombstoned memory cannot join a fresh episode.
        _run(["invalidate", "--id", m1, "--reason", "test tombstone"])
        r = _run(["episode-open", "--namespace", "project:eps", "--json"])
        eid2 = json.loads(r.stdout)["id"]
        r = _run(["episode-add", "--episode", eid2, "--memory", m1])
        self.assertEqual(r.returncode, 2)
        self.assertIn("tombstoned", r.stderr)

        # Unknown ids refuse with stable [zmem] lines.
        r = _run(["episode-add", "--episode", "00000000-0000-0000-0000-000000000000",
                  "--memory", m1])
        self.assertEqual(r.returncode, 2)
        r = _run(["episode-close", "--episode",
                  "00000000-0000-0000-0000-000000000000"])
        self.assertEqual(r.returncode, 2)

    def test_token_count_uses_live_members_only(self):
        self._init()
        m1 = self._add("live member row. one sentence.")
        m2 = self._add("to be tombstoned member. one sentence.")
        r = _run(["episode-open", "--namespace", "project:eps", "--json"])
        eid = json.loads(r.stdout)["id"]
        _run(["episode-add", "--episode", eid, "--memory", m1])
        _run(["episode-add", "--episode", eid, "--memory", m2])
        # m2 is superseded (not removed from the episode — memberships are
        # append-only); close must count ONLY the live member.
        _run(["supersede", "--id", m2])
        r = _run(["episode-close", "--episode", eid, "--json"])
        closed = json.loads(r.stdout)
        conn = sqlite3.connect(self.store)
        try:
            live_content = conn.execute(
                "SELECT content FROM memory WHERE id=?", (m1,)).fetchone()[0]
        finally:
            conn.close()
        from storelib.inject import row_token_cost
        self.assertEqual(closed["token_count"],
                         row_token_cost({"content": live_content}))

    # -- doctor ----------------------------------------------------------------

    def test_doctor_reports_episode_counts(self):
        self._init()
        m1 = self._add("doctor episode count row. one sentence.")
        r = _run(["episode-open", "--namespace", "project:eps", "--json"])
        eid = json.loads(r.stdout)["id"]
        _run(["episode-add", "--episode", eid, "--memory", m1])
        _run(["episode-close", "--episode", eid])
        r = _run(["episode-open", "--namespace", "project:eps", "--json"])
        # second open episode stays open
        rep = subprocess.run(
            [sys.executable, DOCTOR_PY, "--format", "json",
             "--repo-root", str(REPO_ROOT), "--project", str(REPO_ROOT)],
            capture_output=True, text=True, timeout=120,
        )
        # doctor exits 1 when ANY check fails (report-with-fails convention);
        # this test only asserts the episode-tables check, so accept both.
        self.assertIn(rep.returncode, (0, 1), rep.stderr)
        report = json.loads(rep.stdout)
        check = next(c for c in report["checks"] if c["id"] == "episode-tables")
        self.assertEqual(check["status"], "pass", check)
        self.assertEqual(check["details"]["episodes_open"], 1)
        self.assertEqual(check["details"]["episodes_closed"], 1)
        self.assertEqual(check["details"]["memberships"], 1)

    # -- JSONL sync ------------------------------------------------------------

    def test_jsonl_round_trip_and_double_ingest_zero_delta(self):
        self._init()
        # TWO lexically distinct members: a single-member summary's first
        # sentence is near-identical to its own member and legitimately
        # dedup-folds into it (no summary row pinned) — the round-trip must
        # exercise the summary actually being created and carried.
        m1 = self._add("roundtrip member one. one sentence.")
        m2 = self._add("roundtrip member two. entirely different wording.")
        r = _run(["episode-open", "--namespace", "project:eps", "--json"])
        eid = json.loads(r.stdout)["id"]
        _run(["episode-add", "--episode", eid, "--memory", m1])
        _run(["episode-add", "--episode", eid, "--memory", m2])
        _run(["episode-close", "--episode", eid, "--summary"])

        export = os.path.join(self._tmp, "export.jsonl")
        r = _run(["export-jsonl", "--out", export])
        self.assertEqual(r.returncode, 0, r.stderr)
        kinds = [json.loads(line).get("kind")
                 for line in open(export, encoding="utf-8") if line.strip()]
        self.assertIn("memory", kinds)
        self.assertIn("episode", kinds)
        self.assertIn("episode_memory", kinds)

        # Ingest into a fresh store.
        other = os.path.join(self._tmp, "store2.sqlite")
        env = {**os.environ, "ZMEM_STORE": other, "ZMEM_DATA": self._tmp}
        r = _run(["init"], env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = _run(["ingest-jsonl", "--in", export], env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("episodes_added=1", r.stdout)
        listing = json.loads(_run(["episode-list", "--json"], env=env).stdout)
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["member_count"], 2)
        self.assertTrue(listing[0]["summary_memory_id"])

        # Re-ingest: exact no-op for episodes.
        r = _run(["ingest-jsonl", "--in", export], env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("episodes_added=0", r.stdout)
        self.assertIn("episodes_skipped=1", r.stdout)
        listing2 = json.loads(_run(["episode-list", "--json"], env=env).stdout)
        self.assertEqual(listing2, listing)

    def test_membership_with_missing_episode_is_malformed(self):
        self._init()
        m1 = self._add("dangling membership member. one sentence.")
        bad = os.path.join(self._tmp, "bad.jsonl")
        with open(bad, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "kind": "episode_memory",
                "episode_id": "22222222-2222-2222-2222-222222222222",
                "memory_id": m1,
                "added_at": "2026-08-27T00:00:00Z",
            }) + "\n")
        r = _run(["ingest-jsonl", "--in", bad])
        self.assertEqual(r.returncode, 0)  # per-row guard: counted, not fatal
        self.assertIn("malformed line 1", r.stderr)
        self.assertIn("episodes_added=0", r.stdout)

    def test_legacy_rows_without_kind_still_ingest(self):
        self._init()
        m1 = self._add("legacy row shape member. one sentence.")
        legacy = os.path.join(self._tmp, "legacy.jsonl")
        g = _run(["get", "--id", m1])
        row = json.loads(g.stdout)
        minimal = {k: row.get(k) for k in (
            "id", "namespace", "type", "content", "tags", "source_ref",
            "confidence", "signal", "valid_from", "valid_until", "update_of",
            "taint", "ingestion_ts")}
        minimal.update({"superseded_at": None, "supersede_reason": "",
                        "merged_from": None, "trust_score": 1.0,
                        "applied_count": 0, "violated_count": 0, "links": []})
        with open(legacy, "w", encoding="utf-8") as f:
            f.write(json.dumps(minimal) + "\n")
        other = os.path.join(self._tmp, "store3.sqlite")
        env = {**os.environ, "ZMEM_STORE": other, "ZMEM_DATA": self._tmp}
        _run(["init"], env=env)
        r = _run(["ingest-jsonl", "--in", legacy], env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("added=1", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
