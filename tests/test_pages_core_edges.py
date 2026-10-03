"""Focused stale-provenance guards for issue #138 page discovery."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


_FIXTURE_PATH = Path(__file__).resolve().with_name("test_pages_acceptance.py")
_fixture_spec = importlib.util.spec_from_file_location(
    "zmem_pages_acceptance_fixture", _FIXTURE_PATH)
assert _fixture_spec and _fixture_spec.loader
fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(fixture)


class PageCoreEdgeTests(unittest.TestCase):
    def _prepared(self):
        tmp = tempfile.TemporaryDirectory(prefix="zmem-pages-core-edge-")
        root = Path(tmp.name)
        env = fixture._isolated_env(root)
        env.__enter__()
        db, pages = fixture._seed_store(root)
        fixture._copy_base_page(root)
        fixture._refresh(pages, db, root)
        return tmp, env, root, db, pages

    def _close(self, tmp, env, db):
        db.close()
        env.__exit__(None, None, None)
        tmp.cleanup()

    def _candidate(self, pages, db, root):
        return pages._page_candidates_for_selector(
            db, data_dir=str(root / "data"), query="source alpha",
            namespace=fixture.NAMESPACE)

    def test_rekeyed_represented_source_withholds_page(self):
        tmp, env, root, db, pages = self._prepared()
        try:
            db.execute("UPDATE memory SET namespace='project:other' WHERE id=?",
                       (fixture.SOURCE_IDS[1],)); db.commit()
            self.assertEqual(self._candidate(pages, db, root), [])
        finally:
            self._close(tmp, env, db)

    def test_expired_or_page_backed_represented_source_withholds_page(self):
        for sql, value in (
            ("UPDATE memory SET valid_until=? WHERE id=?", fixture.NOW),
            ("UPDATE memory SET source_ref=? WHERE id=?", "page:foreign:v000001"),
        ):
            tmp, env, root, db, pages = self._prepared()
            try:
                db.execute(sql, (value, fixture.SOURCE_IDS[1])); db.commit()
                self.assertEqual(self._candidate(pages, db, root), [])
            finally:
                self._close(tmp, env, db)

    def test_swapped_or_dangling_evidence_endpoint_withholds_page(self):
        for sql, params in (
            ("DELETE FROM memory_evidence WHERE memory_id=? AND evidence_id=?",
             (fixture.SOURCE_IDS[1], "ev-502")),
            ("DELETE FROM evidence WHERE id=?", ("ev-502",)),
        ):
            tmp, env, root, db, pages = self._prepared()
            try:
                changed = db.execute(sql, params).rowcount
                self.assertEqual(changed, 1)
                db.commit()
                self.assertEqual(self._candidate(pages, db, root), [])
            finally:
                self._close(tmp, env, db)

    def test_swapped_live_evidence_endpoint_withholds_page(self):
        """Changing both live grounding edges must invalidate the old page."""
        tmp, env, root, db, pages = self._prepared()
        try:
            changed_memory = db.execute(
                "UPDATE memory_evidence SET evidence_id=? "
                "WHERE memory_id=? AND evidence_id=?",
                ("ev-503", fixture.SOURCE_IDS[1], "ev-502"),
            ).rowcount
            changed_head = db.execute(
                "UPDATE belief_head_evidence SET evidence_id=? "
                "WHERE source_id=? AND evidence_id=?",
                ("ev-503", fixture.SOURCE_IDS[1], "ev-502"),
            ).rowcount
            self.assertEqual((changed_memory, changed_head), (1, 1))
            db.commit()
            self.assertEqual(self._candidate(pages, db, root), [])
        finally:
            self._close(tmp, env, db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
