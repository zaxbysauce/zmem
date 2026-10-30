"""AC2 (#414-2): the hub snapshot payload must carry memory_expiry so
offline serve withholds EXPIRED state rows (frozen acceptance check C2).

Contract this check pins (issue #414 item 2): the hub `snapshot` payload
gains an ADDITIVE key ``"expiry"`` — a list of
``[memory_id, inject_until, expiry_class]`` triplets for in-scope live
state rows — and ``fleet/snapshot.py::_build_from_payload`` restores those
records into the materialized snapshot's ``memory_expiry``.

Legs:
1. withhold — ONE live state row whose inject_until is PAST, its record in
   ``payload["expiry"]``: after ``_build_from_payload`` +
   ``_serve_offline_read`` the row is NOT served and the reason is not
   ``injected``.  At base the unknown payload key is ignored, the side
   table reads empty and the row passes as live -> RED.
2. serve-valid — the same row with a FUTURE inject_until plus its record
   IS served (guards against an over-broad withhold).
3. absence tolerance — a payload WITHOUT the expiry key still builds and
   serves the live row (older producers carry no such key).

Row construction follows the executed probe (by_name dict over
``snap._SNAPSHOT_COLUMNS``, NOT-NULL-safe values).  Isolation: env pinned
to a per-run temp dir BEFORE any fleet/storelib import; sys.modules purged
(tests/test_t11_offline_snapshot*.py discipline); no network; no models.

Runs under pytest and standalone (CI convention):
``python -X utf8 tests/test_snapshot_expiry_parity.py``
"""

from __future__ import annotations
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

CONTENT = "zmem c2 check: endpoint api-c2.example.com rate limit is 100 rps"
PAST = "2026-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"

