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

import calendar
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
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


# ---------------------------------------------------------------------------
# Issue #124 (Workstream E): observational operation feedback — the fixture
# semantics shared by the classes below (ids, namespace, and the seeded
# delivery ledger match tests/fixtures/feedback_session.json exactly).
# ---------------------------------------------------------------------------

SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

FB_NS = "project:feedback-124"
FB_SESS = "00000000-0000-4000-8000-000000000124"
FB_M125 = "00000000-0000-4000-8000-000000000125"
FB_M126 = "00000000-0000-4000-8000-000000000126"
FB_E127 = "00000000-0000-4000-8000-000000000127"
FB_E128 = "00000000-0000-4000-8000-000000000128"
FB_E129 = "00000000-0000-4000-8000-000000000129"
FB_EV130 = "00000000-0000-4000-8000-000000000130"
FB_EV131 = "00000000-0000-4000-8000-000000000131"
FB_EV132 = "00000000-0000-4000-8000-000000000132"
# The pinned digest of tests/fixtures/feedback_expected.json (issue #124).
FB_EXPECTED_SHA256 = ("0bda442e0363e484be95e2a46d95e22c"
                      "17f6a9e5afe3042fb2b12f6e0e2d0e24")


def _fb_epoch(ts: str) -> float:
    return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))


def _fb_sidecar_path(data_dir: str, session_id: str = FB_SESS) -> str:
    stem = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return os.path.join(data_dir, "ops", stem + ".feedback.jsonl")


