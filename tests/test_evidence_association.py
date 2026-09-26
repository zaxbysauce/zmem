"""Issue #171 contract tests for memory/evidence associations.

The tests drive the real ``store.py`` boundary against a fresh temporary store
for every case.  The fixture transport test also pins the checked-in JSONL
files as raw UTF-8 bytes, including the association rows and their ordering.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "skills" / "memory" / "scripts" / "store.py"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "evidence"
ASSOCIATION_INPUT = FIXTURE_DIR / "association-input.jsonl"
PYTHON = sys.executable
MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None

NS = "fixture:evidence-171"
WRITE_NS = "project:evidence-171"
TS = "2026-09-10T00:00:00Z"
MEMORY_1 = "00000000-0000-4000-8000-000000000171"
MEMORY_2 = "00000000-0000-4000-8000-000000000172"
EVIDENCE_1 = "00000000-0000-4000-8000-000000000181"
EVIDENCE_2 = "00000000-0000-4000-8000-000000000182"
EVIDENCE_3 = "00000000-0000-4000-8000-000000000183"
MISSING_EVIDENCE = "00000000-0000-4000-8000-000000000199"
SESSION_ID = "00000000-0000-4000-8000-000000000191"


def _env(scratch: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "ZMEM_STORE": str(scratch / "store.sqlite"),
        "ZMEM_DATA": str(scratch),
        "ZMEM_MODELS_DIR": str(scratch / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_HOME": str(ROOT),
    })
    for key in ("ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE"):
        env.pop(key, None)
    return env


def _run(scratch: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, str(STORE), *args], cwd=ROOT, env=_env(scratch),
        input=input_text, text=True, capture_output=True, timeout=60,
    )


def _init(scratch: Path) -> None:
    result = _run(scratch, "init")
    if result.returncode != 0:
        raise AssertionError(result.stderr)


def _evidence_payload(evidence_id: str, *, lane: str, moment: str,
                      kind: str, excerpt: str, ref_path: str,
                      ref_offset: int) -> dict[str, object]:
    return {
        "id": evidence_id,
        "session_id": SESSION_ID,
        "lane": lane,
        "moment": moment,
        "kind": kind,
        "ts": TS,
        "excerpt": excerpt,
        "ref_path": ref_path,
        "ref_offset": ref_offset,
    }


def _write_evidence(scratch: Path, payload: dict[str, object]) -> None:
    result = _run(
        scratch, "evidence", "write",
        input_text=json.dumps(payload, ensure_ascii=False),
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)


def _add(scratch: Path, content: str, *, namespace: str = WRITE_NS,
         evidence: str | None = None) -> str:
    args = [
        "add", "--namespace", namespace, "--type", "fact", "--content", content,
        "--signal", "test", "--confidence", "0.9", "--json",
    ]
    if evidence is not None:
        args += ["--evidence", evidence]
    result = _run(scratch, *args)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return str(json.loads(result.stdout)["id"])


def _update(scratch: Path, memory_id: str, content: str,
            evidence: str | None = None) -> subprocess.CompletedProcess[str]:
    args = [
        "update", "--id", memory_id, "--content", content, "--json",
    ]
    if evidence is not None:
        args += ["--evidence", evidence]
    return _run(scratch, *args)


def _expected_evidence(evidence_id: str, *, lane: str, moment: str,
                       kind: str, excerpt: str, ref_path: str,
                       ref_offset: int | None) -> dict[str, object]:
    return {
        "id": evidence_id,
        "session_id": SESSION_ID,
        "lane": lane,
        "moment": moment,
        "kind": kind,
        "ts": TS,
        "excerpt": excerpt,
        "ref_path": ref_path,
        "ref_offset": ref_offset,
    }


def _snapshot(scratch: Path) -> tuple[list[tuple], list[tuple]]:
    conn = sqlite3.connect(scratch / "store.sqlite")
    try:
        memories = conn.execute(
            "SELECT id, namespace, type, content, tags, source_ref, confidence, "
            "signal, valid_from, valid_until, update_of, taint, ingestion_ts, "
            "superseded_at, supersede_reason FROM memory ORDER BY id"
        ).fetchall()
        associations = conn.execute(
            "SELECT memory_id, evidence_id FROM memory_evidence "
            "ORDER BY memory_id, evidence_id"
        ).fetchall()
        return memories, associations
    finally:
        conn.close()


def _fixture_import(scratch: Path) -> None:
    _init(scratch)
    result = _run(
        scratch, "ingest-jsonl", "--in", str(ASSOCIATION_INPUT),
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)


class AssociationWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-171-association-")
        self.scratch = Path(self.tmp.name)
        _init(self.scratch)
        _write_evidence(self.scratch, _evidence_payload(
            EVIDENCE_1, lane="claude", moment="pretool", kind="tool_call",
            excerpt="command=git status", ref_path="hooks/zmem-session-start.sh",
            ref_offset=1,
        ))
        _write_evidence(self.scratch, _evidence_payload(
            EVIDENCE_2, lane="codex", moment="pretool", kind="tool_failure",
            excerpt="exit=1", ref_path="hooks/zmem-launch.js", ref_offset=2,
        ))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_add_with_evidence_attaches_in_input_order_but_exports_sorted(self):
        memory_id = _add(self.scratch, "fixture add association", evidence=f" {EVIDENCE_2}, {EVIDENCE_1} ")
        shown = _run(self.scratch, "evidence", "for", "--memory-id", memory_id, "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(
            [row["id"] for row in json.loads(shown.stdout)["evidence"]],
            [EVIDENCE_1, EVIDENCE_2],
        )

        exported = self.scratch / "export.jsonl"
        result = _run(self.scratch, "export-jsonl", "--out", str(exported))
        self.assertEqual(result.returncode, 0, result.stderr)
        associations = [
            json.loads(line) for line in exported.read_text(encoding="utf-8").splitlines()
            if line and json.loads(line).get("table") == "memory_evidence"
        ]
        self.assertEqual(
            [(row["memory_id"], row["evidence_id"]) for row in associations],
            [(memory_id, EVIDENCE_1), (memory_id, EVIDENCE_2)],
        )

    def test_update_attaches_to_new_live_row_and_rolls_back_on_missing_id(self):
        old_id = _add(self.scratch, "fixture update original", evidence=EVIDENCE_1)
        updated = _update(self.scratch, old_id, "fixture update replacement", evidence=EVIDENCE_2)
        self.assertEqual(updated.returncode, 0, updated.stderr)
        new_id = str(json.loads(updated.stdout)["id"])
        self.assertNotEqual(new_id, old_id)

        rows = {row[0]: row for row in _snapshot(self.scratch)[0]}
        old_row = rows[old_id]
        new_row = rows[new_id]
        self.assertIsNotNone(old_row[13])
        self.assertIsNone(new_row[13])
        self.assertEqual(
            _snapshot(self.scratch)[1],
            sorted([(new_id, EVIDENCE_2), (old_id, EVIDENCE_1)]),
        )

        before_bad = _snapshot(self.scratch)
        rejected = _update(self.scratch, new_id, "must not land", evidence=MISSING_EVIDENCE)
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(
            rejected.stderr,
            f"[zmem] evidence id not found: {MISSING_EVIDENCE}\n",
        )
        self.assertEqual(_snapshot(self.scratch), before_bad)


class EvidenceReadCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-171-evidence-read-")
        self.scratch = Path(self.tmp.name)
        _fixture_import(self.scratch)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_evidence_for_returns_sorted_rows_and_namespaces(self):
        result = _run(self.scratch, "evidence", "for", "--memory-id", MEMORY_1, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertEqual(json.loads(result.stdout), {
            "memory_id": MEMORY_1,
            "namespace": NS,
            "evidence": [
                _expected_evidence(
                    EVIDENCE_1, lane="claude", moment="pretool", kind="tool_call",
                    excerpt="command=git status", ref_path="hooks/zmem-session-start.sh",
                    ref_offset=1,
                ),
                _expected_evidence(
                    EVIDENCE_2, lane="codex", moment="pretool", kind="tool_failure",
                    excerpt="exit=1", ref_path="hooks/zmem-launch.js", ref_offset=2,
                ),
            ],
        })

    def test_evidence_scoped_show_returns_sorted_memory_namespaces(self):
        # The checked-in fixture associates EVIDENCE_1 with MEMORY_1. Add the
        # second fixed memory/evidence pair through the public association
        # helper so the scoped response has a meaningful ordering assertion.
        sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))
        from storelib.evidence import attach_memory_evidence
        conn = sqlite3.connect(self.scratch / "store.sqlite")
        try:
            attach_memory_evidence(conn, memory_id=MEMORY_2, evidence_ids=[EVIDENCE_1])
        finally:
            conn.close()

        result = _run(
            self.scratch, "evidence", "scoped-show", "--namespace", NS,
            "--id", EVIDENCE_1, "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["id"], EVIDENCE_1)
        self.assertEqual(
            payload["associations"],
            [
                {"memory_id": MEMORY_1, "namespace": NS},
                {"memory_id": MEMORY_2, "namespace": NS},
            ],
        )


class RecallJsonTest(unittest.TestCase):
    def test_recall_json_contains_evidence_ids(self):
        with tempfile.TemporaryDirectory(prefix="zmem-171-recall-") as td:
            scratch = Path(td)
            _init(scratch)
            _write_evidence(scratch, _evidence_payload(
                EVIDENCE_1, lane="claude", moment="pretool", kind="tool_call",
                excerpt="command=git status", ref_path="hooks/zmem-session-start.sh",
                ref_offset=1,
            ))
            associated = _add(scratch, "fixture-recall-171 associated", evidence=EVIDENCE_1)
            unassociated = _add(scratch, "fixture-recall-171 unassociated")
            result = _run(
                scratch, "recall", "--query", "fixture-recall-171",
                "--namespace", WRITE_NS, "--limit", "10", "--json", "--no-bump",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = json.loads(result.stdout)["results"]
            by_id = {row["id"]: row for row in rows}
            self.assertEqual(by_id[associated]["evidence_ids"], [EVIDENCE_1])
            self.assertEqual(by_id[unassociated]["evidence_ids"], [])

    def test_recent_json_contains_sorted_evidence_ids(self):
        with tempfile.TemporaryDirectory(prefix="zmem-171-recent-") as td:
            scratch = Path(td)
            _init(scratch)
            _write_evidence(scratch, _evidence_payload(
                EVIDENCE_1, lane="claude", moment="pretool", kind="tool_call",
                excerpt="command=git status", ref_path="hooks/zmem-session-start.sh",
                ref_offset=1,
            ))
            _write_evidence(scratch, _evidence_payload(
                EVIDENCE_2, lane="codex", moment="pretool", kind="tool_failure",
                excerpt="exit=1", ref_path="hooks/zmem-launch.js", ref_offset=2,
            ))
            associated = _add(
                scratch, "fixture-recent-171 associated",
                evidence=f"{EVIDENCE_2},{EVIDENCE_1}",
            )
            unassociated = _add(scratch, "fixture-recent-171 unassociated")
            result = _run(
                scratch, "recent", "--namespace", WRITE_NS, "--limit", "10",
                "--json", "--no-bump",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = json.loads(result.stdout)["results"]
            by_id = {row["id"]: row for row in rows}
            self.assertEqual(
                by_id[associated]["evidence_ids"], [EVIDENCE_1, EVIDENCE_2]
            )
            self.assertEqual(by_id[unassociated]["evidence_ids"], [])

    def test_recall_explain_json_contains_sorted_evidence_ids(self):
        with tempfile.TemporaryDirectory(prefix="zmem-171-recall-explain-") as td:
            scratch = Path(td)
            _init(scratch)
            _write_evidence(scratch, _evidence_payload(
                EVIDENCE_1, lane="claude", moment="pretool", kind="tool_call",
                excerpt="command=git status", ref_path="hooks/zmem-session-start.sh",
                ref_offset=1,
            ))
            _write_evidence(scratch, _evidence_payload(
                EVIDENCE_2, lane="codex", moment="pretool", kind="tool_failure",
                excerpt="exit=1", ref_path="hooks/zmem-launch.js", ref_offset=2,
            ))
            associated = _add(
                scratch, "fixture-recall-explain-171 associated",
                evidence=f"{EVIDENCE_2},{EVIDENCE_1}",
            )
            unassociated = _add(scratch, "fixture-recall-explain-171 unassociated")
            result = _run(
                scratch, "recall", "--explain", "--json", "--no-hybrid",
                "--no-bump", "--query", "fixture-recall-explain-171",
                "--namespace", WRITE_NS, "--limit", "10",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            rows = payload["results"]
            by_id = {row["id"]: row for row in rows}
            self.assertIn("explain", payload)
            self.assertEqual(
                by_id[associated]["evidence_ids"], [EVIDENCE_1, EVIDENCE_2]
            )
            self.assertEqual(by_id[unassociated]["evidence_ids"], [])


class McpEvidenceTest(unittest.TestCase):
    @unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
    def test_evidence_for_and_show_are_namespace_scoped(self):
        with tempfile.TemporaryDirectory(prefix="zmem-171-mcp-") as td:
            scratch = Path(td)
            _init(scratch)
            _write_evidence(scratch, _evidence_payload(
                EVIDENCE_1, lane="claude", moment="pretool", kind="tool_call",
                excerpt="command=git status", ref_path="hooks/zmem-session-start.sh",
                ref_offset=1,
            ))
            _write_evidence(scratch, _evidence_payload(
                EVIDENCE_2, lane="codex", moment="pretool", kind="tool_failure",
                excerpt="exit=1", ref_path="hooks/zmem-launch.js", ref_offset=2,
            ))
            _write_evidence(scratch, _evidence_payload(
                EVIDENCE_3, lane="zcode", moment="user_prompt", kind="turn",
                excerpt="turn complete", ref_path="tests/fixtures/evidence/association-input.jsonl",
                ref_offset=3,
            ))
            allowed_memory = _add(
                scratch, "fixture MCP allowed", namespace="project:allowed-171",
                evidence=EVIDENCE_1,
            )
            denied_memory = _add(
                scratch, "fixture MCP denied", namespace="project:denied-171",
                evidence=EVIDENCE_2,
            )

            server_dir = ROOT / "hermes-plugin" / "server"
            sys.path.insert(0, str(server_dir))
            import mcp_server

            saved = {
                key: os.environ.get(key)
                for key in ("ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA",
                            "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
                            "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE",
                            "ZMEM_MCP_DEFAULT_NS")
            }
            token_file = scratch / "token.json"
            token_file.write_bytes(
                b'{"token":"fixture-token-171","namespaces":["project:allowed-171"]}'
            )
            os.environ.update({
                "ZMEM_HOME": str(ROOT),
                "ZMEM_STORE": str(scratch / "store.sqlite"),
                "ZMEM_DATA": str(scratch),
                "ZMEM_MODELS_DIR": str(scratch / "missing-models"),
                "ZMEM_MODEL_AUTODOWNLOAD": "0",
                "ZMEM_MCP_TOKEN_FILE": str(token_file),
                "ZMEM_MCP_DEFAULT_NS": "project:allowed-171",
            })
            os.environ.pop("ZMEM_MCP_TOKEN", None)
            try:
                scoped = mcp_server.build_server("127.0.0.1", 0, False)
                denied_shape = {
                    "error": "namespace_not_allowed",
                    "namespace": None,
                    "detail": "memory is not associated with an allowed namespace",
                }
                foreign = asyncio.run(scoped._tool_manager.call_tool(
                    "evidence_for", {"memory_id": denied_memory}, context=None))
                self.assertEqual(foreign, denied_shape)
                unassociated = asyncio.run(scoped._tool_manager.call_tool(
                    "evidence_show", {
                        "id": EVIDENCE_3, "namespace": "project:allowed-171",
                    }, context=None))
                self.assertEqual(unassociated, {
                    "error": "namespace_not_allowed",
                    "namespace": "project:allowed-171",
                    "detail": "evidence id is not associated with the requested namespace",
                })
                foreign_evidence = asyncio.run(scoped._tool_manager.call_tool(
                    "evidence_show", {
                        "id": EVIDENCE_2, "namespace": "project:allowed-171",
                    }, context=None))
                self.assertEqual(foreign_evidence, unassociated)

                os.environ.pop("ZMEM_MCP_TOKEN_FILE", None)
                os.environ["ZMEM_MCP_TOKEN"] = "fixture-token-171"
                operator = mcp_server.build_server("127.0.0.1", 0, False)
                returned = asyncio.run(operator._tool_manager.call_tool(
                    "evidence_for", {"memory_id": allowed_memory}, context=None))
                self.assertEqual(returned["memory_id"], allowed_memory)
                self.assertEqual([row["id"] for row in returned["evidence"]], [EVIDENCE_1])
                shown = asyncio.run(operator._tool_manager.call_tool(
                    "evidence_show", {"id": EVIDENCE_1}, context=None))
                self.assertEqual(
                    shown["associations"],
                    [{"memory_id": allowed_memory, "namespace": "project:allowed-171"}],
                )
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


class SchemaInvariantTest(unittest.TestCase):
    def test_association_does_not_change_memory_columns(self):
        with tempfile.TemporaryDirectory(prefix="zmem-171-schema-") as td:
            scratch = Path(td)
            _init(scratch)
            conn = sqlite3.connect(scratch / "store.sqlite")
            try:
                before = json.dumps(
                    [list(row) for row in conn.execute("PRAGMA table_info(memory)")],
                    ensure_ascii=False,
                ).encode("utf-8")
            finally:
                conn.close()
            _write_evidence(scratch, _evidence_payload(
                EVIDENCE_1, lane="claude", moment="pretool", kind="tool_call",
                excerpt="command=git status", ref_path="hooks/zmem-session-start.sh",
                ref_offset=1,
            ))
            _add(scratch, "fixture schema invariant", evidence=EVIDENCE_1)
            conn = sqlite3.connect(scratch / "store.sqlite")
            try:
                after = json.dumps(
                    [list(row) for row in conn.execute("PRAGMA table_info(memory)")],
                    ensure_ascii=False,
                ).encode("utf-8")
            finally:
                conn.close()
            self.assertEqual(before, after)

            sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))
            import schema_meta
            from storelib import schema
            self.assertEqual(schema_meta.SUPPORTED_SCHEMA_VERSION, 14)
            self.assertEqual(schema_meta.FORWARD_COMPAT_SCHEMA_VERSION, 14)
            self.assertEqual(schema.SUPPORTED_SCHEMA_VERSION, 14)
            self.assertEqual(schema.FORWARD_COMPAT_SCHEMA_VERSION, 14)


class CliEvidenceAssociationTest(unittest.TestCase):
    def test_add_and_update_evidence_flag_parses_comma_ids(self):
        with tempfile.TemporaryDirectory(prefix="zmem-171-cli-") as td:
            scratch = Path(td)
            _init(scratch)
            for payload in (
                _evidence_payload(
                    EVIDENCE_1, lane="claude", moment="pretool", kind="tool_call",
                    excerpt="command=git status", ref_path="hooks/zmem-session-start.sh",
                    ref_offset=1,
                ),
                _evidence_payload(
                    EVIDENCE_2, lane="codex", moment="pretool", kind="tool_failure",
                    excerpt="exit=1", ref_path="hooks/zmem-launch.js", ref_offset=2,
                ),
            ):
                _write_evidence(scratch, payload)
            old_id = _add(scratch, "fixture parser add", evidence=f" {EVIDENCE_2}, {EVIDENCE_1} ")
            added = json.loads(_run(
                scratch, "evidence", "for", "--memory-id", old_id, "--json"
            ).stdout)
            self.assertEqual([row["id"] for row in added["evidence"]], [EVIDENCE_1, EVIDENCE_2])
            updated = _update(scratch, old_id, "fixture parser update", evidence=f" {EVIDENCE_1} ")
            self.assertEqual(updated.returncode, 0, updated.stderr)
            new_id = str(json.loads(updated.stdout)["id"])
            self.assertEqual(
                [row["id"] for row in json.loads(_run(
                    scratch, "evidence", "for", "--memory-id", new_id, "--json"
                ).stdout)["evidence"]],
                [EVIDENCE_1],
            )

    def test_invalid_evidence_id_exits_two_without_memory_row(self):
        with tempfile.TemporaryDirectory(prefix="zmem-171-cli-invalid-") as td:
            scratch = Path(td)
            _init(scratch)
            rejected = _run(
                scratch, "add", "--namespace", WRITE_NS, "--type", "fact",
                "--content", "must not land", "--signal", "test",
                "--evidence", MISSING_EVIDENCE,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(
                rejected.stderr,
                f"[zmem] evidence id not found: {MISSING_EVIDENCE}\n",
            )
            conn = sqlite3.connect(scratch / "store.sqlite")
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0], 0
                )
            finally:
                conn.close()


class FixtureTransportTest(unittest.TestCase):
    def test_strict_association_missing_endpoint_preserves_physical_line_and_rolls_back(self):
        with tempfile.TemporaryDirectory(prefix="zmem-171-diagnostic-") as td:
            scratch = Path(td)
            memory = {
                "kind": "memory", "id": MEMORY_1, "namespace": NS, "type": "fact",
                "content": "strict association diagnostic", "tags": "", "source_ref": "",
                "confidence": 0.9, "signal": "test", "valid_from": TS,
                "valid_until": "", "update_of": "", "taint": "trusted_internal",
                "ingestion_ts": TS, "superseded_at": None, "supersede_reason": "",
                "merged_from": None, "trust_score": 1.0, "applied_count": 0,
                "violated_count": 0, "links": [],
            }
            malformed = scratch / "missing-association.jsonl"
            payload = (
                json.dumps(memory, separators=(",", ":"))
                + "\n\n"
                + json.dumps(
                    {
                        "table": "memory_evidence",
                        "memory_id": MEMORY_1,
                        "evidence_id": MISSING_EVIDENCE,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            malformed.write_bytes(payload.encode("utf-8"))

            _init(scratch)
            result = _run(scratch, "ingest-jsonl", "--strict", "--in", str(malformed))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(
                result.stderr,
                "[zmem] ingest-jsonl: line 3: memory_evidence endpoint not found\n",
            )

            conn = sqlite3.connect(scratch / "store.sqlite")
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0],
                    0,
                )
            finally:
                conn.close()

    def test_fixture_reexport_is_byte_stable_and_association_import_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="zmem-171-fixture-") as td:
            scratch = Path(td)
            _fixture_import(scratch)
            exported = scratch / "reexport.jsonl"
            first = _run(scratch, "export-jsonl", "--out", str(exported))
            self.assertEqual(first.returncode, 0, first.stderr)
            expected = (FIXTURE_DIR / "expected-association.jsonl").read_bytes()
            self.assertEqual(exported.read_bytes(), expected)
            self.assertTrue(expected.endswith(b"\n"))
            self.assertNotIn(b"\r", expected)

            conn = sqlite3.connect(scratch / "store.sqlite")
            try:
                before = conn.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0]
            finally:
                conn.close()
            second = _run(
                scratch, "ingest-jsonl", "--in", str(ASSOCIATION_INPUT),
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            conn = sqlite3.connect(scratch / "store.sqlite")
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0],
                    before,
                )
            finally:
                conn.close()
            second_export = scratch / "reexport-second.jsonl"
            self.assertEqual(
                _run(scratch, "export-jsonl", "--out", str(second_export)).returncode,
                0,
            )
            self.assertEqual(second_export.read_bytes(), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
