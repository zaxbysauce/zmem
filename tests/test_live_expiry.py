"""Live-expiry runtime tests (zmem issue #174).

Pins the inject_until <= now boundary on the passive injection lane, the
expired/below-relevance silent reasons, the present-time [EXPIRED] markers
vs the --as-of history view, recent's limit*2 over-fetch, and the
fractional-instant gate (#327).

Isolation contract (per the repo's store-test discipline): every test pins
ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR at a fresh nonexistent scratch and
ZMEM_MODEL_AUTODOWNLOAD=0 BEFORE importing storelib, purges store/storelib
from sys.modules on setUp, and restores the ambient env in teardown.
ZMEM_TEST_NOW is NEVER ambient in this process and NEVER set on a writer:
it exists only inside the env of a clock-sensitive READ subprocess,
immediately around the assertion it pins (tests/test_as_of_recall.py
pattern). Side rows are seeded through the real CLI writer (--signal test
--confidence 0.9, above the passive trust bar) or by direct INSERT with
explicit timestamps when the row's ingestion order is part of the contract.

Envelope-content discipline (zmem lesson acaee4a4): the passive selector
wraps recall in a blanket except, so an exit-0 parseable envelope can still
be a silent failure. Every passive-lane assertion goes through the
envelope's results[*]["content"], the rendered fence, the expired count,
and the reason string — never exit-0 alone.

Runs standalone: python tests/test_live_expiry.py
"""

from __future__ import annotations

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

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
FIXTURE_EXPIRY_INPUT = REPO_ROOT / "tests" / "fixtures" / "asof" / "expiry-input.jsonl"

# The contract's fixed seed (issue #174 Fixtures): three `state` rows with
# contents expiry-row-{1,2,3}, classes endpoint/metric/setting, and the
# boundary timestamps pinned below. Boundary law: inject_until <= now is
# EXPIRED (inclusive equality).
NS_FIXTURE = "fixture:expiry-174"
ROW1_ID = "00000000-0000-4000-8000-000000000001"
ROW2_ID = "00000000-0000-4000-8000-000000000002"
ROW3_ID = "00000000-0000-4000-8000-000000000003"
ROW1_CONTENT = "expiry-row-1"
ROW2_CONTENT = "expiry-row-2"
ROW3_CONTENT = "expiry-row-3"
ALL_CONTENTS = (ROW1_CONTENT, ROW2_CONTENT, ROW3_CONTENT)

# The three clock instants every boundary test walks.
T_MINUS_1 = "2026-09-09T23:59:59Z"
T_ZERO = "2026-09-10T00:00:00Z"
T_PLUS_1 = "2026-09-10T00:00:01Z"

PREFIX = "[EXPIRED] "


