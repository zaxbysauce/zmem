"""Adversarial Wave A tests for strict evidence transport and closure."""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))

_ROUTE_ENV_KEYS = (
    "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODEL_URL",
    "ZMEM_EMBED_PROFILE", "ZMEM_CROSS_ENCODER_MODEL", "HOME", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA",
)
_IMPORT_ENV = {key: os.environ.get(key) for key in _ROUTE_ENV_KEYS}
_IMPORT_SANDBOX = Path(tempfile.mkdtemp(prefix="zmem-evidence-strict-import-"))
atexit.register(shutil.rmtree, _IMPORT_SANDBOX, ignore_errors=True)
for _key in _ROUTE_ENV_KEYS:
    os.environ.pop(_key, None)
os.environ.update({
    "ZMEM_STORE": str(_IMPORT_SANDBOX / "store.sqlite"),
    "ZMEM_DATA": str(_IMPORT_SANDBOX / "data"),
    "ZMEM_MODELS_DIR": str(_IMPORT_SANDBOX / "models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "HOME": str(_IMPORT_SANDBOX / "home"),
    "USERPROFILE": str(_IMPORT_SANDBOX / "home"),
    "APPDATA": str(_IMPORT_SANDBOX / "appdata"),
    "LOCALAPPDATA": str(_IMPORT_SANDBOX / "localappdata"),
})
try:
    from storelib import evidence, schema, sync  # noqa: E402
finally:
    for _key, _value in _IMPORT_ENV.items():
        if _value is None:
            os.environ.pop(_key, None)
        else:
            os.environ[_key] = _value


def _uuid(suffix: int) -> str:
    return f"00000000-0000-4000-8000-{suffix:012d}"


def _memory_row(mid: str, *, content: str = "strict memory", namespace: str = "project:p",
                superseded_at: str | None = None) -> dict:
    return {
        "kind": "memory", "id": mid, "namespace": namespace, "type": "fact",
        "content": content, "tags": "", "source_ref": "", "confidence": 0.9,
        "signal": "test", "valid_from": "2026-09-10T00:00:00Z",
        "valid_until": superseded_at or "", "update_of": "", "taint": "trusted_internal",
        "ingestion_ts": "2026-09-10T00:00:00Z", "superseded_at": superseded_at,
        "supersede_reason": "test" if superseded_at else "", "merged_from": None,
        "trust_score": 1.0, "applied_count": 0, "violated_count": 0, "links": [],
    }


def _evidence_row(eid: str, *, ts: str = "2026-09-10T00:00:00Z",
                  excerpt: str = "strict evidence") -> dict:
    digest = hashlib.sha256(f"turn|{ts}|{excerpt}".encode()).hexdigest()
    return {
        "table": "evidence", "id": eid, "session_id": "strict-session",
        "lane": "codex", "moment": "user_prompt", "kind": "turn", "ts": ts,
        "hash": digest, "excerpt": excerpt, "ref_path": "strict.txt", "ref_offset": 0,
    }


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


class _StrictEvidenceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-evidence-strict-")
        self.root = Path(self.tmp.name)
        self.old_env = os.environ.copy()
        for key in _ROUTE_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update({
            "ZMEM_STORE": str(self.root / "store.sqlite"),
            "ZMEM_DATA": str(self.root),
            "ZMEM_MODELS_DIR": str(self.root / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "HOME": str(self.root / "home"),
            "USERPROFILE": str(self.root / "home"),
            "APPDATA": str(self.root / "appdata"),
            "LOCALAPPDATA": str(self.root / "localappdata"),
        })
        self.conn = sqlite3.connect(self.root / "store.sqlite")
        self.conn.row_factory = sqlite3.Row
        schema.init_db(self.conn)
        schema.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    def _assert_empty(self, *tables: str) -> None:
        for table in tables:
            self.assertEqual(
                self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0,
                table,
            )


class StrictDuplicateKeyTest(_StrictEvidenceCase):
    def test_recursive_duplicate_discriminator_id_hash_and_nested_keys(self):
        valid = _evidence_row(_uuid(1001))
        valid_evidence = json.dumps(valid, separators=(",", ":"))
        valid_memory = json.dumps(
            _memory_row(_uuid(1004)), separators=(",", ":")
        )
        nested_dst = _uuid(1005)
        duplicate_lines = [
            valid_evidence.replace(
                '"table":"evidence"', '"table":"evidence","table":"evidence"', 1
            ),
            valid_evidence.replace(
                f'"id":"{valid["id"]}"',
                f'"id":"{valid["id"]}","id":"{valid["id"]}"', 1,
            ),
            valid_evidence.replace(
                f'"hash":"{valid["hash"]}"',
                f'"hash":"{valid["hash"]}","hash":"{valid["hash"]}"', 1,
            ),
            valid_memory.replace(
                '"links":[]',
                (
                    '"links":[{"dst":"%s","relation":"related",'
                    '"score":0.5,"created_at":"","dst":"%s"}]'
                ) % (nested_dst, nested_dst),
                1,
            ),
        ]
        for index, line in enumerate(duplicate_lines):
            with self.subTest(index=index):
                path = self.root / f"duplicate-{index}.jsonl"
                payload = line + "\n"
                if index == 3:
                    payload = json.dumps(
                        _memory_row(nested_dst), separators=(",", ":")
                    ) + "\n" + payload
                path.write_text(payload, encoding="utf-8")
                diagnostic = io.StringIO()
                with contextlib.redirect_stderr(diagnostic):
                    result = sync.cmd_ingest_jsonl_strict(
                        self.conn, in_path=str(path), source_ref=None,
                    )
                self.assertEqual(result, 2)
                self.assertIn("duplicate JSON key", diagnostic.getvalue())
                self._assert_empty("memory", "evidence", "memory_evidence")
        # A valid control proves the test failures above are duplicate-key
        # rejection rather than a permanently unusable destination.
        control = self.root / "control.jsonl"
        _write_rows(control, [valid])
        self.assertEqual(
            sync.cmd_ingest_jsonl_strict(
                self.conn, in_path=str(control), source_ref=None,
            ),
            0,
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 1)


class StrictStagingAndLegacyTest(_StrictEvidenceCase):
    def test_replacement_after_staging_does_not_change_imported_bytes(self):
        old_path = self.root / "staged.jsonl"
        old_memory = _memory_row(_uuid(1101), content="staged old")
        replacement = _memory_row(_uuid(1102), content="replacement new")
        rows = [old_memory, _evidence_row(_uuid(1103))]
        _write_rows(old_path, rows)
        original = sync._strict_ingest_staged

        def replace_after_stage(conn, spool, **kwargs):
            _write_rows(old_path, [replacement, _evidence_row(_uuid(1104))])
            return original(conn, spool, **kwargs)

        sync._strict_ingest_staged = replace_after_stage
        try:
            self.assertEqual(
                sync.cmd_ingest_jsonl(
                    self.conn, in_path=str(old_path), source_ref=None,
                ),
                0,
            )
        finally:
            sync._strict_ingest_staged = original
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM memory WHERE id=?", (_uuid(1101),)
        ).fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM memory WHERE id=?", (_uuid(1102),)
        ).fetchone())

    def test_strict_bad_discriminator_rolls_back_but_legacy_best_effort_survives(self):
        strict_path = self.root / "bad-discriminator.jsonl"
        strict_path.write_text(
            json.dumps(_memory_row(_uuid(1201)), separators=(",", ":"))
            + "\n{\"table\":\n",
            encoding="utf-8",
        )
        self.assertEqual(
            sync.cmd_ingest_jsonl_strict(
                self.conn, in_path=str(strict_path), source_ref=None,
            ),
            2,
        )
        self._assert_empty("memory", "evidence")

        self.assertEqual(
            sync.cmd_ingest_jsonl(
                self.conn, in_path=str(strict_path), source_ref=None,
            ),
            0,
        )
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory WHERE id=?", (_uuid(1201),)
        ).fetchone()[0], 1)

    def test_legacy_detector_ignores_parseable_tail_of_oversized_line(self):
        path = self.root / "oversized-legacy.jsonl"
        valid = json.dumps(_memory_row(_uuid(1251)), separators=(",", ":"))
        # The table-shaped object is deliberately only a tail fragment of one
        # oversized physical line. The detector must not mistake that tail for
        # a second top-level JSON record and route the whole file to strict.
        oversized = "x" * (sync.MAX_LINE_CHARS + 1) + '{"table":"evidence"}'
        path.write_text(valid + "\n" + oversized + "\n", encoding="utf-8")
        self.assertEqual(
            sync.cmd_ingest_jsonl(self.conn, in_path=str(path), source_ref=None),
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM memory WHERE id=?", (_uuid(1251),)
            ).fetchone()[0],
            1,
        )


