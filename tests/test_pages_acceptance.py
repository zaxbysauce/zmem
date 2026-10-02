"""Independent executable acceptance checks for issue #138."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pages" / "repro"
STORE_SCRIPT = SCRIPTS / "store.py"
PAGE_ID = "fixture-page"
NAMESPACE = "project:test"
NOW = "2026-09-10T00:01:00Z"
SOURCE_IDS = [
    "belief:fixture-501",
    "00000000-0000-4000-8000-000000000502",
    "00000000-0000-4000-8000-000000000503",
    "00000000-0000-4000-8000-000000000504",
]
EVIDENCE_IDS = ["ev-501", "ev-502", "ev-503", "ev-504"]
def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
def _tree_bytes(root: Path) -> dict[str, bytes]:
    """Stable byte snapshot used for atomic-publication assertions."""
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)).replace("\\", "/"): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for name, data in _tree_bytes(root).items():
        h.update(name.encode())
        h.update(b"\0")
        h.update(data)
    return h.hexdigest()
@contextlib.contextmanager
def _isolated_env(root: Path):
    old = os.environ.copy()
    os.environ.update({
        "ZMEM_STORE": str(root / "store.sqlite"),
        "ZMEM_DATA": str(root / "data"),
        "ZMEM_MODELS_DIR": str(root / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_HOME": str(root / "home"),
        "HOME": str(root / "home"),
        "USERPROFILE": str(root / "home"),
    })
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)
def _runtime():
    """Import feature/runtime modules only after the disposable env exists."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        import storelib  # type: ignore
        from storelib import beliefs, inject, schema  # type: ignore
        import schema_meta  # type: ignore
        from storelib import delivery_ledger  # type: ignore
        from storelib import pages  # type: ignore
    finally:
        # Leave the path idempotent for a single unittest process, but do not
        # rely on a global storelib import to configure a later test.
        try:
            sys.path.remove(str(SCRIPTS))
        except ValueError:
            pass
    return storelib, beliefs, inject, schema, schema_meta, delivery_ledger, pages
def _source_rows() -> list[dict]:
    return [json.loads(line) for line in
            (FIXTURES / "page-sources.jsonl").read_text(encoding="utf-8").splitlines()]
def _seed_store(root: Path):
    """Seed a private v14 store with one head and four grounded rows."""
    _storelib, _beliefs, _inject, schema, _meta, _ledger, _pages = _runtime()
    db = sqlite3.connect(root / "store.sqlite")
    db.row_factory = sqlite3.Row
    schema.init_db(db)
    schema.migrate(db)
    rows = _source_rows()
    db.execute(
        """INSERT INTO evidence
           (id, session_id, lane, moment, kind, ts, hash, excerpt,
            ref_path, ref_offset)
           VALUES ('ev-501', 's-page-fixture', 'codex', 'user_prompt',
                   'correction', '2026-09-10T00:00:01Z', 'hash-501',
                   'evidence for belief head 501', 'fixture', -1)""",
    )
    for row in rows:
        db.execute(
            """INSERT INTO memory
               (id, namespace, type, content, tags, source_ref, source_hash,
                confidence, signal, valid_from, superseded_at, ingestion_ts,
                retrieval_count, taint, trust_score)
               VALUES (?, ?, ?, ?, ?, '', '', ?, ?, '', NULL, ?, 0, ?, ?)""",
            (row["id"], row["namespace"], row["type"], row["content"],
             row["tags"], row["confidence"], row["signal"],
             row["ingestion_ts"], row["taint"], row["trust_score"]),
        )
        ev = row["evidence"]
        db.execute(
            """INSERT INTO evidence
               (id, session_id, lane, moment, kind, ts, hash, excerpt,
                ref_path, ref_offset)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ev["id"], ev["session_id"], ev["lane"], ev["moment"],
             ev["kind"], ev["ts"], ev["hash"], ev["excerpt"],
             ev["ref_path"], ev["ref_offset"]),
        )
        db.execute(
            "INSERT INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
            (row["id"], ev["id"]),
        )

    # The page source query consumes this pre-existing belief head from #137.
    head_sources = [r["id"] for r in rows[:3]]
    db.execute(
        """INSERT INTO belief_head
           (id, namespace, topic_identity, content, head_state, head_source_id,
            support_count, refresh_watermark, generator_revision, confidence,
            signal, taint, trust_score)
           VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("fixture-501", NAMESPACE, "fixture-501",
         "Fixture topic head summary", head_sources[-1], len(head_sources),
         NOW, "beliefs-v1", 0.90, "user", "trusted_internal", 0.93),
    )
    for row in rows[:3]:
        db.execute(
            """INSERT INTO belief_head_source
               (head_id, source_id, role, source_ingestion_ts, source_checksum)
               VALUES (?, ?, 'support', ?, ?)""",
            ("fixture-501", row["id"], row["ingestion_ts"],
             hashlib.sha256(row["content"].encode()).hexdigest()),
        )
        db.execute(
            "INSERT INTO belief_head_evidence (head_id, source_id, evidence_id) VALUES (?, ?, ?)",
            ("fixture-501", row["id"], row["evidence"]["id"]),
        )
    db.execute(
        "INSERT INTO belief_head_evidence (head_id, source_id, evidence_id) VALUES (?, ?, ?)",
        ("fixture-501", "belief:fixture-501", "ev-501"),
    )
    db.commit()
    return db, _pages