class LiveExpiryTest(unittest.TestCase):
    def setUp(self):
        # Fresh nonexistent scratch per test; the four isolation vars are
        # pinned BEFORE any storelib import (sys.modules purge below, the
        # tests/test_as_of_recall.py:26-81 pattern) so the operator store is
        # never opened and STORE_PATH never freezes onto it.
        self.tmp = tempfile.mkdtemp(prefix="zmem-live-expiry-")
        self.scratch = Path(self.tmp)
        self._saved_env = os.environ.copy()
        for key in list(os.environ):
            if key.startswith("ZMEM_"):
                os.environ.pop(key)
        os.environ.update(self._isolation_env(self.scratch))
        self._purge_storelib()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._purge_storelib()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _purge_storelib():
        for mod in list(sys.modules):
            if mod == "store" or mod.startswith("storelib"):
                del sys.modules[mod]

    @staticmethod
    def _isolation_env(store_dir: Path, now: str | None = None) -> dict:
        """Subprocess env pinned at `store_dir`; ZMEM_TEST_NOW ONLY when the
        leg is a clock-sensitive read (never the writer)."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_")
               and k not in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")}
        env.update({
            "ZMEM_STORE": str(store_dir / "store.sqlite"),
            "ZMEM_DATA": str(store_dir),
            "ZMEM_MODELS_DIR": str(store_dir / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        if now is not None:
            env["ZMEM_TEST_NOW"] = now
        return env

    def run_cli(self, store_dir: Path, *args, now: str | None = None
                ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(STORE_PY), *args],
            capture_output=True, text=True, timeout=180,
            env=self._isolation_env(store_dir, now=now),
        )

    def cli_add_state(self, store_dir: Path, namespace: str, content: str,
                      expiry_class: str, inject_until: str) -> str:
        """Seed one state row through the REAL CLI writer (trust-bar safe:
        --signal test --confidence 0.9 deliver in the passive lane; the
        writer clock is the real one — no ZMEM_TEST_NOW here)."""
        r = self.run_cli(
            store_dir, "add", "--type", "state", "--namespace", namespace,
            "--content", content, "--tags", "", "--signal", "test",
            "--confidence", "0.9", "--expiry-class", expiry_class,
            "--inject-until", inject_until, "--json")
        self.assertEqual(
            r.returncode, 0,
            f"CLI add failed for {content!r}: rc={r.returncode} "
            f"stderr={r.stderr.strip()[:300]}")
        payload = json.loads(r.stdout)
        self.assertEqual(payload.get("result"), "stored",
                         f"CLI add did not store {content!r}: {r.stdout[:200]}")
        return payload["id"]

    def fresh_v15_conn(self, store_dir: Path) -> sqlite3.Connection:
        """A fresh init_db+migrate store at store_dir/store.sqlite."""
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        import storelib.schema as schema  # imported AFTER the env pin
        conn = sqlite3.connect(str(store_dir / "store.sqlite"))
        conn.row_factory = sqlite3.Row
        schema.init_db(conn)
        schema.migrate(conn)
        return conn

    def insert_memory_row(self, conn: sqlite3.Connection, *, mid: str,
                          namespace: str, content: str, valid_from: str,
                          ingestion_ts: str, signal: str = "test",
                          confidence: float = 0.9) -> None:
        """Direct-INSERT one 29-column memory row (explicit timestamps: the
        ingestion order is part of the contract on these rows)."""
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, "
            "source_ref, source_hash, confidence, signal, valid_from, "
            "superseded_at, ingestion_ts, retrieval_count, last_retrieved, "
            "surfaced_count, last_surfaced, merged_from, content_norm, "
            "valid_until, update_of, taint, trust_score, applied_count, "
            "violated_count, embedding, embedding_model, embedded_at, "
            "consolidated_at, supersede_reason) "
            "VALUES (?, ?, 'state', ?, '', '', '', ?, ?, ?, NULL, ?, "
            "0, NULL, 0, NULL, NULL, '', '', '', 'trusted_internal', 1.0, 0, "
            "0, NULL, '', NULL, NULL, '')",
            (mid, namespace, content, confidence, signal, valid_from,
             ingestion_ts),
        )

    def insert_expiry_row(self, conn: sqlite3.Connection, mid: str,
                          inject_until: str, expiry_class: str) -> None:
        conn.execute(
            "INSERT INTO memory_expiry (memory_id, inject_until, expiry_class) "
            "VALUES (?, ?, ?)", (mid, inject_until, expiry_class))

    def seed_fixture_rows(self, store_dir: Path) -> None:
        """Direct-INSERT the three committed expiry-input.jsonl rows (the
        contract's exact bytes, fixed ids ...0001..0003, namespace
        fixture:expiry-174 — the writer rejects that namespace but direct
        INSERT is fine; memory_fts is external-content so the rows are
        searchable as-is)."""
        conn = self.fresh_v15_conn(store_dir)
        try:
            for line in FIXTURE_EXPIRY_INPUT.read_text(
                    encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                conn.execute(
                    "INSERT INTO memory (id, namespace, type, content, tags, "
                    "source_ref, source_hash, confidence, signal, valid_from, "
                    "superseded_at, ingestion_ts, retrieval_count, "
                    "last_retrieved, surfaced_count, last_surfaced, "
                    "merged_from, content_norm, valid_until, update_of, "
                    "taint, trust_score, applied_count, violated_count, "
                    "embedding, embedding_model, embedded_at, consolidated_at, "
                    "supersede_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, NULL, ?, "
                    "0, NULL, 0, NULL, NULL, '', '', '', 'trusted_internal', "
                    "1.0, 0, 0, NULL, '', NULL, NULL, '')",
                    (rec["id"], rec["namespace"], rec["type"], rec["content"],
                     rec.get("tags", ""), rec.get("source_ref", ""),
                     rec.get("confidence", 0.9), rec.get("signal", "grounded"),
                     rec.get("valid_from", rec["ingestion_ts"]),
                     rec["ingestion_ts"]),
                )
                conn.execute(
                    "INSERT INTO memory_expiry (memory_id, inject_until, "
                    "expiry_class) VALUES (?, ?, ?)",
                    (rec["id"], rec["inject_until"], rec["expiry_class"]))
            conn.commit()
        finally:
            conn.close()

    def envelope(self, r: subprocess.CompletedProcess) -> dict:
        """Parse a --json envelope, refusing the exit-0-garbage trap."""
        self.assertEqual(
            r.returncode, 0,
            f"expected exit 0, got {r.returncode}; stderr={r.stderr.strip()[:300]}")
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            self.fail(f"stdout is not JSON: {r.stdout[:300]}")

    @staticmethod
    def contents(env: dict) -> list:
        return [row.get("content", "") for row in env.get("results", [])]

    def assert_boundary(self, env: dict, live_contents: list,
                        all_contents: list, now: str, lane: str) -> None:
        """One passive-lane boundary leg: exact live composition, every
        expired content absent from results AND the rendered fence, exact
        expired count (envelope content, never exit-0 alone)."""
        got = sorted(self.contents(env))
        want = sorted(live_contents)
        for dead in all_contents:
            if dead in live_contents:
                continue
            self.assertNotIn(
                dead, self.contents(env),
                f"expired content {dead!r} present in {lane} results at "
                f"ZMEM_TEST_NOW={now} (contents={self.contents(env)})")
            self.assertNotIn(
                dead, env.get("rendered") or "",
                f"expired content {dead!r} present in {lane} rendered fence "
                f"at ZMEM_TEST_NOW={now}")
        self.assertEqual(
            got, want,
            f"{lane} composition at ZMEM_TEST_NOW={now}: expected {want}, "
            f"got {got}")
        n_expired = len([c for c in all_contents if c not in live_contents])
        self.assertEqual(
            env.get("expired", None), n_expired,
            f"{lane} envelope expired count at ZMEM_TEST_NOW={now}: expected "
            f"{n_expired}, got {env.get('expired', None)!r} (key absent = no "
            f"expiry gate on this lane)")

    # -- tests ------------------------------------------------------------

    def test_for_injection_excludes_t_minus_one_t_and_t_plus_one(self):
        """AC1: the inject_until <= now boundary (inclusive at equality)
        gates recall --for-injection. Row1 (until T_ZERO) delivers ONLY at
        23:59:59Z; row2 (until 23:59:59Z) is absent at EVERY instant; row3
        (until T_PLUS_1) is absent at its equality instant 00:00:01Z."""
        ns = "project:expiry-live"
        d = self.scratch / "boundary"
        d.mkdir()
        self.cli_add_state(d, ns, ROW1_CONTENT, "endpoint", T_ZERO)
        self.cli_add_state(d, ns, ROW2_CONTENT, "metric", T_MINUS_1)
        self.cli_add_state(d, ns, ROW3_CONTENT, "setting", T_PLUS_1)

        instants = [
            (T_MINUS_1, [ROW1_CONTENT, ROW3_CONTENT]),
            (T_ZERO, [ROW3_CONTENT]),
            (T_PLUS_1, []),
        ]
        for now, live in instants:
            env = self.envelope(self.run_cli(
                d, "recall", "--query", "expiry-row", "--namespace", ns,
                "--for-injection", "--json",
                "--session-id", f"live-a-{uuid.uuid4().hex}",
                "--moment", "user_prompt", "--lane", "zcode", now=now))
            self.assert_boundary(env, live, list(ALL_CONTENTS), now, "recall")

        # The all-expired instant names the reason in the envelope itself.
        env_dead = self.envelope(self.run_cli(
            d, "recall", "--query", "expiry-row", "--namespace", ns,
            "--for-injection", "--json",
            "--session-id", f"live-b-{uuid.uuid4().hex}",
            "--moment", "user_prompt", "--lane", "zcode", now=T_PLUS_1))
        self.assertEqual(
            env_dead.get("reason"), "expired",
            f"whole pool expired at {T_PLUS_1}: expected reason='expired', "
            f"got {env_dead.get('reason')!r}")

    def test_expired_only_reports_expired_reason_and_count(self):
        """AC2: a query whose whole matching pool is expired is silent with
        reason=expired and the exact expired count (2)."""
        d = self.scratch / "all-expired"
        d.mkdir()
        ns = "project:expiry-live-c2a"
        self.cli_add_state(d, ns, "alpha beta combination", "endpoint", T_ZERO)
        self.cli_add_state(d, ns, "alpha beta secondary", "metric", T_MINUS_1)

        env = self.envelope(self.run_cli(
            d, "recall", "--query", "alpha beta", "--namespace", ns,
            "--for-injection", "--json",
            "--session-id", f"live-c2a-{uuid.uuid4().hex}",
            "--moment", "user_prompt", "--lane", "zcode", now=T_ZERO))
        self.assertEqual(env.get("reason"), "expired",
                         f"expected reason='expired', got {env.get('reason')!r}")
        self.assertEqual(env.get("expired", None), 2,
                         f"expected expired==2, got {env.get('expired', None)!r}")
        self.assertEqual(self.contents(env), [],
                         f"all-expired pool delivered rows: {self.contents(env)}")

    def test_live_below_relevance_reports_below_relevance(self):
        """AC2: a live low-relevance survivor beside one expired row names
        below-relevance (the space-separated query matches both terms on the
        expired row only, so the live row's lexical lane measures 0.0 and
        fails the relevance floor)."""
        d = self.scratch / "below-rel"
        d.mkdir()
        ns = "project:expiry-live-c2b"
        self.cli_add_state(d, ns, "alpha beta combination", "endpoint", T_ZERO)
        self.cli_add_state(d, ns, "alpha filler zebra note",
                           "endpoint", "2027-01-01T00:00:00Z")

        env = self.envelope(self.run_cli(
            d, "recall", "--query", "alpha beta", "--namespace", ns,
            "--for-injection", "--json",
            "--session-id", f"live-c2b-{uuid.uuid4().hex}",
            "--moment", "user_prompt", "--lane", "zcode", now=T_ZERO))
        self.assertEqual(
            env.get("reason"), "below-relevance",
            f"expected reason='below-relevance', got {env.get('reason')!r}")
        self.assertEqual(env.get("expired", None), 1,
                         f"expected expired==1, got {env.get('expired', None)!r}")
        self.assertEqual(self.contents(env), [],
                         f"below-relevance pool delivered rows: "
                         f"{self.contents(env)}")

    def test_present_recall_marks_expired_but_as_of_does_not(self):
        """AC4: present-time recall --json marks expired rows with the
        '[EXPIRED] ' content prefix and expired=true (row3 live stays
        unmarked), while recall --as-of T_ZERO returns all three contents
        WITHOUT the prefix (history view bypasses the gate)."""
        d = self.scratch / "markers"
        d.mkdir()
        self.seed_fixture_rows(d)

        env = self.envelope(self.run_cli(
            d, "recall", "--query", "expiry-row",
            "--namespace", NS_FIXTURE, "--json", now=T_ZERO))
        marked = {row.get("id"): (row.get("content", ""), row.get("expired"))
                  for row in env.get("results", [])}
        self.assertEqual(set(marked), {ROW1_ID, ROW2_ID, ROW3_ID},
                         f"recall must surface all three rows, got "
                         f"{sorted(marked)}")
        for rid, content in ((ROW1_ID, ROW1_CONTENT),
                             (ROW2_ID, ROW2_CONTENT)):
            got_content, flag = marked.get(rid, ("", None))
            self.assertEqual(got_content, PREFIX + content,
                             f"row {rid} content must be "
                             f"{(PREFIX + content)!r}, got {got_content!r}")
            self.assertIs(flag, True,
                          f"row {rid} must carry expired==true, got {flag!r}")
        live_content, live_flag = marked.get(ROW3_ID, ("", None))
        self.assertEqual(live_content, ROW3_CONTENT,
                         f"live row3 must be unmarked, got {live_content!r}")
        self.assertFalse(live_flag,
                         f"live row3 must not carry a truthy expired flag, "
                         f"got {live_flag!r}")

        aenv = self.envelope(self.run_cli(
            d, "recall", "--query", "expiry-row", "--namespace", NS_FIXTURE,
            "--as-of", T_ZERO, "--json", now=T_ZERO))
        arows = aenv.get("results", [])
        self.assertEqual(sorted(row.get("id") for row in arows),
                         sorted((ROW1_ID, ROW2_ID, ROW3_ID)),
                         f"as-of recall must surface all three rows, got "
                         f"{[row.get('id') for row in arows]}")
        for row in arows:
            self.assertFalse(
                (row.get("content") or "").startswith(PREFIX),
                f"as-of view leaked the expiry marker onto "
                f"{row.get('id')}: {row.get('content')!r}")
            self.assertFalse(row.get("expired"),
                             f"as-of view leaked the expired flag onto "
                             f"{row.get('id')}")
        self.assertEqual(sorted(self.contents(aenv)),
                         sorted(ALL_CONTENTS),
                         "as-of must return all three UNMARKED contents")

    def test_recent_uses_same_boundary_and_overfetch(self):
        """recent --for-injection walks the same inclusive boundary as
        recall, and its source scan is limit*2: with 5 live + 3 expired rows
        in one namespace and --limit 3, three live rows still deliver and
        expired==3 — a limit*1 scan cannot deliver 3 live when the expired
        rows sort FIRST by ingestion."""
        # --- leg a: the three-instant boundary on CLI-seeded rows ---
        ns = "project:expiry-live-recent"
        d = self.scratch / "recent-boundary"
        d.mkdir()
        self.cli_add_state(d, ns, ROW1_CONTENT, "endpoint", T_ZERO)
        self.cli_add_state(d, ns, ROW2_CONTENT, "metric", T_MINUS_1)
        self.cli_add_state(d, ns, ROW3_CONTENT, "setting", T_PLUS_1)
        for now, live in [
            (T_MINUS_1, [ROW1_CONTENT, ROW3_CONTENT]),
            (T_ZERO, [ROW3_CONTENT]),
            (T_PLUS_1, []),
        ]:
            env = self.envelope(self.run_cli(
                d, "recent", "--for-injection", "--json", "--namespace", ns,
                "--limit", "10", "--session-id", f"live-r-{uuid.uuid4().hex}",
                "--moment", "user_prompt", "--lane", "zcode", now=now))
            self.assert_boundary(env, live, list(ALL_CONTENTS), now, "recent")

        # --- leg b: the limit*2 over-fetch, composition-proven ---
        ns2 = "project:expiry-overfetch"
        d2 = self.scratch / "recent-overfetch"
        d2.mkdir()
        conn = self.fresh_v15_conn(d2)
        try:
            # The three expired rows ingest LATER than the five live rows, so
            # the ingestion_ts DESC scan meets them FIRST: a limit*1 scan
            # would spend every slot on expired rows and deliver nothing.
            for i in (1, 2, 3):
                mid = f"00000000-0000-4000-8000-0000000000d{i}"
                self.insert_memory_row(
                    conn, mid=mid, namespace=ns2,
                    content=f"overfetch-dead-{i}",
                    valid_from="2026-09-01T00:00:00Z",
                    ingestion_ts="2027-01-01T00:00:00Z")
                self.insert_expiry_row(conn, mid, T_MINUS_1, "endpoint")
            for i in (1, 2, 3, 4, 5):
                mid = f"00000000-0000-4000-8000-0000000000b{i}"
                self.insert_memory_row(
                    conn, mid=mid, namespace=ns2,
                    content=f"overfetch-live-{i}",
                    valid_from="2026-09-01T00:00:00Z",
                    ingestion_ts="2026-09-02T00:00:00Z")
                self.insert_expiry_row(conn, mid, "2027-06-01T00:00:00Z",
                                       "setting")
            conn.commit()
        finally:
            conn.close()

        env = self.envelope(self.run_cli(
            d2, "recent", "--for-injection", "--json", "--namespace", ns2,
            "--limit", "3", "--session-id", f"live-of-{uuid.uuid4().hex}",
            "--moment", "user_prompt", "--lane", "zcode", now=T_ZERO))
        delivered = self.contents(env)
        self.assertEqual(
            len(delivered), 3,
            f"limit 3 with 3 expired rows sorting first must still deliver 3 "
            f"LIVE rows (the limit*2 scan), got {delivered}")
        for content in delivered:
            self.assertTrue(
                content.startswith("overfetch-live-"),
                f"a non-live row spent a delivery slot: {content!r}")
        self.assertNotIn("overfetch-dead-1", env.get("rendered") or "",
                         "an expired row leaked into the rendered fence")
        self.assertEqual(
            env.get("expired", None), 3,
            f"expected expired==3 (the pre-gate top-limit window is the three "
            f"expired rows), got {env.get('expired', None)!r}")

    def test_fractional_inject_until_gates_by_true_instant(self):
        """#327 (absorbed by #174): fractional inject_until timestamps gate
        by their TRUE instant, not by whole-second truncation — a
        00:00:00.5Z row delivers at 00:00:00Z and expires at 00:00:01Z."""
        # Parser leg: fractional forms parse to their true epoch.
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib.schema import _parse_iso_to_epoch
        self.assertEqual(_parse_iso_to_epoch("2026-12-31T23:59:59.5Z"),
                         1798761599.5)
        self.assertEqual(_parse_iso_to_epoch("2026-09-10T00:00:00.5Z"),
                         1788998400.5)
        self.assertEqual(_parse_iso_to_epoch("2026-12-31T23:59:59Z"),
                         1798761599.0)

        # Delivery leg: the .5Z instant gates both directions.
        d = self.scratch / "fractional"
        d.mkdir()
        ns = "project:expiry-live-frac"
        fractional_until = "2026-09-10T00:00:00.5Z"
        content = "fractional-row"
        self.cli_add_state(d, ns, content, "endpoint", fractional_until)

        live = self.envelope(self.run_cli(
            d, "recall", "--query", "fractional-row", "--namespace", ns,
            "--for-injection", "--json",
            "--session-id", f"live-f1-{uuid.uuid4().hex}",
            "--moment", "user_prompt", "--lane", "zcode", now=T_ZERO))
        self.assertIn(
            content, self.contents(live),
            f"a row with inject_until {fractional_until} must still DELIVER "
            f"at ZMEM_TEST_NOW={T_ZERO} (whole-second 00:00:00 is before the "
            f"fractional instant), got {self.contents(live)}")
        self.assertEqual(live.get("expired", None), 0,
                         f"at {T_ZERO} expected expired==0, got "
                         f"{live.get('expired', None)!r}")

        dead = self.envelope(self.run_cli(
            d, "recall", "--query", "fractional-row", "--namespace", ns,
            "--for-injection", "--json",
            "--session-id", f"live-f2-{uuid.uuid4().hex}",
            "--moment", "user_prompt", "--lane", "zcode", now=T_PLUS_1))
        self.assertNotIn(
            content, self.contents(dead),
            f"a row with inject_until {fractional_until} must be EXCLUDED at "
            f"ZMEM_TEST_NOW={T_PLUS_1}, got {self.contents(dead)}")
        self.assertNotIn(content, dead.get("rendered") or "",
                         "the fractional row leaked into the rendered fence "
                         "after expiry")
        self.assertEqual(dead.get("expired", None), 1,
                         f"at {T_PLUS_1} expected expired==1, got "
                         f"{dead.get('expired', None)!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
