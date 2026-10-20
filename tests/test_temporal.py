"""Issue #178 acceptance suite: temporal parsing, event occurrence writes,
and the temporal candidate arm.

Classes (the issue's declared set):
  RelativePhraseTest  — all 20 fixture phrases match their exact expected
                        UTC ranges (byte-compare against the generated
                        phrases.expected.json) and future/implausible
                        endpoints clamp.
  OccurrenceWriteTest — atomic event occurrence writes, non-event rejection
                        (CLI exit 2, no row), the four update/fold transition
                        tests, and the tombstone cleanup coverage across
                        supersede / consolidate absorb / organize.
  TemporalArmTest     — the in-window event ranks first under the seeded
                        as-of fixture, the default/env cap contract, and the
                        temporal arm is additive beside the graph arm.

Run: python tests/test_temporal.py   (no pytest — repo convention)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Env pin — BEFORE any storelib import (house contract, the #177
# tests/test_auto_retain.py pattern: storelib freezes STORE_PATH on first
# import; the operator's ~/.zmem is never touched).
# ---------------------------------------------------------------------------
import os
import sys
import subprocess
import shutil
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
STORE_PY = SCRIPTS_DIR / "store.py"

_TMP = tempfile.mkdtemp(prefix="zmem-178-temporal-")
STORE_PATH = os.path.join(_TMP, "store.sqlite")
os.environ["ZMEM_STORE"] = STORE_PATH
os.environ["ZMEM_DATA"] = _TMP
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_TMP, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
# The temporal-cap env is varied per test below; start from the default.
os.environ.pop("ZMEM_ARM_CAP_TEMPORAL", None)
# Writer time is always a fixed `now=` argument (never ZMEM_TEST_NOW — that
# seam is recall-only per the issue); the recall tests set it around calls.
os.environ.pop("ZMEM_TEST_NOW", None)
# No auto entity links in the fixtures (the test_graph_seed_arm.py pin).
os.environ["ZMEM_LINK_THRESHOLD"] = "1.01"
os.environ.setdefault("ZMEM_EMBED_PROFILE", "minilm")  # model-absent shape

sys.path.insert(0, str(SCRIPTS_DIR))

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import unittest  # noqa: E402
import uuid  # noqa: E402

from storelib import recall as recall_mod  # noqa: E402
from storelib.consolidate import _absorb_into_keeper  # noqa: E402
from storelib.links import add_link_pair  # noqa: E402
from storelib.organize import _supersede_stale_summaries  # noqa: E402
from storelib.schema import (  # noqa: E402
    _normalize_content, _prepare_store, connect)
from storelib.temporal import (  # noqa: E402
    MAX_TEMPORAL_DAYS, TEMPORAL_ARM_DEFAULT_CAP, clamp_range,
    parse_relative_range)
from storelib.write import add_memory, supersede_memory, update_memory  # noqa: E402

FIXED_NOW = "2026-09-10T15:30:00Z"
PROMPT_TS = "2026-09-10T12:00:00Z"
NS = "project:occ178"
ARM_NS = "project:asof"
QUERY = "last week's outage"
IN_WINDOW_ID = "00000000-0000-4000-8000-000000000178"
SUPERSEDED_ID = "00000000-0000-4000-8000-000000000180"
UUID_SHAPE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _reset_store_file():
    """Per-test isolation on the frozen module path (the graph-arm pattern:
    delete the file while no connection is open)."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(STORE_PATH + suffix)
        except OSError:
            pass


