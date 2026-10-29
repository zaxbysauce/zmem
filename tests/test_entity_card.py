"""Tests for issue #179 (Workstream K PR 8): entity_card construction,
refresh, provenance, and entity-lane recall.

Covers the issue's five declared classes:
  - EntityCardBuildTest — the six-mention byte oracle and the four-mention
    threshold (zero cards, zero card provenance rows);
  - EntityCardCliTest — `store.py entity-card build|refresh --json` (exact
    result/key order, exit-2 duplicate build) plus the failure cases: a
    forced link failure leaves the pre-refresh SHA-256 / live card /
    tombstone state / memory_entity links unchanged, and a dry-run cadence
    leaves the SQLite SHA-256 unchanged;
  - EntityCardRefreshTest — the SHA-256 no-op second sweep and the
    new-mention tombstone (non-empty valid_until, replacement update_of);
  - EntityRecallTest — same-fence suppression of the six represented ids,
    namespace / as_of predicates honored, unrelated entity retained, with
    UUID generation patched so the fixture card id is
    00000000-0000-4000-8000-000000000179 (card_recall.jsonl);
  - EntityConfigTest — defaults (5, 8); 0 / -1 / abc rejected before any
    SQLite byte change (exit 2 from the CLI); ZMEM_CARD_TOP_N=2 accepted
    while retaining the six-mention threshold.

Every store test sets ZMEM_STORE / ZMEM_DATA / ZMEM_MODELS_DIR /
ZMEM_MODEL_AUTODOWNLOAD to a scratch directory before storelib loads (the
per-test loader re-derives env state through store.py's
`_refresh_env_state`); the operator's ~/.zmem is never touched. Direct
SQLite test connections execute PRAGMA foreign_keys=ON and assert the
returned value is 1 before reading or writing; production code does not
rely on foreign-key enforcement.

Run: python tests/test_entity_card.py   (no pytest — repo convention)
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "entity_card"

NOW = "2026-09-10T00:00:00Z"
ENTITY_ID = "entity-acme"
CARD_ID = "00000000-0000-4000-8000-000000000179"
BETA_IDS = ("00000000-0000-4000-8000-000000000181",
            "00000000-0000-4000-8000-000000000182")

SIX_RECORDS = [
    json.loads(line) for line in
    (FIXTURES / "six_mentions.jsonl").read_text(encoding="utf-8").splitlines()
]
FOUR_RECORDS = [
    json.loads(line) for line in
    (FIXTURES / "four_mentions.jsonl").read_text(encoding="utf-8").splitlines()
]
EXPECTED_CARD_BYTES = (FIXTURES / "expected_acme_card.txt").read_bytes()

# scripts/ on the path so the in-process helpers can import storelib.*
# directly (the functions under test take the connection explicitly).
sys.path.insert(0, str(SCRIPTS_DIR))


def _fixture_sha(name: str) -> str:
    return hashlib.sha256(
        (FIXTURES / name).read_bytes()).hexdigest()


def _load_store(store_path: Path, data_dir: Path):
    """Load store.py fresh against one scratch store (env-derived paths are
    re-resolved through the shim's `_refresh_env_state`)."""
    env = {
        "ZMEM_STORE": str(store_path),
        "ZMEM_DATA": str(data_dir),
        "ZMEM_MODELS_DIR": str(data_dir / "no-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
    }
    spec = importlib.util.spec_from_file_location(
        f"zmem_store_entity_card_{uuid.uuid4().hex}", STORE_PY)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, env, clear=False):
        spec.loader.exec_module(mod)
    return mod


def _seed(store_path, records, *, with_beta: bool = False) -> None:
    """Seed entity-acme (kind project, canonical name Acme, aliases `acme`
    and `acme corp`) plus the fixture's live mentions and their direct
    memory_entity links in ONE transaction (the c4 loader contract)."""
    conn = sqlite3.connect(store_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        fk = conn.execute("PRAGMA foreign_keys").fetchone()
        assert fk and fk[0] == 1, f"PRAGMA foreign_keys=ON reported {fk!r}"
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO entity (id, kind, canonical_name, created_at, updated_at)"
            " VALUES (?, 'project', 'Acme', ?, ?)",
            (ENTITY_ID, NOW, NOW))
        for alias in ("acme", "acme corp"):
            conn.execute(
                "INSERT INTO entity_alias (entity_id, alias_norm) VALUES (?, ?)",
                (ENTITY_ID, alias))
        for r in records:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, source_ref,"
                " confidence, signal, valid_from, valid_until, update_of,"
                " ingestion_ts, superseded_at, content_norm, taint)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'none', '', '', '', ?, NULL, ?,"
                " 'trusted_internal')",
                (r["id"], r["namespace"], r["type"], r["content"], r["tags"],
                 r["source_ref"], r["confidence"], r["ingestion_ts"],
                 " ".join(r["content"].lower().split())))
            conn.execute(
                "INSERT INTO memory_entity (memory_id, entity_id, role)"
                " VALUES (?, ?, 'mentions')", (r["id"], ENTITY_ID))
        if with_beta:
            # An UNRELATED entity with its own ordinary rows: the same-fence
            # test asserts it retains them.
            conn.execute(
                "INSERT INTO entity (id, kind, canonical_name, created_at, updated_at)"
                " VALUES ('entity-beta', 'project', 'Beta', ?, ?)", (NOW, NOW))
            conn.execute(
                "INSERT INTO entity_alias (entity_id, alias_norm)"
                " VALUES ('entity-beta', 'beta')")
            for i, (bid, ts) in enumerate(zip(
                    BETA_IDS, ("2026-09-10T00:00:01Z", "2026-09-10T00:00:02Z"))):
                conn.execute(
                    "INSERT INTO memory (id, namespace, type, content, tags,"
                    " source_ref, confidence, signal, valid_from, valid_until,"
                    " update_of, ingestion_ts, superseded_at, content_norm, taint)"
                    " VALUES (?, 'project:entity-card', 'fact', ?, '',"
                    " 'fixture:beta:00%d', 0.8, 'none', '', '', '', ?, NULL, ?,"
                    " 'trusted_internal')" % (i + 1),
                    (bid, f"Beta note {i + 1}", ts,
                     f"beta note {i + 1}"))
                conn.execute(
                    "INSERT INTO memory_entity (memory_id, entity_id, role)"
                    " VALUES (?, 'entity-beta', 'mentions')", (bid,))
        conn.commit()
    finally:
        conn.close()