class StrictReferenceAtomicityTest(_StrictEvidenceCase):
    def test_duplicate_association_and_membership_primary_keys_reject(self):
        memory_id = _uuid(1301)
        evidence_id = _uuid(1302)
        assoc_path = self.root / "duplicate-association.jsonl"
        assoc = {"table": "memory_evidence", "memory_id": memory_id,
                 "evidence_id": evidence_id}
        _write_rows(assoc_path, [_memory_row(memory_id), _evidence_row(evidence_id), assoc, assoc])
        self.assertEqual(
            sync.cmd_ingest_jsonl(self.conn, in_path=str(assoc_path), source_ref=None),
            2,
        )
        self._assert_empty("memory", "evidence", "memory_evidence")

        episode_id = _uuid(1303)
        membership_path = self.root / "duplicate-membership.jsonl"
        membership = {"kind": "episode_memory", "episode_id": episode_id,
                      "memory_id": memory_id, "added_at": "2026-09-10T00:00:00Z"}
        episode = {
            "kind": "episode", "id": episode_id, "namespace": "project:p",
            "started_at": "2026-09-10T00:00:00Z", "ended_at": "",
            "summary_memory_id": "", "token_count": 0,
        }
        _write_rows(membership_path, [_memory_row(memory_id), episode, membership, membership])
        self.assertEqual(
            sync.cmd_ingest_jsonl_strict(
                self.conn, in_path=str(membership_path), source_ref=None,
            ),
            2,
        )
        self._assert_empty("memory", "episode", "episode_memory")

    def test_dangling_references_reject_before_mutation(self):
        path = self.root / "dangling.jsonl"
        _write_rows(path, [
            _evidence_row(_uuid(1401)),
            {"table": "memory_evidence", "memory_id": _uuid(1402),
             "evidence_id": _uuid(1401)},
        ])
        self.assertEqual(
            sync.cmd_ingest_jsonl_strict(
                self.conn, in_path=str(path), source_ref=None,
            ),
            2,
        )
        self._assert_empty("memory", "evidence", "memory_evidence")

    def test_late_apply_failure_rolls_back_every_table(self):
        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith("INSERT OR IGNORE INTO memory_evidence"):
                    raise sqlite3.OperationalError("injected late association failure")
                return super().execute(sql, parameters)

        conn = sqlite3.connect(self.root / "late-failure.sqlite", factory=FailingConnection)
        conn.row_factory = sqlite3.Row
        schema.init_db(conn)
        schema.migrate(conn)
        memory_id = _uuid(1501)
        linked_id = _uuid(1504)
        episode_id = _uuid(1502)
        evidence_id = _uuid(1503)
        path = self.root / "late-failure.jsonl"
        linked_memory = _memory_row(linked_id, content="Paris")
        source_memory = _memory_row(memory_id, content="Alice visited Paris")
        source_memory["links"] = [{
            "dst": linked_id, "relation": "related", "score": 0.5,
            "created_at": "2026-09-10T00:00:00Z",
        }]
        _write_rows(path, [
            source_memory,
            linked_memory,
            {
                "kind": "episode", "id": episode_id, "namespace": "project:p",
                "started_at": "2026-09-10T00:00:00Z", "ended_at": "",
                "summary_memory_id": memory_id, "token_count": 1,
            },
            {"kind": "episode_memory", "episode_id": episode_id,
             "memory_id": memory_id, "added_at": "2026-09-10T00:00:00Z"},
            _evidence_row(evidence_id),
            {"table": "episode_evidence", "episode_id": episode_id,
             "evidence_id": evidence_id},
            {"table": "memory_evidence", "memory_id": memory_id,
             "evidence_id": evidence_id},
        ])
        snapshot_tables = (
            "memory", "episode", "episode_memory", "evidence",
            "episode_evidence", "memory_evidence", "memory_link",
            "entity", "entity_alias", "memory_entity", "memory_fts",
        )
        before_schema = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index') "
            "ORDER BY type, name"
        ).fetchall()
        before_rows = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in snapshot_tables
        }
        try:
            self.assertEqual(
                sync.cmd_ingest_jsonl_strict(conn, in_path=str(path), source_ref=None),
                2,
            )
            after_schema = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index') "
                "ORDER BY type, name"
            ).fetchall()
            self.assertEqual(after_schema, before_schema)
            after_rows = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in snapshot_tables
            }
            self.assertEqual(after_rows, before_rows)
        finally:
            conn.close()


