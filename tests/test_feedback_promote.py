"""Tests for the v12 usage-feedback CLI and its promote-ladder invariants
(issue #64, 9.4).

Covers:
  - `feedback --applied|--violated` increments exactly one counter (the other
    stays 0) and prints a one-line JSON summary.
  - Refusals: both flags / neither flag -> exit 2 (argparse); unknown id and
    tombstoned id -> exit 1 with a stable stderr message.
  - The violated tier: the violated_count 1->2 crossing applies the ONE-TIME
    TRUST_VIOLATION_FLOOR_DROP (0.15) to trust_score, clamped at 0.0; later
    violations never re-drop; `signal` is never auto-changed.
  - HOOK INVARIANCE: recall (explicit AND --no-bump) and recent --no-bump
    leave applied_count/violated_count untouched — hooks can never advance
    the Voyager counters.
  - SOURCE SCAN: no hook script, hooks/lib, hermes-plugin, or MCP server file
    invokes `feedback` or writes the counters — the "hooks cannot increment"
    gate, enforced structurally.
  - SYNC: export carries the counters; a v11-era export line (fields
    stripped) ingests with defaults 0; malformed counter values (negative,
    float, bool, string) are refused fail-closed; ingest never applies the
    trust drop.

Drives the REAL store.py CLI via subprocess against a throwaway temp store
(ZMEM_STORE isolated — never the operator's home store).

Run: python tests/test_feedback_promote.py   (no pytest — repo convention)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"

# schema_meta loaded standalone (constants-only, dependency-free) so the
# migration assertions track the CURRENT supported version across bumps.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "zmem_schema_meta_v", REPO_ROOT / "skills" / "memory" / "scripts" / "schema_meta.py")
_schema_meta = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_schema_meta)
SUPPORTED_VERSION = str(_schema_meta.SUPPORTED_SCHEMA_VERSION)
PYTHON = sys.executable
NS = "project:feedback-test"
TRUST_DROP = 0.15


class FeedbackTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-feedback-")
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = {**os.environ, "ZMEM_STORE": self.store}
        for k in ("ZMEM_DATA", "ZMEM_BACKUP_DIR"):
            self.env.pop(k, None)
        r = self._run(
            "add", "--namespace", NS, "--type", "fact",
            "--content", "cache the compiled regex for hot loop paths",
            "--signal", "test", "--confidence", "0.9",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(self.store)
        try:
            self.memory_id = conn.execute(
                "SELECT id FROM memory WHERE superseded_at IS NULL").fetchone()[0]
        finally:
            conn.close()

    def tearDown(self):
        try:
            os.remove(self.store)
        except OSError:
            pass

    def _run(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=60,
        )

    def _row(self):
        conn = sqlite3.connect(self.store)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM memory WHERE id=?", (self.memory_id,)).fetchone()
        finally:
            conn.close()


class TestFeedbackCLI(FeedbackTestBase):
    def test_applied_increments_only_applied(self):
        r = self._run("feedback", "--id", self.memory_id, "--applied")
        self.assertEqual(r.returncode, 0, r.stderr)
        result = json.loads(r.stdout)
        self.assertEqual(result["verdict"], "applied")
        self.assertEqual(result["applied_count"], 1)
        self.assertEqual(result["violated_count"], 0)
        row = self._row()
        self.assertEqual(row["applied_count"], 1)
        self.assertEqual(row["violated_count"], 0)

    def test_violated_increments_only_violated(self):
        r = self._run("feedback", "--id", self.memory_id, "--violated")
        self.assertEqual(r.returncode, 0, r.stderr)
        result = json.loads(r.stdout)
        self.assertEqual(result["verdict"], "violated")
        self.assertEqual(result["applied_count"], 0)
        self.assertEqual(result["violated_count"], 1)

    def test_counters_increment_repeatedly(self):
        for expected in (1, 2, 3):
            r = self._run("feedback", "--id", self.memory_id, "--applied")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["applied_count"], expected)

    def test_both_flags_refused_exit_2(self):
        r = self._run("feedback", "--id", self.memory_id,
                      "--applied", "--violated")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self._assert_untouched()

    def test_neither_flag_refused_exit_2(self):
        r = self._run("feedback", "--id", self.memory_id)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self._assert_untouched()

    def test_missing_id_flag_refused_exit_2(self):
        r = self._run("feedback", "--applied")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self._assert_untouched()

    def test_unknown_id_exit_1(self):
        r = self._run("feedback", "--id",
                      "00000000-0000-0000-0000-000000000000", "--applied")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no live memory", r.stderr)
        self._assert_untouched()

    def test_tombstoned_id_exit_1(self):
        r = self._run("supersede", "--id", self.memory_id, "--reason", "stale")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run("feedback", "--id", self.memory_id, "--applied")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no live memory", r.stderr)
        # Feedback on a dead row must be a full no-op: the tombstoned row's
        # counters stay exactly as they were at supersede time (never
        # silently incremented by a refused write).
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT applied_count, violated_count FROM memory WHERE id=?",
                (self.memory_id,)).fetchone()
            self.assertEqual((row[0], row[1]), (0, 0))
        finally:
            conn.close()

    def _assert_untouched(self):
        row = self._row()
        self.assertEqual(row["applied_count"], 0)
        self.assertEqual(row["violated_count"], 0)


class TestViolatedTrustTier(FeedbackTestBase):
    def test_trust_drop_fires_once_at_crossing(self):
        self._run("feedback", "--id", self.memory_id, "--violated")
        row = self._row()
        self.assertAlmostEqual(row["trust_score"], 1.0, places=6,
                               msg="1st violation must NOT drop trust yet")
        r = self._run("feedback", "--id", self.memory_id, "--violated")
        self.assertEqual(r.returncode, 0, r.stderr)
        result = json.loads(r.stdout)
        self.assertTrue(result["trust_dropped"])
        self.assertAlmostEqual(result["trust_score"], 1.0 - TRUST_DROP, places=6)

    def test_trust_drop_does_not_repeat(self):
        for _ in range(4):
            self._run("feedback", "--id", self.memory_id, "--violated")
        row = self._row()
        self.assertEqual(row["violated_count"], 4)
        self.assertAlmostEqual(row["trust_score"], 1.0 - TRUST_DROP, places=6,
                               msg="drop must apply exactly once (at the 1->2 crossing)")

    def test_signal_never_changes(self):
        for _ in range(3):
            self._run("feedback", "--id", self.memory_id, "--violated")
        self.assertEqual(self._row()["signal"], "test")

    def test_trust_clamps_at_zero(self):
        conn = sqlite3.connect(self.store)
        try:
            conn.execute("UPDATE memory SET trust_score=0.1 WHERE id=?",
                         (self.memory_id,))
            conn.commit()
        finally:
            conn.close()
        for _ in range(2):
            self._run("feedback", "--id", self.memory_id, "--violated")
        row = self._row()
        self.assertEqual(row["violated_count"], 2)
        self.assertAlmostEqual(row["trust_score"], 0.0, places=6,
                               msg="trust floor is 0.0 (clamp), never negative")

    def test_applied_never_drops_trust(self):
        for _ in range(5):
            self._run("feedback", "--id", self.memory_id, "--applied")
        self.assertAlmostEqual(self._row()["trust_score"], 1.0, places=6)


class TestHookInvariance(FeedbackTestBase):
    """The issue gate: hooks / --no-bump / PreCompact / Hermes prefetch can
    never advance the Voyager counters. Every passive surface routes through
    `recall --no-bump` / `recent --no-bump`; drive those real invocations."""

    def _feedback_counts(self):
        row = self._row()
        return row["applied_count"], row["violated_count"]

    def test_no_bump_recall_leaves_counters_untouched(self):
        before = self._feedback_counts()
        r = self._run("recall", "--query", "cache regex", "--namespace", NS,
                      "--no-bump", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._feedback_counts(), before)

    def test_explicit_recall_leaves_counters_untouched(self):
        before = self._feedback_counts()
        r = self._run("recall", "--query", "cache regex", "--namespace", NS,
                      "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._feedback_counts(), before)

    def test_no_bump_recent_leaves_counters_untouched(self):
        before = self._feedback_counts()
        r = self._run("recent", "--namespace", NS, "--no-bump", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._feedback_counts(), before)

    def test_feedback_only_changes_via_feedback_cli(self):
        # End-to-end ordering proof: recall (both modes) then feedback.
        self._run("recall", "--query", "cache", "--no-bump", "--json")
        self._run("recall", "--query", "cache", "--json")
        self.assertEqual(self._feedback_counts(), (0, 0))
        self._run("feedback", "--id", self.memory_id, "--applied")
        self.assertEqual(self._feedback_counts(), (1, 0))


class TestHookSourceScan(unittest.TestCase):
    """Structural gate: no passive surface (hooks, hooks/lib, hermes-plugin,
    MCP server) may invoke `feedback` or write the counters. This is the
    source-level half of "critic confirms hooks cannot increment" — the
    behavioral half is TestHookInvariance above."""

    def test_no_passive_surface_references_feedback_or_counters(self):
        surfaces = list((REPO_ROOT / "hooks").rglob("*.py"))
        surfaces += list((REPO_ROOT / "hooks").rglob("*.sh"))
        surfaces += list((REPO_ROOT / "hooks").rglob("*.js"))
        surfaces += list((REPO_ROOT / "hooks").rglob("*.mjs"))
        surfaces += list((REPO_ROOT / "hooks").rglob("*.cjs"))
        # Hook CONFIG files are executable surfaces too: a feedback dispatch
        # can be wired entirely inside a JSON hook definition.
        surfaces += list((REPO_ROOT / "hooks").rglob("*.json"))
        surfaces += [REPO_ROOT / "hermes-plugin" / "__init__.py"]
        surfaces += list((REPO_ROOT / "hermes-plugin" / "server").rglob("*.py"))
        offenders = []
        for path in surfaces:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            # A store.py argv token "feedback" in ANY quoting style — the
            # subcommand must be unreachable from passive surfaces. (Prose
            # mentions like "PR feedback" in comments do NOT match: the token
            # must appear QUOTED, i.e. as an argv element.)
            if re.search(r"[\"']feedback[\"']", text):
                offenders.append(f"{path.name}: feedback invocation")
            if "applied_count" in text or "violated_count" in text:
                offenders.append(f"{path.name}: counter write")
        self.assertEqual(offenders, [],
                         "passive surfaces must never touch the Voyager "
                         f"counters: {offenders}")


class MigrationV12Test(unittest.TestCase):
    """v11 -> v12 migration on a POPULATED store (issue #64).

    Each schema bump carries a populated-legacy test (v8->v9 in
    test_update_invalidate, v9->v10 in test_entity, v10->v11 in
    test_memory_links). v12 is the simplest bump — two probe-guarded
    ADD COLUMN DEFAULT 0, no backfill, no side effects — and this pins
    exactly that: rows preserved, version bumped, counters read 0, and a
    re-open is idempotent.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-v12mig-")
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = {**os.environ, "ZMEM_STORE": self.store}
        for k in ("ZMEM_DATA", "ZMEM_BACKUP_DIR"):
            self.env.pop(k, None)
        # Hand-plant a v11-era store: no counter columns, populated rows.
        conn = sqlite3.connect(self.store)
        try:
            conn.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO meta(key, value) VALUES ('schema_version', '11');
                CREATE TABLE memory(
                    id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                    type TEXT NOT NULL, content TEXT NOT NULL,
                    superseded_at TEXT, ingestion_ts TEXT NOT NULL);
                INSERT INTO memory(id, namespace, type, content,
                                   superseded_at, ingestion_ts) VALUES
                    ('aaaaaaaa-0000-4000-8000-000000000001',
                     'project:mig', 'fact', 'v11 row one', NULL, '2026-01-01T00:00:00Z'),
                    ('aaaaaaaa-0000-4000-8000-000000000002',
                     'project:mig', 'fact', 'v11 row two', NULL, '2026-01-02T00:00:00Z');
            """)
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        try:
            os.remove(self.store)
        except OSError:
            pass

    def test_populated_v11_store_migrates_to_v12_losslessly(self):
        # A writable subcommand is the migration trigger (the real flow).
        r = subprocess.run(
            [PYTHON, str(STORE_PY), "get", "--id",
             "aaaaaaaa-0000-4000-8000-000000000001"],
            env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        row = json.loads(r.stdout)
        self.assertEqual(row["applied_count"], 0)
        self.assertEqual(row["violated_count"], 0)
        self.assertIn("v11 row one", row["content"])

        conn = sqlite3.connect(self.store)
        try:
            ver = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            n = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
            cols = {c[1] for c in conn.execute("PRAGMA table_info(memory)")}
            mins = conn.execute(
                "SELECT MIN(applied_count), MIN(violated_count), "
                "MAX(applied_count) FROM memory").fetchone()
        finally:
            conn.close()
        # v13 (issue #65): the walk continues past v12 to the current
        # SUPPORTED_SCHEMA_VERSION (13 adds the additive episode tables).
        self.assertEqual(ver, SUPPORTED_VERSION)
        self.assertEqual(n, 2, "no rows may be lost in migration")
        self.assertIn("applied_count", cols)
        self.assertIn("violated_count", cols)
        self.assertEqual(mins, (0, 0, 0), "counters default to 0 on migrate")

        # Second writable run is an idempotent no-op (version stays pinned).
        r = subprocess.run(
            [PYTHON, str(STORE_PY), "get", "--id",
             "aaaaaaaa-0000-4000-8000-000000000002"],
            env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(self.store)
        try:
            ver = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(ver, SUPPORTED_VERSION)


class TestFeedbackSync(FeedbackTestBase):
    def test_export_carries_counters(self):
        self._run("feedback", "--id", self.memory_id, "--applied")
        self._run("feedback", "--id", self.memory_id, "--violated")
        out = os.path.join(self.tmp, "export.jsonl")
        r = self._run("export-jsonl", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [json.loads(l) for l in Path(out).read_text(
            encoding="utf-8").splitlines() if l.strip()]
        row = next(l for l in lines if l["id"] == self.memory_id)
        self.assertEqual(row["applied_count"], 1)
        self.assertEqual(row["violated_count"], 1)

    def test_v11_era_export_line_ingests_with_defaults(self):
        out = os.path.join(self.tmp, "v11.jsonl")
        r = self._run("export-jsonl", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [json.loads(l) for l in Path(out).read_text(
            encoding="utf-8").splitlines() if l.strip()]
        for obj in lines:
            obj.pop("applied_count", None)
            obj.pop("violated_count", None)
        v11_path = os.path.join(self.tmp, "v11_stripped.jsonl")
        Path(v11_path).write_text(
            "\n".join(json.dumps(o, ensure_ascii=False) for o in lines) + "\n",
            encoding="utf-8")

        # Fresh store: the stripped (v11-era) export must ingest cleanly with
        # counter defaults 0 and trust 1.0 (drop NOT re-applied on ingest).
        fresh = os.path.join(self.tmp, "fresh.sqlite")
        env = {**self.env, "ZMEM_STORE": fresh}
        r = subprocess.run([PYTHON, str(STORE_PY), "init"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [PYTHON, str(STORE_PY), "ingest-jsonl", "--in", v11_path],
            env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(fresh)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT applied_count, violated_count, trust_score FROM memory "
                "WHERE id=?", (self.memory_id,)).fetchone()
            self.assertEqual(row["applied_count"], 0)
            self.assertEqual(row["violated_count"], 0)
            self.assertAlmostEqual(row["trust_score"], 1.0, places=6)
        finally:
            conn.close()

    def test_ingest_never_applies_trust_drop(self):
        # A violated row (trust dropped) round-trips verbatim: the counters and
        # the already-dropped trust arrive as data, and ingest does NOT apply
        # the drop a second time.
        for _ in range(2):
            self._run("feedback", "--id", self.memory_id, "--violated")
        dropped = self._row()["trust_score"]
        self.assertAlmostEqual(dropped, 1.0 - TRUST_DROP, places=6)
        out = os.path.join(self.tmp, "roundtrip.jsonl")
        self._run("export-jsonl", "--out", out)

        fresh = os.path.join(self.tmp, "fresh2.sqlite")
        env = {**self.env, "ZMEM_STORE": fresh}
        subprocess.run([PYTHON, str(STORE_PY), "init"], env=env,
                       capture_output=True, text=True, timeout=60)
        r = subprocess.run(
            [PYTHON, str(STORE_PY), "ingest-jsonl", "--in", out,
             "--allow-tombstones"],
            env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(fresh)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT applied_count, violated_count, trust_score FROM memory "
                "WHERE id=?", (self.memory_id,)).fetchone()
            # The row here is live (supersede never ran): counters arrive
            # verbatim; trust arrives already dropped and is NOT dropped again.
            self.assertEqual(row["violated_count"], 2)
            self.assertAlmostEqual(row["trust_score"], 1.0 - TRUST_DROP,
                                   places=6)
        finally:
            conn.close()

    def test_malformed_counter_values_refused(self):
        base = {
            "id": "f0000000-0000-4000-8000-000000000001",
            "namespace": NS, "type": "fact",
            "content": "a malformed-counter sync row",
            "tags": "", "source_ref": "session:sync-test",
            "confidence": 0.9, "signal": "test",
            "valid_from": "2026-01-01T00:00:00Z", "valid_until": "",
            "update_of": "", "taint": "trusted_internal",
            "ingestion_ts": "2026-01-01T00:00:00Z",
            "superseded_at": None, "supersede_reason": None,
            "merged_from": None, "trust_score": 1.0, "links": [],
        }
        bad_values = (-1, 1.5, True, "3")
        for field in ("applied_count", "violated_count"):
            for bad in bad_values:
                obj = dict(base)
                obj["id"] = (
                    f"f0000000-0000-4000-8000-{abs(hash((field, bad))) % 10**12:012d}")
                obj[field] = bad
                path = os.path.join(self.tmp, "bad.jsonl")
                Path(path).write_text(json.dumps(obj) + "\n", encoding="utf-8")
                r = self._run("ingest-jsonl", "--in", path)
                self.assertEqual(r.returncode, 0, r.stderr)  # file-level rc: counted
                self.assertIn("malformed", r.stderr,
                              f"{field}={bad!r} must be refused fail-closed")
                conn = sqlite3.connect(self.store)
                try:
                    n = conn.execute(
                        "SELECT count(*) FROM memory WHERE content LIKE "
                        "'a malformed-counter sync row'").fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(n, 0, f"{field}={bad!r} row must NOT be stored")


if __name__ == "__main__":
    unittest.main(verbosity=2)