def _cli_env(store_path: Path, data_dir: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "ZMEM_STORE": str(store_path),
        "ZMEM_DATA": str(data_dir),
        "ZMEM_MODELS_DIR": str(data_dir / "no-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
    })
    for interferer in ("ZMEM_BACKUP_DIR", "ZMEM_MMR_LAMBDA", "ZMEM_TEST_NOW",
                       "ZMEM_CARD_MIN_MENTIONS", "ZMEM_CARD_TOP_N"):
        env.pop(interferer, None)
    return env


def _run_cli(store_path: Path, data_dir: Path, *args, env=None):
    return subprocess.run(
        [PYTHON, str(STORE_PY), *args],
        capture_output=True, text=True, timeout=300,
        env=env if env is not None else _cli_env(store_path, data_dir),
        cwd=str(REPO_ROOT),
    )


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _CardStoreTestCase(unittest.TestCase):
    """Fresh scratch store per test; never the operator's ~/.zmem."""

    def make_store(self, prefix="zmem-179-"):
        tmp = Path(tempfile.mkdtemp(prefix=prefix))
        self.addCleanup(shutil.rmtree, tmp, True)
        store = tmp / "store.sqlite"
        mod = _load_store(store, tmp)
        conn = mod.connect()
        mod.init_db(conn)
        mod.migrate(conn)
        self.addCleanup(self._close, conn)
        self.store = store
        self.tmp = tmp
        self.mod = mod
        return mod, conn

    @staticmethod
    def _close(conn):
        try:
            conn.close()
        except Exception:
            pass

    def ro_conn(self):
        conn = sqlite3.connect(self.store)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        fk = conn.execute("PRAGMA foreign_keys").fetchone()
        assert fk and fk[0] == 1, f"PRAGMA foreign_keys=ON reported {fk!r}"
        self.addCleanup(conn.close)
        return conn

    def live_card(self, conn):
        # PR #385 review F-7/PRR-009: the SELECT carries `type` so the
        # card-type assertion actually reads a fetched column (the former
        # helper omitted it and the assertion degenerated into theater).
        return conn.execute(
            "SELECT id, type, content, tags, source_ref, signal, namespace,"
            " update_of, valid_until, superseded_at FROM memory"
            " WHERE type='entity_card' AND superseded_at IS NULL").fetchall()


class EntityCardBuildTest(_CardStoreTestCase):
    def test_six_mentions_match_expected_bytes(self):
        """The six-mention fixture creates exactly one live entity_card row:
        type entity_card, source_ref card:entity-acme, signal none, kind
        project, sorted aliases, the issue's ordered source ids, and content
        byte-equal to expected_acme_card.txt."""
        from storelib.organize import refresh_entity_cards
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)

        counts = refresh_entity_cards(conn, now=NOW)
        self.assertEqual(
            counts,
            {"created": 1, "refreshed": 0, "unchanged": 0,
             "removed": 0, "skipped": 0},
            counts)

        cards = self.live_card(conn)
        self.assertEqual(len(cards), 1, "exactly one live card")
        card = cards[0]
        self.assertEqual(card["type"], "entity_card")
        self.assertEqual(card["source_ref"], f"card:{ENTITY_ID}")
        self.assertEqual(card["signal"], "none")
        self.assertEqual(card["namespace"], "user:global")
        self.assertEqual(card["content"].encode("utf-8"),
                         EXPECTED_CARD_BYTES)
        self.assertEqual(card["tags"], "entity_card_sha256=" + hashlib.sha256(
            EXPECTED_CARD_BYTES).hexdigest())

        # Kind + sorted aliases come from the entity row.
        ent = conn.execute(
            "SELECT kind, canonical_name FROM entity WHERE id=?",
            (ENTITY_ID,)).fetchone()
        self.assertEqual(ent["kind"], "project")
        self.assertEqual(ent["canonical_name"], "Acme")
        self.assertIn("Aliases: acme, acme corp\n", card["content"])

        # Ordered selected source ids: the Sources lines of the card.
        source_lines = [
            line[2:] for line in card["content"].splitlines()
            if line.startswith("- ")
            and line[2:] in {r["id"] for r in SIX_RECORDS}
            and card["content"].splitlines().index(line)
            > card["content"].splitlines().index("Sources:")]
        self.assertEqual(
            source_lines,
            ["00000000-0000-4000-8000-000000000176",
             "00000000-0000-4000-8000-000000000173",
             "00000000-0000-4000-8000-000000000172",
             "00000000-0000-4000-8000-000000000171",
             "00000000-0000-4000-8000-000000000175",
             "00000000-0000-4000-8000-000000000174"])

        # Direct entity link: the card's ONLY link is the entity itself, and
        # the write minted no garbage entities.
        links = conn.execute(
            "SELECT entity_id FROM memory_entity WHERE memory_id=?",
            (card["id"],)).fetchall()
        self.assertEqual([r[0] for r in links], [ENTITY_ID])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0], 1)
        print("expected_acme_card.txt sha256:",
              _fixture_sha("expected_acme_card.txt"))

    def test_four_mentions_create_no_card(self):
        """The four-mention fixture creates zero card rows and zero card
        provenance rows; the four_mentions.expected.json oracle prints its
        SHA-256."""
        from storelib.organize import refresh_entity_cards
        mod, conn = self.make_store()
        _seed(self.store, FOUR_RECORDS)

        counts = refresh_entity_cards(conn, now=NOW)
        self.assertEqual(counts["created"], 0, counts)
        self.assertEqual(counts["skipped"], 1, counts)

        cards = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE type='entity_card'").fetchone()[0]
        provenance = conn.execute(
            "SELECT COUNT(*) FROM memory_entity me JOIN memory m"
            " ON m.id = me.memory_id WHERE m.type='entity_card'").fetchone()[0]
        self.assertEqual(cards, 0)
        self.assertEqual(provenance, 0)
        expected = (FIXTURES / "four_mentions.expected.json").read_bytes()
        self.assertEqual(
            json.dumps({"cards": cards, "provenance": provenance},
                       separators=(",", ":")).encode("utf-8") + b"\n",
            expected)
        print("four_mentions.expected.json sha256:",
              _fixture_sha("four_mentions.expected.json"))
        print("four_mentions.jsonl sha256:",
              _fixture_sha("four_mentions.jsonl"))


