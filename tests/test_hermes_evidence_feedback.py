"""Issue #164 PR-review feedback round (run pr302-20261010): regression pins
for the swarm-pr-review findings on PR #302.

Complements tests/test_hermes_evidence.py with the review-validated gaps:
the real flagged-success CLI contract (exit 0 + byte-exact envelope + SQL row
with unredacted digests), the export→ingest JSONL round trip, admission-gate
coverage (empty child, leading-dash ids, non-string result/child, explicit
empty parent, the positive provider-session fallback, writer saturation), the
LF-free excerpt-cap boundary, envelope-verification observability, CLI
payload key-set and whitespace-id validation, and the surrogate-safe digest.
Store isolation pins land before any storelib-adjacent import (issue #61
hazard); ~/.zmem is never touched.
"""

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-164-fb-"))
os.environ["ZMEM_STORE"] = str(_SCRATCH / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)
os.environ["ZMEM_MODELS_DIR"] = str(_SCRATCH / "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

_CHILD = "00000000-0000-4000-8000-000000000164"
_PARENT = "00000000-0000-4000-8000-000000000165"
_TASK_SHA = "b2d89077f3c1c86f9aec81e9e688b8ed902780ecc00333f33a839486ed8105ee"
_RESULT_SHA = "514bf87b606c5d7f24f179cf9b3b5bf1ccb20136386184b6bbfa207a078663cb"
_STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"


def _load_provider():
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", mp)
    spec = importlib.util.spec_from_file_location(
        "zmem_hermes_164_fb", REPO_ROOT / "hermes-plugin" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_hermes_164_fb"] = module
    spec.loader.exec_module(module)
    return module


def _cli(args, input_text=None, env=None):
    return subprocess.run(
        [sys.executable, str(_STORE_PY), *args],
        input=input_text, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180, cwd=str(REPO_ROOT), env=env)


def _compact(child=_CHILD, parent=_PARENT):
    return json.dumps({
        "child_session_id": child, "parent_session_id": parent,
        "result_sha256": _RESULT_SHA, "task_sha256": _TASK_SHA,
    }, sort_keys=True, separators=(",", ":"))


def _flags(child=_CHILD, parent=_PARENT):
    return ["evidence", "write", "--kind", "delegation",
            "--session-id", parent, "--lane", "hermes-provider",
            "--moment", "subagent",
            "--ref-path", f"delegation:{parent}:{child}"]


def _ok_envelope():
    return {"ok": True, "stdout": json.dumps({"ok": True, "id": str(uuid.uuid4())}) + "\n",
            "stderr": "", "returncode": 0}


class _Seam:
    def __init__(self, module, reply):
        self.calls = []
        self._module = module
        self._real = module._run_store
        self._reply = reply

    def _fake(self, args, *pos, **kwargs):
        self.calls.append({"argv": list(args),
                           "input_text": kwargs.get("input_text")})
        reply = self._reply
        if callable(reply):
            return reply(args, *pos, **kwargs)
        return reply

    def __enter__(self):
        self._module._run_store = self._fake
        return self

    def __exit__(self, *exc):
        self._module._run_store = self._real
        return False


class HermesEvidenceFeedbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_provider()
        proc = _cli(["init"])
        assert proc.returncode == 0, proc.stderr

    def _provider(self):
        provider = self.module.ZmemMemoryProvider()
        provider.initialize(_PARENT, hermes_home=str(_SCRATCH))
        return provider

    def test_flagged_cli_success_envelope_and_row(self):
        """The real flagged success path: exit 0, the exact stdout envelope
        bytes, and a stored row carrying the verbatim (unredacted) digests."""
        payload = _compact() + "\n"
        proc = _cli(_flags(), input_text=payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        envelope = json.loads(proc.stdout)
        self.assertEqual(envelope["ok"], True)
        self.assertTrue(envelope["id"])
        # Byte-exact stdout: the CLI's sorted-key envelope with default
        # separators — id first, then ok.
        self.assertEqual(
            proc.stdout,
            '{"id": "%s", "ok": true}\n' % envelope["id"])
        conn = sqlite3.connect(os.environ["ZMEM_STORE"])
        try:
            row = conn.execute(
                "SELECT kind, session_id, lane, moment, ref_path, ref_offset,"
                " excerpt, hash FROM evidence WHERE id=?",
                (envelope["id"],)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "delegation")
        self.assertEqual(row[1], _PARENT)
        self.assertEqual(row[2], "hermes-provider")
        self.assertEqual(row[3], "subagent")
        self.assertEqual(row[4], f"delegation:{_PARENT}:{_CHILD}")
        self.assertIsNone(row[5])
        self.assertEqual(row[6], _compact())
        self.assertIn(_TASK_SHA, row[6])
        self.assertIn(_RESULT_SHA, row[6])
        self.assertNotIn("[REDACTED_SECRET]", row[6])

    def test_delegation_export_ingest_round_trip(self):
        """A store holding a flagged delegation row re-imports its own JSONL
        export (PRR-001: strict-import re-redaction broke the hash)."""
        store_a = Path(tempfile.mkdtemp(prefix="zmem-164-fb-a-"))
        store_b = Path(tempfile.mkdtemp(prefix="zmem-164-fb-b-"))
        env_a = dict(os.environ, ZMEM_STORE=str(store_a / "store.sqlite"),
                     ZMEM_DATA=str(store_a))
        env_b = dict(os.environ, ZMEM_STORE=str(store_b / "store.sqlite"),
                     ZMEM_DATA=str(store_b))
        self.assertEqual(_cli(["init"], env=env_a).returncode, 0)
        self.assertEqual(_cli(["init"], env=env_b).returncode, 0)
        proc = _cli(_flags(), input_text=_compact() + "\n", env=env_a)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        export_path = store_a / "export.jsonl"
        proc = _cli(["export-jsonl", "--out", str(export_path)], env=env_a)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = _cli(["ingest-jsonl", "--in", str(export_path)], env=env_b)
        self.assertEqual(proc.returncode, 0,
                         "round trip failed: %s" % proc.stderr)
        conn = sqlite3.connect(str(store_b / "store.sqlite"))
        try:
            rows = conn.execute(
                "SELECT kind, excerpt FROM evidence WHERE kind='delegation'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], _compact())

    def test_envelope_verification_observable(self):
        """The rejection branches log distinct debug messages, so the
        envelope contract is observable (a flipped ok-check now fails)."""
        provider = self._provider()
        with _Seam(self.module, {"ok": True, "stdout": "not-json{",
                                  "stderr": "", "returncode": 0}):
            with self.assertLogs(level="DEBUG") as logs:
                provider.on_delegation("t", "r", child_session_id=_CHILD,
                                       parent_session_id=_PARENT)
        self.assertTrue(any("envelope malformed" in line
                            for line in logs.output))
        with _Seam(self.module, {"ok": True, "stdout": '{"ok":true}',
                                  "stderr": "", "returncode": 0}):
            with self.assertLogs(level="DEBUG") as logs:
                provider.on_delegation("t", "r", child_session_id=_CHILD,
                                       parent_session_id=_PARENT)
        self.assertTrue(any("envelope rejected" in line
                            for line in logs.output))
        with _Seam(self.module, _ok_envelope()):
            with self.assertNoLogs(level="DEBUG"):
                provider.on_delegation("t", "r", child_session_id=_CHILD,
                                       parent_session_id=_PARENT)

    def test_excerpt_cap_boundary_lf_free(self):
        """The provider measures the LF-free excerpt, exactly like the store:
        a 400-char excerpt is sent (store-legal), 401 is skipped.  Both ids
        are capped at 128, so the boundary is reached by lengthening the
        parent."""
        provider = self._provider()
        skeleton = len(json.dumps({
            "child_session_id": "", "parent_session_id": "",
            "result_sha256": _RESULT_SHA, "task_sha256": _TASK_SHA,
        }, sort_keys=True, separators=(",", ":")))

        def ids_for(target_len):
            parent_len = min(128, target_len - skeleton)
            child_len = target_len - skeleton - parent_len
            return "p" * parent_len, "c" * child_len

        for target in (399, 400, 401):
            parent_id, child_id = ids_for(target)
            excerpt = json.dumps({
                "child_session_id": child_id, "parent_session_id": parent_id,
                "result_sha256": _RESULT_SHA, "task_sha256": _TASK_SHA,
            }, sort_keys=True, separators=(",", ":"))
            self.assertEqual(len(excerpt), target)
            with _Seam(self.module, _ok_envelope()) as seam:
                self.assertIsNone(provider.on_delegation(
                    "t", "r", child_session_id=child_id,
                    parent_session_id=parent_id))
            expected_calls = 1 if target <= 400 else 0
            self.assertEqual(len(seam.calls), expected_calls, msg=target)

    def test_cli_excerpt_cap_boundary_accepts_400(self):
        base = _compact()
        deficit = 400 - len(base)
        payload = _compact(child=_CHILD + "f" * deficit) + "\n"
        proc = _cli(_flags(), input_text=payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"ok": true', proc.stdout)

    def test_admission_gates(self):
        """Empty child, leading-dash ids, non-string result/child, and an
        explicitly empty parent_session_id all skip with zero subprocess
        calls; a missing kwarg falls back positively to the provider
        session."""
        provider = self._provider()
        cases = [
            {"child_session_id": "", "parent_session_id": _PARENT},
            {"child_session_id": _CHILD, "parent_session_id": "-abc"},
            {"child_session_id": "-abc", "parent_session_id": _PARENT},
            {"child_session_id": 123, "parent_session_id": _PARENT},
            {"child_session_id": _CHILD, "parent_session_id": ""},
        ]
        for kwargs in cases:
            with _Seam(self.module, _ok_envelope()) as seam:
                self.assertIsNone(provider.on_delegation("t", "r", **kwargs),
                                  msg=repr(kwargs))
            self.assertEqual(seam.calls, [], msg=repr(kwargs))
        with _Seam(self.module, 12345) as seam:
            self.assertIsNone(provider.on_delegation(
                "t", 12345, child_session_id=_CHILD,
                parent_session_id=_PARENT))
        self.assertEqual(seam.calls, [])
        # Positive fallback: no kwarg -> provider's initialized session.
        with _Seam(self.module, _ok_envelope()) as seam:
            self.assertIsNone(provider.on_delegation(
                "t", "r", child_session_id=_CHILD))
        self.assertEqual(len(seam.calls), 1)
        idx = seam.calls[0]["argv"].index("--session-id")
        self.assertEqual(seam.calls[0]["argv"][idx + 1], _PARENT)

    def test_surrogate_hash_survives(self):
        digest = self.module._delegation_hash("bad \ud800 surrogate")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            digest,
            hashlib.sha256(
                "bad \ud800 surrogate".encode("utf-8", "surrogatepass")
            ).hexdigest())

    def test_inflight_saturation_skips(self):
        provider = self._provider()
        sem = self.module._DELEGATION_INFLIGHT
        acquired = []
        while sem.acquire(blocking=False):
            acquired.append(True)
            if len(acquired) >= 8:
                break
        try:
            with _Seam(self.module, _ok_envelope()) as seam:
                self.assertIsNone(provider.on_delegation(
                    "t", "r", child_session_id=_CHILD,
                    parent_session_id=_PARENT))
            self.assertEqual(seam.calls, [])
        finally:
            for _ in acquired:
                sem.release()

    def test_cli_payload_key_set_enforced(self):
        missing = json.loads(_compact())
        del missing["result_sha256"]
        proc = _cli(_flags(), input_text=json.dumps(missing))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("evidence write failed", proc.stderr)
        extra = json.loads(_compact())
        extra["unexpected"] = "x"
        proc = _cli(_flags(), input_text=json.dumps(extra))
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("x", proc.stderr)

    def test_cli_whitespace_id_rejected(self):
        payload = _compact(child="bad id with spaces")
        proc = _cli(_flags(), input_text=payload)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("bad id", proc.stderr)

    def test_cli_empty_session_id_selects_flagged(self):
        """An explicitly empty --session-id value must select flagged mode
        (sentinel detection), not fall back to the legacy stdin shape — the
        legacy path would reject these flags as unrecognized arguments."""
        proc = _cli(["evidence", "write", "--session-id", "",
                     "--kind", "delegation"],
                    input_text=_compact())
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("unrecognized arguments", proc.stderr)
        self.assertIn("evidence write failed", proc.stderr)


    def _ingest_crafted(self, row):
        """Write one crafted JSONL evidence row and ingest it into a fresh
        store; returns (returncode, evidence-row count)."""
        store_a = Path(tempfile.mkdtemp(prefix="zmem-164-fb-craft-"))
        store_b = Path(tempfile.mkdtemp(prefix="zmem-164-fb-target-"))
        env_b = dict(os.environ, ZMEM_STORE=str(store_b / "store.sqlite"),
                     ZMEM_DATA=str(store_b))
        self.assertEqual(_cli(["init"], env=env_b).returncode, 0)
        export_path = store_a / "crafted.jsonl"
        export_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        proc = _cli(["ingest-jsonl", "--in", str(export_path)], env=env_b)
        conn = sqlite3.connect(str(store_b / "store.sqlite"))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM evidence").fetchone()[0]
        finally:
            conn.close()
        return proc.returncode, count

    def test_strict_import_rejects_self_hashed_non_delegation(self):
        """A remote-authored NON-delegation row with a self-computed verbatim
        hash and redactable content must not land unredacted (review round 2:
        the verbatim branch is delegation-gated)."""
        excerpt = json.dumps({"note": "sk-ant-" + "A" * 40}, sort_keys=True,
                             separators=(",", ":"))
        ts = "2026-10-10T00:00:00Z"
        row = {
            "table": "evidence",
            "id": str(uuid.uuid4()),
            "session_id": "sync-session",
            "lane": "hermes-provider",
            "moment": "pretool",
            "kind": "tool_call",
            "ts": ts,
            "excerpt": excerpt,
            "hash": hashlib.sha256(
                f"tool_call|{ts}|{excerpt}".encode("utf-8")).hexdigest(),
            "ref_path": "hermes://sync/1",
            "ref_offset": None,
        }
        rc, count = self._ingest_crafted(row)
        self.assertNotEqual(rc, 0)
        self.assertEqual(count, 0)

    def test_strict_import_rejects_delegation_whitespace_id_self_hash(self):
        """A delegation-kind row whose payload id carries whitespace fails
        the writer-shape validation, so its self-computed verbatim hash
        cannot land it (purge-survivor vector closed at import too)."""
        excerpt = _compact(child="bad id with spaces")
        ts = "2026-10-10T00:00:00Z"
        row = {
            "table": "evidence",
            "id": str(uuid.uuid4()),
            "session_id": _PARENT,
            "lane": "hermes-provider",
            "moment": "subagent",
            "kind": "delegation",
            "ts": ts,
            "excerpt": excerpt,
            "hash": hashlib.sha256(
                f"delegation|{ts}|{excerpt}".encode("utf-8")).hexdigest(),
            "ref_path": f"delegation:{_PARENT}:bad id",
            "ref_offset": None,
        }
        rc, count = self._ingest_crafted(row)
        self.assertNotEqual(rc, 0)
        self.assertEqual(count, 0)


    def test_strict_import_rejects_delegation_duplicate_key_self_hash(self):
        """A delegation-kind row whose excerpt carries duplicate JSON keys
        fails the strict-pairs shape validation (the raw string is stored
        verbatim, so earlier duplicate values would smuggle unseen text)."""
        smuggled = (
            '{"child_session_id":"bad id with spaces","child_session_id":'
            '"' + _CHILD + '","parent_session_id":"' + _PARENT + '"'
            ',"result_sha256":"' + _RESULT_SHA + '","task_sha256":"'
            + _TASK_SHA + '"}')
        ts = "2026-10-10T00:00:00Z"
        row = {
            "table": "evidence",
            "id": str(uuid.uuid4()),
            "session_id": _PARENT,
            "lane": "hermes-provider",
            "moment": "subagent",
            "kind": "delegation",
            "ts": ts,
            "excerpt": smuggled,
            "hash": hashlib.sha256(
                f"delegation|{ts}|{smuggled}".encode("utf-8")).hexdigest(),
            "ref_path": f"delegation:{_PARENT}:{_CHILD}",
            "ref_offset": None,
        }
        rc, count = self._ingest_crafted(row)
        self.assertNotEqual(rc, 0)
        self.assertEqual(count, 0)


    def test_strict_import_rejects_delegation_empty_id_self_hash(self):
        """A delegation-kind row with an empty child id and a verbatim
        self-hash fails the writer-shape validation (the writer rejects
        empty ids), so it cannot land via the verbatim path."""
        excerpt = _compact(child="")
        ts = "2026-10-10T00:00:00Z"
        row = {
            "table": "evidence",
            "id": str(uuid.uuid4()),
            "session_id": _PARENT,
            "lane": "hermes-provider",
            "moment": "subagent",
            "kind": "delegation",
            "ts": ts,
            "excerpt": excerpt,
            "hash": hashlib.sha256(
                f"delegation|{ts}|{excerpt}".encode("utf-8")).hexdigest(),
            "ref_path": f"delegation:{_PARENT}:",
            "ref_offset": None,
        }
        rc, count = self._ingest_crafted(row)
        self.assertNotEqual(rc, 0)
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
