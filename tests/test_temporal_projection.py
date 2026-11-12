"""Issue #389 item 2 (collated trace 248-389-temporal-asof-integrity):
the temporal recall arm projects the occurrence triple onto recalled
event rows.

Pins:
  A. TRIPLE ON TEMPORAL ROWS — recall --json rows reached via the
     temporal arm carry occurred_start / occurred_end / mentioned_at
     (WHEN the event happened) with the stored values.
  B. STRIP-KEY ADDITIVITY — stripping the three keys leaves exactly the
     pre-change row key set: nothing else was added, removed, or renamed.
  C. NON-EVENT / NON-TEMPORAL ROWS — the three keys appear on NO other
     row.
  D. DUAL-SOURCE ROW — a row reached by BOTH the fts arm and the temporal
     arm still carries the triple.
  E. NORMAL-JSON LANE — the plain (non-injection) envelope carries the
     same three keys on temporal rows.

Run: python tests/test_temporal_projection.py   (no pytest — repo
convention)
"""

from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(REPO_ROOT / "tests"))

_TMP = tempfile.mkdtemp(prefix="zmem-389-projection-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ["ZMEM_DATA"] = _TMP
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_TMP, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ["ZMEM_LINK_THRESHOLD"] = "1.01"
os.environ.setdefault("ZMEM_EMBED_PROFILE", "minilm")
for _k in ("ZMEM_TEST_NOW", "ZMEM_ARM_CAP_TEMPORAL"):
    os.environ.pop(_k, None)

import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)
sys.path.insert(0, str(SCRIPTS_DIR))

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import unittest  # noqa: E402

import storelib.recall as recall_mod  # noqa: E402
from storelib.schema import _prepare_store, connect  # noqa: E402
from storelib.write import add_memory  # noqa: E402

NS = "project:occ389proj"
PROMPT_TS = "2026-09-10T15:30:00Z"
QUERY = "outage during last week"
TRIPLE = ("2026-09-01T08:00:00Z", "2026-09-03T08:00:00Z",
          "2026-09-03T09:00:00Z")
OCC_KEYS = ("occurred_start", "occurred_end", "mentioned_at")
# The pre-change injection-lane row key set (captured at base 1e05cc49;
# the evidence_* keys attach only on the normal JSON lane, entities on
# both).
BASE_ROW_KEYS = {
    "id", "namespace", "type", "content", "tags", "confidence", "signal",
    "source_ref", "valid_from", "valid_until", "update_of", "taint",
    "applied_count", "violated_count", "stale", "prompt_injection_risk",
    "trust_score", "entities",
    "_score", "_stale_note", "_rel_lex", "_rel_cos", "_rel_ent",
    "_rel_graph", "_graph_arrival_only", "_temporal_arrival_only",
}


def _reset():
    base = os.environ["ZMEM_STORE"]
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(base + suffix)
        except OSError:
            pass
    conn = connect()
    _prepare_store(conn)
    return conn


def _recall(conn, query=QUERY, **kw):
    os.environ["ZMEM_TEST_NOW"] = PROMPT_TS
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            recall_mod.recall_memory(
                conn, query=query, namespace=NS, as_json=True,
                no_bump=True, no_telemetry=True, for_injection=True,
                prompt_timestamp=PROMPT_TS, **kw)
    finally:
        os.environ.pop("ZMEM_TEST_NOW", None)
    return json.loads(buf.getvalue())


def _recall_normal(conn, query=QUERY):
    os.environ["ZMEM_TEST_NOW"] = PROMPT_TS
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            recall_mod.recall_memory(
                conn, query=query, namespace=NS, as_json=True,
                no_bump=True, no_telemetry=True,
                prompt_timestamp=PROMPT_TS)
    finally:
        os.environ.pop("ZMEM_TEST_NOW", None)
    return json.loads(buf.getvalue())


class TemporalProjectionTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset()
        self.addCleanup(self._close)
        with contextlib.redirect_stdout(io.StringIO()):
            self.event_id = str(add_memory(
                self.conn, namespace=NS, type_="event", content=QUERY,
                signal="test", now=TRIPLE[2],
                occurred_start=TRIPLE[0], occurred_end=TRIPLE[1],
                mentioned_at=TRIPLE[2]))
            self.fact_id = str(add_memory(
                self.conn, namespace=NS, type_="fact",
                content="unrelated fact about harbor cranes",
                signal="test", now=TRIPLE[2]))

    def _close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def test_temporal_row_carries_occurrence_triple(self):
        env = _recall(self.conn)
        rows = [r for r in env["results"] if r["id"] == self.event_id]
        self.assertTrue(rows, "the event must recall via the temporal arm")
        self.assertGreaterEqual(
            env.get("arms", {}).get("temporal", {}).get("post", 0), 1)
        row = rows[0]
        self.assertEqual(
            (row.get("occurred_start"), row.get("occurred_end"),
             row.get("mentioned_at")), TRIPLE)

    def test_strip_key_additivity(self):
        env = _recall(self.conn)
        row = [r for r in env["results"] if r["id"] == self.event_id][0]
        stripped = {k: v for k, v in row.items() if k not in OCC_KEYS}
        self.assertEqual(
            set(stripped), BASE_ROW_KEYS,
            "stripping the three occurrence keys must leave EXACTLY the "
            "pre-change key set — purely additive projection")

    def test_non_temporal_rows_carry_no_occurrence_keys(self):
        env = _recall(self.conn, query="harbor cranes")
        rows = [r for r in env["results"] if r["id"] == self.fact_id]
        self.assertTrue(rows, "the fact must recall by content")
        for k in OCC_KEYS:
            self.assertNotIn(k, rows[0],
                             "non-temporal rows must not carry %s" % k)

    def test_dual_source_row_carries_triple(self):
        # The event's content ALSO matches the query tokens, so the row is
        # reached by BOTH the fts arm and the temporal arm.
        env = _recall(self.conn)
        row = [r for r in env["results"] if r["id"] == self.event_id][0]
        self.assertEqual(
            (row.get("occurred_start"), row.get("occurred_end"),
             row.get("mentioned_at")), TRIPLE)

    def test_normal_json_lane_carries_triple(self):
        env = _recall_normal(self.conn)
        rows = [r for r in env["results"] if r["id"] == self.event_id]
        self.assertTrue(rows)
        row = rows[0]
        self.assertEqual(
            (row.get("occurred_start"), row.get("occurred_end"),
             row.get("mentioned_at")), TRIPLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