# -- env pinned BEFORE any fleet/storelib import (and before the issue-#244
# store isolation guard loads, which reads the ambient env at import time).
_TMP = tempfile.mkdtemp(prefix="zmem-c2-snap-")
os.environ.update({
    "ZMEM_HOME": str(REPO_ROOT),
    "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
    "ZMEM_DATA": _TMP,
    "ZMEM_MODELS_DIR": os.path.join(_TMP, "missing-models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
    "ZMEM_NAMESPACE": "user:global",
    "ZMEM_MCP_TOKEN": "HUB_TOKEN",
})
for _k in ("ZMEM_BACKUP_DIR", "ZMEM_HOME_EXTRA", "CLAUDE_PLUGIN_DATA",
           "ZCODE_PLUGIN_DATA", "ZMEM_MCP_TOKENS_FILE"):
    os.environ.pop(_k, None)
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

_SAVED_MODULES = {key: value for key, value in list(sys.modules.items())
                  if key in ("fleet", "storelib", "host")
                  or key.startswith(("fleet.", "storelib.", "host."))}
for _name in list(_SAVED_MODULES):
    del sys.modules[_name]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
    _PATH_INSERTED = True
else:
    _PATH_INSERTED = False
_PINNED_KEYS = ("ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING",
                "ZMEM_NAMESPACE", "ZMEM_MCP_TOKEN", "ZMEM_BACKUP_DIR",
                "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
                "ZMEM_MCP_TOKENS_FILE")
_SAVED_ENV = {key: os.environ.get(key) for key in _PINNED_KEYS}

import fleet.snapshot as snap  # noqa: E402


def tearDownModule():  # noqa: N802 - unittest hook
    """Restore the ambient env / module cache (pytest co-run citizenship;
    the tests/test_t11_offline_snapshot*.py discipline)."""
    for key in [key for key in list(sys.modules)
                if key in ("fleet", "storelib", "host")
                or key.startswith(("fleet.", "storelib.", "host."))]:
        del sys.modules[key]
    sys.modules.update(_SAVED_MODULES)
    if _PATH_INSERTED:
        try:
            sys.path.remove(str(SCRIPTS_DIR))
        except ValueError:  # pragma: no cover - defensive
            pass
    for key, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(_TMP, ignore_errors=True)


def _row_values():
    """The hub payload's single live state row (NOT-NULL-safe values)."""
    return {
        "id": uuid.uuid4().hex,
        "namespace": "user:global",
        "type": "state",
        "content": CONTENT,
        "tags": "c2",
        "source_ref": "c2",
        "confidence": 0.9,
        "signal": "test",
        "valid_from": "2026-10-30T00:00:00Z",
        "valid_until": FUTURE,
        "update_of": "",
        "taint": "trusted_internal",
        "ingestion_ts": "2026-10-30T00:00:00Z",
        "superseded_at": None,
        "supersede_reason": None,
        "merged_from": None,
        "trust_score": 0.5,
        "applied_count": 0,
        "violated_count": 0,
        "embedding": None,
        "embedding_model": None,
        "retrieval_count": 0,
        "last_retrieved": None,
        "surfaced_count": 0,
        "last_surfaced": None,
    }


def _payload(row_id: str, inject_until: str | None) -> dict:
    by_name = _row_values()
    by_name["id"] = row_id
    row = []
    for col in (c.strip() for c in snap._SNAPSHOT_COLUMNS.split(",")):
        if col not in by_name:
            raise SystemExit(f"column gap: {col!r} not in row dict")
        row.append(by_name[col])
    payload = {
        "ok": True,
        "principal": "hub-principal",
        "columns": snap._SNAPSHOT_COLUMNS,
        "embedding_b64": True,
        "embed_profile": "minilm",
        "rows": [row],
        "revocations": [],
        "snapshot_revision": 3,
        "payload_bytes": 64,
    }
    if inject_until is not None:
        # The ADDITIVE expiry family the fix ships: triplets of
        # [memory_id, inject_until, expiry_class].
        payload["expiry"] = [[row_id, inject_until, "endpoint"]]
    return payload


class SnapshotExpiryParityTest(unittest.TestCase):
    """_build_from_payload restores payload expiry; serve honors it."""

    def _serve(self, payload):
        tmp = tempfile.mkdtemp(prefix="zmem-c2-leg-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = dict(os.environ)
        env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
        env["ZMEM_DATA"] = tmp
        snap._build_from_payload(payload, env)
        return snap._serve_offline_read(
            env, query="rate limit", namespace="user:global",
            moment="user_prompt", session_id="c2-check", lane=None)

    def _row_served(self, envelope) -> bool:
        return any("api-c2.example.com" in str(r.get("content", ""))
                   for r in (envelope or {}).get("results", []))

    def test_leg1_expired_state_row_withheld_offline(self):
        row_id = uuid.uuid4().hex
        envelope, state = self._serve(_payload(row_id, PAST))
        self.assertEqual(state, "valid",
                         "leg 1: the materialized snapshot must be valid")
        self.assertFalse(
            self._row_served(envelope),
            "leg 1: EXPIRED state row (inject_until="
            f"{PAST}, record carried in payload['expiry']) was injected "
            "offline — _build_from_payload must restore the expiry record "
            "so the selector withholds it")
        self.assertNotEqual(
            (envelope or {}).get("reason"), "injected",
            "leg 1: the offline envelope claims reason=injected for an "
            "expired state row")

    def test_leg2_live_state_row_with_record_served(self):
        row_id = uuid.uuid4().hex
        envelope, state = self._serve(_payload(row_id, FUTURE))
        self.assertEqual(state, "valid")
        self.assertTrue(
            self._row_served(envelope),
            "leg 2: a live state row with a FUTURE inject_until and its "
            "expiry record present must still be served (no over-broad "
            "withhold)")

    def test_leg3_payload_without_expiry_key_still_serves(self):
        row_id = uuid.uuid4().hex
        envelope, state = self._serve(_payload(row_id, None))
        self.assertEqual(state, "valid",
                         "leg 3: an older producer's payload (no expiry "
                         "key) must still build a valid snapshot")
        self.assertTrue(
            self._row_served(envelope),
            "leg 3: an older producer's payload without the expiry key "
            "must still serve the live row")


if __name__ == "__main__":
    unittest.main(verbosity=2)
