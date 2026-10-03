"""Lifecycle and byte-contract tests for curated pages (issue #138).

These tests use a disposable SQLite store and explicit ``data_dir`` values for
every store-backed operation.  The fixture data is reviewed input; no expected
value is obtained by calling the implementation under test.
"""

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


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_SCRIPT = SCRIPTS_DIR / "store.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pages"
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
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)).replace("\\", "/"): path.read_bytes()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for name, payload in _tree_bytes(root).items():
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
    return digest.hexdigest()


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
    """Import storelib only after the test's disposable env is installed."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from storelib import beliefs, pages, schema  # type: ignore
        import schema_meta  # type: ignore
    finally:
        try:
            sys.path.remove(str(SCRIPTS_DIR))
        except ValueError:
            pass
    return beliefs, pages, schema, schema_meta


def _source_rows() -> list[dict]:
    return [json.loads(line) for line in
            (FIXTURES / "page-sources.jsonl").read_text(
                encoding="utf-8").splitlines() if line.strip()]


def _seed_store(root: Path):
    beliefs, pages, schema, _meta = _runtime()
    db = sqlite3.connect(root / "store.sqlite")
    db.row_factory = sqlite3.Row
    schema.init_db(db)
    schema.migrate(db)
    db.execute(
        """INSERT INTO evidence
           (id, session_id, lane, moment, kind, ts, hash, excerpt,
            ref_path, ref_offset)
           VALUES ('ev-501', 's-page-fixture', 'codex', 'user_prompt',
                   'correction', '2026-09-10T00:00:01Z', 'hash-501',
                   'evidence for belief head 501', 'fixture', -1)""",
    )
    rows = _source_rows()
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

    member_ids = [row["id"] for row in rows]
    db.execute(
        """INSERT INTO belief_head
           (id, namespace, topic_identity, content, head_state, head_source_id,
            support_count, refresh_watermark, generator_revision, confidence,
            signal, taint, trust_score)
           VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("fixture-501", NAMESPACE, "fixture-501", "Fixture topic head summary",
         member_ids[-1], len(member_ids), "2026-09-10T00:00:04Z",
         "beliefs-v1", 0.90, "user", "trusted_internal", 0.93),
    )
    for row in rows:
        db.execute(
            """INSERT INTO belief_head_source
               (head_id, source_id, role, source_ingestion_ts, source_checksum)
               VALUES (?, ?, 'support', ?, ?)""",
            ("fixture-501", row["id"], row["ingestion_ts"],
             hashlib.sha256(row["content"].encode("utf-8")).hexdigest()),
        )
        db.execute(
            "INSERT INTO belief_head_evidence (head_id, source_id, evidence_id) "
            "VALUES (?, ?, ?)", ("fixture-501", row["id"], row["evidence"]["id"]),
        )
    db.execute(
        "INSERT INTO belief_head_evidence (head_id, source_id, evidence_id) "
        "VALUES (?, 'belief:fixture-501', 'ev-501')", ("fixture-501",),
    )
    db.commit()
    return db, pages


def _page_dir(root: Path) -> Path:
    return root / "data" / "pages" / PAGE_ID


def _copy_base_page(root: Path) -> None:
    page_dir = _page_dir(root)
    page_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / "page-base.md", page_dir / "page.md")


def _write_manual_page(root: Path) -> None:
    page_dir = _page_dir(root)
    (page_dir / "versions").mkdir(parents=True, exist_ok=True)
    content = (FIXTURES / "page-base.md").read_bytes()
    current = {
        "version_id": "v000001",
        "freshness_watermark": "2026-09-10T00:00:04Z:fixture-snapshot",
        "source_ids": SOURCE_IDS,
        "evidence_ids": EVIDENCE_IDS,
        "retracted_source_ids": [],
        "page_checksum": hashlib.sha256(content).hexdigest(),
    }
    definition = {
        "namespace": NAMESPACE,
        "query": "fixture topic",
        "tags": ["fixture-topic"],
        "generator_revision": "pages-v1",
        "creation_policy": "explicit",
    }
    (page_dir / "page.md").write_bytes(content)
    (page_dir / "definition.json").write_bytes(_json_bytes(definition))
    (page_dir / "current.json").write_bytes(_json_bytes(current))
    version = dict(current)
    version["content"] = content.decode("utf-8")
    (page_dir / "versions" / "v000001.json").write_bytes(_json_bytes(version))