def _cli_env(store=None):
    env = {
        **{k: v for k, v in os.environ.items()
           if not k.startswith("ZMEM_")},
        "ZMEM_STORE": store or STORE_PATH,
        "ZMEM_DATA": _TMP,
        "ZMEM_MODELS_DIR": os.path.join(_TMP, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_EMBED_PROFILE": "minilm",
        "ZMEM_LINK_THRESHOLD": "1.01",
    }
    return env


def _read_asof_fixture():
    text = (FIXTURES_DIR / "asof" / "input.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if len(rows) != 4:
        raise AssertionError("asof fixture must hold exactly 4 rows")
    for row in rows:
        if row.get("namespace") != ARM_NS:
            raise AssertionError("asof fixture namespace drifted")
        if row.get("prompt_timestamp") != PROMPT_TS:
            raise AssertionError("asof fixture prompt_timestamp drifted")
    return rows


def _seed_asof_store(rows, store=None):
    """Seed the issue fixture rows exactly like the frozen check driver:
    CLI `add` per row (the event rows through the new occurrence flags),
    then ONE direct SQLite pass pinning each row's fixture id and validity
    window (the row with a non-empty valid_until is the superseded one and
    also gets its tombstone), renaming the id in EVERY memory_id side table.
    """
    store = store or STORE_PATH
    env = _cli_env(store)
    env["ZMEM_TEST_NOW"] = PROMPT_TS
    env.pop("ZMEM_ARM_CAP_TEMPORAL", None)
    _reset_store_file()
    generated = {}
    for row in rows:
        argv = [
            sys.executable, str(STORE_PY), "add",
            "--namespace", row["namespace"],
            "--type", row["type"],
            "--content", row["content"],
            "--confidence", "0.9",
            "--signal", "user",
            "--json",
        ]
        if row["type"] == "event":
            argv += [
                "--occurred-start", row["occurred_start"],
                "--occurred-end", row["occurred_end"],
                "--mentioned-at", row["mentioned_at"],
            ]
        proc = subprocess.run(
            argv, cwd=str(REPO_ROOT), env=env, capture_output=True,
            text=True, timeout=300)
        assert proc.returncode == 0, (
            "add of fixture row %s exited %d: %s"
            % (row["id"], proc.returncode,
               (proc.stderr or proc.stdout).strip()[-300:]))
        lines = [line for line in proc.stdout.splitlines()
                 if line.startswith("{")]
        payload = json.loads(lines[-1])
        assert payload.get("ok") or payload.get("id"), payload
        generated[row["id"]] = payload["id"]

    conn = sqlite3.connect(store)
    try:
        mid_tables = []
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall():
            try:
                columns = [
                    c[1] for c in
                    conn.execute('PRAGMA table_info("%s")' % table).fetchall()
                ]
            except sqlite3.Error:
                continue
            if table != "memory" and "memory_id" in columns:
                mid_tables.append(table)
        for row in rows:
            gen = generated[row["id"]]
            conn.execute(
                "UPDATE memory SET valid_from = ?, valid_until = ? "
                "WHERE id = ?",
                (row.get("valid_from", ""), row.get("valid_until", ""), gen),
            )
            if row.get("valid_until"):
                conn.execute(
                    "UPDATE memory SET superseded_at = ? WHERE id = ?",
                    (row["valid_until"], gen),
                )
            conn.execute(
                "UPDATE memory SET id = ? WHERE id = ?", (row["id"], gen)
            )
            for table in mid_tables:
                conn.execute(
                    'UPDATE "%s" SET memory_id = ? WHERE memory_id = ?'
                    % table,
                    (row["id"], gen),
                )
        conn.commit()
    finally:
        conn.close()
    # Direct test-connection contract (the issue's mandatory pin): a
    # separate read connection asserts PRAGMA foreign_keys=ON is honored —
    # production code never relies on FK enforcement, and the pin pass above
    # is the frozen check driver's byte-exact shape (renames ids in the
    # parent and every memory_id side table, which an FK cascade-free
    # constraint would refuse).
    verify = sqlite3.connect(store)
    try:
        verify.execute("PRAGMA foreign_keys=ON")
        assert verify.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        for row in rows:
            found = verify.execute(
                "SELECT COUNT(*) FROM memory WHERE id=? AND namespace=?",
                (row["id"], row["namespace"])).fetchone()[0]
            assert found == 1, "fixture id %s not pinned" % row["id"]
    finally:
        verify.close()


def _recall_envelope(conn, **kw):
    os.environ["ZMEM_TEST_NOW"] = PROMPT_TS
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            recall_mod.recall_memory(
                conn, query=kw.pop("query", QUERY),
                namespace=kw.pop("namespace", ARM_NS), as_json=True,
                no_bump=True, no_telemetry=True, for_injection=True,
                prompt_timestamp=PROMPT_TS, **kw)
    finally:
        os.environ.pop("ZMEM_TEST_NOW", None)
    return json.loads(buf.getvalue())


class RelativePhraseTest(unittest.TestCase):
    """The 20 fixture phrases resolve to their exact UTC ranges."""

    def test_twenty_phrases_match_expected_ranges(self):
        phrases_path = FIXTURES_DIR / "temporal" / "phrases.json"
        expected_path = FIXTURES_DIR / "temporal" / "phrases.expected.json"
        records = json.loads(phrases_path.read_text(encoding="utf-8"))
        self.assertEqual(len(records), 20)
        self.assertEqual(len({r["phrase"] for r in records}), 20,
                         "phrases must be unique")
        # Per-record: the parser output equals the fixture's own expected
        # pair (canonical UTC strings, exact).
        for record in records:
            derived = parse_relative_range(
                record["phrase"], prompt_timestamp=record["now"])
            self.assertEqual(
                derived,
                (record["expected_start"], record["expected_end"]),
                "phrase %r against now=%r must derive (%r, %r)"
                % (record["phrase"], record["now"],
                   record["expected_start"], record["expected_end"]))
        # Byte-contract: re-running the generator's projection over the
        # fixture must equal phrases.expected.json byte-for-byte.
        projection = [
            {"phrase": r["phrase"],
             "expected_start": parse_relative_range(
                 r["phrase"], prompt_timestamp=r["now"])[0],
             "expected_end": parse_relative_range(
                 r["phrase"], prompt_timestamp=r["now"])[1]}
            for r in records
        ]
        payload = (json.dumps(projection, ensure_ascii=False,
                              separators=(",", ":")) + "\n").encode("utf-8")
        self.assertEqual(payload, expected_path.read_bytes(),
                         "the derived projection must be byte-identical to "
                         "phrases.expected.json")

    def test_future_and_implausible_dates_are_clamped(self):
        self.assertEqual(MAX_TEMPORAL_DAYS, 3650)
        self.assertEqual(TEMPORAL_ARM_DEFAULT_CAP, 4)
        # A future END clamps down to the prompt timestamp.
        self.assertEqual(
            clamp_range("2026-09-09T00:00:00Z", "2026-09-15T00:00:00Z",
                        prompt_timestamp=PROMPT_TS),
            ("2026-09-09T00:00:00Z", "2026-09-10T12:00:00Z"))
        # BOTH endpoints in the future collapse onto the prompt -> empty.
        self.assertEqual(
            clamp_range("2026-09-20T08:00:00Z", "2026-09-22T08:00:00Z",
                        prompt_timestamp=PROMPT_TS),
            ("", ""))
        # An implausibly old START clamps up to prompt - 3650 days
        # (2026-09-10 minus 3650 calendar days lands on 2016-09-12 —
        # the leap days in the window).
        self.assertEqual(
            clamp_range("2010-01-01T00:00:00Z", "2020-01-01T00:00:00Z",
                        prompt_timestamp=PROMPT_TS),
            ("2016-09-12T12:00:00Z", "2020-01-01T00:00:00Z"))
        # BOTH endpoints below the floor collapse -> empty.
        self.assertEqual(
            clamp_range("2000-01-01T00:00:00Z", "2005-01-01T00:00:00Z",
                        prompt_timestamp=PROMPT_TS),
            ("", ""))
        # Malformed, inverted, and collapsed inputs never raise.
        self.assertEqual(
            clamp_range("not-a-date", "2026-09-10T00:00:00Z",
                        prompt_timestamp=PROMPT_TS), ("", ""))
        self.assertEqual(
            clamp_range("2026-09-10T00:00:00Z", "2026-09-09T00:00:00Z",
                        prompt_timestamp=PROMPT_TS), ("", ""))
        self.assertEqual(
            clamp_range("2026-09-10T00:00:00Z", "2026-09-10T00:00:00Z",
                        prompt_timestamp=PROMPT_TS), ("", ""))
        self.assertEqual(
            clamp_range("2026-09-09T00:00:00Z", "2026-09-10T00:00:00Z",
                        prompt_timestamp="garbage"), ("", ""))
        # The parser itself: unrecognized / malformed -> ("", "").
        self.assertEqual(parse_relative_range("sometime", prompt_timestamp=
                                              FIXED_NOW), ("", ""))
        self.assertEqual(parse_relative_range("today", prompt_timestamp=
                                              "not-a-timestamp"), ("", ""))


class OccurrenceWriteTest(unittest.TestCase):
    """Atomic occurrence writes + the update/fold transition matrix + the
    tombstone cleanup sites. Writer time is ALWAYS a fixed `now=` argument
    (never ZMEM_TEST_NOW)."""

    def setUp(self):
        _reset_store_file()
        self.conn = connect()
        _prepare_store(self.conn)
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def _occurrence(self, memory_id):
        row = self.conn.execute(
            "SELECT occurred_start, occurred_end, mentioned_at "
            "FROM memory_occurrence WHERE memory_id=?",
            (memory_id,)).fetchone()
        return tuple(row) if row is not None else None

    def _count(self, table, where="1=1", args=()):
        return self.conn.execute(
            "SELECT COUNT(*) FROM %s WHERE %s" % (table, where),
            args).fetchone()[0]

    def test_event_writes_occurrence_atomically(self):
        res = add_memory(
            self.conn, namespace=NS, type_="event",
            content="outage during last week",
            signal="test", now=FIXED_NOW,
            occurred_start="2026-09-01T08:00:00Z",
            occurred_end="2026-09-03T08:00:00Z",
            mentioned_at="2026-09-03T09:00:00Z")
        mid = str(res)
        self.assertFalse(res.deduped)
        self.assertEqual(
            self._occurrence(mid),
            ("2026-09-01T08:00:00Z", "2026-09-03T08:00:00Z",
             "2026-09-03T09:00:00Z"))
        # Empty mentioned_at defaults to the writer's fixed now.
        res2 = add_memory(
            self.conn, namespace=NS, type_="event",
            content="separate incident window",
            signal="test", now=FIXED_NOW,
            occurred_start="2026-09-02T08:00:00Z",
            occurred_end="2026-09-02T09:00:00Z")
        self.assertEqual(
            self._occurrence(str(res2)),
            ("2026-09-02T08:00:00Z", "2026-09-02T09:00:00Z", FIXED_NOW))
        # ATOMICITY: a malformed range refuses BEFORE any INSERT — no
        # partial memory row, no occurrence row.
        for bad_start, bad_end in (
            ("not-a-date", "2026-09-03T08:00:00Z"),
            ("2026-09-03T08:00:00Z", "2026-09-01T08:00:00Z"),  # reversed
            ("", "2026-09-03T08:00:00Z"),                       # missing
        ):
            with self.assertRaises(
                    ValueError,
                    msg="bad range %r..%r must raise" % (bad_start, bad_end)):
                add_memory(
                    self.conn, namespace=NS, type_="event",
                    content="atomicity probe %s" % bad_start,
                    signal="test", now=FIXED_NOW,
                    occurred_start=bad_start, occurred_end=bad_end)
        self.assertEqual(self._count("memory", "namespace=?", (NS,)), 2,
                         "a refused occurrence write must leave no partial "
                         "memory row")
        self.assertEqual(self._count("memory_occurrence"), 2)

    def test_non_event_rejects_occurrence_fields(self):
        # In-process: the writer's contract ValueError, before mutation.
        with self.assertRaises(ValueError) as caught:
            add_memory(
                self.conn, namespace=NS, type_="fact",
                content="plain fact with occurrence args",
                signal="test", now=FIXED_NOW,
                occurred_start="2026-09-01T08:00:00Z",
                occurred_end="2026-09-02T08:00:00Z")
        self.assertIn("occurrence fields require type=event",
                      str(caught.exception))
        self.assertEqual(self._count("memory", "namespace=?", (NS,)), 0)
        self.assertEqual(self._count("memory_occurrence"), 0)
        # CLI: exit 2 with NO row (argparse cross-field hook fires first).
        env = _cli_env()
        proc = subprocess.run(
            [sys.executable, str(STORE_PY), "add",
             "--namespace", NS, "--type", "fact",
             "--content", "cli fact with occurrence args",
             "--occurred-start", "2026-09-01T08:00:00Z",
             "--occurred-end", "2026-09-02T08:00:00Z"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=120)
        self.assertEqual(proc.returncode, 2,
                         "non-event occurrence args must exit 2 (stderr=%r)"
                         % proc.stderr[-300:])
        self.assertIn("occurred", proc.stderr)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM memory WHERE namespace=?", (NS,)).fetchone()
        self.assertEqual(rows[0], 0, "no row may exist after the refusal")

    def test_update_event_to_event_inherits_occurrence(self):
        res = add_memory(
            self.conn, namespace=NS, type_="event",
            content="event before a content edit",
            signal="test", now=FIXED_NOW,
            occurred_start="2026-09-01T08:00:00Z",
            occurred_end="2026-09-03T08:00:00Z",
            mentioned_at="2026-09-03T09:00:00Z")
        old_id = str(res)
        upd, created_new = update_memory(
            self.conn, mid=old_id,
            content="event after a content edit",
            now="2026-09-10T16:00:00Z")
        self.assertTrue(created_new)
        new_id = str(upd)
        self.assertNotEqual(new_id, old_id)
        # The replacement row INHERITS the exact occurrence triple; the old
        # id's side-table row is gone (an event never drops out of the arm).
        self.assertEqual(
            self._occurrence(new_id),
            ("2026-09-01T08:00:00Z", "2026-09-03T08:00:00Z",
             "2026-09-03T09:00:00Z"))
        self.assertIsNone(self._occurrence(old_id))
        old = self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?",
            (old_id,)).fetchone()
        self.assertIsNotNone(old[0])

    def test_update_event_to_non_event_cleans_occurrence(self):
        res = add_memory(
            self.conn, namespace=NS, type_="event",
            content="event becoming a fact",
            signal="test", now=FIXED_NOW,
            occurred_start="2026-09-01T08:00:00Z",
            occurred_end="2026-09-02T08:00:00Z")
        old_id = str(res)
        upd, created_new = update_memory(
            self.conn, mid=old_id, content="event becoming a fact",
            type_="fact", now="2026-09-10T16:00:00Z")
        self.assertTrue(created_new)
        self.assertIsNone(self._occurrence(old_id),
                          "the old id's occurrence must be cleaned")
        self.assertIsNone(self._occurrence(str(upd)),
                          "a fact replacement carries no occurrence")
        self.assertEqual(self._count("memory_occurrence"), 0)

    def test_update_non_event_to_event_refused(self):
        res = add_memory(
            self.conn, namespace=NS, type_="fact",
            content="fact that must not become an event here",
            signal="test", now=FIXED_NOW)
        mid = str(res)
        with self.assertRaises(ValueError) as caught:
            update_memory(
                self.conn, mid=mid, content="attempted event transition",
                type_="event", now="2026-09-10T16:00:00Z")
        self.assertIn("occurrence", str(caught.exception))
        # Nothing was written: the fact stays live, no occurrence row, and
        # the row count is unchanged.
        row = self.conn.execute(
            "SELECT superseded_at, type FROM memory WHERE id=?",
            (mid,)).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(row[1], "fact")
        self.assertEqual(self._count("memory", "namespace=?", (NS,)), 1)
        self.assertEqual(self._count("memory_occurrence"), 0)

    def test_dedup_fold_drops_occurrence_args_with_warning(self):
        first = add_memory(
            self.conn, namespace=NS, type_="event",
            content="duplicate outage narrative",
            signal="test", now=FIXED_NOW,
            occurred_start="2026-09-01T08:00:00Z",
            occurred_end="2026-09-02T08:00:00Z",
            mentioned_at="2026-09-02T09:00:00Z")
        keeper_id = str(first)
        # The re-add folds onto the keeper; its DIFFERENT occurrence args
        # are dropped with one warning and the keeper keeps ITS occurrence.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            second = add_memory(
                self.conn, namespace=NS, type_="event",
                content="duplicate outage narrative",
                signal="test", now=FIXED_NOW,
                occurred_start="2026-09-04T08:00:00Z",
                occurred_end="2026-09-05T08:00:00Z",
                mentioned_at="2026-09-05T09:00:00Z")
        self.assertTrue(second.deduped)
        self.assertEqual(str(second), keeper_id)
        self.assertIn("kept its own occurrence data", buf.getvalue())
        self.assertEqual(
            self._occurrence(keeper_id),
            ("2026-09-01T08:00:00Z", "2026-09-02T08:00:00Z",
             "2026-09-02T09:00:00Z"))
        self.assertEqual(self._count("memory_occurrence"), 1,
                         "the fold must never insert a second occurrence row")

    def test_supersede_and_consolidate_and_organize_clean_occurrence(self):
        # --- supersede: the occurrence dies with the parent tombstone. ---
        res = add_memory(
            self.conn, namespace=NS, type_="event",
            content="superseded event occurrence",
            signal="test", now=FIXED_NOW,
            occurred_start="2026-09-01T08:00:00Z",
            occurred_end="2026-09-02T08:00:00Z")
        self.assertIsNotNone(self._occurrence(str(res)))
        self.assertTrue(
            supersede_memory(self.conn, str(res), "test reason"))
        self.assertIsNone(self._occurrence(str(res)),
                          "supersede must clean memory_occurrence in the "
                          "same transaction")

        # --- consolidate absorb: the ABSORBED row's occurrence is cleaned. ---
        keeper = add_memory(
            self.conn, namespace=NS, type_="fact",
            content="cluster keeper narrative about rotating credentials",
            signal="test", now=FIXED_NOW)
        absorbed = add_memory(
            self.conn, namespace=NS, type_="event",
            content="cluster keeper narrative about rotating credentials now",
            signal="test", now=FIXED_NOW,
            occurred_start="2026-09-02T08:00:00Z",
            occurred_end="2026-09-02T10:00:00Z")
        keeper_row = self.conn.execute(
            "SELECT * FROM memory WHERE id=?", (str(keeper),)).fetchone()
        absorbed_row = self.conn.execute(
            "SELECT * FROM memory WHERE id=?", (str(absorbed),)).fetchone()
        self.assertIsNotNone(self._occurrence(str(absorbed)))
        _absorb_into_keeper(
            self.conn, keeper_row, absorbed_row, now="2026-09-10T17:00:00Z")
        self.conn.commit()
        self.assertIsNone(self._occurrence(str(absorbed)),
                          "the consolidate absorb must clean the absorbed "
                          "row's occurrence in the same transaction")
        self.assertIsNotNone(self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?",
            (str(absorbed),)).fetchone()[0])

        # --- organize stale-summary re-key: the tombstoned summary's
        # occurrence (here: an imported stray row keyed to a summary) is
        # cleaned with the tombstone. ---
        summary = add_memory(
            self.conn, namespace=NS, type_="fact",
            content="organize summary row for the topic guard",
            source_ref="organize:topicA", signal="test", now=FIXED_NOW)
        member_id = str(uuid.uuid4())
        self.conn.execute(
            "UPDATE memory SET merged_from=? WHERE id=?",
            (member_id, str(summary)))
        # A fact cannot carry an occurrence through the writer; insert the
        # stray directly (the imported-data shape the cleanup must cover).
        self.conn.execute(
            "INSERT INTO memory_occurrence (memory_id, occurred_start, "
            "occurred_end, mentioned_at) VALUES (?,?,?,?)",
            (str(summary), "2026-09-01T00:00:00Z", "2026-09-01T01:00:00Z",
             "2026-09-01T02:00:00Z"))
        self.conn.commit()
        self.assertIsNotNone(self._occurrence(str(summary)))
        _supersede_stale_summaries(
            self.conn, topic_key=member_id, keep_id="not-the-summary")
        self.conn.commit()
        self.assertIsNone(self._occurrence(str(summary)),
                          "organize's stale-summary tombstone must clean the "
                          "occurrence row in the same transaction")
        self.assertIsNotNone(self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?",
            (str(summary),)).fetchone()[0])