class EntityCardCliTest(_CardStoreTestCase):
    def test_build_and_refresh_commands(self):
        """`entity-card build|refresh ENTITY --json`: exact created /
        updated / unchanged / no_card objects with the exact key order
        (result, id, entity_id, source_ref, warnings), exit 2 for a
        duplicate build, id/source_ref null for no_card."""
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)
        conn.close()

        # build -> created
        r = _run_cli(self.store, self.tmp, "entity-card", "build",
                     ENTITY_ID, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        obj = json.loads(r.stdout)
        self.assertEqual(list(obj.keys()),
                         ["result", "id", "entity_id", "source_ref",
                          "warnings"])
        self.assertEqual(obj["result"], "created")
        self.assertEqual(obj["entity_id"], ENTITY_ID)
        self.assertEqual(obj["source_ref"], f"card:{ENTITY_ID}")
        self.assertIsInstance(obj["id"], str)
        self.assertTrue(obj["id"])
        self.assertEqual(obj["warnings"], [])
        card_id = obj["id"]

        # duplicate build -> exit 2
        r2 = _run_cli(self.store, self.tmp, "entity-card", "build",
                      ENTITY_ID, "--json")
        self.assertEqual(r2.returncode, 2, r2.stdout)
        self.assertIn("entity-card", r2.stderr)

        # refresh (nothing changed) -> unchanged
        r3 = _run_cli(self.store, self.tmp, "entity-card", "refresh",
                      ENTITY_ID, "--json")
        self.assertEqual(r3.returncode, 0, r3.stderr)
        obj3 = json.loads(r3.stdout)
        self.assertEqual(obj3["result"], "unchanged")
        self.assertEqual(obj3["id"], card_id)

        # One new fixed-timestamp mention -> refresh -> updated
        conn = self.ro_conn()
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, source_ref,"
            " confidence, signal, valid_from, valid_until, update_of,"
            " ingestion_ts, superseded_at, content_norm, taint)"
            " VALUES (?, 'project:entity-card', 'fact', 'Acme deployment 7', '',"
            " 'fixture:acme:007', 0.99, 'none', '', '', '',"
            " '2026-09-10T00:00:07Z', NULL, 'acme deployment 7',"
            " 'trusted_internal')",
            ("00000000-0000-4000-8000-000000000177",))
        conn.execute(
            "INSERT INTO memory_entity (memory_id, entity_id, role)"
            " VALUES (?, ?, 'mentions')",
            ("00000000-0000-4000-8000-000000000177", ENTITY_ID))
        conn.commit()
        conn.close()

        r4 = _run_cli(self.store, self.tmp, "entity-card", "refresh",
                      ENTITY_ID, "--json")
        self.assertEqual(r4.returncode, 0, r4.stderr)
        obj4 = json.loads(r4.stdout)
        self.assertEqual(obj4["result"], "updated")
        self.assertNotEqual(obj4["id"], card_id)

        # refresh again -> unchanged
        r5 = _run_cli(self.store, self.tmp, "entity-card", "refresh",
                      ENTITY_ID, "--json")
        self.assertEqual(json.loads(r5.stdout)["result"], "unchanged")

        # Below threshold: tombstone three mentions, refresh -> no_card with
        # null id/source_ref and the live card tombstoned; then build also
        # reports no_card with no write.
        conn = self.ro_conn()
        conn.executemany(
            "UPDATE memory SET superseded_at='2026-09-10T04:00:00Z',"
            " valid_until='2026-09-10T04:00:00Z' WHERE id=?",
            [("00000000-0000-4000-8000-000000000175",),
             ("00000000-0000-4000-8000-000000000176",),
             ("00000000-0000-4000-8000-000000000177",)])
        conn.commit()
        conn.close()

        r6 = _run_cli(self.store, self.tmp, "entity-card", "refresh",
                      ENTITY_ID, "--json")
        self.assertEqual(r6.returncode, 0, r6.stderr)
        obj6 = json.loads(r6.stdout)
        self.assertEqual(obj6["result"], "no_card")
        self.assertIsNone(obj6["id"])
        self.assertIsNone(obj6["source_ref"])
        self.assertEqual(obj6["entity_id"], ENTITY_ID)

        conn = self.ro_conn()
        live = self.live_card(conn)
        self.assertEqual(len(live), 0, "card tombstoned below threshold")
        conn.close()

        r7 = _run_cli(self.store, self.tmp, "entity-card", "build",
                      ENTITY_ID, "--json")
        self.assertEqual(r7.returncode, 0, r7.stderr)
        self.assertEqual(json.loads(r7.stdout)["result"], "no_card")

        # Unresolvable entity -> exit 2.
        r8 = _run_cli(self.store, self.tmp, "entity-card", "build",
                      "no-such-entity", "--json")
        self.assertEqual(r8.returncode, 2, r8.stdout)

    def test_forced_link_failure_is_atomic(self):
        """A forced direct-link failure rolls back the WHOLE refresh: the
        pre-refresh SQLite SHA-256, live card id, tombstone state, and
        memory_entity link set are unchanged."""
        from storelib.entity import refresh_entity_card
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)

        class _FailingCardLinkConn(sqlite3.Connection):
            fail_card_link = False

            def execute(self, sql, params=()):
                if (self.fail_card_link
                        and "INSERT OR IGNORE INTO memory_entity" in sql
                        and isinstance(params, (tuple, list))
                        and len(params) >= 2
                        and params[1] == ENTITY_ID):
                    raise sqlite3.IntegrityError("forced card link failure")
                return super().execute(sql, params)

        # First sweep creates the live card.
        from storelib.organize import refresh_entity_cards
        counts = refresh_entity_cards(conn, now=NOW)
        self.assertEqual(counts["created"], 1, counts)
        card = self.live_card(conn)[0]
        conn.close()

        # A new mention would force a replacement refresh. The pre-refresh
        # snapshot (SHA, links, card rows) is taken AFTER this insert.
        seed_conn = self.ro_conn()
        seed_conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, source_ref,"
            " confidence, signal, valid_from, valid_until, update_of,"
            " ingestion_ts, superseded_at, content_norm, taint)"
            " VALUES (?, 'project:entity-card', 'fact', 'Acme deployment 7', '',"
            " 'fixture:acme:007', 0.99, 'none', '', '', '',"
            " '2026-09-10T00:00:07Z', NULL, 'acme deployment 7',"
            " 'trusted_internal')",
            ("00000000-0000-4000-8000-000000000177",))
        seed_conn.execute(
            "INSERT INTO memory_entity (memory_id, entity_id, role)"
            " VALUES (?, ?, 'mentions')",
            ("00000000-0000-4000-8000-000000000177", ENTITY_ID))
        seed_conn.commit()
        seed_conn.close()

        sha_before = _file_sha(self.store)
        snap = self.ro_conn()
        links_before = snap.execute(
            "SELECT memory_id, entity_id FROM memory_entity"
            " ORDER BY memory_id, entity_id").fetchall()
        cards_before = snap.execute(
            "SELECT id, superseded_at, valid_until, update_of FROM memory"
            " WHERE type='entity_card' ORDER BY id").fetchall()
        snap.close()

        failing = sqlite3.connect(self.store,
                                  factory=_FailingCardLinkConn)
        failing.row_factory = sqlite3.Row
        failing.fail_card_link = True
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                refresh_entity_card(
                    failing, entity_id=ENTITY_ID,
                    content="Entity: Acme\nKind: project\n...",
                    now=NOW)
        finally:
            failing.close()

        self.assertEqual(_file_sha(self.store), sha_before,
                         "a failed card refresh must not change store bytes")
        after = self.ro_conn()
        links_after = after.execute(
            "SELECT memory_id, entity_id FROM memory_entity"
            " ORDER BY memory_id, entity_id").fetchall()
        cards_after = after.execute(
            "SELECT id, superseded_at, valid_until, update_of FROM memory"
            " WHERE type='entity_card' ORDER BY id").fetchall()
        self.assertEqual(links_after, links_before,
                         "memory_entity links unchanged")
        self.assertEqual(cards_after, cards_before,
                         "no new card, no tombstone, no lineage change")
        live = self.live_card(after)
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["id"], card["id"])
        after.close()

    def test_dry_run_cadence_sha_unchanged(self):
        """A dry-run cadence computes the same card counts read-only: the
        SQLite SHA-256 is unchanged."""
        from storelib.organize import organize
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)
        conn.close()

        sha_before = _file_sha(self.store)
        conn = mod.connect()
        report = organize(conn, dry_run=True, force=True)
        conn.close()
        self.assertEqual(_file_sha(self.store), sha_before,
                         "dry-run cadence must not change store bytes")
        cards = report["entity_cards"]
        self.assertEqual(
            tuple(cards.keys()),
            ("created", "refreshed", "unchanged", "removed", "skipped"))
        self.assertEqual(cards["created"], 1, cards)
        self.assertEqual(cards["skipped"], 0, cards)