def _page_dir(root: Path) -> Path:
    return root / "data" / "pages" / PAGE_ID
def _copy_base_page(root: Path) -> None:
    page_dir = _page_dir(root)
    page_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / "page-base.md", page_dir / "page.md")
def _write_manual_page(root: Path) -> None:
    """Create the smallest valid read/list fixture without SQLite."""
    page_dir = _page_dir(root)
    (page_dir / "versions").mkdir(parents=True, exist_ok=True)
    page = (FIXTURES / "page-base.md").read_bytes()
    checksum = hashlib.sha256(page).hexdigest()
    current = {
        "version_id": "v000001",
        "freshness_watermark": "2026-09-10T00:00:04Z:fixture-snapshot",
        "source_ids": SOURCE_IDS,
        "evidence_ids": EVIDENCE_IDS,
        "retracted_source_ids": [],
        "page_checksum": checksum,
    }
    definition = {
        "namespace": NAMESPACE,
        "query": "fixture topic",
        "tags": ["fixture-topic"],
        "generator_revision": "pages-v1",
        "creation_policy": "explicit",
    }
    (page_dir / "page.md").write_bytes(page)
    (page_dir / "definition.json").write_bytes(_json_bytes(definition))
    (page_dir / "current.json").write_bytes(_json_bytes(current))
    version = dict(current)
    version["content"] = page.decode("utf-8")
    (page_dir / "versions" / "v000001.json").write_bytes(_json_bytes(version))
def _current(root: Path) -> dict:
    return json.loads((_page_dir(root) / "current.json").read_text(encoding="utf-8"))
def _refresh(pages, db, root: Path, *, adapter=None, query="fixture topic"):
    return pages.page_refresh(
        db, data_dir=str(root / "data"), page_id=PAGE_ID, query=query,
        namespace=NAMESPACE, tags=("fixture-topic",),
        llm_local=adapter is not None, adapter=adapter, now=NOW,
    )
def _first_citation(payload: dict) -> str:
    """Find a real candidate citation without assuming adapter payload nesting."""
    def walk(value):
        if isinstance(value, dict):
            for key in ("evidence_ids", "citations", "source_ids"):
                values = value.get(key)
                if isinstance(values, list):
                    for item in values:
                        if isinstance(item, str) and item.startswith(("ev-", "belief:", "00000000-")):
                            return item
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return None
    return walk(payload) or "ev-502"