class TemporalArmTest(unittest.TestCase):
    """The capped temporal candidate arm over the seeded as-of fixture."""

    def setUp(self):
        rows = _read_asof_fixture()
        _seed_asof_store(rows)
        self.addCleanup(os.environ.pop, "ZMEM_ARM_CAP_TEMPORAL", None)
        self.conn = connect()
        self.addCleanup(self.conn.close)

    def test_last_weeks_outage_ranks_in_window_event_first(self):
        env = _recall_envelope(self.conn)
        ids = [r["id"] for r in env["results"]]
        self.assertIn(IN_WINDOW_ID, ids,
                      "the in-window event must be recalled")
        self.assertNotIn(SUPERSEDED_ID, ids,
                         "the superseded event must stay absent")
        # "event first": the temporal+FTS event outranks the FTS-only fact.
        self.assertEqual(ids[0], IN_WINDOW_ID,
                         "the in-window event must rank first, got %s" % ids)
        self.assertEqual(
            env["arms"]["temporal"],
            {"pre": 1, "post": 1, "cap": 4},
            "exactly one temporal candidate: the future event's occurrence "
            "cannot overlap 'last week' and the superseded one must not "
            "qualify (arms=%r)" % (env["arms"],))
        # The full five-arm accounting shape is present.
        self.assertEqual(set(env["arms"]),
                         {"fts", "vec", "entity", "graph", "temporal"})

    def test_default_and_env_cap_are_four_by_default(self):
        # Default: absent env -> cap 4 (pinned above via the seeded store).
        env = _recall_envelope(self.conn)
        self.assertEqual(env["arms"]["temporal"]["cap"], 4)
        # 0 disables the arm: post 0.
        os.environ["ZMEM_ARM_CAP_TEMPORAL"] = "0"
        env0 = _recall_envelope(self.conn)
        self.assertEqual(env0["arms"]["temporal"]["cap"], 0)
        self.assertEqual(env0["arms"]["temporal"]["post"], 0)
        # 2 leaves the single candidate: post 1.
        os.environ["ZMEM_ARM_CAP_TEMPORAL"] = "2"
        env2 = _recall_envelope(self.conn)
        self.assertEqual(env2["arms"]["temporal"]["cap"], 2)
        self.assertEqual(env2["arms"]["temporal"]["post"], 1)
        # Invalid text: exactly ONE warning on stderr, cap falls back to 4.
        os.environ["ZMEM_ARM_CAP_TEMPORAL"] = "not-a-number"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            env_bad = _recall_envelope(self.conn)
        self.assertEqual(env_bad["arms"]["temporal"]["cap"], 4)
        self.assertEqual(env_bad["arms"]["temporal"]["post"], 1)
        self.assertEqual(err.getvalue().count("ZMEM_ARM_CAP_TEMPORAL"), 1,
                         "exactly one invalid-cap warning, got %r"
                         % err.getvalue())

    def test_temporal_arm_is_additive_to_graph(self):
        # A graph fixture (the test_graph_seed_arm.py shape) PLUS one event
        # whose ONLY route into the pool is the temporal arm: its content
        # shares no query token, has no entity, and the vec lane is absent.
        hit = add_memory(
            self.conn, namespace=ARM_NS, type_="fact",
            content="ripgrep is a fast code search tool for large repos",
            tags="grapharm", signal="test", confidence=0.9,
            source_ref="session:178-graph-H", now=FIXED_NOW)
        target = add_memory(
            self.conn, namespace=ARM_NS, type_="fact",
            content="quarterly rotate the deployment credentials in the vault",
            tags="grapharm", signal="test", confidence=0.9,
            source_ref="session:178-graph-T", now=FIXED_NOW)
        lone_event = add_memory(
            self.conn, namespace=ARM_NS, type_="event",
            content="vendor maintenance window notification",
            tags="grapharm", signal="test", confidence=0.9,
            source_ref="session:178-graph-E", now=FIXED_NOW,
            occurred_start="2026-09-01T08:00:00Z",
            occurred_end="2026-09-02T08:00:00Z")
        hit_id, target_id, event_id = str(hit), str(target), str(lone_event)
        # Entity `ripgrep` (aliases ripgrep/rg) on the hit row + an edge to
        # the content-disjoint target — the graph arm's rescue shape.
        eid = str(uuid.uuid4())
        stamp = FIXED_NOW
        self.conn.execute(
            "INSERT INTO entity (id, kind, canonical_name, created_at,"
            " updated_at) VALUES (?,?,?,?,?)",
            (eid, "tool", "ripgrep", stamp, stamp))
        for alias in ("ripgrep", "rg"):
            self.conn.execute(
                "INSERT OR IGNORE INTO entity_alias (entity_id, alias_norm)"
                " VALUES (?,?)", (eid, _normalize_content(alias)))
        self.conn.execute(
            "INSERT OR IGNORE INTO memory_entity (memory_id, entity_id,"
            " role) VALUES (?,?,'mentions')", (hit_id, eid))
        self.conn.commit()
        add_link_pair(self.conn, hit_id, target_id, "related", score=0.85)

        query = "search code with rg from last week"
        env = _recall_envelope(self.conn, query=query)
        self.assertIn(target_id, env["candidate_ids"],
                      "the graph arm must still rescue the linked target")
        self.assertIn(event_id, env["candidate_ids"],
                      "the temporal-only event must enter through the arm")
        self.assertGreaterEqual(env["arms"]["graph"]["post"], 1)
        # Two temporal candidates: the fixture's in-window event plus the
        # lone event added above (both overlap "last week").
        self.assertEqual(env["arms"]["temporal"]["post"], 2)
        # Disabling the temporal arm removes ONLY its contribution — the
        # graph rescue is untouched (additivity, both directions).
        os.environ["ZMEM_ARM_CAP_TEMPORAL"] = "0"
        try:
            off = _recall_envelope(self.conn, query=query)
            self.assertNotIn(event_id, off["candidate_ids"],
                             "cap 0 must drop the temporal-only event")
            self.assertIn(target_id, off["candidate_ids"],
                          "the graph arm's rescue must survive the temporal "
                          "cap change")
            self.assertGreaterEqual(off["arms"]["graph"]["post"], 1)
            self.assertEqual(off["arms"]["temporal"]["post"], 0)
        finally:
            os.environ.pop("ZMEM_ARM_CAP_TEMPORAL", None)



