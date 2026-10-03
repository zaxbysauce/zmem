"""Focused regressions for confirmed PR273 production findings.

These checks use the frozen pages acceptance store but own their assertions.
They do not regenerate or alter the frozen fixture corpus.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_helpers():
    path = Path(__file__).with_name("test_pages_acceptance.py")
    spec = importlib.util.spec_from_file_location("pages_feedback_helpers", path)
    if spec is None or spec.loader is None:
        raise ImportError("unable to load pages acceptance helpers")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_HELPERS = _load_helpers()
_PAGE_ID = _HELPERS.PAGE_ID
_NOW = _HELPERS.NOW


class PagesReviewFeedback(unittest.TestCase):
    @staticmethod
    def _selector_row(row_id: str, content_size: int, score: float, *, page=False,
                      represented=()):
        row = {
            "id": row_id,
            "namespace": _HELPERS.NAMESPACE,
            "type": "page" if page else "fact",
            "content": ("p" if page else "c") * content_size,
            "confidence": score,
            "signal": "test",
            "taint": "trusted_internal",
            "trust_score": score,
            "_score": score,
        }
        if page:
            row.update({
                "represented_ids": list(represented),
                "source_ids": list(represented),
                "evidence_ids": ["ev-" + row_id],
                "version_id": "v000001",
                "freshness_watermark": "fixture",
                "page_checksum": "fixture",
            })
        return row

    def _run_mocked_selector(self, pages, inject, db, root, *, canonical, page_rows,
                             budget, session):
        """Use real selector budgeting with only source discovery/read mocked."""
        from storelib import recall

        def fake_recall(_conn, **kwargs):
            kwargs["_capture"].update({
                "results": [dict(row) for row in canonical],
                "candidate_ids": [row["id"] for row in canonical],
                "reason": "injected",
                "tokens_used": 0,
                "budget_dropped": 0,
                "budget_admission": 0,
                "budget_truncated": 0,
                "budget_dropped_protected": 0,
                "budget_note": "",
                "arms": {},
            })

        with patch.object(pages, "_page_candidates_for_selector", return_value=page_rows), \
                patch.object(recall, "recall_memory", side_effect=fake_recall):
            return inject.select_and_budget_for_injection(
                db, query="fixture pages", namespace=_HELPERS.NAMESPACE,
                moment="user_prompt", session_id=session, lane="codex",
                budget_tokens=budget, data_dir=str(root / "data"),
            )

    def test_contested_head_is_excluded_from_refresh_and_committed_selector(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-feedback-head-") as tmp:
            root = Path(tmp)
            with _HELPERS._isolated_env(root):
                db, pages = _HELPERS._seed_store(root)
                try:
                    _HELPERS._copy_base_page(root)
                    _HELPERS._refresh(pages, db, root)
                    db.execute(
                        "UPDATE belief_head SET head_state='contested' WHERE id='fixture-501'"
                    )
                    db.commit()
                    candidates = pages._page_candidates_for_selector(
                        db, data_dir=str(root / "data"), query="fixture topic",
                        namespace=_HELPERS.NAMESPACE,
                    )
                    self.assertEqual(candidates, [])
                    _HELPERS._refresh(pages, db, root)
                    current = _HELPERS._current(root)
                    self.assertNotIn("belief:fixture-501", current["source_ids"])
                finally:
                    db.close()

    def test_tag_like_metacharacters_are_literal(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-feedback-tags-") as tmp:
            root = Path(tmp)
            with _HELPERS._isolated_env(root):
                db, pages = _HELPERS._seed_store(root)
                try:
                    db.execute(
                        "UPDATE memory SET tags='literal_tag' WHERE id=?",
                        ("00000000-0000-4000-8000-000000000502",),
                    )
                    db.execute(
                        "UPDATE memory SET tags='literalXtag' WHERE id=?",
                        ("00000000-0000-4000-8000-000000000503",),
                    )
                    db.execute(
                        "UPDATE memory SET tags='literal%tag' WHERE id=?",
                        ("00000000-0000-4000-8000-000000000504",),
                    )
                    db.commit()
                    rows = pages._source_rows(
                        db, query="fixture topic", namespace=_HELPERS.NAMESPACE,
                        tags=("literal_tag",), as_of=None, data_dir=str(root / "data"), now=_NOW,
                    )
                    ids = {row["id"] for row in rows}
                    self.assertIn("00000000-0000-4000-8000-000000000502", ids)
                    self.assertNotIn("00000000-0000-4000-8000-000000000503", ids)
                    percent_rows = pages._source_rows(
                        db, query="fixture topic", namespace=_HELPERS.NAMESPACE,
                        tags=("literal%tag",), as_of=None, data_dir=str(root / "data"), now=_NOW,
                    )
                    percent_ids = {row["id"] for row in percent_rows}
                    self.assertIn("00000000-0000-4000-8000-000000000504", percent_ids)
                    self.assertNotIn("00000000-0000-4000-8000-000000000502", percent_ids)
                finally:
                    db.close()

    def test_page_relevance_uses_recall_fts_token_and_prefix_semantics(self):
        """Derived pages cannot turn interior substrings into lexical hits."""
        with tempfile.TemporaryDirectory(prefix="zmem-pages-feedback-relevance-") as tmp:
            root = Path(tmp)
            with _HELPERS._isolated_env(root):
                db, pages = _HELPERS._seed_store(root)
                try:
                    _HELPERS._copy_base_page(root)
                    _HELPERS._refresh(pages, db, root)

                    def candidates(query):
                        return pages._page_candidates_for_selector(
                            db, data_dir=str(root / "data"), query=query,
                            namespace=_HELPERS.NAMESPACE,
                        )

                    # ``afe`` is an interior substring of stable ``cafe``;
                    # ordinary recall's unicode61 FTS prefix expression does
                    # not admit it. The prior raw ``term in haystack`` did.
                    self.assertEqual(candidates("afe"), [])
                    self.assertEqual(len(candidates("caf")), 1)
                    self.assertEqual(len(candidates("cafe")), 1)

                    # Short terms are exact, so an exact represented-source
                    # tag works while an interior/prefix fragment does not.
                    db.execute(
                        "UPDATE memory SET content=?, tags=? WHERE id=?",
                        ("unrelated ordinary content", "id", "00000000-0000-4000-8000-000000000502"),
                    )
                    db.commit()
                    self.assertEqual(len(candidates("id")), 1)
                    self.assertEqual(candidates("ca"), [])

                    # Exercise the shared predicate with page-neutral content
                    # so Unicode folding and internal punctuation are proven
                    # through the actual unicode61 tokenizer, never a Python
                    # approximation of it.
                    source = [{"content": "Caf\u00e9 run-book", "tags": "release-notes"}]
                    self.assertEqual(pages._page_fts_coverage("neutral", source, "caf"), (1, 1.0))
                    self.assertEqual(pages._page_fts_coverage("neutral", source, "cafe"), (1, 1.0))
                    self.assertEqual(pages._page_fts_coverage("neutral", source, "release-note"), (1, 1.0))
                    self.assertEqual(pages._page_fts_coverage("neutral", source, "af"), (0, 0.0))
                finally:
                    db.close()

    def test_projection_reparse_refuses_before_publication(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-feedback-safe-") as tmp:
            root = Path(tmp)
            with _HELPERS._isolated_env(root):
                db, pages = _HELPERS._seed_store(root)
                try:
                    _HELPERS._copy_base_page(root)
                    _HELPERS._refresh(pages, db, root)
                    directory = root / "data" / "pages" / _PAGE_ID
                    projection = directory / "page.md"
                    original = projection.read_bytes()
                    # Windows CI can deny developer symlink creation, so model
                    # the filesystem reparse predicate directly.  The writer
                    # reaches this check only after the committed metadata
                    # read and before it snapshots/replaces the projection.
                    with patch.object(
                        pages, "_is_reparse",
                        side_effect=lambda path: Path(path).name == "page.md",
                    ):
                        with self.assertRaisesRegex(pages.PageError, "unsafe page artifact"):
                            _HELPERS._refresh(pages, db, root)
                    self.assertEqual(projection.read_bytes(), original)
                finally:
                    db.close()

    def test_budget_dropped_page_restores_represented_canonical_rows(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-feedback-budget-") as tmp:
            root = Path(tmp)
            with _HELPERS._isolated_env(root):
                db, pages = _HELPERS._seed_store(root)
                try:
                    _HELPERS._copy_base_page(root)
                    _HELPERS._refresh(pages, db, root)

                    _storelib, _beliefs, inject, _schema, _meta, _ledger, _pages = _HELPERS._runtime()
                    payload = inject.select_and_budget_for_injection(
                        db, query="source alpha", namespace=_HELPERS.NAMESPACE,
                        moment="user_prompt", session_id="feedback-budget-drop", lane="codex",
                        budget_tokens=220, data_dir=str(root / "data"),
                        exclude_ids=["belief:fixture-501"],
                    )
                    self.assertTrue(any(str(item).startswith("page:")
                                        for item in payload["candidate_ids"]))
                    self.assertFalse(any(row.get("type") == "page" for row in payload["results"]))
                    self.assertTrue(any(
                        row.get("id") == "00000000-0000-4000-8000-000000000502"
                        for row in payload["results"]
                    ))
                    self.assertIn("budget_note", payload)
                    self.assertEqual(
                        payload["tokens_used"],
                        sum(inject.estimate_tokens(row.get("content", "") or "")
                            for row in payload["results"]),
                    )
                finally:
                    db.close()

    def test_credential_page_is_withheld_without_suppressing_or_ledgering_sources(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-feedback-secret-") as tmp:
            root = Path(tmp)
            with _HELPERS._isolated_env(root):
                db, pages = _HELPERS._seed_store(root)
                try:
                    _HELPERS._copy_base_page(root)

                    def adapter(_payload):
                        return {"operations": [{
                            "op": "replace_section",
                            "section_id": "refresh",
                            "markdown": "sshpass -p pw180F",
                            "citations": ["ev-502"],
                        }]}

                    _HELPERS._refresh(pages, db, root, adapter=adapter)
                    _storelib, _beliefs, inject, _schema, _meta, ledger, _pages = _HELPERS._runtime()
                    session = "credential-page-marker"
                    payload = inject.select_and_budget_for_injection(
                        db, query="fixture topic", namespace=_HELPERS.NAMESPACE,
                        moment="user_prompt", session_id=session, lane="codex",
                        budget_tokens=1500, data_dir=str(root / "data"),
                        ops_tokens=[], exclude_ids=["belief:fixture-501"],
                    )
                    page_rows = [row for row in payload["results"]
                                 if row.get("type") == "page"]
                    self.assertEqual(len(page_rows), 1)
                    self.assertTrue(page_rows[0].get("withheld_for_secret"))
                    self.assertEqual(page_rows[0].get("content"), "")
                    self.assertEqual(payload.get("secret_withheld"), 1)
                    self.assertNotIn("sshpass", payload["rendered"])
                    self.assertTrue(any(
                        row.get("id") == "00000000-0000-4000-8000-000000000502"
                        for row in payload["results"]
                    ))
                    ledger_path = Path(ledger.ledger_path(str(root / "data"), session))
                    self.assertTrue(ledger_path.exists())
                    entries = json.loads(ledger_path.read_text(encoding="utf-8"))["entries"]
                    self.assertFalse(any(entry.get("id") == page_rows[0]["id"]
                                         for entry in entries))

                    # The id/type-only marker is part of the same final
                    # admission pass as canonical fallbacks.  A constrained
                    # envelope may drop other rows, but it cannot append a
                    # free marker or let the secret page hide its source.
                    tight = inject.select_and_budget_for_injection(
                        db, query="fixture topic", namespace=_HELPERS.NAMESPACE,
                        moment="user_prompt", session_id="credential-page-tight",
                        lane="codex", budget_tokens=220, data_dir=str(root / "data"),
                        ops_tokens=[], exclude_ids=["belief:fixture-501"],
                    )
                    tight_pages = [row for row in tight["results"]
                                   if row.get("type") == "page"]
                    self.assertEqual(len(tight_pages), 1)
                    self.assertTrue(tight_pages[0].get("withheld_for_secret"))
                    self.assertEqual(tight_pages[0].get("content"), "")
                    self.assertLessEqual(tight["budget_admission"], tight["tokens_budget"])
                    self.assertLessEqual(
                        inject.estimate_tokens(tight["rendered"]), tight["tokens_budget"]
                    )
                    self.assertTrue(any(
                        row.get("id") == "00000000-0000-4000-8000-000000000502"
                        for row in tight["results"]
                    ))
                    self.assertNotIn("sshpass", tight["rendered"])
                finally:
                    db.close()

    def test_multi_page_fallback_reintroduces_only_evicted_sibling_sources(self):
        """A surviving page cannot keep an evicted sibling's sources hidden."""
        with tempfile.TemporaryDirectory(prefix="zmem-pages-feedback-multi-") as tmp:
            root = Path(tmp)
            with _HELPERS._isolated_env(root):
                db, pages = _HELPERS._seed_store(root)
                try:
                    _storelib, _beliefs, inject, _schema, _meta, _ledger, _pages = _HELPERS._runtime()
                    canonical = [
                        self._selector_row("canonical-a", 160, 0.20),
                        self._selector_row("canonical-b", 48, 0.60),
                        self._selector_row("canonical-c", 350, 0.95),
                        self._selector_row("unrelated", 32, 0.50),
                    ]
                    candidates = [
                        self._selector_row("page:a", 160, 0.99, page=True,
                                           represented=("canonical-a",)),
                        self._selector_row("page:b", 160, 0.60, page=True,
                                           represented=("canonical-b",)),
                        self._selector_row("page:c", 1200, 0.10, page=True,
                                           represented=("canonical-c",)),
                    ]
                    payload = self._run_mocked_selector(
                        pages, inject, db, root, canonical=canonical,
                        page_rows=candidates, budget=400, session="multi-page-fallback",
                    )
                    result_ids = {row["id"] for row in payload["results"]}
                    self.assertIn("page:a", result_ids)
                    self.assertNotIn("page:b", result_ids)
                    self.assertNotIn("page:c", result_ids)
                    self.assertIn("canonical-b", result_ids)
                    self.assertIn("canonical-c", result_ids)
                    self.assertIn("unrelated", result_ids)
                    self.assertNotIn("canonical-a", result_ids)
                    self.assertGreaterEqual(payload["budget_dropped"], 1)
                finally:
                    db.close()

    def test_page_and_unrelated_row_fit_without_represented_row_waste(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-feedback-capacity-") as tmp:
            root = Path(tmp)
            with _HELPERS._isolated_env(root):
                db, pages = _HELPERS._seed_store(root)
                try:
                    _storelib, _beliefs, inject, _schema, _meta, _ledger, _pages = _HELPERS._runtime()
                    canonical = [
                        self._selector_row("represented-one", 320, 0.80),
                        self._selector_row("represented-two", 320, 0.70),
                        self._selector_row("unrelated-small", 48, 0.60),
                    ]
                    page = self._selector_row(
                        "page:summary", 200, 0.99, page=True,
                        represented=("represented-one", "represented-two"),
                    )
                    payload = self._run_mocked_selector(
                        pages, inject, db, root, canonical=canonical,
                        page_rows=[page], budget=350, session="page-capacity",
                    )
                    result_ids = {row["id"] for row in payload["results"]}
                    self.assertIn("page:summary", result_ids)
                    self.assertIn("unrelated-small", result_ids)
                    self.assertNotIn("represented-one", result_ids)
                    self.assertNotIn("represented-two", result_ids)
                finally:
                    db.close()


if __name__ == "__main__":
    unittest.main()
