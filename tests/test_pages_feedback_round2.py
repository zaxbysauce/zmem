"""Decisive round-two coverage for the curated-pages repair contract.

The module owns only disposable stores and new assertions.  The acceptance
fixtures are read through the existing helper module; no frozen fixture or
golden input is changed here.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


def _load_helpers():
    path = Path(__file__).with_name("test_pages_acceptance.py")
    spec = importlib.util.spec_from_file_location("round2_pages_helpers", path)
    if spec is None or spec.loader is None:
        raise ImportError("unable to load page acceptance helpers")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_H = _load_helpers()
_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "memory" / "scripts"


def _runtime_with_inject():
    sys.path.insert(0, str(_SCRIPTS))
    try:
        from storelib import inject  # type: ignore
    finally:
        try:
            sys.path.remove(str(_SCRIPTS))
        except ValueError:
            pass
    return inject


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")) + "\n").encode("utf-8")


def _version(root: Path, version_id: str | None = None) -> dict:
    current = _H._current(root)
    selected = version_id or current["version_id"]
    return json.loads((_H._page_dir(root) / "versions" / f"{selected}.json")
                  .read_text(encoding="utf-8"))


def _version_for_page(root: Path, page_id: str,
                      version_id: str | None = None) -> dict:
    page_dir = root / "data" / "pages" / page_id
    current = json.loads((page_dir / "current.json").read_text(encoding="utf-8"))
    selected = version_id or current["version_id"]
    return json.loads((page_dir / "versions" / f"{selected}.json")
                      .read_text(encoding="utf-8"))


def _rewrite_version(root: Path, mutate) -> None:
    current = _H._current(root)
    path = _H._page_dir(root) / "versions" / f"{current['version_id']}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    path.write_bytes(_json_bytes(value))


def _passive(inject, db, root: Path, query: str, *, session: str) -> dict:
    return inject.select_and_budget_for_injection(
        db, query=query, namespace=_H.NAMESPACE, moment="user_prompt",
        session_id=session, lane="codex", budget_tokens=1600,
        data_dir=str(root / "data"), ops_tokens=[],
        exclude_ids=["belief:fixture-501"],
    )


class Round2PagesFeedback(unittest.TestCase):
    def _seed(self, root: Path):
        db, pages = _H._seed_store(root)
        self.addCleanup(db.close)
        return db, pages

    def test_never_grounded_rows_are_excluded_but_dangling_published_authority_refuses(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-evidence-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            _H._copy_base_page(root)
            _H._refresh(pages, db, root)
            before = _H._tree_bytes(_H._page_dir(root))
            db.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref, source_hash,
                    confidence, signal, valid_from, superseded_at, ingestion_ts,
                    retrieval_count, taint, trust_score)
                   VALUES ('never-grounded-r2', ?, 'lesson', ?,
                           'fixture-topic', '', '', .8, 'test', '', NULL,
                           ?, 0, 'trusted_internal', .8)""",
                (_H.NAMESPACE, "new ungrounded source alpha", _H.NOW),
            )
            db.commit()
            _H._refresh(pages, db, root)
            self.assertNotIn("never-grounded-r2", _H._current(root)["source_ids"])
            self.assertNotEqual(before, _H._tree_bytes(_H._page_dir(root)))

            # Removing the endpoint while leaving memory_evidence is a
            # dangling association. A previously published bullet must
            # remain fail-closed and the old commit must survive.
            stable = _H._tree_bytes(_H._page_dir(root))
            db.execute("DELETE FROM evidence WHERE id='ev-503'")
            db.commit()
            with self.assertRaises(pages.PageError):
                _H._refresh(pages, db, root)
            self.assertEqual(stable, _H._tree_bytes(_H._page_dir(root)))

    def test_zero_eligible_snapshot_refuses_without_bootstrap_commit(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-zero-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            db.execute("DELETE FROM memory")
            db.execute("DELETE FROM belief_head")
            db.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref, source_hash,
                    confidence, signal, valid_from, superseded_at, ingestion_ts,
                    retrieval_count, taint, trust_score)
                   VALUES ('never-grounded-only', ?, 'lesson',
                           'no evidence source', 'fixture-topic', '', '', .8,
                           'test', '', NULL, ?, 0, 'trusted_internal', .8)""",
                (_H.NAMESPACE, _H.NOW),
            )
            db.commit()
            _H._copy_base_page(root)
            with self.assertRaises(pages.PageError):
                _H._refresh(pages, db, root)
            self.assertFalse((_H._page_dir(root) / "current.json").exists())

    def test_markup_only_and_stopword_queries_are_withheld_while_blank_query_is_recent(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-fts-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            _H._copy_base_page(root)
            _H._refresh(pages, db, root)
            for query in ("bullet", "section", "source_ids", "ev-502", "why", "do it"):
                with self.subTest(query=query):
                    self.assertEqual(
                        pages._page_candidates_for_selector(
                            db, data_dir=str(root / "data"), query=query,
                            namespace=_H.NAMESPACE),
                        [],
                    )
            self.assertEqual(
                len(pages._page_candidates_for_selector(
                    db, data_dir=str(root / "data"), query="", namespace=_H.NAMESPACE)),
                1,
            )
            inject = _runtime_with_inject()
            page_result = _passive(inject, db, root, "source alpha", session="r2-fts-page")
            self.assertTrue(any(row.get("type") == "page" for row in page_result["results"]))
            markup_result = _passive(inject, db, root, "bullet section", session="r2-fts-markup")
            self.assertFalse(any(row.get("type") == "page" for row in markup_result["results"]))

    def test_tags_preserve_case_internal_spaces_commas_and_metacharacters(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-tags-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            source = "00000000-0000-4000-8000-000000000502"
            db.execute(
                "UPDATE memory SET tags=? WHERE id=?",
                ("my tag,comma,100%tag,under_score", source),
            )
            db.commit()
            def ids(tag: str):
                return {row["id"] for row in pages._source_rows(
                    db, query="fixture topic", namespace=_H.NAMESPACE,
                    tags=(tag,), as_of=None, data_dir=str(root / "data"), now=_H.NOW)}
            self.assertIn(source, ids("my tag"))
            self.assertNotIn(source, ids("mytag"))
            self.assertNotIn(source, ids("MY TAG"))
            self.assertIn(source, ids("comma"))
            self.assertIn(source, ids("100%tag"))
            self.assertNotIn(source, ids("100Xtag"))
            self.assertIn(source, ids("under_score"))
            self.assertNotIn(source, ids("underXscore"))

    def test_unmatched_tag_cannot_publish_a_synthetic_belief_head(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-head-tags-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            source = _H.SOURCE_IDS[1]
            db.execute("DELETE FROM belief_head_evidence")
            db.execute("DELETE FROM belief_head_source")
            db.execute("DELETE FROM belief_head")
            db.execute("UPDATE memory SET tags='other-tag' WHERE id=?", (source,))
            db.execute(
                """INSERT INTO belief_head
                   (id, namespace, topic_identity, content, head_state,
                    head_source_id, support_count, refresh_watermark,
                    generator_revision, confidence, signal, taint, trust_score)
                   VALUES ('synthetic-r2', ?, 'synthetic-r2',
                           'Fixture topic synthetic summary', 'active',
                           ?, 1, ?, 'beliefs-v1', .9, 'user',
                           'trusted_internal', .9)""",
                (_H.NAMESPACE, source, _H.NOW),
            )
            db.execute(
                """INSERT INTO belief_head_source
                   (head_id, source_id, role, source_ingestion_ts, source_checksum)
                   VALUES ('synthetic-r2', ?, 'support', ?, ?)""",
                (source, _H.NOW, hashlib.sha256(b"Fixture topic source alpha").hexdigest()),
            )
            db.execute(
                """INSERT INTO belief_head_evidence
                   (head_id, source_id, evidence_id)
                   VALUES ('synthetic-r2', ?, 'ev-502')""",
                (source,),
            )
            db.commit()
            rows = pages._source_rows(
                db, query="fixture topic", namespace=_H.NAMESPACE,
                tags=("wanted-tag",), as_of=None,
                data_dir=str(root / "data"), now=_H.NOW)
            self.assertFalse(any(row.get("id") == "belief:synthetic-r2" for row in rows))

    def test_injected_page_admission_failure_falls_back_within_original_budget(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-budget-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            _H._copy_base_page(root)
            _H._refresh(pages, db, root)
            inject = _runtime_with_inject()
            budget = 180
            real_apply = inject.apply_token_budget
            fault = {"raised": False}
            def fail_once(*args, **kwargs):
                if not fault["raised"]:
                    fault["raised"] = True
                    raise RuntimeError("admission seam fault")
                return real_apply(*args, **kwargs)
            with patch.object(inject, "apply_token_budget", side_effect=fail_once):
                result = inject.select_and_budget_for_injection(
                    db, query="source alpha", namespace=_H.NAMESPACE,
                    moment="user_prompt", session_id="r2-budget-fault",
                    lane="codex", budget_tokens=budget, data_dir=str(root / "data"),
                    ops_tokens=[], exclude_ids=["belief:fixture-501"],
                )
            self.assertLessEqual(inject.estimate_tokens(result.get("rendered", "")), budget)
            self.assertFalse(any(row.get("type") == "page" for row in result["results"]))
            self.assertTrue(any(row.get("id") == _H.SOURCE_IDS[1]
                                for row in result["results"]), result)

    def test_content_change_same_source_id_withholds_then_refresh_recovers(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-hash-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            _H._copy_base_page(root)
            _H._refresh(pages, db, root)
            inject = _runtime_with_inject()
            self.assertTrue(any(row.get("type") == "page"
                                for row in _passive(inject, db, root, "source alpha", session="r2-hash-before")["results"]))
            changed = "source alpha changed under the same canonical id"
            db.execute("UPDATE memory SET content=? WHERE id=?", (changed, _H.SOURCE_IDS[1]))
            db.commit()
            self.assertEqual(
                pages._page_candidates_for_selector(
                    db, data_dir=str(root / "data"), query="source alpha",
                    namespace=_H.NAMESPACE),
                [],
            )
            _H._refresh(pages, db, root)
            current = _H._current(root)
            version = _version(root)
            self.assertEqual(
                version["source_content_hashes"][_H.SOURCE_IDS[1]],
                hashlib.sha256(changed.encode("utf-8")).hexdigest(),
            )
            self.assertTrue(any(row.get("type") == "page"
                                for row in _passive(inject, db, root, "changed canonical", session="r2-hash-after")["results"]))
            self.assertEqual(current["version_id"], "v000002")

    def test_content_change_during_adapter_refuses_and_preserves_prior_commit(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-hash-race-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            _H._copy_base_page(root)
            _H._refresh(pages, db, root)
            before = _H._tree_bytes(_H._page_dir(root))

            def adapter(payload):
                db.execute("UPDATE memory SET content=? WHERE id=?",
                           ("boundary changed content", _H.SOURCE_IDS[1]))
                db.commit()
                return {"operations": [{
                    "op": "replace_section", "section_id": "refresh",
                    "markdown": "changed", "citations": ["ev-502"],
                }]}

            with self.assertRaises(pages.PageError):
                _H._refresh(pages, db, root, adapter=adapter)
            self.assertEqual(before, _H._tree_bytes(_H._page_dir(root)))

    def test_v2_private_fields_and_legacy_malformed_unknown_versions(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-schema-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            _H._copy_base_page(root)
            _H._refresh(pages, db, root)
            version = _version(root)
            self.assertEqual(set(version["source_content_hashes"]),
                             set(_H.SOURCE_IDS[1:]))
            self.assertNotIn("belief:fixture-501", version["source_content_hashes"])
            read = pages.page_read(data_dir=str(root / "data"), page_id=_H.PAGE_ID)
            self.assertEqual(set(read), {
                "version_id", "freshness_watermark", "source_ids",
                "evidence_ids", "retracted_source_ids", "page_checksum",
                "content", "bullet_sources", "namespace",
            })
            self.assertNotIn("source_content_hashes", read)
            self.assertNotIn("format_version", read)
            injected = pages.page_for_injection(
                data_dir=str(root / "data"), page_id=_H.PAGE_ID)
            self.assertEqual(set(injected), {
                "id", "namespace", "type", "content", "source_ref",
                "source_ids", "evidence_ids", "version_id",
                "freshness_watermark", "page_checksum",
            })
            listed = pages.page_list(
                data_dir=str(root / "data"), namespace=_H.NAMESPACE)[0]
            self.assertEqual(set(listed), {
                "id", "namespace", "query", "tags", "version_id",
                "freshness_watermark", "source_ids", "evidence_ids",
                "retracted_source_ids", "page_checksum",
            })
            self.assertNotIn("source_content_hashes", injected)
            self.assertNotIn("format_version", injected)
            self.assertNotIn("source_content_hashes", listed)
            self.assertNotIn("format_version", listed)

            # A mapless v1 artifact remains readable as immutable history,
            # but passive delivery withholds it until ordinary refresh.
            _rewrite_version(root, lambda value: (value.pop("format_version", None),
                                                   value.pop("source_content_hashes", None)))
            self.assertEqual(pages.page_read(data_dir=str(root / "data"), page_id=_H.PAGE_ID)["version_id"], "v000001")
            self.assertEqual(pages._page_candidates_for_selector(
                db, data_dir=str(root / "data"), query="source alpha",
                namespace=_H.NAMESPACE), [])
            _H._refresh(pages, db, root)
            self.assertEqual(_version(root)["format_version"], 2)

            _rewrite_version(root, lambda value: value.update({
                "source_content_hashes": {"wrong": "0" * 64},
            }))
            with self.assertRaises(pages.PageError):
                pages.page_read(data_dir=str(root / "data"), page_id=_H.PAGE_ID)
            _rewrite_version(root, lambda value: value.update({"format_version": 9}))
            with self.assertRaises(pages.PageError):
                pages.page_read(data_dir=str(root / "data"), page_id=_H.PAGE_ID)

    def test_public_page_id_policy_rejects_aliases_and_accepts_64_char_id(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-id-policy-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            _H._copy_base_page(root)
            _H._refresh(pages, db, root)
            pages_root = root / "data" / "pages"
            valid_64 = "a" * 64
            shutil.copytree(_H._page_dir(root), pages_root / valid_64)
            accepted = pages.page_read(
                data_dir=str(root / "data"), page_id=valid_64)
            self.assertEqual(accepted["namespace"], _H.NAMESPACE)

            # Materialize aliases beside a valid committed page. This makes
            # the read refusal discriminate validation from a mere missing
            # path error (the old policy accepted these names).
            materialized = ("page" if sys.platform == "win32" else "page.")
            aliases = ("UpperCase", "café", "a" * 65, ".page", "page.")
            for alias in aliases:
                shutil.copytree(
                    _H._page_dir(root),
                    pages_root / (materialized if alias == "page." else alias),
                )
            before_invalid = sorted(path.name for path in pages_root.iterdir())
            for page_id in aliases:
                with self.subTest(page_id=page_id):
                    with self.assertRaisesRegex(pages.PageError, "^invalid page id$"):
                        pages.page_read(
                            data_dir=str(root / "data"), page_id=page_id)
            for page_id in ("CON", "con.txt"):
                with self.subTest(page_id=page_id):
                    with self.assertRaisesRegex(pages.PageError, "^invalid page id$"):
                        pages.page_refresh(
                            db, data_dir=str(root / "data"), page_id=page_id,
                            query="fixture topic", namespace=_H.NAMESPACE,
                            tags=("fixture-topic",), now=_H.NOW)
            self.assertEqual(
                sorted(path.name for path in pages_root.iterdir()),
                before_invalid,
            )

    def test_foreign_page_and_foreign_sources_never_enter_project_selector(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-foreign-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            _H._copy_base_page(root)
            _H._refresh(pages, db, root)
            foreign_id = "foreign-source-r2"
            foreign_ev = "foreign-ev-r2"
            db.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref, source_hash,
                    confidence, signal, valid_from, superseded_at, ingestion_ts,
                    retrieval_count, taint, trust_score)
                   VALUES (?, 'foreign:other', 'lesson', 'Foreign source alpha',
                           'fixture-topic', '', '', .8, 'test', '', NULL,
                           ?, 0, 'trusted_internal', .8)""",
                (foreign_id, _H.NOW),
            )
            db.execute(
                """INSERT INTO evidence
                   (id, session_id, lane, moment, kind, ts, hash, excerpt,
                    ref_path, ref_offset)
                   VALUES (?, 's-foreign', 'codex', 'user_prompt', 'correction',
                           ?, 'hash-foreign', 'foreign', 'fixture', -1)""",
                (foreign_ev, _H.NOW),
            )
            db.execute("INSERT INTO memory_evidence (memory_id,evidence_id) VALUES (?,?)",
                       (foreign_id, foreign_ev))
            db.commit()
            source_page = _H._page_dir(root)
            foreign_page = root / "data" / "pages" / "foreign-page"
            shutil.copytree(source_page, foreign_page)
            definition_path = foreign_page / "definition.json"
            definition = json.loads(definition_path.read_text(encoding="utf-8"))
            definition["namespace"] = "foreign:other"
            definition_path.write_bytes(_json_bytes(definition))
            current_path = foreign_page / "current.json"
            current = json.loads(current_path.read_text(encoding="utf-8"))
            version_path = foreign_page / "versions" / f"{current['version_id']}.json"
            version = json.loads(version_path.read_text(encoding="utf-8"))
            version["source_ids"] = [foreign_id]
            version["evidence_ids"] = [foreign_ev]
            version["source_content_hashes"] = {foreign_id: hashlib.sha256(
                b"Foreign source alpha").hexdigest()}
            version["bullet_sources"] = {
                "foreign-bullet": {"source_ids": [foreign_id], "evidence_ids": [foreign_ev]}
            }
            current["source_ids"] = [foreign_id]
            current["evidence_ids"] = [foreign_ev]
            version_path.write_bytes(_json_bytes(version))
            current_path.write_bytes(_json_bytes(current))
            candidates = pages._page_candidates_for_selector(
                db, data_dir=str(root / "data"), query="source alpha",
                namespace=_H.NAMESPACE)
            self.assertNotIn("page:foreign-page:v000001",
                             {row["id"] for row in candidates})

    def test_cap_is_counted_and_refresh_reenters_without_foreign_displacement(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-cap-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            _H._copy_base_page(root)
            _H._refresh(pages, db, root)
            pages_root = root / "data" / "pages"
            base_dir = _H._page_dir(root)
            for i in range(52):
                shutil.copytree(base_dir, pages_root / f"page-{i:02d}")
            for i in range(5):
                foreign = pages_root / f"foreign-{i:02d}"
                shutil.copytree(base_dir, foreign)
                definition_path = foreign / "definition.json"
                definition = json.loads(definition_path.read_text(encoding="utf-8"))
                definition["namespace"] = "foreign:other"
                definition_path.write_bytes(_json_bytes(definition))
            real = pages.page_for_injection
            calls = {"count": 0}
            def counted(**kwargs):
                calls["count"] += 1
                return real(**kwargs)
            with patch.object(pages, "page_for_injection", side_effect=counted):
                candidates = pages._page_candidates_for_selector(
                    db, data_dir=str(root / "data"), query="source alpha",
                    namespace=_H.NAMESPACE)
            self.assertLessEqual(calls["count"], 50)
            ids = {row["id"] for row in candidates}
            self.assertNotIn("page:page-51:v000001", ids)
            self.assertFalse(any(row["namespace"] == "foreign:other" for row in candidates))

            # An unchanged refresh creates history but keeps the source
            # snapshot watermark, so it cannot jump the deterministic
            # cutoff by publication time alone.
            page_51 = pages_root / "page-51"
            before_current = json.loads(
                (page_51 / "current.json").read_text(encoding="utf-8"))
            before_refresh = _version_for_page(
                root, "page-51", before_current["version_id"])
            pages.page_refresh(
                db, data_dir=str(root / "data"), page_id="page-51",
                query="fixture topic", namespace=_H.NAMESPACE,
                tags=("fixture-topic",), now=_H.NOW,
            )
            after_current = json.loads(
                (page_51 / "current.json").read_text(encoding="utf-8"))
            after_refresh = _version_for_page(
                root, "page-51", after_current["version_id"])
            self.assertEqual(after_current["version_id"], "v000002")
            self.assertEqual(after_refresh["freshness_watermark"],
                             before_refresh["freshness_watermark"])
            unchanged_ids = {row["id"] for row in pages._page_candidates_for_selector(
                db, data_dir=str(root / "data"), query="source alpha",
                namespace=_H.NAMESPACE)}
            self.assertNotIn("page:page-51:v000002", unchanged_ids)

            db.execute("UPDATE memory SET ingestion_ts=? WHERE id=?",
                       ("2026-09-11T00:00:00Z", _H.SOURCE_IDS[1]))
            db.commit()
            pages.page_refresh(
                db, data_dir=str(root / "data"), page_id="page-51",
                query="fixture topic", namespace=_H.NAMESPACE,
                tags=("fixture-topic",), now="2026-09-12T00:00:00Z",
            )
            ids_after = {row["id"] for row in pages._page_candidates_for_selector(
                db, data_dir=str(root / "data"), query="source alpha",
                namespace=_H.NAMESPACE)}
            self.assertIn("page:page-51:v000003", ids_after)

    def test_bootstrap_retry_preserves_orphan_and_interruptions_preserve_state(self):
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption.__name__):
                tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-recovery-")
                self.addCleanup(shutil.rmtree, tmp, True)
                root = Path(tmp)
                with _H._isolated_env(root):
                    db, pages = self._seed(root)
                    _H._copy_base_page(root)
                    _H._refresh(pages, db, root)
                    before = _H._tree_bytes(_H._page_dir(root))
                    calls = {"count": 0}
                    real_replace = pages.os.replace
                    def interrupting(source, target):
                        calls["count"] += 1
                        if calls["count"] == 2:
                            raise interruption("cooperative publish stop")
                        return real_replace(source, target)
                    with patch.object(pages.os, "replace", side_effect=interrupting):
                        with self.assertRaises(interruption):
                            _H._refresh(pages, db, root)
                    self.assertEqual(before, _H._tree_bytes(_H._page_dir(root)))
                    # A matching definition without a pointer is a
                    # bootstrap interruption. The orphan version remains.
                    page_dir = _H._page_dir(root)
                    current_path = page_dir / "current.json"
                    current_path.unlink()
                    orphan = page_dir / "versions" / "v999999.json"
                    orphan.write_bytes((page_dir / "versions" / "v000001.json").read_bytes())
                    _H._refresh(pages, db, root)
                    self.assertTrue(orphan.exists())
                    self.assertTrue(current_path.exists())

    def test_postpointer_interrupt_propagates_after_new_commit_is_valid(self):
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption.__name__):
                tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-postpointer-")
                self.addCleanup(shutil.rmtree, tmp, True)
                root = Path(tmp)
                with _H._isolated_env(root):
                    db, pages = self._seed(root)
                    _H._copy_base_page(root)
                    _H._refresh(pages, db, root)
                    calls = {"count": 0}
                    real_sync = pages._fsync_directory
                    def interrupting(directory):
                        calls["count"] += 1
                        if calls["count"] == 2:
                            raise interruption("post-pointer sync stop")
                        return real_sync(directory)
                    with patch.object(pages, "_fsync_directory", side_effect=interrupting):
                        with self.assertRaises(interruption):
                            _H._refresh(pages, db, root)
                    current = _H._current(root)
                    self.assertEqual(current["version_id"], "v000002")
                    self.assertTrue((_H._page_dir(root) / "versions" / "v000001.json").exists())
                    self.assertTrue((_H._page_dir(root) / "versions" / "v000002.json").exists())
                    self.assertEqual(
                        pages.page_read(data_dir=str(root / "data"), page_id=_H.PAGE_ID)["version_id"],
                        "v000002",
                    )

    def test_adapter_stale_bullet_is_retracted_but_live_bullet_survives(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-bullets-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            unique_id = "unique-adapter-source-r2"
            db.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref, source_hash,
                    confidence, signal, valid_from, superseded_at, ingestion_ts,
                    retrieval_count, taint, trust_score)
                   VALUES (?, ?, 'lesson', 'independent adapter source',
                           'fixture-topic', '', '', .8, 'test', '', NULL,
                           ?, 0, 'trusted_internal', .8)""",
                (unique_id, _H.NAMESPACE, _H.NOW),
            )
            db.execute(
                """INSERT INTO evidence
                   (id, session_id, lane, moment, kind, ts, hash, excerpt,
                    ref_path, ref_offset)
                   VALUES ('ev-unique-r2', 's-unique-r2', 'codex', 'user_prompt',
                           'correction', ?, 'hash-unique-r2', 'unique', 'fixture', -1)""",
                (_H.NOW,),
            )
            db.execute("INSERT INTO memory_evidence (memory_id,evidence_id) VALUES (?, 'ev-unique-r2')",
                       (unique_id,))
            db.commit()
            _H._copy_base_page(root)
            _H._refresh(pages, db, root)
            def adapter(_payload):
                return {"operations": [
                    {"op": "append_bullet", "section_id": "stable",
                     "markdown": "live adapter bullet", "citations": ["ev-unique-r2"]},
                    {"op": "append_bullet", "section_id": "stable",
                     "markdown": "stale adapter bullet", "citations": ["ev-504"]},
                ]}
            _H._refresh(pages, db, root, adapter=adapter)
            original = _version(root, "v000001")
            prior = _version(root)
            added = set(prior["bullet_sources"]) - set(original["bullet_sources"])
            prior_content = (_H._page_dir(root) / "page.md").read_text(encoding="utf-8")
            live_ids = [re.search(r"<!-- bullet:([A-Za-z0-9._-]+) -->\s*live adapter bullet",
                                  prior_content).group(1)]
            stale_ids = [re.search(r"<!-- bullet:([A-Za-z0-9._-]+) -->\s*stale adapter bullet",
                                   prior_content).group(1)]
            self.assertTrue(set(live_ids + stale_ids).issubset(added))
            self.assertTrue(live_ids and stale_ids)
            db.execute("UPDATE memory SET superseded_at=? WHERE id=?",
                       (_H.NOW, _H.SOURCE_IDS[3]))
            db.commit()
            _H._refresh(pages, db, root)
            final = _version(root)
            self.assertTrue(all(ident in final["bullet_sources"] for ident in live_ids))
            self.assertTrue(all(ident not in final["bullet_sources"] for ident in stale_ids))
            content = (_H._page_dir(root) / "page.md").read_text(encoding="utf-8")
            self.assertIn("live adapter bullet", content)
            self.assertNotIn("stale adapter bullet", content)

    def test_two_refreshes_serialize_and_leave_readable_commits(self):
        tmp = tempfile.mkdtemp(prefix="zmem-pages-r2-concurrent-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp)
        with _H._isolated_env(root):
            db, pages = self._seed(root)
            _H._copy_base_page(root)
            db.close()
            def refresh_once():
                local = sqlite3.connect(root / "store.sqlite", timeout=15)
                local.row_factory = sqlite3.Row
                try:
                    return pages.page_refresh(
                        local, data_dir=str(root / "data"), page_id=_H.PAGE_ID,
                        query="fixture topic", namespace=_H.NAMESPACE,
                        tags=("fixture-topic",), now=_H.NOW)
                finally:
                    local.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _item: refresh_once(), range(2)))
            self.assertEqual([result["success"] for result in results], [True, True])
            self.assertTrue((_H._page_dir(root) / "current.json").exists())
            self.assertIn("content", pages.page_read(
                data_dir=str(root / "data"), page_id=_H.PAGE_ID))


if __name__ == "__main__":
    unittest.main()