class OperationFeedbackTest(FeedbackTestBase):
    """Issue #124: one host operation event increments exactly the counters
    of the delivered memories it observationally matches, at most once per
    (session, event, memory), through the real store.py CLI."""

    def setUp(self):
        super().setUp()
        # `operation-feedback` reads its data_dir (ledger + sidecar) from
        # ZMEM_DATA; FeedbackTestBase strips that key, so re-pin it to the
        # throwaway store's own directory.
        self.env["ZMEM_DATA"] = self.tmp
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._seed_feedback_fixture()

    def _seed_feedback_fixture(self):
        conn = sqlite3.connect(self.store)
        try:
            conn.executemany(
                "INSERT INTO memory (id, namespace, type, content, tags,"
                " signal, confidence, ingestion_ts, source_ref, content_norm)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(FB_M125, FB_NS, "lesson",
                  "python tests/test_feedback_promote.py guard",
                  "tests,python", "test", 0.9, "2026-09-10T09:00:00Z", "", "a"),
                 (FB_M126, FB_NS, "lesson", "git status --short advice",
                  "git", "test", 0.9, "2026-09-10T09:00:00Z", "", "b")])
            conn.executemany(
                "INSERT INTO memory_evidence (memory_id, evidence_id)"
                " VALUES (?,?)",
                [(FB_M125, FB_EV130), (FB_M126, FB_EV131)])
            conn.commit()
        finally:
            conn.close()
        ops = os.path.join(self.tmp, "ops")
        os.makedirs(ops, exist_ok=True)
        ledger = os.path.join(
            ops, hashlib.sha256(FB_SESS.encode("utf-8")).hexdigest()[:32]
            + ".ledger")
        with open(ledger, "w", encoding="utf-8") as f:
            json.dump({"entries": [
                {"id": FB_M125, "moment": "pretool",
                 "ts": _fb_epoch("2026-09-10T10:00:00Z"),
                 "text": "python tests/test_feedback_promote.py"},
                {"id": FB_M126, "moment": "pretool",
                 "ts": _fb_epoch("2026-09-10T10:01:00Z"),
                 "text": "git status --short"}]}, f)

    def _opfb(self, *args):
        return self._run("operation-feedback", *args)

    def _counters(self, memory_id):
        conn = sqlite3.connect(self.store)
        try:
            return conn.execute(
                "SELECT applied_count, violated_count FROM memory WHERE id=?",
                (memory_id,)).fetchone()
        finally:
            conn.close()

    def _sidecar_bytes(self):
        with open(_fb_sidecar_path(self.tmp), "rb") as f:
            return f.read()

    def test_injected_matching_failure_increments_violated(self):
        r = self._opfb(
            "--session-id", FB_SESS, "--event-id", FB_E127,
            "--operation-token", "python",
            "--operation-token", "tests/test_feedback_promote.py",
            "--outcome", "failure", "--evidence-id", FB_EV130,
            "--now", "2026-09-10T10:01:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        rows = json.loads(r.stdout)
        self.assertEqual(rows, [{
            "memory_id": FB_M125, "verdict": "violated", "overlap": 2,
            "evidence_id": FB_EV130, "event_id": FB_E127, "session_id": FB_SESS,
        }])
        self.assertEqual(sorted(rows[0]),
                         ["event_id", "evidence_id", "memory_id",
                          "overlap", "session_id", "verdict"],
                         "the returned row carries EXACTLY the six keys")
        self.assertEqual(self._counters(FB_M125), (0, 1))
        self.assertEqual(self._counters(FB_M126), (0, 0))

    def test_injected_matching_success_increments_applied(self):
        r = self._opfb(
            "--session-id", FB_SESS, "--event-id", FB_E128,
            "--operation-token", "git", "--operation-token", "status",
            "--operation-token=--short",
            "--outcome", "success", "--evidence-id", FB_EV131,
            "--now", "2026-09-10T10:02:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(r.stdout)
        self.assertEqual(rows, [{
            "memory_id": FB_M126, "verdict": "applied", "overlap": 2,
            "evidence_id": FB_EV131, "event_id": FB_E128, "session_id": FB_SESS,
        }])
        self.assertEqual(self._counters(FB_M126), (1, 0))
        self.assertEqual(self._counters(FB_M125), (0, 0))

    def test_unrelated_operation_does_not_increment(self):
        r = self._opfb(
            "--session-id", FB_SESS, "--event-id", FB_E129,
            "--operation-token", "bun", "--operation-token", "test",
            "--operation-token", "unrelated",
            "--outcome", "success", "--evidence-id", FB_EV132,
            "--now", "2026-09-10T10:03:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [])
        self.assertEqual(self._counters(FB_M125), (0, 0))
        self.assertEqual(self._counters(FB_M126), (0, 0))
        # The in-window non-match writes exactly ONE unmatched sidecar line,
        # byte-identical to the committed fixture's third line.
        expected_lines = (FIXTURES_DIR / "feedback_sidecar_expected.jsonl") \
            .read_bytes().splitlines(keepends=True)
        self.assertEqual(self._sidecar_bytes(), expected_lines[2])

    def test_mismatched_evidence_id_drops_match(self):
        # Phase 4.5 reviewer-flagged coverage gap: an event whose evidence id
        # is NOT associated with the matched memory must not move the counter
        # -- the association gate drops the row and the event is recorded
        # unmatched, so deleting the association read cannot pass silently.
        r = self._opfb(
            "--session-id", FB_SESS, "--event-id", FB_E127,
            "--operation-token", "python",
            "--operation-token", "tests/test_feedback_promote.py",
            "--outcome", "failure", "--evidence-id", FB_EV132,
            "--now", "2026-09-10T10:01:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [])
        self.assertEqual(self._counters(FB_M125), (0, 0))
        sidecar = os.path.join(
            self.tmp, "ops",
            hashlib.sha256(FB_SESS.encode("utf-8")).hexdigest()[:32]
            + ".feedback.jsonl")
        lines = Path(sidecar).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual((record["memory_id"], record["overlap"],
                          record["verdict"], record["evidence_id"]),
                         ("", 0, "unmatched", FB_EV132))

    def test_same_session_event_cannot_double_count(self):
        args = (
            "--session-id", FB_SESS, "--event-id", FB_E127,
            "--operation-token", "python",
            "--operation-token", "tests/test_feedback_promote.py",
            "--outcome", "failure", "--evidence-id", FB_EV130,
            "--now", "2026-09-10T10:01:00Z")
        first = self._opfb(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self._opfb(*args)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout), [],
                         "a replayed event returns no new rows")
        self.assertEqual(self._counters(FB_M125), (0, 1),
                         "violated_count must stay at 1 across the repeat")
        lines = self._sidecar_bytes().splitlines(keepends=True)
        self.assertEqual(len(lines), 1,
                         "the sidecar must hold exactly one matching line")
        self.assertEqual(json.loads(lines[0])["verdict"], "violated")
        self.assertEqual(json.loads(lines[0])["memory_id"], FB_M125)

    def test_cli_parser_flags_and_exit_codes(self):
        # --help names every flag with the exact prescribed help strings.
        h = self._run("operation-feedback", "--help")
        self.assertEqual(h.returncode, 0, h.stderr)
        for flag in ("--session-id", "--event-id", "--operation-token",
                     "--outcome", "--evidence-id", "--now"):
            self.assertIn(flag, h.stdout, f"--help lacks {flag}")
        for text in ("session id owning the delivered rows",
                     "stable host operation event id",
                     "normalized operation token (repeatable)",
                     "completed operation outcome",
                     "associated evidence id",
                     "fixed ISO-8601 UTC time for tests"):
            self.assertIn(text, h.stdout, f"--help lacks help text {text!r}")
        self.assertIn("{success,failure}", h.stdout)
        # Argparse usage errors exit 2: missing required flag, bad --outcome.
        missing = self._opfb("--session-id", FB_SESS, "--outcome", "success")
        self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
        bad = self._opfb("--session-id", FB_SESS, "--event-id", FB_E128,
                         "--outcome", "bogus")
        self.assertEqual(bad.returncode, 2, bad.stdout + bad.stderr)
        # Success path with --now: exit 0, one-row compact sorted-key list.
        r = self._opfb(
            "--session-id", FB_SESS, "--event-id", FB_E128,
            "--operation-token", "git", "--operation-token", "status",
            "--operation-token=--short",
            "--outcome", "success", "--evidence-id", FB_EV131,
            "--now", "2026-09-10T10:02:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.endswith("\n"))
        self.assertEqual(json.loads(r.stdout), [{
            "memory_id": FB_M126, "verdict": "applied", "overlap": 2,
            "evidence_id": FB_EV131, "event_id": FB_E128, "session_id": FB_SESS,
        }])
        # Operational failure: exit 1, no stdout, stable stderr envelope.
        op = self._opfb("--session-id", FB_SESS, "--event-id", FB_E127,
                        "--operation-token", "python",
                        "--outcome", "failure", "--now", "not-a-timestamp")
        self.assertEqual(op.returncode, 1, op.stdout + op.stderr)
        self.assertEqual(op.stdout, "")
        self.assertTrue(op.stderr.startswith("[zmem] operation-feedback:"),
                        f"stderr was {op.stderr!r}")


class FeedbackFixtureBytesTest(unittest.TestCase):
    """The committed expected-fixture bytes are reproducible from the session
    fixture through the REAL #156 matcher (issue #124 AC11)."""

    def test_fixture_generator_matches_expected_bytes(self):
        expected = (FIXTURES_DIR / "feedback_expected.json").read_bytes()
        self.assertEqual(hashlib.sha256(expected).hexdigest(),
                         FB_EXPECTED_SHA256,
                         "the committed fixture itself drifted")
        with tempfile.TemporaryDirectory(prefix="zmem-fbgen-") as scratch:
            gen = os.path.join(scratch, "gen.json")
            r = subprocess.run(
                [PYTHON, str(FIXTURES_DIR / "make_feedback_expected.py"),
                 "--input", str(FIXTURES_DIR / "feedback_session.json"),
                 "--output", gen],
                capture_output=True, text=True, timeout=180)
            self.assertEqual(r.returncode, 0, r.stderr)
            produced = Path(gen).read_bytes()
        self.assertEqual(
            produced, expected,
            "generator drift: produced sha256="
            f"{hashlib.sha256(produced).hexdigest()} expected sha256="
            f"{hashlib.sha256(expected).hexdigest()}")


class FeedbackRecallProjectionTest(unittest.TestCase):
    """Issue #124: recall / recent / injection row dicts project the counters
    as integers while the human fenced renderer never shows them (or any
    evidence id).

    Driven in a SUBPROCESS with the env pinned before the storelib import
    (the STORE_PATH-freeze rule) — this file's every other class drives the
    real store.py the same way, and an in-process import here would evict a
    storelib singleton that co-run modules may still be using.
    """

    PROJ_NS = "project:feedback-124"

    _DRIVER = r"""
import calendar, contextlib, io, json, os, sys

tmp, scripts_dir, evidence_id = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
os.environ["ZMEM_DATA"] = tmp
os.environ["ZMEM_MODELS_DIR"] = os.path.join(tmp, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
for key in ("ZMEM_BACKUP_DIR", "CLAUDE_PLUGIN_DATA",
            "ZCODE_PLUGIN_DATA", "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW"):
    os.environ.pop(key, None)
sys.path.insert(0, scripts_dir)

from storelib.inject import select_and_budget_for_injection
from storelib.recall import recall_memory, recent_memory
from storelib.schema import connect, init_db, migrate
from storelib.write import add_memory

NS = "project:feedback-124"
conn = connect()
init_db(conn)
migrate(conn)
with contextlib.redirect_stdout(io.StringIO()):
    mid_py = add_memory(
        conn, namespace=NS, type_="lesson",
        content="python tests/test_feedback_promote.py guard",
        tags="tests,python", signal="test", confidence=0.9,
        source_ref="session:fb-proj")
    mid_git = add_memory(
        conn, namespace=NS, type_="lesson",
        content="git status --short advice", tags="git", signal="test",
        confidence=0.9, source_ref="session:fb-proj")
conn.execute("UPDATE memory SET applied_count=3, violated_count=1 "
             "WHERE id=?", (mid_py,))
conn.execute("INSERT INTO memory_evidence (memory_id, evidence_id) "
             "VALUES (?,?)", (mid_py, evidence_id))
conn.commit()

with contextlib.redirect_stdout(io.StringIO()):
    rows = recall_memory(conn, query="python tests guard", namespace=NS,
                         limit=5, no_bump=True, no_telemetry=True)
    recent = recent_memory(conn, namespace=NS, limit=5, no_bump=True,
                           no_telemetry=True)
    envelope = select_and_budget_for_injection(
        conn, query="python tests guard", namespace=NS,
        moment="user_prompt", session_id="fb-proj-session", data_dir=tmp)
conn.close()


def pick(seq, mid):
    return next((r for r in seq if r.get("id") == mid), None)


def shape(row):
    if row is None:
        return None
    return {"applied": row.get("applied_count"),
            "violated": row.get("violated_count"),
            "types": [type(row.get("applied_count")).__name__,
                      type(row.get("violated_count")).__name__],
            "keys": sorted(row)}


out = {
    "mid_py": mid_py,
    "mid_git": mid_git,
    "recall": shape(pick(rows, mid_py)),
    "recent": shape(pick(recent, mid_py)),
    "injection": shape(pick(envelope.get("results", []), mid_py)),
}
rendered = envelope.get("rendered") or ""
out["rendered"] = {
    "empty": not rendered.strip(),
    "names_applied_count": "applied_count" in rendered,
    "names_violated_count": "violated_count" in rendered,
    "names_evidence_id": "evidence_id" in rendered,
    "carries_the_evidence_uuid": evidence_id in rendered,
}
print(json.dumps(out))
"""

    def test_recall_and_recent_and_injection_project_counters(self):
        with tempfile.TemporaryDirectory(prefix="zmem-fbproj-") as tmp:
            driver = os.path.join(tmp, "driver.py")
            with open(driver, "w", encoding="utf-8") as f:
                f.write(self._DRIVER)
            r = subprocess.run(
                [PYTHON, driver, tmp, str(SCRIPTS_DIR), FB_EV130],
                capture_output=True, text=True, timeout=180)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout.strip().splitlines()[-1])
        for surface in ("recall", "recent", "injection"):
            shape = out[surface]
            self.assertIsNotNone(
                shape, f"the seeded python lesson must surface via {surface}")
            self.assertEqual(shape["applied"], 3, f"{surface}: {shape}")
            self.assertEqual(shape["violated"], 1, f"{surface}: {shape}")
            self.assertEqual(shape["types"], ["int", "int"],
                             f"{surface} counters must be ints, never "
                             f"bools/strings: {shape}")
        rendered = out["rendered"]
        self.assertFalse(rendered["empty"],
                         "the fenced renderer must have output")
        self.assertFalse(rendered["names_applied_count"], rendered)
        self.assertFalse(rendered["names_violated_count"], rendered)
        self.assertFalse(rendered["names_evidence_id"], rendered)
        self.assertFalse(rendered["carries_the_evidence_uuid"], rendered)


class TestFeedbackWriterExclusivity(unittest.TestCase):
    """Guardrail (issue #124): storelib/write.py feedback_memory stays the
    ONLY counter writer. Every SQL ``UPDATE memory`` whose statement region
    touches applied_count / violated_count must live in write.py."""

    # The "statement region": the UPDATE plus its immediate assignment /
    # read-back context. 400 chars is tight enough that schema.py's distant
    # ALTER-column commentary and sync.py's ingest prose cannot bleed into a
    # counter-writing statement, while write.py's feedback_memory UPDATE (the
    # ``{column} = {column} + 1`` increment followed by its counter SELECT)
    # is fully covered.
    _STATEMENT_WINDOW = 400

    def test_only_feedback_memory_writes_counters(self):
        storelib_dir = REPO_ROOT / "skills" / "memory" / "scripts" / "storelib"
        update_re = re.compile(r"UPDATE\s+memory", re.IGNORECASE)
        offenders = {}
        for path in sorted(storelib_dir.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            for m in update_re.finditer(src):
                region = src[max(0, m.start() - self._STATEMENT_WINDOW):
                             m.end() + self._STATEMENT_WINDOW]
                if re.search(r"applied_count|violated_count", region):
                    offenders.setdefault(path.name, 0)
                    offenders[path.name] += 1
        self.assertEqual(
            set(offenders), {"write.py"},
            f"counter-writing UPDATE memory statements escaped write.py "
            f"(the sole feedback_memory writer): {offenders}. Fix the "
            f"offending module, never weaken this pin.")
        self.assertGreaterEqual(offenders.get("write.py", 0), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