class EntityCardRefreshTest(_CardStoreTestCase):
    def test_unchanged_refresh_is_sha_noop(self):
        """A second sweep with unchanged mentions reports unchanged=1 and
        leaves the SQLite file SHA-256 byte-identical."""
        from storelib.organize import refresh_entity_cards
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)
        first = refresh_entity_cards(conn, now=NOW)
        self.assertEqual(first["created"], 1, first)
        conn.close()

        sha_before = _file_sha(self.store)

        conn2 = sqlite3.connect(self.store)
        conn2.row_factory = sqlite3.Row
        try:
            second = refresh_entity_cards(
                conn2, now="2026-09-10T12:00:00Z")
        finally:
            conn2.close()

        self.assertEqual(second["unchanged"], 1, second)
        self.assertEqual(second["created"], 0, second)
        self.assertEqual(_file_sha(self.store), sha_before,
                         "an unchanged refresh performs no SQL write")

    def test_new_mention_tombstones_previous_card(self):
        """One fixed-timestamp mention creates a replacement: a new live
        card id, the prior row carries a non-empty valid_until and is not
        live, and the replacement's update_of equals the prior id."""
        from storelib.organize import refresh_entity_cards
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)
        refresh_entity_cards(conn, now=NOW)
        old = self.live_card(conn)[0]

        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, source_ref,"
            " confidence, signal, valid_from, valid_until, update_of,"
            " ingestion_ts, superseded_at, content_norm, taint)"
            " VALUES (?, 'project:entity-card', 'fact', 'Acme deployment 7', '',"
            " 'fixture:acme:007', 0.99, 'none', '', '', '',"
            " '2026-09-10T00:00:07Z', NULL, 'acme deployment 7',"
            " 'trusted_internal')",
            ("00000000-0000-4000-8000-000000000177",))
        conn.execute(
            "INSERT INTO memory_entity (memory_id, entity_id, role)"
            " VALUES (?, ?, 'mentions')",
            ("00000000-0000-4000-8000-000000000177", ENTITY_ID))
        conn.commit()

        counts = refresh_entity_cards(conn, now="2026-09-10T01:00:00Z")
        self.assertEqual(counts["refreshed"], 1, counts)

        live = self.live_card(conn)
        self.assertEqual(len(live), 1)
        new = live[0]
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(new["update_of"], old["id"])

        prior = conn.execute(
            "SELECT superseded_at, valid_until FROM memory WHERE id=?",
            (old["id"],)).fetchone()
        self.assertIsNotNone(prior["superseded_at"])
        self.assertTrue(prior["valid_until"],
                        "the tombstoned card keeps a non-empty valid_until")

        # The new card's first mention is the highest-scoring new row.
        self.assertIn(
            "- score=0.990000 last_seen=2026-09-10T00:00:07Z"
            " id=00000000-0000-4000-8000-000000000177"
            " source_ref=fixture:acme:007 text=Acme deployment 7",
            new["content"])