class StrictLargeScopeTest(_StrictEvidenceCase):
    def test_large_export_and_retention_avoid_variable_limit(self):
        memory_id = _uuid(1601)
        self.conn.execute(
            "INSERT INTO memory(id, namespace, type, content, ingestion_ts) "
            "VALUES (?, 'project:p', 'fact', 'large scope', '2026-09-10T00:00:00Z')",
            (memory_id,),
        )
        rows = []
        for index in range(1700):
            evidence_id = _uuid(1702 + index)
            rows.append((memory_id, evidence_id))
            evidence.write_evidence(
                self.conn, session_id="large", lane="codex", moment="pretool",
                kind="tool_call", ts="2026-09-10T00:00:00Z", excerpt=f"row-{index}",
                ref_path="large.jsonl", ref_offset=index, id=evidence_id,
            )
        self.conn.executemany(
            "INSERT INTO memory_evidence(memory_id, evidence_id) VALUES (?, ?)", rows
        )
        self.conn.commit()
        out = self.root / "large-export.jsonl"
        old_limit = self.conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        try:
            self.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 100)
            self.assertEqual(
                sync.cmd_export_jsonl(
                    self.conn, out=str(out), namespace="project:p",
                ),
                0,
            )
            exported = [
                json.loads(line)
                for line in out.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                sum(row.get("table") == "evidence" for row in exported), 1700
            )
            self.assertEqual(
                sum(row.get("table") == "memory_evidence" for row in exported), 1700
            )

            os.environ["ZMEM_EVIDENCE_DAYS"] = "0"
            os.environ["ZMEM_EVIDENCE_CAP"] = "500"
            result = evidence.sweep_evidence(
                self.conn, now_ts="2026-09-10T00:00:00Z"
            )
            self.assertEqual(result["expired"], 0)
            self.assertEqual(result["capped"], 1200)
            self.assertEqual(result["memory_links"], 1200)
            self.assertEqual(
                self.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 500
            )
        finally:
            self.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit)