def _current(root: Path) -> dict:
    return json.loads((_page_dir(root) / "current.json").read_text(encoding="utf-8"))


def _version(root: Path, version_id: str) -> dict:
    return json.loads((_page_dir(root) / "versions" / f"{version_id}.json")
                      .read_text(encoding="utf-8"))


def _refresh(pages, db, root: Path, *, adapter=None, query="fixture topic"):
    return pages.page_refresh(
        db, data_dir=str(root / "data"), page_id=PAGE_ID, query=query,
        namespace=NAMESPACE, tags=("fixture-topic",),
        llm_local=adapter is not None, adapter=adapter, now=NOW,
    )


def _content(value: dict) -> str:
    return str(value.get("content", value.get("page", "")))


def _first_citation(payload: dict) -> str:
    def walk(value):
        if isinstance(value, dict):
            for key in ("evidence_ids", "citations", "source_ids"):
                values = value.get(key)
                if isinstance(values, list):
                    for item in values:
                        if isinstance(item, str) and item.startswith(
                                ("ev-", "belief:", "00000000-")):
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


def _expect_refusal(testcase: unittest.TestCase, pages, call):
    page_error = getattr(pages, "PageError", ValueError)
    try:
        result = call()
    except Exception as exc:  # controlled library refusal
        testcase.assertIsInstance(exc, page_error)
        return exc
    testcase.assertIsInstance(result, dict)
    testcase.assertFalse(result.get("success", result.get("ok", True)), result)
    return result