class ScopedPromptTimestampTest(unittest.TestCase):
    """Reviewer round-2 Critical: the scoped recall/explain paths honor
    --prompt-timestamp (a divergent ZMEM_TEST_NOW must NOT win)."""

    def test_scoped_recall_and_explain_honor_prompt_timestamp(self):
        import subprocess
        scratch = tempfile.mkdtemp(prefix="zmem-178-scoped-")
        self.addCleanup(shutil.rmtree, scratch, True)
        env = dict(os.environ)
        for key in ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_TEST_NOW"):
            env.pop(key, None)
        env.update({
            "ZMEM_DATA": scratch,
            "ZMEM_STORE": os.path.join(scratch, "store.sqlite"),
            "ZMEM_MODELS_DIR": os.path.join(scratch, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            # Divergent clock: "last week" from THIS instant does NOT
            # overlap the seeded 2026-09-01..03 window.
            "ZMEM_TEST_NOW": "2026-10-18T12:00:00Z",
        })
        def run_cli(args):
            return subprocess.run(
                [sys.executable, str(STORE_PY), *args],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env, timeout=120, check=False,
                cwd=str(REPO_ROOT))
        implicit_ns = "project:github.com/zaxbysauce/zmem"
        run_cli(["init"])
        add = run_cli([
            "add", "--type", "event",
            "--namespace", implicit_ns,
            "--content", "outage during last week at the scoped cluster",
            "--confidence", "0.9", "--signal", "user",
            "--occurred-start", "2026-09-01T08:00:00Z",
            "--occurred-end", "2026-09-03T08:00:00Z",
            "--mentioned-at", "2026-09-03T09:00:00Z"])
        self.assertEqual(add.returncode, 0, add.stderr)
        # NO --namespace on the recall side: the CLI's implicit-scope lane
        # (args.namespace is None) is the path _scoped_tier_pools serves —
        # the round-3 finding. cwd=REPO_ROOT resolves the implicit scope to
        # this repository's project namespace, so seed into it explicitly.
        scoped_args = [
            "recall", "--query", "last week outage scoped cluster",
            "--prompt-timestamp", "2026-09-10T12:00:00Z",
            "--json"]
        result = run_cli(scoped_args)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(result.stdout)
        arms = doc.get("arms", {})
        self.assertGreaterEqual(
            arms.get("temporal", {}).get("pre", 0), 1,
            f"scoped recall dropped --prompt-timestamp: {arms}")
        explain = run_cli(["recall", *scoped_args[1:], "--explain"])
        self.assertEqual(explain.returncode, 0, explain.stderr)
        edoc = json.loads(explain.stdout)
        earms = (edoc.get("explain", {}) or {}).get("arms", edoc.get("arms", {}))
        self.assertGreaterEqual(
            (earms.get("temporal", {}) or {}).get("pre", 0), 1,
            f"scoped explain dropped --prompt-timestamp: {earms}")


class GraphTemporalOverlapTest(unittest.TestCase):
    """Reviewer round-2: a row arriving via BOTH graph and temporal is not
    graph-arrival-only (bump-eligible, not link-rescued)."""

    def test_graph_and_temporal_row_is_not_graph_arrival_only(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib.schema import connect, _prepare_store
        from storelib.write import add_memory
        import storelib.recall as recall_mod
        scratch = tempfile.mkdtemp(prefix="zmem-178-overlap-")
        self.addCleanup(shutil.rmtree, scratch, True)
        os.environ["ZMEM_STORE"] = os.path.join(scratch, "store.sqlite")
        os.environ["ZMEM_DATA"] = scratch
        for key in ("ZMEM_TEST_NOW",):
            os.environ.pop(key, None)
        conn = connect()
        _prepare_store(conn)
        # Seed a seed row + an overlapping event row sharing its entity so
        # the graph arm rescues the event while the temporal arm also
        # selects it.
        seed_row = add_memory(conn, namespace="project:overlap",
                   type_="fact", content="the alpha clustermaintenance ledger",
                   tags="entity:project:alpha", confidence=0.9,
                   signal="user", now="2026-09-01T00:00:00Z")
        # The event shares NO query term and NO entity tag with the query:
        # it arrives ONLY via the planted graph hop and the temporal window,
        # so the graph_only_ids subtraction is the deciding line (a query
        # term or entity hit would mask it — the round-3 vacuity).
        event_row = add_memory(conn, namespace="project:overlap",
                   type_="event",
                   content="unplanned downtime window at the facility",
                   tags="", confidence=0.9,
                   signal="user", now="2026-09-05T00:00:00Z",
                   occurred_start="2026-09-01T08:00:00Z",
                   occurred_end="2026-09-03T08:00:00Z")
        # Plant a real graph edge seed->event so the graph arm RESCUES the
        # event row through one hop (the round-3 finding: without a graph
        # arrival the subtraction is untested — graph_ids was empty).
        conn.execute(
            "INSERT OR REPLACE INTO memory_link "
            "(src_id, dst_id, relation, score, created_at) "
            "VALUES (?,?,?,?,?)",
            (str(seed_row), str(event_row), "related", 0.9,
             "2026-09-05T00:00:00Z"))
        conn.commit()
        saved_env = {k: os.environ.get(k) for k in
                     ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_TEST_NOW")}
        try:
            rows = recall_mod.recall_memory(
                conn, query="alpha clustermaintenance ledger last week outage",
                namespace="project:overlap", limit=5,
                prompt_timestamp="2026-09-10T12:00:00Z",
                no_telemetry=True)
        finally:
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertTrue(rows, "no rows recalled")
        overlap = [r for r in rows
                   if "unplanned downtime window" in (r.get("content") or "")]
        self.assertTrue(overlap, "the graph+temporal event row is absent")
        row = overlap[0]
        self.assertIs(
            row.get("_graph_arrival_only"), False,
            f"row misclassified graph-arrival-only: the graph+temporal "
            f"overlap must stay bump-eligible "
            f"(_graph_arrival_only={row.get('_graph_arrival_only')})")
        conn.close()



class TemporalClampWindowTest(unittest.TestCase):
    """Final-critic round-1 Critical: the arm applies clamp_range after
    parse_relative_range (Design 2) — future phrases collapse to an empty
    arm and present-period windows truncate at the prompt instant."""

    def test_future_phrase_yields_empty_arm_and_present_period_truncates(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        import storelib.recall as recall_mod
        from storelib import temporal as temporal_mod
        self.assertEqual(
            recall_mod._temporal_window("what happened tomorrow",
                                        "2026-09-10T12:00:00Z"),
            ("", ""),
            "future phrase must clamp-collapse to an empty window")
        self.assertEqual(
            recall_mod._temporal_window("in two days",
                                        "2026-09-10T12:00:00Z"),
            ("", ""))
        start, end = recall_mod._temporal_window(
            "this week", "2026-09-10T12:00:00Z")
        self.assertEqual(start, "2026-09-07T00:00:00Z")
        self.assertEqual(end, "2026-09-10T12:00:00Z",
                         "present-period window must truncate at the prompt")
        # The raw parse (pre-clamp) genuinely differs for this phrase —
        # proving the clamp is the deciding step.
        raw_start, raw_end = temporal_mod.parse_relative_range(
            "this week", prompt_timestamp="2026-09-10T12:00:00Z")
        self.assertEqual(raw_end, "2026-09-14T00:00:00Z")


class TemporalReviewRegressionTest(unittest.TestCase):
    """One discriminating pin per v18 review-round fix. Each fails when its
    fix is reverted — the property the original 20-phrase suite lacked."""

    def test_word_boundary_match_rejects_superstrings(self):
        """CL2-07: "this yearly planning cycle" contains "this year" as a
        SUBSTRING but is not the phrase — the arm must stay empty.
        Reverts if: the match goes back to `phrase in text`."""
        sys.path.insert(0, str(SCRIPTS_DIR))
        import storelib.recall as recall_mod
        self.assertEqual(
            recall_mod._temporal_window("notes from this yearly planning "
                                        "cycle", "2026-09-10T12:00:00Z"),
            ("", ""), "superstring false-activated the arm")
        self.assertEqual(
            recall_mod._temporal_window("the next weekly sync notes",
                                        "2026-09-10T12:00:00Z"),
            ("", ""))
        # The real phrases still match through the same path.
        self.assertNotEqual(
            recall_mod._temporal_window("this year", "2026-09-10T12:00:00Z"),
            ("", ""))

    def test_prompt_timestamp_is_normalized_at_entry(self):
        """CL2-05: a programmatic caller passing a valid non-canonical
        ISO-8601 instant still activates the arm (PRR-022 discipline).
        Reverts if: the raw string flows into temporal._parse_ts, which
        accepts only the exact Z form."""
        sys.path.insert(0, str(SCRIPTS_DIR))
        import storelib.recall as recall_mod
        canonical = recall_mod._temporal_window(
            "last week", "2026-09-10T12:00:00Z")
        for variant in ("2026-09-10T12:00:00+00:00",
                        "2026-09-10 12:00:00",
                        "2026-09-10T14:00:00+02:00"):
            self.assertEqual(
                recall_mod._temporal_window("last week", variant),
                canonical,
                "non-canonical prompt timestamp silently emptied the arm")

    def test_extreme_prompt_timestamp_never_raises(self):
        """CL2-15: the documented never-raises contract holds at the
        boundary years the CLI validator accepts.
        Reverts if: the inner body is un-wrapped again."""
        sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib import temporal as temporal_mod
        for phrase in ("this year", "tomorrow", "last week", "past 30 days"):
            for prompt in ("9999-12-31T23:59:59Z", "0001-01-01T00:00:00Z"):
                try:
                    start, end = temporal_mod.parse_relative_range(
                        phrase, prompt_timestamp=prompt)
                except (OverflowError, ValueError) as exc:
                    self.fail("parse_relative_range(%r, %r) raised %s"
                              % (phrase, prompt, exc))
                self.assertIsInstance(start, str)
                self.assertIsInstance(end, str)

    def test_update_fold_prints_the_occurrence_discard(self):
        """CL2-03: the update dedup-fold must print the occurrence notice —
        the PR's own disclosed contract ("dedup folds drop args with a
        visible warning") and the add-path fold precedent. The fold targets
        a keeper that already carries its own occurrence, so the keeper-wins
        branch fires. Reverts if: the fold returns before the occurrence
        block again (the silent-discard defect)."""
        import subprocess
        scratch = tempfile.mkdtemp(prefix="zmem-178-foldpin-")
        self.addCleanup(shutil.rmtree, scratch, True)
        env = dict(os.environ)
        for key in ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                    "ZMEM_MODEL_AUTODOWNLOAD"):
            env.pop(key, None)
        env.update({
            "ZMEM_DATA": scratch,
            "ZMEM_STORE": os.path.join(scratch, "store.sqlite"),
            "ZMEM_MODELS_DIR": os.path.join(scratch, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })

        def run_cli(args):
            return subprocess.run(
                [sys.executable, str(STORE_PY), *args],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env, timeout=120, check=False,
                cwd=str(REPO_ROOT))

        run_cli(["init"])
        ns = "project:fold-pin"
        add1 = run_cli([
            "add", "--type", "event", "--namespace", ns,
            "--content", "outage during last week at the fold cluster",
            "--occurred-start", "2026-09-01T08:00:00Z",
            "--occurred-end", "2026-09-03T08:00:00Z"])
        self.assertEqual(add1.returncode, 0, add1.stderr)
        add2 = run_cli([
            "add", "--type", "event", "--namespace", ns,
            "--content", "unrelated seed so the second event stands alone",
            "--occurred-start", "2026-10-01T08:00:00Z",
            "--occurred-end", "2026-10-03T08:00:00Z"])
        self.assertEqual(add2.returncode, 0, add2.stderr)
        ev2 = re.search(r"added memory ([0-9a-f-]{36})", add2.stdout).group(1)
        # Updating ev2's content to ev1's exact text folds ev2 into ev1
        # (similarity 1.000 >= 0.85); the keeper already carries its own
        # occurrence, so the keeper-wins warning must fire.
        upd = run_cli([
            "update", "--id", ev2, "--namespace", ns,
            "--content", "outage during last week at the fold cluster"])
        self.assertEqual(upd.returncode, 0, upd.stderr)
        combined = upd.stdout + upd.stderr
        self.assertIn("merged into existing live memory", combined,
                      "the update did not fold at all — pin is vacuous")
        self.assertIn("kept its own occurrence data", combined,
                      "fold discarded the inherited occurrence SILENTLY: "
                      + combined[-400:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