class StrictClosureRoundTripTest(_StrictEvidenceCase):
    def _seed_parent(self, mid: str, superseded_at: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO memory(id, namespace, type, content, ingestion_ts, "
            "valid_until, superseded_at, supersede_reason) VALUES (?, 'project:p', "
            "'fact', ?, '2026-09-10T00:00:00Z', ?, ?, ?)",
            (mid, mid, superseded_at or "", superseded_at, "test" if superseded_at else ""),
        )

    def _seed_evidence(self, eid: str, mid: str) -> None:
        evidence.write_evidence(
            self.conn, session_id="closure", lane="codex", moment="user_prompt",
            kind="turn", ts="2026-09-10T00:00:00Z", excerpt=mid,
            ref_path="closure.txt", ref_offset=0, id=eid,
        )
        self.conn.execute(
            "INSERT INTO memory_evidence(memory_id, evidence_id) VALUES (?, ?)",
            (mid, eid),
        )

    def _import_and_counts(self, source: Path, suffix: str) -> tuple[int, int, int]:
        destination = sqlite3.connect(self.root / f"dest-{suffix}.sqlite")
        destination.row_factory = sqlite3.Row
        schema.init_db(destination)
        schema.migrate(destination)
        try:
            self.assertEqual(
                sync.cmd_ingest_jsonl_strict(
                    destination, in_path=str(source), source_ref=None,
                ),
                0,
            )
            return tuple(
                destination.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("memory", "evidence", "memory_evidence")
            )
        finally:
            destination.close()

    def test_unscoped_live_and_superseded_associations_roundtrip(self):
        live = _uuid(1701)
        superseded = _uuid(1702)
        live_evidence = _uuid(1703)
        old_evidence = _uuid(1704)
        self._seed_parent(live)
        self._seed_parent(superseded, "2026-09-11T00:00:00Z")
        self._seed_evidence(live_evidence, live)
        self._seed_evidence(old_evidence, superseded)
        self.conn.commit()

        live_export = self.root / "live-only.jsonl"
        all_export = self.root / "include-superseded.jsonl"
        self.assertEqual(sync.cmd_export_jsonl(self.conn, out=str(live_export)), 0)
        self.assertEqual(
            sync.cmd_export_jsonl(
                self.conn, out=str(all_export), include_superseded=True,
            ),
            0,
        )
        self.assertEqual(self._import_and_counts(live_export, "live"), (1, 2, 1))
        self.assertEqual(self._import_and_counts(all_export, "all"), (2, 2, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