class EntityRecallTest(_CardStoreTestCase):
    def test_card_suppresses_six_sources_in_same_fence(self):
        """Query `Acme` with the global tier included: the fixture card id
        (...179, via the card_recall.jsonl pair) is present, all six
        represented ids are absent from the same fence, namespace and as_of
        predicates are honored, and an unrelated entity retains its ordinary
        rows."""
        from storelib.organize import refresh_entity_cards
        from storelib.recall import recall_memory
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS, with_beta=True)

        with mock.patch("storelib.write.uuid.uuid4",
                        return_value=uuid.UUID(CARD_ID)):
            counts = refresh_entity_cards(conn, now=NOW)
        self.assertEqual(counts["created"], 1, counts)
        card = self.live_card(conn)[0]
        self.assertEqual(card["id"], CARD_ID,
                         "UUID patch must mint the fixture card id")

        # The card_recall pair stays byte-identical to the issue contract.
        pair_a = (FIXTURES / "card_recall.jsonl").read_bytes()
        pair_b = (FIXTURES / "card_recall.expected.json").read_bytes()
        self.assertEqual(pair_a, pair_b)
        record = json.loads(pair_a.decode("utf-8"))
        self.assertEqual(record["expected_card_id"], CARD_ID)
        print("card_recall.jsonl sha256:", _fixture_sha("card_recall.jsonl"))
        print("card_recall.expected.json sha256:",
              _fixture_sha("card_recall.expected.json"))

        # Same fence: card present, six represented ids suppressed.
        rows = recall_memory(conn, query=record["query"],
                             namespace=record["namespace"],
                             include_global=record["include_global"],
                             limit=5)
        ids = [r["id"] for r in rows]
        self.assertIn(CARD_ID, ids, ids)
        for sid in record["suppressed_ids"]:
            self.assertNotIn(sid, ids,
                             f"represented source {sid} must be suppressed")

        # Namespace predicate: a project-only recall selects no card and
        # applies no suppression.
        rows_ns = recall_memory(conn, query="Acme",
                                namespace=record["namespace"], limit=5)
        ids_ns = [r["id"] for r in rows_ns]
        self.assertNotIn(CARD_ID, ids_ns, ids_ns)
        self.assertTrue(
            set(record["suppressed_ids"]) & set(ids_ns),
            "project-only recall keeps its ordinary source rows")

        # as_of predicate: before the card was born, the card is absent and
        # the sources are not suppressed.
        rows_asof = recall_memory(conn, query="Acme",
                                  namespace=record["namespace"],
                                  include_global=True, limit=5,
                                  as_of="2026-09-09T23:59:59Z")
        ids_asof = [r["id"] for r in rows_asof]
        self.assertNotIn(CARD_ID, ids_asof, ids_asof)
        self.assertTrue(
            set(record["suppressed_ids"]) & set(ids_asof),
            "an as_of fence without the card keeps the source rows")

        # An unrelated entity retains its ordinary rows.
        rows_beta = recall_memory(conn, query="Beta",
                                  namespace=record["namespace"],
                                  include_global=True, limit=5)
        ids_beta = [r["id"] for r in rows_beta]
        self.assertIn(BETA_IDS[0], ids_beta, ids_beta)
        self.assertIn(BETA_IDS[1], ids_beta, ids_beta)

        # The final row keeps the existing entities metadata shape.
        for r in rows:
            self.assertIsInstance(r.get("entities"), list)

    def test_unscoped_recall_suppresses_sources_too(self):
        """Review r1 regression: namespace=None — the DEFAULT
        `store.py recall --query` surface — renders ONE all-namespace
        fence (the project-tier pool covers every namespace and the
        entity arm promotes the card without an ns clause), so the card
        is present and its six represented sources are suppressed; the
        explain pipeline's unscoped presentation mirrors the fence."""
        from storelib.organize import refresh_entity_cards
        from storelib.recall import explain_recall, recall_memory
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS, with_beta=True)
        with mock.patch("storelib.write.uuid.uuid4",
                        return_value=uuid.UUID(CARD_ID)):
            counts = refresh_entity_cards(conn, now=NOW)
        self.assertEqual(counts["created"], 1, counts)
        record = json.loads(
            (FIXTURES / "card_recall.jsonl").read_bytes().decode("utf-8"))

        rows = recall_memory(conn, query=record["query"], limit=10)
        ids = [r["id"] for r in rows]
        self.assertIn(CARD_ID, ids, ids)
        for sid in record["suppressed_ids"]:
            self.assertNotIn(
                sid, ids, f"unscoped fence must suppress source {sid}")

        explained = explain_recall(conn, query=record["query"], limit=10)
        explained_ids = [r.get("id") for r in explained]
        self.assertIn(CARD_ID, explained_ids, explained_ids)
        for sid in record["suppressed_ids"]:
            self.assertNotIn(
                sid, explained_ids,
                f"unscoped explain fence must suppress source {sid}")


class EntityConfigTest(_CardStoreTestCase):
    ENV_VARS = ("ZMEM_CARD_MIN_MENTIONS", "ZMEM_CARD_TOP_N")
    BAD_VALUES = ("0", "-1", "abc")

    def test_defaults_are_five_and_eight(self):
        from storelib.entity import card_config_from_env
        for v in self.ENV_VARS:
            os.environ.pop(v, None)
        self.assertEqual(card_config_from_env(), (5, 8))

    def test_invalid_values_raise_before_any_write(self):
        """0 / -1 / abc for each variable raise ValueError (naming the
        variable and bound) before any SQLite byte changes; the CLI exits 2
        with the store bytes unchanged.

        The store is SETTLED first (one idempotent `init` run) because every
        store.py open pays `_prepare_store`'s physical WAL normalization
        once after a foreign direct-SQLite write — the failing invocations
        must then leave the bytes untouched."""
        from storelib.entity import card_config_from_env
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)
        conn.close()
        settle = _run_cli(self.store, self.tmp, "init")
        self.assertEqual(settle.returncode, 0, settle.stderr)
        sha_before = _file_sha(self.store)

        for var in self.ENV_VARS:
            for value in self.BAD_VALUES:
                with mock.patch.dict(os.environ, {var: value}):
                    with self.assertRaises(ValueError) as ctx:
                        card_config_from_env()
                    self.assertIn(var, str(ctx.exception))
                    self.assertIn(">= 1", str(ctx.exception))
                env = _cli_env(self.store, self.tmp)
                env[var] = value
                r = _run_cli(self.store, self.tmp, "entity-card", "build",
                             ENTITY_ID, "--json", env=env)
                self.assertEqual(r.returncode, 2, (var, value, r.stdout))
                self.assertEqual(_file_sha(self.store), sha_before,
                                 f"{var}={value} must fail before any write")

    def test_top_n_two_accepted_with_threshold_retained(self):
        """ZMEM_CARD_TOP_N=2 is accepted: the six-mention entity still
        qualifies (threshold stays 5) and the card carries exactly two
        mention lines and two source lines."""
        from storelib.entity import card_config_from_env
        from storelib.organize import refresh_entity_cards
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)

        with mock.patch.dict(os.environ, {"ZMEM_CARD_TOP_N": "2"}):
            min_mentions, top_n = card_config_from_env()
            self.assertEqual((min_mentions, top_n), (5, 2))
            counts = refresh_entity_cards(
                conn, min_mentions=min_mentions, top_n=top_n, now=NOW)
        self.assertEqual(counts["created"], 1, counts)

        card = self.live_card(conn)[0]
        lines = card["content"].splitlines()
        mentions = [l for l in lines
                    if l.startswith("- score=")]
        sources_at = lines.index("Sources:")
        source_lines = lines[sources_at + 1:]
        self.assertEqual(len(mentions), 2, card["content"])
        self.assertEqual(len(source_lines), 2, card["content"])
        # The two selected rows are the confidence leaders (...176 then
        # ...173 per the fixture's ordering).
        self.assertIn("00000000-0000-4000-8000-000000000176", mentions[0])
        self.assertIn("00000000-0000-4000-8000-000000000173", mentions[1])
        # Last seen still spans EVERY live source row, not only the top 2.
        self.assertIn("Last seen: 2026-09-10T00:00:06Z", lines)


