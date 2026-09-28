"""Issue #255 (Q01) acceptance checks: the `purge` subcommand.

Frozen acceptance contract for hard-deletion of one memory id across every
surface that can still name it or quote its bytes:

  AC1 PurgeContentTest      - the memory row AND its FTS index entry vanish,
                              neighbours untouched.
  AC2 PurgeLineageTest      - purging a row that `update` created also removes
                              the tombstoned predecessor it replaced.
  AC3 PurgeBytesTest        - no needle bytes (marker + fake token) remain in
                              store.sqlite or its WAL sidecar.
  AC4 PurgeSideTablesTest   - no side-table row names the purged id (links,
                              entities, episodes, evidence joins, belief-head
                              sources), while unrelated links survive.
  AC5 PurgeEvidenceTest     - evidence referenced ONLY by the purged id goes
                              with it; evidence shared with a survivor stays.
  AC6 PurgeDerivedCopiesTest- derived copies (consolidate keeper merge,
                              two-source belief head, extractive episode
                              summary) are rewritten, deleted, or the purge is
                              REFUSED naming the derived row — never left
                              quoting the purged content.
  AC7 PurgeLedgerTest       - per-session delivery ledgers under
                              <ZMEM_DATA>/ops are scrubbed of the purged row
                              while other delivered ids stay recorded.
  AC8 PurgeDenyListTest     - ingest-jsonl of a pre-purge export does NOT
                              resurrect the purged id (deny list).
  AC9 PurgeBackupScrubTest  - --scrub-backups rewrites backup snapshots
                              (including a prerestore-* copy) free of needle
                              bytes, keeping them valid sqlite with the
                              non-purged rows intact.

At the base (purge not yet implemented) argparse rejects the subcommand, so
every NEW-SURFACE check fails at the purge step with
`invalid choice: 'purge'` (exit 2) — except PurgeDerivedCopiesTest, whose
contract branch asserts that refusal shape is NOT the "invalid choice" one.

Isolation fixture (tests/test_zero_write_passive.py / test_jsonl_sync.py
pattern): throwaway ZMEM_STORE/ZMEM_DATA per test via tempfile.mkdtemp with
explicit cleanup (never a context-managed TemporaryDirectory — open sqlite
handles are a Windows PermissionError hazard), manual capture mode, the
embedding model pinned absent, and ambient ZMEM_* knobs stripped so no
exported variable leaks into a subprocess. storelib is NEVER imported
in-process here; the one place its code is needed (delivery-ledger reads)
runs in a child interpreter.

Run: python tests/test_q01_purge.py   (no pytest)
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable
NS = "project:q01purge"

# The fake token shared with tests/test_redaction.py:31 — never a credential.
FAKE = "ghp_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
NEEDLE = "zebraquux"

TARGET_CONTENT = f"deploy token {FAKE} {NEEDLE} marker"
OTHER_CONTENT = "unrelated flange calibration lesson kept"

# Child-interpreter snippet: read the delivery ledger exactly the way the
# hooks' store does — storelib.delivery_ledger imported from the repo's
# scripts dir in a fresh process (the parent never imports storelib).
_LEDGER_CHILD = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from storelib.delivery_ledger import delivered_ids\n"
    "for _id in delivered_ids(sys.argv[2], sys.argv[3]):\n"
    "    print(_id)\n"
)


class _PurgeBase(unittest.TestCase):
    """One throwaway store + its pinned subprocess env (model absent)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-q01-purge-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.data_dir = self.tmp
        # Fresh env per test derived from os.environ with pinned overrides —
        # never rely on ambient exported ZMEM_* values.
        self.env = {**os.environ}
        self.env["ZMEM_STORE"] = self.store
        self.env["ZMEM_DATA"] = self.data_dir
        self.env["ZMEM_MODELS_DIR"] = os.path.join(self.tmp, "no-such-models")
        self.env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        self.env["ZMEM_CAPTURE_MODE"] = "manual"
        for var in (
            "ZMEM_TEST_NOW", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
            "ZMEM_BACKUP_DIR", "ZMEM_BACKUP_INTERVAL_DAYS",
            "ZMEM_CROSS_PROJECT", "ZMEM_CROSS_PROJECT_HAZARD_VERBS",
            "ZMEM_AUTO_REKEY", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
            "ZMEM_QUERY_CONTEXT", "ZMEM_CROSS_ENCODER",
        ):
            self.env.pop(var, None)

    # -- subprocess surfaces -------------------------------------------------

    def _run(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=120,
            cwd=str(REPO_ROOT),
        )

    def add_row(self, content, ns=NS, *, tags="", source_ref=""):
        """Seed through the REAL CLI add --json; return the new row id.

        add --json prints a pure-JSON envelope on stdout (human progress goes
        to stderr): {"id": ..., "result": "stored"|"deduped", "warnings": ...}.
        """
        args = ["add", "--namespace", ns, "--type", "lesson",
                "--content", content, "--signal", "test",
                "--confidence", "0.9", "--json"]
        if tags:
            args += ["--tags", tags]
        if source_ref:
            args += ["--source-ref", source_ref]
        r = self._run(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["id"]

    def _purge(self, *ids, extra=()):
        """`store.py purge --id <id> ...` — one --id flag per id, in ONE
        invocation, plus optional extra flags per test. Returns the
        CompletedProcess; asserts NOTHING about the exit code (callers
        decide what success/failure means for their surface)."""
        args = ["purge"]
        for mid in ids:
            args += ["--id", mid]
        args += list(extra)
        return self._run(*args)

    # -- sqlite surfaces -----------------------------------------------------

    def qone(self, sql, params=()):
        """Scalar read against ZMEM_STORE via a read-only handle, closed
        promptly (Windows open-handle hazard)."""
        conn = sqlite3.connect(
            "file:" + self.store.replace(os.sep, "/") + "?mode=ro", uri=True)
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def qall(self, sql, params=()):
        conn = sqlite3.connect(
            "file:" + self.store.replace(os.sep, "/") + "?mode=ro", uri=True)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _exec(self, *statements):
        """Direct-SQL seeds for the side-table shapes the real writers build.
        One rw connection: execute all, commit, close (Windows)."""
        conn = sqlite3.connect(self.store)
        try:
            for sql, params in statements:
                conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    # -- needle / ledger helpers ----------------------------------------------

    @staticmethod
    def _needle_count(paths, needles=(NEEDLE, FAKE.lower())):
        """Case-insensitive combined occurrence count of the needles across
        the given files' raw bytes (missing files contribute 0)."""
        total = 0
        for p in paths:
            if p and os.path.exists(p):
                data = Path(p).read_bytes().lower()
                total += sum(data.count(n.encode("utf-8")) for n in needles)
        return total

    def _delivered_ids(self, session_id):
        """delivered_ids(<data_dir>, <session>) computed in a CHILD process
        that imports storelib.delivery_ledger from the repo's scripts dir —
        the same module the hooks' store uses to read the ledger."""
        r = subprocess.run(
            [PYTHON, "-c", _LEDGER_CHILD, str(SCRIPTS_DIR), self.data_dir,
             session_id],
            env=self.env, capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return [line for line in r.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# AC1: the row and only the row (memory table + FTS index)
# ---------------------------------------------------------------------------
class PurgeContentTest(_PurgeBase):
    def test_purge_removes_target_and_only_target(self):
        target = self.add_row(TARGET_CONTENT)
        other = self.add_row(OTHER_CONTENT)
        # Precondition: exactly one FTS-indexed row carries the needle.
        self.assertEqual(
            self.qone("SELECT COUNT(*) FROM memory_fts "
                      "WHERE memory_fts MATCH 'zebraquux'"), 1)

        r = self._purge(target)
        self.assertEqual(r.returncode, 0, r.stderr)

        observed = (
            self.qone("SELECT COUNT(*) FROM memory WHERE id=?", (target,)),
            self.qone("SELECT COUNT(*) FROM memory_fts "
                      "WHERE memory_fts MATCH 'zebraquux'"),
            self.qone("SELECT content FROM memory WHERE id=?", (other,))
            == OTHER_CONTENT,
            self.qone("SELECT COUNT(*) FROM memory_fts "
                      "WHERE memory_fts MATCH 'flange'"),
        )
        self.assertEqual(observed, (0, 0, True, 1),
                         "purge must remove the target row AND its FTS entry "
                         "while the other row and its FTS entry survive "
                         "byte-identical")


# ---------------------------------------------------------------------------
# AC2: update lineage — purging the successor also removes the predecessor
# ---------------------------------------------------------------------------
class PurgeLineageTest(_PurgeBase):
    def test_purge_removes_update_of_predecessor(self):
        target = self.add_row(TARGET_CONTENT)
        r = self._run("update", "--id", target, "--content",
                      "replacement deploy token note zebraquux", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        new_id = json.loads(r.stdout)["id"]
        # Precondition: the successor really replaced the target.
        self.assertEqual(
            self.qone("SELECT update_of FROM memory WHERE id=?", (new_id,)),
            target)

        r = self._purge(new_id)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            self.qone("SELECT COUNT(*) FROM memory WHERE id IN (?, ?)",
                      (new_id, target)), 0,
            "purging the update successor must also remove the tombstoned "
            "predecessor it quotes — the secret lives in both contents")


# ---------------------------------------------------------------------------
# AC3: no needle bytes left in the store file or its WAL
# ---------------------------------------------------------------------------
class PurgeBytesTest(_PurgeBase):
    def test_no_needle_bytes_left_in_store_or_wal(self):
        target = self.add_row(TARGET_CONTENT)
        wal = self.store + "-wal"
        needles = [NEEDLE, FAKE.lower()]
        self.assertGreater(
            self._needle_count([self.store, wal], needles), 0,
            "precondition: the seeded target must put needle bytes in the "
            "store (or its WAL)")

        r = self._purge(target)
        self.assertEqual(r.returncode, 0, r.stderr)

        self.assertEqual(
            self._needle_count([self.store, wal], needles), 0,
            "needle bytes survived purge somewhere in store.sqlite or "
            "store.sqlite-wal (freelist pages, WAL frames, FTS shadow...)")


# ---------------------------------------------------------------------------
# AC4: every side table that names the id
# ---------------------------------------------------------------------------
class PurgeSideTablesTest(_PurgeBase):
    def test_purge_leaves_no_side_table_row_naming_the_id(self):
        target = self.add_row(TARGET_CONTENT)
        other = self.add_row(OTHER_CONTENT)
        third = self.add_row("third neutral row about torque tables")
        now = "2026-09-28T00:00:00Z"
        self._exec(
            # symmetric `related` pair, both directions naming the target
            ("INSERT INTO memory_link (src_id, dst_id, relation, score, "
             "created_at) VALUES (?, ?, 'related', 0.9, ?)",
             (target, other, now)),
            ("INSERT INTO memory_link (src_id, dst_id, relation, score, "
             "created_at) VALUES (?, ?, 'related', 0.9, ?)",
             (other, target, now)),
            # entity graph the target mentions (canonical name carries the
            # needle, so the entity must not be left orphaned-but-named)
            ("INSERT INTO entity (id, kind, canonical_name, created_at, "
             "updated_at) VALUES "
             "('ent-q01', 'other', 'Zebraquux Relay', ?, ?)", (now, now)),
            ("INSERT INTO entity_alias (entity_id, alias_norm) "
             "VALUES ('ent-q01', 'zebraquux relay')", ()),
            ("INSERT INTO memory_entity (memory_id, entity_id, role) "
             "VALUES (?, 'ent-q01', 'mentions')", (target,)),
            # episode summarized by the target + episode membership
            ("INSERT INTO episode (id, namespace, started_at, ended_at, "
             "summary_memory_id, token_count) VALUES "
             "('ep-q01', ?, ?, '', ?, 12)", (NS, now, target)),
            ("INSERT INTO episode_memory (episode_id, memory_id) "
             "VALUES ('ep-q01', ?)", (target,)),
            # evidence join naming the target
            ("INSERT INTO memory_evidence (memory_id, evidence_id) "
             "VALUES (?, 'ev-q01')", (target,)),
            # belief head quoting the target content + its source/evidence
            ("INSERT INTO belief_head (id, namespace, topic_identity, "
             "content, head_state, head_source_id, support_count, "
             "refresh_watermark, generator_revision) VALUES "
             "('bh-q01', ?, 'q01-topic', ?, 'active', ?, 1, ?, 'test')",
             (NS, TARGET_CONTENT, target, now)),
            ("INSERT INTO belief_head_source (head_id, source_id, role, "
             "source_ingestion_ts, source_checksum) VALUES "
             "('bh-q01', ?, 'support', ?, 'ck-target')", (target, now)),
            ("INSERT INTO belief_head_evidence (head_id, source_id, "
             "evidence_id) VALUES ('bh-q01', ?, 'ev-q01')", (target,)),
            # negative control: a link between two survivors stays
            ("INSERT INTO memory_link (src_id, dst_id, relation, score, "
             "created_at) VALUES (?, ?, 'related', 0.9, ?)",
             (other, third, now)),
        )

        r = self._purge(target)
        self.assertEqual(r.returncode, 0, r.stderr)

        sidecount = (
            self.qone("SELECT COUNT(*) FROM memory_link "
                      "WHERE src_id=? OR dst_id=?", (target, target))
            + self.qone("SELECT COUNT(*) FROM memory_entity "
                        "WHERE memory_id=?", (target,))
            + self.qone("SELECT COUNT(*) FROM episode_memory "
                        "WHERE memory_id=?", (target,))
            + self.qone("SELECT COUNT(*) FROM memory_evidence "
                        "WHERE memory_id=?", (target,))
            + self.qone("SELECT COUNT(*) FROM belief_head_source "
                        "WHERE source_id=?", (target,))
            + self.qone("SELECT COUNT(*) FROM belief_head_evidence "
                        "WHERE source_id=?", (target,))
            + self.qone("SELECT COUNT(*) FROM episode "
                        "WHERE summary_memory_id=?", (target,))
            + self.qone("SELECT COUNT(*) FROM entity WHERE id='ent-q01'")
            + self.qone("SELECT COUNT(*) FROM entity_alias "
                        "WHERE entity_id='ent-q01'")
        )
        otherlink = self.qone(
            "SELECT COUNT(*) FROM memory_link WHERE src_id=? AND dst_id=?",
            (other, third))
        self.assertEqual(
            (sidecount, otherlink), (0, 1),
            "every side-table row naming the purged id must go with it; "
            "the survivor-to-survivor link must survive")


# ---------------------------------------------------------------------------
# AC5: evidence lifecycle — only- referenced goes, shared stays
# ---------------------------------------------------------------------------
class PurgeEvidenceTest(_PurgeBase):
    def test_purge_removes_evidence_only_the_id_references(self):
        target = self.add_row(TARGET_CONTENT)
        other = self.add_row(OTHER_CONTENT)
        now = "2026-09-28T00:00:00Z"
        self._exec(
            # evidence only the target references (needle in the excerpt)
            ("INSERT INTO evidence (id, session_id, lane, moment, kind, ts, "
             "hash, excerpt, ref_path, ref_offset) VALUES "
             "('ev-q01e', 'sess-q01', 'claude', 'user_prompt', 'tool_call', "
             "?, 'ck-ev', 'echo zebraquux checked', 'transcript.jsonl', 0)",
             (now,)),
            ("INSERT INTO episode (id, namespace, started_at) "
             "VALUES ('ep-q01e', ?, ?)", (NS, now)),
            ("INSERT INTO episode_evidence (episode_id, evidence_id) "
             "VALUES ('ep-q01e', 'ev-q01e')", ()),
            ("INSERT INTO memory_evidence (memory_id, evidence_id) "
             "VALUES (?, 'ev-q01e')", (target,)),
            # negative control: evidence shared with a surviving row
            ("INSERT INTO evidence (id, session_id, lane, moment, kind, ts, "
             "hash, excerpt, ref_path, ref_offset) VALUES "
             "('shared', 'sess-q01', 'claude', 'user_prompt', 'tool_call', "
             "?, 'ck-shared', 'shared evidence text', 'transcript.jsonl', 0)",
             (now,)),
            ("INSERT INTO memory_evidence (memory_id, evidence_id) "
             "VALUES (?, 'shared')", (target,)),
            ("INSERT INTO memory_evidence (memory_id, evidence_id) "
             "VALUES (?, 'shared')", (other,)),
        )

        r = self._purge(target)
        self.assertEqual(r.returncode, 0, r.stderr)

        c1 = (self.qone("SELECT COUNT(*) FROM evidence WHERE id='ev-q01e'")
              + self.qone("SELECT COUNT(*) FROM episode_evidence "
                          "WHERE evidence_id='ev-q01e'"))
        c2 = self.qone("SELECT COUNT(*) FROM evidence WHERE id='shared'")
        c3 = self.qone("SELECT COUNT(*) FROM memory_evidence "
                       "WHERE memory_id=? AND evidence_id='shared'", (other,))
        self.assertEqual(
            (c1, c2, c3), (0, 1, 1),
            "evidence referenced only by the purged id must be deleted with "
            "its episode join; evidence shared with a survivor must stay "
            "and keep the survivor's link")


# ---------------------------------------------------------------------------
# AC6: derived copies quoting the purged content
# ---------------------------------------------------------------------------
class PurgeDerivedCopiesTest(_PurgeBase):
    def test_derived_copies_are_rewritten_deleted_or_refused(self):
        target = self.add_row(TARGET_CONTENT)
        other = self.add_row(OTHER_CONTENT)
        now = "2026-09-28T00:00:00Z"

        other_content = self.qone("SELECT content FROM memory WHERE id=?",
                                  (other,))
        # 1. keeper: the exact merged shape consolidate.py:437 writes
        #    (keeper content + separator + absorbed content, merged_from set).
        separator = f"\n\n--- merged from {target} ---\n"
        merged = other_content + separator + TARGET_CONTENT
        # 3. extractive episode summary added through the real CLI first, so
        #    the episode UPDATE below can point at a real memory row.
        summary = self.add_row(
            "episode summary: deploy token marker zebraquux reviewed",
            tags="summary,episode", source_ref="episode:ep-q01k")
        self.assertNotEqual(summary, target)
        self._exec(
            ("UPDATE memory SET content=?, merged_from=? WHERE id=?",
             (merged, target, other)),
            # 2. two-source belief head quoting the target (beliefs.py
            #    251-257 shape: head content is a member's content, sources
            #    are join rows)
            ("INSERT INTO belief_head (id, namespace, topic_identity, "
             "content, head_state, head_source_id, support_count, "
             "refresh_watermark, generator_revision) VALUES "
             "('bh-q01k', ?, 'q01k-topic', ?, 'active', ?, 2, ?, 'test')",
             (NS, TARGET_CONTENT, other, now)),
            ("INSERT INTO belief_head_source (head_id, source_id, role, "
             "source_ingestion_ts, source_checksum) VALUES "
             "('bh-q01k', ?, 'support', ?, 'ck-target')", (target, now)),
            ("INSERT INTO belief_head_source (head_id, source_id, role, "
             "source_ingestion_ts, source_checksum) VALUES "
             "('bh-q01k', ?, 'support', ?, 'ck-other')", (other, now)),
            # episode whose summary row quotes the needle and whose member
            # set includes the target
            ("INSERT INTO episode (id, namespace, started_at, ended_at, "
             "summary_memory_id, token_count) VALUES "
             "('ep-q01k', ?, ?, '', '', 0)", (NS, now)),
            ("UPDATE episode SET summary_memory_id=? WHERE id='ep-q01k'",
             (summary,)),
            ("INSERT INTO episode_memory (episode_id, memory_id) "
             "VALUES ('ep-q01k', ?)", (target,)),
        )

        r = self._purge(target)
        if r.returncode != 0:
            # A purge-own refusal is acceptable, but it must NAME the derived
            # row it refused for — and it must not be argparse confusion.
            self.assertNotIn(
                "invalid choice", r.stderr,
                "purge must exist and fail for a substantive reason, not be "
                "rejected by the CLI surface itself")
            self.assertTrue(
                other in r.stderr or "bh-q01k" in r.stderr
                or summary in r.stderr,
                "a purge-own refusal must name the derived row (keeper id, "
                "belief head id, or summary id) it refused for: "
                + r.stderr)
        else:
            leaked_rows = self.qone(
                "SELECT COUNT(*) FROM memory WHERE LOWER(content) LIKE ?",
                ("%zebraquux%",))
            leaked_heads = self.qone(
                "SELECT COUNT(*) FROM belief_head "
                "WHERE LOWER(content) LIKE ?", ("%zebraquux%",))
            leaked_merge = self.qone(
                "SELECT COUNT(*) FROM memory WHERE merged_from LIKE ?",
                (f"%{target}%",))
            self.assertEqual(
                (leaked_rows, leaked_heads, leaked_merge), (0, 0, 0),
                "a successful purge must rewrite or delete every derived "
                "copy quoting the purged content (memory rows, belief heads, "
                "merged_from provenance)")


# ---------------------------------------------------------------------------
# AC7: per-session delivery ledgers under <ZMEM_DATA>/ops
# ---------------------------------------------------------------------------
class PurgeLedgerTest(_PurgeBase):
    def test_purge_scrubs_delivery_ledgers(self):
        target = self.add_row(TARGET_CONTENT)
        clean = self.add_row(
            "deploy token marker checklist for the release runner")
        # One passive recall mirroring the hook argv — this is what writes
        # the delivery ledger for the session.
        r = self._run("recall", "--query", "deploy token marker",
                      "--namespace", NS, "--limit", "5", "--no-bump",
                      "--for-injection", "--json", "--session-id", "sess-q01",
                      "--moment", "user_prompt", "--lane", "claude")
        self.assertEqual(r.returncode, 0, r.stderr)

        ledger_files = glob.glob(
            os.path.join(self.data_dir, "ops", "*.ledger*"))
        self.assertGreater(
            self._needle_count(ledger_files), 0,
            "precondition: the delivered target must have put needle bytes "
            "in the session ledger")
        self.assertIn(
            clean, self._delivered_ids("sess-q01"),
            "precondition: the clean row must be recorded as delivered")

        r = self._purge(target)
        self.assertEqual(r.returncode, 0, r.stderr)

        ledger_files = glob.glob(
            os.path.join(self.data_dir, "ops", "*.ledger*"))
        needles_in_ledgers = self._needle_count(ledger_files)
        clean_id_still_delivered = (
            clean in self._delivered_ids("sess-q01"))
        self.assertEqual(
            (needles_in_ledgers, clean_id_still_delivered), (0, True),
            "purge must scrub the purged row's text from the delivery "
            "ledgers while the surviving row's delivery record stays intact")


# ---------------------------------------------------------------------------
# AC8: the deny list — a pre-purge sync file cannot resurrect the id
# ---------------------------------------------------------------------------
class PurgeDenyListTest(_PurgeBase):
    def test_ingest_jsonl_does_not_reinsert_purged_id(self):
        target = self.add_row(TARGET_CONTENT)
        peer = os.path.join(self.tmp, "peer.jsonl")
        r = self._run("export-jsonl", "--out", peer, "--namespace", NS)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(
            f'"id": "{target}"',
            Path(peer).read_text(encoding="utf-8"),
            "precondition: the exported sync file carries the target id")

        r = self._purge(target)
        self.assertEqual(r.returncode, 0, r.stderr)

        r = self._run("ingest-jsonl", "--in", peer)
        self.assertEqual(
            (r.returncode,
             self.qone("SELECT COUNT(*) FROM memory WHERE id=?", (target,))),
            (0, 0),
            "ingest must succeed (exit 0) yet skip the purged id — the deny "
            "list must outlive the row it names")


# ---------------------------------------------------------------------------
# AC9: --scrub-backups rewrites snapshots (incl. a prerestore-* copy)
# ---------------------------------------------------------------------------
class PurgeBackupScrubTest(_PurgeBase):
    def test_scrub_leaves_no_needle_bytes_in_prerestore_snapshot(self):
        target = self.add_row(TARGET_CONTENT)
        other = self.add_row(OTHER_CONTENT)
        backups = os.path.join(self.tmp, "backups")
        r = self._run("backup", "--out-dir", backups)
        self.assertEqual(r.returncode, 0, r.stderr)

        snaps = sorted(glob.glob(os.path.join(backups, "store-*.sqlite")),
                       key=os.path.getmtime)
        self.assertTrue(snaps, "backup produced no store-*.sqlite snapshot")
        snap = os.path.join(backups, "prerestore-20260927T000000Z.sqlite")
        shutil.copy2(snaps[-1], snap)
        for sidecar in ("-wal", "-shm"):
            src = snaps[-1] + sidecar
            if os.path.exists(src):
                shutil.copy2(src, snap + sidecar)
        self.assertGreater(
            self._needle_count([snap, snap + "-wal"]), 0,
            "precondition: the pre-purge snapshot holds the needle bytes")

        r = self._purge(target,
                        extra=["--scrub-backups", "--out-dir", backups])
        self.assertEqual(r.returncode, 0, r.stderr)

        snap_exists = os.path.exists(snap)
        conn = sqlite3.connect(
            "file:" + snap.replace(os.sep, "/") + "?mode=ro", uri=True)
        try:
            other_count = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE id=?",
                (other,)).fetchone()[0]
            integrity = conn.execute(
                "PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        needles = self._needle_count([snap, snap + "-wal"])
        self.assertEqual(
            (snap_exists, other_count, integrity, needles),
            (True, 1, "ok", 0),
            "--scrub-backups must rewrite the snapshot free of needle bytes "
            "while keeping it a valid database whose non-purged rows survive")


if __name__ == "__main__":
    unittest.main()