class PagesAcceptanceChecks(unittest.TestCase):
    """One executable row per numbered issue acceptance criterion."""

    def test_ac1_store_independent_read_and_list(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-ac1-") as tmp:
            root = Path(tmp)
            _write_manual_page(root)
            before = _tree_digest(root)
            with _isolated_env(root):
                _storelib, _beliefs, _inject, _schema, _meta, _ledger, pages = _runtime()
                read = pages.page_read(data_dir=str(root / "data"), page_id=PAGE_ID)
                listed = pages.page_list(data_dir=str(root / "data"), namespace=NAMESPACE)
                self.assertIsInstance(read, dict)
                content = read.get("content", read.get("page", ""))
                self.assertEqual(content.encode("utf-8"), (FIXTURES / "page-base.md").read_bytes())
                self.assertEqual(len(listed), 1)
                self.assertNotIn("content", listed[0])
                env = os.environ.copy()
                # The passive CLI resolves sidecars from the explicit store
                # parent before ZMEM_DATA. Align this absent store with the
                # manually seeded page root; read/list must never create it.
                env["ZMEM_STORE"] = str(root / "data" / "store.sqlite")
                cli_read = subprocess.run(
                    [sys.executable, str(STORE_SCRIPT), "page", "read", "--id", PAGE_ID],
                    cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
                    check=False,
                )
                self.assertEqual(cli_read.returncode, 0, cli_read.stderr)
                cli_read_payload = json.loads(cli_read.stdout)
                if isinstance(cli_read_payload, dict) and "result" in cli_read_payload:
                    cli_read_payload = cli_read_payload["result"]
                cli_content = cli_read_payload.get("content", cli_read_payload.get("page", ""))
                self.assertEqual(cli_content.encode("utf-8"), (FIXTURES / "page-base.md").read_bytes())
                cli_list = subprocess.run(
                    [sys.executable, str(STORE_SCRIPT), "page", "list", "--namespace", NAMESPACE],
                    cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
                    check=False,
                )
                self.assertEqual(cli_list.returncode, 0, cli_list.stderr)
                cli_list_payload = json.loads(cli_list.stdout)
                if isinstance(cli_list_payload, dict):
                    cli_list_payload = cli_list_payload.get("results", cli_list_payload.get("pages", []))
                self.assertEqual(len(cli_list_payload), 1)
            self.assertEqual(before, _tree_digest(root))
            self.assertFalse((root / "store.sqlite").exists())
            self.assertFalse((root / "data" / "store.sqlite").exists())

    def test_ac2_refresh_is_deterministic_and_versions_are_immutable(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-ac2-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = _seed_store(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                first_current = _current(root)
                first_page = (_page_dir(root) / "page.md").read_bytes()
                first_version = (_page_dir(root) / "versions" / "v000001.json").read_bytes()
                _refresh(pages, db, root)
                second_current = _current(root)
                second_page = (_page_dir(root) / "page.md").read_bytes()
                second_version = (_page_dir(root) / "versions" / "v000002.json").read_bytes()
                self.assertEqual(first_current["version_id"], "v000001")
                self.assertEqual(second_current["version_id"], "v000002")
                self.assertEqual(first_page, second_page)
                self.assertEqual(first_current["freshness_watermark"], second_current["freshness_watermark"])
                self.assertEqual(first_current["source_ids"], second_current["source_ids"])
                self.assertEqual(first_current["evidence_ids"], second_current["evidence_ids"])
                self.assertEqual(first_current["page_checksum"], second_current["page_checksum"])
                self.assertEqual(first_version, second_version.replace(b'"v000002"', b'"v000001"'))
            db.close()

    def test_ac3_valid_delta_is_byte_local_and_failures_are_atomic(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-ac3-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = _seed_store(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                stable_before = (_page_dir(root) / "page.md").read_bytes().split(
                    b"<!-- section:refresh -->", 1)[0]
                fixture_patch = json.loads((FIXTURES / "patches.json").read_text(encoding="utf-8"))
                payloads = []

                def valid_adapter(payload):
                    payloads.append(payload)
                    citation = _first_citation(payload)
                    return {"operations": [
                        {"op": "replace_section", "section_id": "refresh",
                         "markdown": "Patched caf\u00e9", "citations": [citation]},
                        {"op": "append_bullet", "section_id": "refresh",
                         "markdown": "Patched bullet", "citations": [citation]},
                    ]}

                # The dynamic callback uses a real in-scope citation, while the
                # fixture records the exact operation shape for reviewer replay.
                self.assertEqual(fixture_patch["valid"]["operations"][0]["op"], "replace_section")
                _refresh(pages, db, root, adapter=valid_adapter)
                patched = (_page_dir(root) / "page.md").read_bytes()
                self.assertEqual(patched.split(b"<!-- section:refresh -->", 1)[0], stable_before)
                self.assertIn("Patched caf\u00e9".encode("utf-8"), patched)
                self.assertTrue(payloads)
                self.assertLessEqual(len(payloads[-1].get("candidates", [])), 20)

                before_failure = _tree_bytes(_page_dir(root))

                def adapter_exception(_payload):
                    raise RuntimeError("adapter failure")

                refused_adapters = [
                    ("unknown-target", lambda _payload: {
                        "operations": fixture_patch["invalid"]["operations"]}),
                    ("unresolved-citation", lambda _payload: {
                        "operations": fixture_patch["unresolved_citation"]["operations"]}),
                    ("empty-result", lambda _payload: {}),
                    ("adapter-exception", adapter_exception),
                ]
                for label, refused in refused_adapters:
                    with self.subTest(failure=label):
                        try:
                            result = _refresh(pages, db, root, adapter=refused)
                        except Exception:
                            result = None
                        else:
                            self.assertFalse(result.get("success", result.get("ok", True)))
                        self.assertEqual(before_failure, _tree_bytes(_page_dir(root)))

                # Fault the second atomic rename after staging has become
                # visible.  The candidate must leave current/page/history and
                # the staged directory exactly as they were before the call.
                replace_calls = 0
                real_replace = pages.os.replace

                def fail_after_first_replace(src, dst):
                    nonlocal replace_calls
                    replace_calls += 1
                    if replace_calls == 2:
                        raise OSError("injected publication fault")
                    return real_replace(src, dst)

                with patch.object(pages.os, "replace", fail_after_first_replace):
                    try:
                        result = _refresh(pages, db, root, adapter=valid_adapter)
                    except Exception:
                        result = None
                    else:
                        self.assertFalse(result.get("success", result.get("ok", True)))
                self.assertGreaterEqual(replace_calls, 2)
                self.assertEqual(before_failure, _tree_bytes(_page_dir(root)))
            db.close()

    def test_ac4_tombstone_retracts_only_the_next_version(self):
        tombstone_id = "00000000-0000-4000-8000-000000000504"
        with tempfile.TemporaryDirectory(prefix="zmem-pages-ac4-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = _seed_store(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                prior = _current(root)
                prior_version = json.loads(
                    (_page_dir(root) / "versions" / "v000001.json").read_text(encoding="utf-8"))
                self.assertIn(tombstone_id, prior["source_ids"])
                db.execute("UPDATE memory SET superseded_at=? WHERE id=?", (NOW, tombstone_id))
                db.commit()
                _refresh(pages, db, root)
                current = _current(root)
                current_version = json.loads(
                    (_page_dir(root) / "versions" / "v000002.json").read_text(encoding="utf-8"))
                current_bytes = (_page_dir(root) / "page.md").read_bytes()
                historical = pages.page_read(
                    data_dir=str(root / "data"), page_id=PAGE_ID,
                    version_id="v000001",
                )
                historical_content = historical.get(
                    "content", historical.get("page", ""))
                self.assertEqual(historical.get("version_id"), "v000001")
                self.assertIn("Fixture topic source tombstone", historical_content)
                self.assertNotIn("Fixture topic source tombstone", current_bytes.decode("utf-8"))
                self.assertIn(tombstone_id, prior_version["source_ids"])
                self.assertNotIn(tombstone_id, current["source_ids"])
                self.assertIn(tombstone_id, current.get("retracted_source_ids", []))
                self.assertNotIn(tombstone_id, current_version["source_ids"])
                self.assertIn(tombstone_id, prior_version.get("source_ids", []))
            db.close()

    def test_ac5_selector_budget_fence_ledger_and_zero_page_suppression(self):
        session = "pages-ac5-session"
        with tempfile.TemporaryDirectory(prefix="zmem-pages-ac5-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                os.environ["ZMEM_INJECT_TOKEN_BUDGET"] = "1500"
                os.environ["ZMEM_INJECT_FLOOR_PROMPT"] = "0.25"
                os.environ["ZMEM_INJECT_FLOOR_GATE_NONE"] = "0.4"
                db, pages = _seed_store(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                db.execute(
                    """INSERT INTO memory
                       (id, namespace, type, content, tags, source_ref, source_hash,
                        confidence, signal, valid_from, superseded_at, ingestion_ts,
                        retrieval_count, taint, trust_score)
                       VALUES (?, ?, 'fact', ?, 'fixture-topic', '', '',
                               0.30, 'none', '', NULL, ?, 0,
                               'trusted_internal', 0.30)""",
                    ("00000000-0000-4000-8000-000000000506", NAMESPACE,
                     "Fixture topic below gate floor", "2026-09-10T00:00:06Z"),
                )
                db.commit()
                _storelib, _beliefs, inject, _schema, _meta, ledger, _pages = _runtime()
                payload = inject.select_and_budget_for_injection(
                    db, query="fixture topic", namespace=NAMESPACE,
                    moment="user_prompt", session_id=session, lane="codex",
                    budget_tokens=1500, data_dir=str(root / "data"),
                )
                required = {"results", "count", "omitted", "reason", "excluded",
                            "candidate_ids", "tokens_used", "tokens_budget",
                            "budget_dropped", "budget_admission", "budget_truncated",
                            "budget_dropped_protected", "arms", "rendered"}
                self.assertTrue(required.issubset(payload))
                self.assertLessEqual(payload["tokens_used"], 1500)
                self.assertEqual(payload["tokens_budget"], 1500)
                self.assertIn("<<<ZMEM_UNTRUSTED_FENCE>>>", payload["rendered"])
                self.assertIn("<<<END_ZMEM_UNTRUSTED_FENCE>>>", payload["rendered"])
                self.assertIn("untrusted", payload["rendered"].lower())
                self.assertFalse(any(r.get("id") == "00000000-0000-4000-8000-000000000506"
                                     for r in payload["results"]),
                                 "signal=none row below the gate floor was delivered")
                page_ids = [r["id"] for r in payload["results"] if r.get("type") == "page"]
                self.assertTrue(page_ids, payload)
                ledger_file = Path(ledger.ledger_path(str(root / "data"), session))
                self.assertTrue(ledger_file.exists())
                delivered_payload = json.loads(ledger_file.read_text(encoding="utf-8"))
                self.assertIsInstance(delivered_payload, dict)
                delivered = delivered_payload["entries"]
                self.assertIsInstance(delivered, list)
                self.assertTrue(any(e.get("id") in page_ids for e in delivered))

                filtered = inject.select_and_budget_for_injection(
                    db, query="source alpha", namespace=NAMESPACE,
                    moment="user_prompt", session_id="pages-ac5-filtered",
                    lane="codex", budget_tokens=1500, data_dir=str(root / "data"),
                    exclude_ids=page_ids + ["belief:fixture-501"],
                )
                self.assertFalse(any(r.get("type") == "page" for r in filtered["results"]))
                self.assertTrue(any(r.get("id") == "00000000-0000-4000-8000-000000000502"
                                    for r in filtered["results"]))

                budgeted = inject.select_and_budget_for_injection(
                    db, query="source alpha", namespace=NAMESPACE,
                    moment="user_prompt", session_id="pages-ac5-budget",
                    lane="codex", budget_tokens=220, data_dir=str(root / "data"),
                    exclude_ids=["belief:fixture-501"],
                )
                self.assertTrue(any(pid in budgeted["candidate_ids"] for pid in page_ids))
                self.assertFalse(any(r.get("type") == "page" for r in budgeted["results"]))
                self.assertGreaterEqual(budgeted["budget_dropped"], 1)
                self.assertTrue(any(r.get("id") == "00000000-0000-4000-8000-000000000502"
                                    for r in budgeted["results"]))
                budget_ledger = Path(ledger.ledger_path(
                    str(root / "data"), "pages-ac5-budget"))
                budget_payload = json.loads(budget_ledger.read_text(encoding="utf-8"))
                self.assertIsInstance(budget_payload, dict)
                budget_entries = budget_payload["entries"]
                self.assertIsInstance(budget_entries, list)
                self.assertFalse(any(e.get("id") in page_ids for e in budget_entries))

                os.environ["ZMEM_INJECT_FLOOR_PROMPT"] = "0.91"
                floored = inject.select_and_budget_for_injection(
                    db, query="source alpha", namespace=NAMESPACE,
                    moment="user_prompt", session_id="pages-ac5-floor",
                    lane="codex", budget_tokens=1500, data_dir=str(root / "data"),
                    exclude_ids=["belief:fixture-501"],
                )
                self.assertTrue(any(pid in floored["candidate_ids"] for pid in page_ids))
                self.assertFalse(any(r.get("type") == "page" for r in floored["results"]))
                floor_ledger = Path(ledger.ledger_path(
                    str(root / "data"), "pages-ac5-floor"))
                if floor_ledger.exists():
                    floor_payload = json.loads(floor_ledger.read_text(encoding="utf-8"))
                    self.assertIsInstance(floor_payload, dict)
                    floor_entries = floor_payload["entries"]
                    self.assertIsInstance(floor_entries, list)
                    self.assertFalse(any(e.get("id") in page_ids for e in floor_entries))
            db.close()

    def test_ac6_provenance_checksum_and_query_never_enters_memory(self):
        marker = "page-query-never-memory"
        with tempfile.TemporaryDirectory(prefix="zmem-pages-ac6-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = _seed_store(root)
                _copy_base_page(root)
                before = db.execute("SELECT id, content FROM memory ORDER BY id").fetchall()
                _refresh(pages, db, root, query="fixture topic " + marker)
                current = _current(root)
                injected = pages.page_for_injection(data_dir=str(root / "data"), page_id=PAGE_ID)
                self.assertEqual(set(injected), {
                    "id", "namespace", "type", "content", "source_ref",
                    "source_ids", "evidence_ids", "version_id",
                    "freshness_watermark", "page_checksum",
                })
                self.assertEqual(injected["type"], "page")
                self.assertRegex(injected["page_checksum"], r"^[0-9a-f]{64}$")
                self.assertEqual(injected["page_checksum"], current["page_checksum"])
                self.assertEqual(injected["version_id"], current["version_id"])
                self.assertEqual(injected["source_ids"], current["source_ids"])
                self.assertEqual(injected["evidence_ids"], current["evidence_ids"])
                page_bytes = (_page_dir(root) / "page.md").read_bytes()
                for token in current["source_ids"] + current["evidence_ids"]:
                    self.assertIn(token.encode(), page_bytes)
                after = db.execute("SELECT id, content FROM memory ORDER BY id").fetchall()
                self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
                self.assertNotIn(marker, "\n".join(r[1] for r in after))
            db.close()

    def test_ac7_reject_gate_omits_runbook_and_preserves_type_vocabulary(self):
        gate = REPO_ROOT / "evidence" / "gates" / "172-observation.json"
        self.assertEqual(gate.read_bytes(), b'{"observation":"reject"}\n')
        with tempfile.TemporaryDirectory(prefix="zmem-pages-ac7-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = _seed_store(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                _storelib, _beliefs, _inject, _schema, schema_meta, _ledger, _pages = _runtime()
                current = _current(root)
                definition = json.loads(
                    (_page_dir(root) / "definition.json").read_text(encoding="utf-8"))
                self.assertNotIn("runbook", current)
                self.assertNotIn("runbook", definition)
                self.assertNotIn("page", schema_meta.ALLOWED_TYPES)
                effective = getattr(schema_meta, "allowed_types", None)
                if effective is not None:
                    self.assertEqual(tuple(effective()), tuple(schema_meta.ALLOWED_TYPES))
            db.close()
if __name__ == "__main__":
    unittest.main()