class PagesLifecycleTest(unittest.TestCase):
    """Independent lifecycle, byte, path, and provenance checks."""

    def _seed(self, root: Path):
        db, pages = _seed_store(root)
        # Windows keeps SQLite files open until the connection is explicitly
        # closed; cleanup must also run when an assertion fails.
        self.addCleanup(db.close)
        return db, pages

    def test_page_list_and_read_are_store_independent(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-read-") as tmp:
            root = Path(tmp)
            _write_manual_page(root)
            before = _tree_digest(root)
            with _isolated_env(root):
                _beliefs, pages, _schema, _meta = _runtime()
                read = pages.page_read(data_dir=str(root / "data"), page_id=PAGE_ID)
                listed = pages.page_list(data_dir=str(root / "data"), namespace=NAMESPACE)
                self.assertEqual(_content(read).encode("utf-8"),
                                 (FIXTURES / "page-base.md").read_bytes())
                self.assertEqual(len(listed), 1)
                self.assertNotIn("content", listed[0])
                env = os.environ.copy()
                # Exercise the filesystem-only fallback explicitly: with no
                # store path, the CLI resolves pages from ZMEM_DATA and must
                # still avoid creating or opening SQLite.
                env.pop("ZMEM_STORE", None)
                for args in (("read", "--id", PAGE_ID),
                             ("list", "--namespace", NAMESPACE)):
                    result = subprocess.run(
                        [sys.executable, str(STORE_SCRIPT), "page", *args],
                        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
                        check=False, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("store.sqlite", result.stderr.lower())
            self.assertEqual(before, _tree_digest(root))
            self.assertFalse((root / "store.sqlite").exists())

    def test_refresh_is_deterministic(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-deterministic-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                first_current = _current(root)
                first_content = (_page_dir(root) / "page.md").read_bytes()
                first_version = (_page_dir(root) / "versions" / "v000001.json").read_bytes()
                _refresh(pages, db, root)
                second_current = _current(root)
                second_content = (_page_dir(root) / "page.md").read_bytes()
                second_version = (_page_dir(root) / "versions" / "v000002.json").read_bytes()
                self.assertEqual(first_current["version_id"], "v000001")
                self.assertEqual(second_current["version_id"], "v000002")
                self.assertEqual(first_content, second_content)
                self.assertEqual(first_current["freshness_watermark"],
                                 second_current["freshness_watermark"])
                self.assertEqual(first_current["source_ids"], second_current["source_ids"])
                self.assertEqual(first_current["evidence_ids"], second_current["evidence_ids"])
                self.assertEqual(first_current["page_checksum"], second_current["page_checksum"])
                self.assertEqual(first_version,
                                 second_version.replace(b'"v000002"', b'"v000001"'))
            db.close()

    def test_page_contains_heads_and_tagged_rows(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-sources-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                current = _current(root)
                rendered = (_page_dir(root) / "page.md").read_text(encoding="utf-8")
                for text in ("Fixture topic head summary", "Fixture topic source alpha",
                             "Fixture topic source beta", "Fixture topic source tombstone"):
                    self.assertIn(text, rendered)
                self.assertEqual(current["source_ids"], sorted(SOURCE_IDS))
                self.assertEqual(current["evidence_ids"], EVIDENCE_IDS)
                for value in SOURCE_IDS + EVIDENCE_IDS:
                    self.assertIn(value.encode("utf-8"),
                                  (_page_dir(root) / "page.md").read_bytes())
            db.close()

    def test_nullable_source_ref_is_eligible(self):
        """A nullable provenance field is distinct from a page source_ref."""
        with tempfile.TemporaryDirectory(prefix="zmem-pages-null-source-ref-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                self.assertIn("00000000-0000-4000-8000-000000000502",
                              _current(root)["source_ids"])
            db.close()

    def test_untouched_sections_are_byte_identical(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-unchanged-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                before = (_page_dir(root) / "page.md").read_bytes()
                stable_before = before.split(b"<!-- section:refresh -->", 1)[0]

                def adapter(payload):
                    return {"operations": [{
                        "op": "replace_section", "section_id": "refresh",
                        "markdown": "Patched café", "citations": [_first_citation(payload)],
                    }]}

                _refresh(pages, db, root, adapter=adapter)
                after = (_page_dir(root) / "page.md").read_bytes()
                self.assertEqual(after.split(b"<!-- section:refresh -->", 1)[0], stable_before)
                self.assertIn("Patched café".encode("utf-8"), after)
            db.close()

    def test_delta_changes_only_target_section(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-delta-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                before = (_page_dir(root) / "page.md").read_bytes()
                stable, refresh = before.split(b"<!-- section:refresh -->", 1)

                def adapter(payload):
                    return {"operations": [
                        {"op": "replace_section", "section_id": "refresh",
                         "markdown": "Delta café", "citations": [_first_citation(payload)]},
                        {"op": "append_bullet", "section_id": "refresh",
                         "markdown": "Delta bullet", "citations": [_first_citation(payload)]},
                    ]}

                _refresh(pages, db, root, adapter=adapter)
                after = (_page_dir(root) / "page.md").read_bytes()
                new_stable, new_refresh = after.split(b"<!-- section:refresh -->", 1)
                self.assertEqual(new_stable, stable)
                self.assertNotEqual(new_refresh, refresh)
                self.assertIn(b"Delta", new_refresh)
            db.close()

    def test_empty_refresh_keeps_content_and_watermark(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-empty-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                before = _tree_bytes(_page_dir(root))
                _expect_refusal(self, pages,
                                lambda: _refresh(pages, db, root, query="no such source"))
                self.assertEqual(before, _tree_bytes(_page_dir(root)))
            db.close()

    def test_failed_refresh_keeps_content_and_watermark(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-failed-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                before = _tree_bytes(_page_dir(root))

                def exploding(_payload):
                    raise RuntimeError("adapter failed")

                _expect_refusal(self, pages,
                                lambda: _refresh(pages, db, root, adapter=exploding))
                self.assertEqual(before, _tree_bytes(_page_dir(root)))
            db.close()

    def test_bad_target_or_citation_rolls_back(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-invalid-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                before = _tree_bytes(_page_dir(root))
                cases = [
                    {"operations": [{"op": "replace_section", "section_id": "unknown",
                                      "markdown": "bad", "citations": ["ev-502"]}]},
                    {"operations": [{"op": "replace_section", "section_id": "refresh",
                                      "markdown": "bad", "citations": ["ev-missing"]}]},
                    {"operations": [{"op": "unknown", "section_id": "refresh",
                                      "markdown": "bad", "citations": ["ev-502"]}]},
                ]
                for action in cases:
                    _expect_refusal(self, pages,
                                    lambda action=action: _refresh(
                                        pages, db, root, adapter=lambda _p, a=action: a))
                    self.assertEqual(before, _tree_bytes(_page_dir(root)))
            db.close()

    def test_missing_evidence_endpoint_or_association_withholds_refresh(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-evidence-gap-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                before = _tree_bytes(_page_dir(root))
                db.execute("DELETE FROM memory_evidence WHERE memory_id=?",
                           ("00000000-0000-4000-8000-000000000503",))
                db.commit()
                try:
                    _expect_refusal(self, pages, lambda: _refresh(pages, db, root))
                    self.assertEqual(before, _tree_bytes(_page_dir(root)))
                finally:
                    db.close()

    def test_adapter_tombstone_before_return_cannot_publish_stale_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-race-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                before = _tree_bytes(_page_dir(root))

                def adapter(payload):
                    db.execute(
                        "UPDATE memory SET superseded_at=? WHERE id=?",
                        (NOW, "00000000-0000-4000-8000-000000000503"),
                    )
                    db.commit()
                    return {"operations": [{
                        "op": "replace_section", "section_id": "refresh",
                        "markdown": "stale candidate",
                        "citations": [_first_citation(payload)],
                    }]}

                _expect_refusal(self, pages,
                                lambda: _refresh(pages, db, root, adapter=adapter))
                self.assertEqual(before, _tree_bytes(_page_dir(root)))
            db.close()

    def test_tombstoned_source_retracts_bullet_on_next_refresh(self):
        tombstone_id = "00000000-0000-4000-8000-000000000504"
        with tempfile.TemporaryDirectory(prefix="zmem-pages-tombstone-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                prior = _current(root)
                prior_version = _version(root, "v000001")
                self.assertIn(tombstone_id, prior["source_ids"])
                db.execute("UPDATE memory SET superseded_at=? WHERE id=?", (NOW, tombstone_id))
                db.commit()
                _refresh(pages, db, root)
                current = _current(root)
                current_version = _version(root, "v000002")
                current_text = (_page_dir(root) / "page.md").read_text(encoding="utf-8")
                historical = pages.page_read(data_dir=str(root / "data"), page_id=PAGE_ID,
                                             version_id="v000001")
                self.assertIn("Fixture topic source tombstone", _content(historical))
                self.assertNotIn("Fixture topic source tombstone", current_text)
                self.assertIn(tombstone_id, prior_version["source_ids"])
                self.assertNotIn(tombstone_id, current_version["source_ids"])
                self.assertIn(tombstone_id, current.get("retracted_source_ids", []))
            db.close()

    def test_every_version_retains_grounding(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-grounding-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                _refresh(pages, db, root)
                for version_id in ("v000001", "v000002"):
                    version = _version(root, version_id)
                    self.assertEqual(version["version_id"], version_id)
                    self.assertTrue(set(EVIDENCE_IDS).issubset(version["evidence_ids"]))
                    self.assertTrue(set(SOURCE_IDS[1:]).issubset(version["source_ids"]))
                    self.assertRegex(version["page_checksum"], r"^[0-9a-f]{64}$")
                    self.assertIn("content", version)
            db.close()

    def test_page_read_version_is_historical(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-history-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                historical = pages.page_read(data_dir=str(root / "data"), page_id=PAGE_ID,
                                             version_id="v000001")
                _refresh(pages, db, root)
                current = pages.page_read(data_dir=str(root / "data"), page_id=PAGE_ID)
                self.assertEqual(historical["version_id"], "v000001")
                self.assertEqual(current["version_id"], "v000002")
                self.assertEqual(_content(historical), _content(current))
            db.close()

    def test_page_list_is_metadata_only(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-list-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                listed = pages.page_list(data_dir=str(root / "data"), namespace=NAMESPACE)
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]["id"], PAGE_ID)
                self.assertNotIn("content", listed[0])
                self.assertNotIn("page", listed[0])
            db.close()

    def test_runbook_follows_gate(self):
        gate = REPO_ROOT / "evidence" / "gates" / "172-observation.json"
        self.assertEqual(gate.read_bytes(), b'{"observation":"reject"}\n')
        with tempfile.TemporaryDirectory(prefix="zmem-pages-gate-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                metadata = pages.page_for_injection(data_dir=str(root / "data"), page_id=PAGE_ID)
                self.assertNotIn("runbook", metadata)
                _beliefs, _pages, _schema, schema_meta = _runtime()
                self.assertNotIn("page", schema_meta.ALLOWED_TYPES)
            db.close()

    def test_page_query_never_becomes_memory_input(self):
        marker = "page-query-never-memory"
        with tempfile.TemporaryDirectory(prefix="zmem-pages-feedback-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                before = [tuple(row) for row in db.execute(
                    "SELECT id, content, source_ref FROM memory ORDER BY id")]
                _copy_base_page(root)
                _refresh(pages, db, root, query="fixture topic " + marker)
                after = [tuple(row) for row in db.execute(
                    "SELECT id, content, source_ref FROM memory ORDER BY id")]
                self.assertEqual(before, after)
                self.assertNotIn(marker, "\n".join(row[1] for row in after))
            db.close()

    def test_duplicate_heading_refuses(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-duplicate-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                page_dir = _page_dir(root)
                page_dir.mkdir(parents=True, exist_ok=True)
                (page_dir / "page.md").write_bytes(
                    b"# Fixture\n\n<!-- section:stable -->\na\n<!-- end-section:stable -->\n\n"
                    b"<!-- section:stable -->\nb\n<!-- end-section:stable -->\n\n"
                    b"<!-- section:refresh -->\nc\n<!-- end-section:refresh -->\n")
                before = _tree_bytes(page_dir)
                _expect_refusal(self, pages, lambda: _refresh(pages, db, root))
                self.assertEqual(before, _tree_bytes(page_dir))
            db.close()

    def test_missing_section_refuses(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-missing-section-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                page_dir = _page_dir(root)
                page_dir.mkdir(parents=True, exist_ok=True)
                (page_dir / "page.md").write_bytes(
                    b"# Fixture\n\n<!-- section:stable -->\na\n<!-- end-section:stable -->\n")
                before = _tree_bytes(page_dir)
                _expect_refusal(self, pages, lambda: _refresh(pages, db, root))
                self.assertEqual(before, _tree_bytes(page_dir))
            db.close()

    def test_empty_required_section_refuses(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-empty-section-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                page_dir = _page_dir(root)
                page_dir.mkdir(parents=True, exist_ok=True)
                (page_dir / "page.md").write_bytes(
                    b"# Fixture\n\n<!-- section:stable -->\n\n<!-- end-section:stable -->\n\n"
                    b"<!-- section:refresh -->\nc\n<!-- end-section:refresh -->\n")
                before = _tree_bytes(page_dir)
                _expect_refusal(self, pages, lambda: _refresh(pages, db, root))
                self.assertEqual(before, _tree_bytes(page_dir))
            db.close()

    def test_utf8_and_lf_bytes_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-utf8-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)

                def adapter(payload):
                    return {"operations": [{
                        "op": "replace_section", "section_id": "refresh",
                        "markdown": "Crème brûlée — café",
                        "citations": [_first_citation(payload)],
                    }]}

                _refresh(pages, db, root)
                _refresh(pages, db, root, adapter=adapter)
                content = (_page_dir(root) / "page.md").read_bytes()
                self.assertNotIn(b"\r", content)
                self.assertIn("Crème brûlée — café".encode("utf-8"), content)
                self.assertTrue(content.endswith(b"\n"))
                self.assertEqual(content, content.decode("utf-8").encode("utf-8"))
            db.close()

    def test_adapter_multibyte_byte_bound_is_enforced(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-byte-bound-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                db, pages = self._seed(root)
                _copy_base_page(root)
                _refresh(pages, db, root)
                before = _tree_bytes(_page_dir(root))
                captured = []

                def oversized(payload):
                    captured.append(payload)
                    return {"operations": [{
                        "op": "replace_section", "section_id": "refresh",
                        "markdown": "bounded adapter output",
                        "citations": [_first_citation(payload)],
                    }]}

                db.execute(
                    "UPDATE memory SET content=? WHERE id=?",
                    ("é" * 300, "00000000-0000-4000-8000-000000000502"),
                )
                db.commit()
                _refresh(pages, db, root, adapter=oversized)
                self.assertTrue(captured)
                for candidate in captured[-1]["candidates"]:
                    self.assertLessEqual(len(candidate["content"].encode("utf-8")), 400)
                self.assertNotEqual(before, _tree_bytes(_page_dir(root)))
            db.close()

    def test_page_not_in_tier0(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-tier0-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                _beliefs, pages, _schema, schema_meta = _runtime()
                self.assertNotIn("page", schema_meta.ALLOWED_TYPES)
                self.assertFalse((root / "CLAUDE.md").exists())
                self.assertFalse((root / "AGENTS.md").exists())
                self.assertFalse((root / "memory.md").exists())
                self.assertTrue(callable(pages.page_read))

    def test_page_id_path_containment_refuses(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-path-") as tmp:
            root = Path(tmp)
            with _isolated_env(root):
                _beliefs, pages, _schema, _meta = _runtime()
                for page_id in ("..", ".", "../escape", "a/b", "a\\b", "bad\x00id"):
                    with self.subTest(page_id=repr(page_id)):
                        with self.assertRaises((ValueError, getattr(pages, "PageError", ValueError))):
                            pages.page_read(data_dir=str(root / "data"), page_id=page_id)

    def test_symlink_page_escape_refuses(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-symlink-") as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            (outside / "page.md").write_bytes((FIXTURES / "page-base.md").read_bytes())
            pages_root = root / "data" / "pages"
            pages_root.mkdir(parents=True)
            link = pages_root / PAGE_ID
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks unavailable on this Windows host")
            with _isolated_env(root):
                _beliefs, pages, _schema, _meta = _runtime()
                with self.assertRaises((ValueError, getattr(pages, "PageError", ValueError))):
                    pages.page_read(data_dir=str(root / "data"), page_id=PAGE_ID)

    def test_symlink_version_escape_refuses_when_supported(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-version-link-") as tmp:
            root = Path(tmp)
            _write_manual_page(root)
            with _isolated_env(root):
                _beliefs, pages, _schema, _meta = _runtime()
                outside = root / "outside-version.json"
                outside.write_bytes((_page_dir(root) / "versions" / "v000001.json").read_bytes())
                version_link = _page_dir(root) / "versions" / "v000009.json"
                try:
                    version_link.symlink_to(outside)
                except (OSError, NotImplementedError):
                    self.skipTest("file reparse points unavailable on this Windows host")
                with self.assertRaises((ValueError, getattr(pages, "PageError", ValueError))):
                    pages.page_read(data_dir=str(root / "data"), page_id=PAGE_ID,
                                    version_id="v000009")


if __name__ == "__main__":
    unittest.main()
