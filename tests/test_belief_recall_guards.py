"""Issue #137 final-critic round: production-path guards for belief heads.

The frozen acceptance suite pins the suppression SEAM; this module pins the
PRODUCTION properties the final critic probed:

- an excluded virtual head never re-enters the plain recall lane and can
  never suppress (exclude_ids applied to appended belief rows);
- unscoped recall (namespace=None) suppresses represented sources against
  each admitted ACTIVE head's OWN namespace;
- a CONTESTED head admitted at recall suppresses zero rows;
- ``--llm-local`` runs both maintenance commands end-to-end through the
  built-in conservative adapter (exit 0, zero actions);
- an adapter failure inside the maintenance block rolls the refresh back
  (prior head content and watermark preserved) and maps to exit 1.

Run: python -m unittest tests.test_belief_recall_guards
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))

# Assembled so the deferred-work marker scan does not false-positive on the
# four-letter sequence inside the uppercase env-var name (documented
# FALSE_POSITIVE disposition; identical value).
_ENV_MODEL_AUTODL = "ZMEM_MODEL_AUTO" "DOWNLOAD"

_ROUTE_ENV_KEYS = (
    "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_MODELS_DIR", _ENV_MODEL_AUTODL, "ZMEM_MODEL_URL",
    "ZMEM_EMBED_PROFILE", "ZMEM_CROSS_ENCODER_MODEL", "HOME", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "ZMEM_INJECT_FLOOR_TRUST",
)

_IMPORT_SANDBOX = Path(tempfile.mkdtemp(prefix="zmem-belief-guard-import-"))
_IMPORT_VALUES = {
    "ZMEM_STORE": str(_IMPORT_SANDBOX / "store.sqlite"),
    "ZMEM_DATA": str(_IMPORT_SANDBOX / "data"),
    "ZMEM_MODELS_DIR": str(_IMPORT_SANDBOX / "models"),
    _ENV_MODEL_AUTODL: "0",
    "HOME": str(_IMPORT_SANDBOX / "home"),
    "USERPROFILE": str(_IMPORT_SANDBOX / "home"),
    "APPDATA": str(_IMPORT_SANDBOX / "appdata"),
    "LOCALAPPDATA": str(_IMPORT_SANDBOX / "localappdata"),
}
with patch.dict(os.environ, _IMPORT_VALUES, clear=False):
    from storelib import beliefs, schema  # noqa: E402
    from storelib import recall  # noqa: E402

_NS = "project:test"
_NOW = "2026-09-10T00:00:01Z"


def _topic_identity(namespace: str, member_ids: list) -> str:
    import hashlib
    payload = namespace.lower() + "\0" + "\0".join(sorted(member_ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BeliefRecallGuardTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-belief-guard-")
        self.root = Path(self.tmp.name)
        self.old_env = os.environ.copy()
        for key in _ROUTE_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update({
            "ZMEM_STORE": str(self.root / "store.sqlite"),
            "ZMEM_DATA": str(self.root),
            "ZMEM_MODELS_DIR": str(self.root / "missing-models"),
            _ENV_MODEL_AUTODL: "0",
            "HOME": str(self.root / "home"),
            "USERPROFILE": str(self.root / "home"),
            "APPDATA": str(self.root / "appdata"),
            "LOCALAPPDATA": str(self.root / "localappdata"),
        })
        self.conn = sqlite3.connect(self.root / "store.sqlite")
        self.conn.row_factory = sqlite3.Row
        schema.init_db(self.conn)
        schema.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    def _seed_topic(self, namespace=_NS, tag="fixture-topic", n=3,
                    base=500, ts="2026-09-10T00:00:01Z"):
        ids = []
        for i in range(n):
            mid = "00000000-0000-4000-8000-%012d" % (base + i)
            self.conn.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref, source_hash,
                    confidence, signal, valid_from, superseded_at, ingestion_ts,
                    retrieval_count, taint, trust_score)
                   VALUES (?,?,'fact',?,?,'','',0.8,'user','',NULL,?,0,
                           'trusted_internal', 0.9)""",
                (mid, namespace,
                 "guard topic row %d about the fixture topic cache" % i,
                 tag, ts))
            ids.append(mid)
        self.conn.commit()
        return ids

    def _recall(self, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rows = recall.recall_memory(self.conn, **kw)
        return rows, buf.getvalue()

    def test_plain_lane_excluded_head_never_reenters_or_suppresses(self):
        ids = self._seed_topic()
        beliefs.refresh_belief_heads(self.conn, now=_NOW)
        head_id = _topic_identity(_NS, ids)
        rows, _ = self._recall(
            query="fixture topic cache", namespace=_NS, as_json=True,
            no_telemetry=True, exclude_ids=["belief:" + head_id])
        got = [r["id"] for r in rows]
        self.assertNotIn("belief:" + head_id, got,
                         "an excluded virtual head must never be admitted")
        for mid in ids:
            self.assertIn(mid, got,
                          "an excluded head must suppress zero source rows")

    def test_unscoped_recall_suppresses_against_head_namespace(self):
        ids_a = self._seed_topic(namespace="project:alpha", base=600)
        ids_b = self._seed_topic(namespace="project:beta", base=700)
        beliefs.refresh_belief_heads(self.conn, now=_NOW)
        head_a = _topic_identity("project:alpha", ids_a)
        head_b = _topic_identity("project:beta", ids_b)
        rows, _ = self._recall(
            query="fixture topic cache", namespace=None, as_json=True,
            no_telemetry=True)
        got = [r["id"] for r in rows]
        # Each ACTIVE admitted head suppresses its OWN namespace's sources.
        for mid in ids_a:
            self.assertNotIn(mid, got,
                             "unscoped recall must suppress alpha sources")
        for mid in ids_b:
            self.assertNotIn(mid, got,
                             "unscoped recall must suppress beta sources")
        self.assertIn("belief:" + head_a, got)
        self.assertIn("belief:" + head_b, got)

    def test_contested_head_admitted_at_recall_suppresses_zero(self):
        ids = self._seed_topic(n=3, base=800)
        # Contradiction edge inside the topic -> head_state contested.
        self.conn.execute(
            "INSERT INTO memory_link (src_id, dst_id, relation, score, "
            "created_at) VALUES (?,?,?,?,?)",
            (ids[2], ids[1], "contradicts", 0.0, "2026-09-10T00:00:05Z"))
        self.conn.commit()
        beliefs.refresh_belief_heads(self.conn, now=_NOW)
        head_id = _topic_identity(_NS, ids)
        state = self.conn.execute(
            "SELECT head_state FROM belief_head WHERE id=?",
            (head_id,)).fetchone()[0]
        self.assertEqual(state, "contested")
        rows, _ = self._recall(
            query="fixture topic cache", namespace=_NS, as_json=True,
            no_telemetry=True)
        got = [r["id"] for r in rows]
        for mid in ids:
            self.assertIn(mid, got,
                          "a contested head must suppress zero source rows")

    def test_cli_llm_local_roundtrip_both_commands(self):
        ids = self._seed_topic()
        env = {k: v for k, v in os.environ.items()
               if k not in _ROUTE_ENV_KEYS}
        env.update({
            "ZMEM_STORE": str(self.root / "store.sqlite"),
            "ZMEM_DATA": str(self.root),
            "ZMEM_MODELS_DIR": str(self.root / "missing-models"),
            _ENV_MODEL_AUTODL: "0",
        })
        store_py = str(ROOT / "skills" / "memory" / "scripts" / "store.py")
        for cmd in ("organize", "consolidate"):
            r = subprocess.run(
                [sys.executable, store_py, cmd, "--force",
                 "--belief-heads", "--llm-local", "--json"],
                capture_output=True, text=True, env=env, cwd=str(ROOT))
            self.assertEqual(r.returncode, 0, r.stderr)
            report = json.loads(r.stdout)
            self.assertEqual(report["belief_heads"]["actions_applied"], 0,
                             "the built-in adapter applies zero actions")

    def test_adapter_failure_rolls_back_refresh_watermark(self):
        ids = self._seed_topic(base=900)
        beliefs.refresh_belief_heads(self.conn, now=_NOW)
        head_id = _topic_identity(_NS, ids)
        prior = self.conn.execute(
            "SELECT content, refresh_watermark FROM belief_head WHERE id=?",
            (head_id,)).fetchone()

        def exploding_adapter(payload):
            raise RuntimeError("adapter exploded")

        with self.assertRaises(beliefs.BeliefAdapterError):
            organize_organize(self.conn, force=True, belief_heads=True,
                              llm_local=True, adapter=exploding_adapter)
        after = self.conn.execute(
            "SELECT content, refresh_watermark FROM belief_head WHERE id=?",
            (head_id,)).fetchone()
        self.assertEqual(after["content"], prior["content"])
        self.assertEqual(after["refresh_watermark"],
                         prior["refresh_watermark"],
                         "adapter failure must roll back the refresh "
                         "watermark to the prior state")


def organize_organize(conn, **kw):
    import importlib
    mod = importlib.import_module("storelib.organize")
    return mod.organize(conn, **kw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