def _seed_qualifying_beta(conn, count=5, first_ts_hour=0):
    """Seed entity-beta with `count` qualifying live mentions (>= 5 by
    default) directly, for sweep-isolation tests that need a SECOND
    qualifying entity ordered after entity-acme."""
    for i in range(count):
        mid = f"00000000-0000-4000-8000-{i + 1:012d}"
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags,"
            " source_ref, confidence, signal, valid_from, valid_until,"
            " update_of, ingestion_ts, superseded_at, content_norm, taint)"
            " VALUES (?, 'project:entity-card', 'fact', ?, '',"
            " 'fixture:beta:qual%d', 0.9, 'none', '', '', '', ?, NULL, ?,"
            " 'trusted_internal')" % (i + 1),
            (mid, f"Beta qualifying note {i + 1}",
             f"2026-09-10T0{first_ts_hour}:00:{i + 1:02d}Z",
             f"beta qualifying note {i + 1}"))
        conn.execute(
            "INSERT INTO memory_entity (memory_id, entity_id, role)"
            " VALUES (?, 'entity-beta', 'mentions')", (mid,))
    conn.commit()


class EntityCardWriterGuardTest(_CardStoreTestCase):
    """PR #385 review F-2/F-7 (PRR-001/PRR-009): behavioral pins for the two
    writer guards on BOTH write paths — add_memory and update_memory."""

    def test_add_memory_refuses_card_type_without_force_append(self):
        from storelib.write import add_memory
        mod, conn = self.make_store()
        with self.assertRaises(ValueError) as ctx:
            add_memory(conn, namespace="user:global", type_="entity_card",
                       content="forged card", signal="none")
        self.assertIn("card-refresh-only", str(ctx.exception))
        rows = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE type='entity_card'"
        ).fetchone()[0]
        self.assertEqual(rows, 0, "a refused add must write nothing")

    def test_add_memory_refuses_watermark_tag(self):
        from storelib.write import add_memory
        mod, conn = self.make_store()
        with self.assertRaises(ValueError) as ctx:
            add_memory(conn, namespace="project:guard", type_="fact",
                       content="ordinary", signal="none",
                       tags="entity_card_sha256=" + "0" * 64)
        self.assertIn("reserved", str(ctx.exception))

    def test_add_memory_refuses_card_source_ref(self):
        from storelib.write import add_memory
        mod, conn = self.make_store()
        with self.assertRaises(ValueError) as ctx:
            add_memory(conn, namespace="project:guard", type_="fact",
                       content="ordinary", signal="none",
                       source_ref="card:entity-acme")
        self.assertIn("card:", str(ctx.exception))
        rows = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE source_ref LIKE 'card:%'"
        ).fetchone()[0]
        self.assertEqual(rows, 0)

    def test_update_refuses_type_conversion_into_card(self):
        from storelib.write import add_memory, update_memory
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)
        res = add_memory(conn, namespace="project:guard", type_="fact",
                         content="an ordinary row", signal="none", now=NOW)
        with self.assertRaises(ValueError) as ctx:
            update_memory(conn, mid=str(res), content="forged card",
                          type_="entity_card", now=NOW)
        self.assertIn("card-refresh-only", str(ctx.exception))
        after = conn.execute(
            "SELECT type FROM memory WHERE id=?", (str(res),)).fetchone()
        self.assertEqual(after["type"], "fact",
                         "a refused update must not mutate the old row")

    def test_update_refuses_watermark_tag(self):
        from storelib.write import add_memory, update_memory
        mod, conn = self.make_store()
        res = add_memory(conn, namespace="project:guard", type_="fact",
                         content="an ordinary row", signal="none", now=NOW)
        with self.assertRaises(ValueError) as ctx:
            update_memory(conn, mid=str(res), content="edited",
                          tags="entity_card_sha256=" + "1" * 64, now=NOW)
        self.assertIn("reserved", str(ctx.exception))

    def test_update_refuses_card_source_ref_claim(self):
        from storelib.write import add_memory, update_memory
        mod, conn = self.make_store()
        res = add_memory(conn, namespace="project:guard", type_="fact",
                         content="an ordinary row", signal="none", now=NOW)
        with self.assertRaises(ValueError) as ctx:
            update_memory(conn, mid=str(res), content="edited",
                          source_ref="card:someone-else", now=NOW)
        self.assertIn("card:", str(ctx.exception))

    def test_content_only_card_edit_still_allowed(self):
        """The state precedent: updating a PRE-EXISTING card row's content
        (no type/tag/source_ref override) inherits the card identity and
        succeeds; the transient watermark desync self-heals at the next
        refresh, which recomputes the digest over the new bytes."""
        from storelib.organize import refresh_entity_cards
        from storelib.write import update_memory
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)
        refresh_entity_cards(conn, now=NOW)
        card = self.live_card(conn)[0]
        result, created_new = update_memory(
            conn, mid=card["id"], content=card["content"] + "\nhand edit",
            now=NOW)
        self.assertTrue(created_new)
        rows = conn.execute(
            "SELECT id, type, source_ref, superseded_at FROM memory"
            " WHERE id=?", (str(result),)).fetchone()
        self.assertEqual(rows["type"], "entity_card")
        self.assertEqual(rows["source_ref"], f"card:{ENTITY_ID}")
        self.assertIsNone(rows["superseded_at"])
        # The replaced card refreshes cleanly afterwards (self-heal).
        counts = refresh_entity_cards(conn, now="2026-09-10T02:00:00Z")
        self.assertEqual(counts["refreshed"], 1, counts)


class EntityCardSweepTest(_CardStoreTestCase):
    """PR #385 review F-4/F-7c (PRR-003/PRR-009): the cadence sweep's
    removed count and its per-entity isolation."""

    def test_sweep_removes_below_threshold_card(self):
        """Build a card on six mentions, supersede enough mentions that the
        entity falls below five live, re-run the sweep: removed==1 and the
        old card is tombstoned with a non-empty valid_until."""
        from storelib.organize import refresh_entity_cards
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)
        first = refresh_entity_cards(conn, now=NOW)
        self.assertEqual(first["created"], 1, first)
        old = self.live_card(conn)[0]

        conn.executemany(
            "UPDATE memory SET superseded_at='2026-09-10T04:00:00Z',"
            " valid_until='2026-09-10T04:00:00Z' WHERE id=?",
            [("00000000-0000-4000-8000-000000000171",),
             ("00000000-0000-4000-8000-000000000172",)])
        conn.commit()

        second = refresh_entity_cards(conn, now="2026-09-10T05:00:00Z")
        self.assertEqual(second["removed"], 1, second)
        self.assertEqual(second["created"], 0, second)
        tomb = conn.execute(
            "SELECT superseded_at, valid_until FROM memory WHERE id=?",
            (old["id"],)).fetchone()
        self.assertIsNotNone(tomb["superseded_at"])
        self.assertTrue(tomb["valid_until"])
        self.assertEqual(len(self.live_card(conn)), 0)

    def test_oversized_card_entity_does_not_abort_sweep(self):
        """An entity whose card would exceed MAX_CONTENT_CHARS (a >64KiB
        mention) is skipped with one error line; a LATER qualifying entity
        still gets its card (PRR-003: ContentTooLarge aborted the whole
        sweep at base)."""
        from schema_meta import MAX_CONTENT_CHARS
        from storelib.organize import refresh_entity_cards
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS, with_beta=True)
        _seed_qualifying_beta(conn)
        # One acme mention grows past the content cap; the rendered card
        # embeds its single-lined text, so the card itself overflows.
        conn.execute(
            "UPDATE memory SET content=? WHERE id=?",
            ("Acme deployment 4 " + "x" * MAX_CONTENT_CHARS,
             "00000000-0000-4000-8000-000000000174"))
        conn.commit()

        with mock.patch("sys.stderr", new_callable=lambda: __import__(
                "io").StringIO()) as err:
            counts = refresh_entity_cards(conn, now=NOW)
        self.assertEqual(counts["skipped"], 1, counts)
        self.assertEqual(
            counts["created"], 1,
            f"the later qualifying entity still gets its card: {counts}")
        self.assertIn("entity-acme", err.getvalue())
        self.assertIn("content is", err.getvalue())
        # Exactly one live card: beta's. Acme wrote no card rows at all.
        cards = self.live_card(conn)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["source_ref"], "card:entity-beta")
        acme_cards = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE type='entity_card'"
            " AND source_ref='card:entity-acme'").fetchone()[0]
        self.assertEqual(acme_cards, 0)

    def test_invalid_card_env_does_not_abort_organize(self):
        """ZMEM_CARD_MIN_MENTIONS=abc skips the card stage (one stderr line,
        zero counts) and every later organize stage still runs (PRR-003:
        the config error aborted organize after backfill writes at base)."""
        from storelib.organize import organize
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)
        with mock.patch.dict(os.environ, {"ZMEM_CARD_MIN_MENTIONS": "abc"}):
            with mock.patch("sys.stderr", new_callable=lambda: __import__(
                    "io").StringIO()) as err:
                report = organize(conn, force=True)
        self.assertEqual(
            report["entity_cards"],
            {"created": 0, "refreshed": 0, "unchanged": 0, "removed": 0,
             "skipped": 0},
            report["entity_cards"])
        self.assertIn("entity cards skipped", err.getvalue())
        self.assertIn("ZMEM_CARD_MIN_MENTIONS", err.getvalue())
        # Later stages ran: consolidation and topic clustering executed.
        self.assertIsNotNone(report["consolidate"], report)
        self.assertIsInstance(report["topics"], list, report)
        cards = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE type='entity_card'"
        ).fetchone()[0]
        self.assertEqual(cards, 0, "an invalid config writes no cards")


class EntityCardRecallGuardTest(_CardStoreTestCase):
    """PR #385 review F-5/F-7d/F-8 (PRR-005/PRR-008/PRR-009): the recent
    lane never renders cards, suppressed rows never re-enter through link
    expansion, the scoped suppression sites, and the card taint floor."""

    def test_recent_path_returns_zero_card_rows(self):
        """The passive recent pull on a card-bearing store renders ZERO
        entity_card rows and suppresses nothing — the sources stay (cards
        serve the entity arm only; the query-less branch that rendered
        card+sources together is the contract break this closes)."""
        from storelib.organize import refresh_entity_cards
        from storelib.recall import recent_memory
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS, with_beta=True)
        counts = refresh_entity_cards(conn, now=NOW)
        self.assertEqual(counts["created"], 1, counts)

        for kwargs in (
            {"namespace": "project:entity-card", "include_global": True},
            {"namespace": "project:entity-card", "include_global": False},
            {},  # the unscoped default surface
        ):
            rows = recent_memory(conn, limit=10, **kwargs)
            types = [r["type"] for r in rows]
            self.assertNotIn("entity_card", types, (kwargs, types))
        # With no card rendered, nothing was suppressed: the passive pull
        # keeps its ordinary source rows.
        rows = recent_memory(conn, namespace="project:entity-card",
                             limit=10)
        ids = {r["id"] for r in rows}
        self.assertTrue(
            ids & {r["id"] for r in SIX_RECORDS},
            "the recent lane keeps the ordinary (non-card) source rows")

    def test_suppressed_source_does_not_reenter_via_link_expansion(self):
        """A suppressed source row linked to a SURVIVING row must not
        re-enter the fence as a 1-hop expansion neighbour (the F-5 re-entry
        hole): seed a `related` edge between the CARD (the row the fence
        renders in place of its sources) and one suppressed acme source,
        query the fixture's proven `Acme` query, and assert the source
        stays absent while the card renders."""
        from storelib.organize import refresh_entity_cards
        from storelib.recall import recall_memory
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS, with_beta=True)
        refresh_entity_cards(conn, now=NOW)
        card_id = self.live_card(conn)[0]["id"]
        suppressed = "00000000-0000-4000-8000-000000000171"
        conn.execute(
            "INSERT INTO memory_link (src_id, dst_id, relation, score,"
            " created_at) VALUES (?, ?, 'related', 0.9, ?)",
            (card_id, suppressed, NOW))
        conn.commit()

        rows = recall_memory(conn, query="Acme",
                             namespace="project:entity-card",
                             include_global=True, limit=5,
                             link_hops=1, link_budget=3)
        ids = [r["id"] for r in rows]
        self.assertIn(card_id, ids, ids)
        self.assertNotIn(suppressed, ids,
                         "a suppressed source must not re-enter via the "
                         "card's own link edge")
        for r in SIX_RECORDS:
            self.assertNotIn(r["id"], ids,
                             f"represented source {r['id']} stays suppressed")

    def test_scoped_recall_suppression_both_ways(self):
        """The scoped five-tier surface: with the global tier included the
        card renders and its sources are suppressed; scoped WITHOUT the
        global tier no card is selected and the sources stay."""
        from storelib.organize import refresh_entity_cards
        from storelib.recall import recall_memory
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)
        refresh_entity_cards(conn, now=NOW)
        scopes = {"project": "project:entity-card"}

        with_global = recall_memory(
            conn, query="Acme", scopes=scopes, include_global=True, limit=5)
        ids = [r["id"] for r in with_global]
        self.assertTrue(
            any(r["type"] == "entity_card" for r in with_global), ids)
        for r in SIX_RECORDS:
            self.assertNotIn(r["id"], ids,
                             f"scoped+global suppresses source {r['id']}")

        without_global = recall_memory(
            conn, query="Acme", scopes=scopes, include_global=False,
            limit=5)
        ids2 = [r["id"] for r in without_global]
        self.assertFalse(
            any(r["type"] == "entity_card" for r in without_global), ids2)
        self.assertTrue(
            set(ids2) & {r["id"] for r in SIX_RECORDS},
            "scoped without the global tier keeps its source rows")

    def test_untrusted_web_source_makes_card_untrusted(self):
        """F-8: one untrusted_web mention makes the CARD untrusted_web, and
        the passive no_bump fence then omits the card AND its sources
        consistently (suppression ran at the merge; the omit filter drops
        the untrusted card after it)."""
        from storelib.organize import refresh_entity_cards
        from storelib.recall import recall_memory
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS, with_beta=True)
        conn.execute(
            "UPDATE memory SET taint='untrusted_web' WHERE id=?",
            ("00000000-0000-4000-8000-000000000173",))
        conn.commit()
        counts = refresh_entity_cards(conn, now=NOW)
        self.assertEqual(counts["created"], 1, counts)
        card = self.live_card(conn)[0]
        self.assertEqual(card["type"], "entity_card")
        taint = conn.execute(
            "SELECT taint FROM memory WHERE id=?", (card["id"],)
        ).fetchone()["taint"]
        self.assertEqual(taint, "untrusted_web")

        passive = recall_memory(
            conn, query="Acme", namespace="project:entity-card",
            include_global=True, limit=5, no_bump=True)
        passive_ids = [r["id"] for r in passive]
        self.assertNotIn(card["id"], passive_ids,
                         "the untrusted card is filtered on the passive lane")
        for r in SIX_RECORDS:
            self.assertNotIn(r["id"], passive_ids,
                             "the represented sources stay suppressed")


class EntityCardTocTouTest(_CardStoreTestCase):
    """PR #385 review F-6/PRR-004: the post-lock re-read closes the
    plan-to-apply window — a card committed between the pre-lock read and
    the BEGIN IMMEDIATE is superseded, never duplicated."""

    def test_post_lock_reread_supersedes_interleaved_card(self):
        from storelib.entity import (
            build_entity_card_text, live_card_row, refresh_entity_card)
        mod, conn = self.make_store()
        _seed(self.store, SIX_RECORDS)
        # v1 via the real builder (entity module, not the store shim).
        content_a, _src_a, taint_a = build_entity_card_text(
            conn, entity_id=ENTITY_ID, min_mentions=5, top_n=8)
        self.assertIsNotNone(content_a)
        refresh_entity_card(
            conn, entity_id=ENTITY_ID, content=content_a, taint=taint_a,
            now=NOW)
        v1 = live_card_row(conn, ENTITY_ID)["id"]

        # New mention -> new card bytes. The pre-lock read is forced STALE
        # (first call returns None, simulating a plan snapshot taken before
        # a concurrent writer committed v1); every later call reads real.
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags,"
            " source_ref, confidence, signal, valid_from, valid_until,"
            " update_of, ingestion_ts, superseded_at, content_norm, taint)"
            " VALUES (?, 'project:entity-card', 'fact', 'Acme deployment 7',"
            " '', 'fixture:acme:007', 0.99, 'none', '', '', '',"
            " '2026-09-10T00:00:07Z', NULL, 'acme deployment 7',"
            " 'trusted_internal')",
            ("00000000-0000-4000-8000-000000000177",))
        conn.execute(
            "INSERT INTO memory_entity (memory_id, entity_id, role)"
            " VALUES (?, ?, 'mentions')",
            ("00000000-0000-4000-8000-000000000177", ENTITY_ID))
        conn.commit()
        content_b, _src_b, taint_b = build_entity_card_text(
            conn, entity_id=ENTITY_ID, min_mentions=5, top_n=8)
        self.assertNotEqual(content_a, content_b)

        calls = {"n": 0}
        real = live_card_row

        def stale_then_real(c, eid):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # the stale pre-plan view
            return real(c, eid)

        with mock.patch("storelib.entity.live_card_row",
                        new=stale_then_real):
            new_id, changed = refresh_entity_card(
                conn, entity_id=ENTITY_ID, content=content_b,
                taint=taint_b, now="2026-09-10T01:00:00Z")
        self.assertTrue(changed)
        self.assertGreaterEqual(calls["n"], 2,
                                "the post-lock re-read must have run")
        live = conn.execute(
            "SELECT id, update_of FROM memory WHERE type='entity_card'"
            " AND source_ref=? AND superseded_at IS NULL",
            (f"card:{ENTITY_ID}",)).fetchall()
        self.assertEqual(len(live), 1,
                         "exactly ONE live card after the interleaved write")
        self.assertEqual(live[0]["id"], new_id)
        self.assertEqual(live[0]["update_of"], v1,
                         "the post-lock decision superseded v1")
        tomb = conn.execute(
            "SELECT superseded_at, valid_until FROM memory WHERE id=?",
            (v1,)).fetchone()
        self.assertIsNotNone(tomb["superseded_at"])
        self.assertTrue(tomb["valid_until"])


class EntityCardCliRefusalTest(_CardStoreTestCase):
    """PR #385 review F-7e: the behavioral CLI pin for the add refusal."""

    def test_add_type_entity_card_exits_2(self):
        mod, conn = self.make_store()
        r = _run_cli(self.store, self.tmp, "add", "--namespace",
                     "project:guard", "--type", "entity_card",
                     "--content", "forged", "--json")
        self.assertEqual(r.returncode, 2, (r.stdout, r.stderr))
        self.assertIn("entity_card", r.stderr)
        rows = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE type='entity_card'"
        ).fetchone()[0]
        self.assertEqual(rows, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
