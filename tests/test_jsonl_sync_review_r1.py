"""Driver legs pinning the review-r1 fixes (decode boundary + oversize drain)."""
import json
import os
import pathlib
import sys
import tempfile
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _store_guard  # noqa: F401

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
NS = "project:github.com/zaxbysauce/zmem"


def _load(tmp):
    import importlib.util
    env = {"ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
           "ZMEM_MODELS_DIR": os.path.join(tmp, "no-models"),
           "ZMEM_MODEL_AUTODOWNLOAD": "0"}
    with unittest.mock.patch.dict(os.environ, env, clear=False):
        spec = importlib.util.spec_from_file_location(
            "zmem_revfix_store", str(STORE_PY))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    conn = mod.connect()
    mod.init_db(conn)
    mod.migrate(conn)
    return mod, conn


class ReviewR1DecodeBoundaryTest(unittest.TestCase):
    def test_expiry_after_decode_failure_never_applies(self):
        import shutil
        import sqlite3
        tmp = tempfile.mkdtemp(prefix="zmem-revfix-decode-")
        self.addCleanup(shutil.rmtree, tmp, True)
        mod, conn = _load(tmp)
        # A live state row whose side record we then delete: the corrupt
        # file's post-failure tail would rewrite it if the gate were absent.
        mid = mod.add_memory(conn, namespace=NS, type_="state",
                             content="decode boundary probe: pool size is 8",
                             expiry_class="setting")
        conn.execute("DELETE FROM memory_expiry WHERE memory_id=?", (mid,))
        mod._commit(conn)

        feed = os.path.join(tmp, "corrupt.jsonl")
        with open(feed, "wb") as f:
            # Padding so the corruption is not in the first decode chunk.
            f.write((json.dumps({"kind": "memory",
                                 "id": "80000000-0000-4000-8000-000000000001",
                                 "namespace": NS, "type": "fact",
                                 "content": "x" * 10_000, "tags": "",
                                 "source_ref": "", "confidence": 0.8,
                                 "signal": "test",
                                 "valid_from": "2026-01-01T00:00:00Z",
                                 "ingestion_ts": "2026-01-01T00:00:00Z",
                                 "superseded_at": None,
                                 "supersede_reason": ""}) + "\n").encode())
            f.write(b"\xff\xfe invalid utf8 \x80\n")
            f.write((json.dumps({"kind": "memory_expiry", "memory_id": mid,
                                 "inject_until": "2032-12-31T00:00:00Z",
                                 "expiry_class": "metric"}) + "\n").encode())

        from storelib import sync as sync_mod
        import contextlib
        cap = os.path.join(tmp, "err.log")
        with open(cap, "w", encoding="utf-8") as ch:
            with contextlib.redirect_stderr(ch):
                rc = sync_mod.cmd_ingest_jsonl(conn, in_path=feed,
                                               source_ref=None)
        self.assertEqual(rc, 2, "the unreadable file must exit 2")
        row = conn.execute(
            "select count(*) from memory_expiry where memory_id=?",
            (mid,)).fetchone()[0]
        self.assertEqual(row, 0,
                         "an expiry record after the decode failure must "
                         "never apply (the base partial-progress boundary)")
        log = pathlib.Path(cap).read_text(encoding="utf-8")
        self.assertIn("cannot read", log)


class ReviewR1OversizeNumberingTest(unittest.TestCase):
    def test_oversized_line_keeps_counters_exact(self):
        import shutil
        tmp = tempfile.mkdtemp(prefix="zmem-revfix-oversize-")
        self.addCleanup(shutil.rmtree, tmp, True)
        mod, conn = _load(tmp)
        feed = os.path.join(tmp, "oversize.jsonl")
        with open(feed, "wb") as f:
            f.write(b("{" + "x" * 1_100_000 + "}\n"))
            f.write((json.dumps({"kind": "memory",
                                 "id": "81000000-0000-4000-8000-000000000001",
                                 "namespace": NS, "type": "state",
                                 "content": "oversize probe: pool size is 9",
                                 "tags": "", "source_ref": "",
                                 "confidence": 0.8, "signal": "test",
                                 "valid_from": "2026-01-01T00:00:00Z",
                                 "ingestion_ts": "2026-01-01T00:00:00Z",
                                 "superseded_at": None,
                                 "supersede_reason": ""}) + "\n").encode())
            f.write((json.dumps({"kind": "memory_expiry",
                                 "memory_id": "81000000-0000-4000-8000-000000000001",
                                 "inject_until": "2027-01-01T00:00:00Z",
                                 "expiry_class": "setting"}) + "\n").encode())

        from storelib import sync as sync_mod
        import contextlib
        cap = os.path.join(tmp, "err.log")
        out = os.path.join(tmp, "out.log")
        with open(cap, "w", encoding="utf-8") as ch, \
                open(out, "w", encoding="utf-8") as oh:
            with contextlib.redirect_stderr(ch), contextlib.redirect_stdout(oh):
                rc = sync_mod.cmd_ingest_jsonl(conn, in_path=feed,
                                               source_ref=None)
        self.assertEqual(rc, 0)
        summary = pathlib.Path(out).read_text(encoding="utf-8")
        m_applied = summary.split("expiry_applied=")[1].split()[0]
        m_skipped = summary.split("expiry_skipped=")[1].split()[0]
        self.assertEqual((m_applied, m_skipped), ("1", "0"),
                         f"counters must stay exact after an oversized line "
                         f"(no phantom skip from pass-A/B line desync): "
                         f"{summary.strip()[-200:]}")


def b(s):
    return s.encode()


class ReviewR2LastParsedLineTest(unittest.TestCase):
    """Reviewer r2 Important: a record on the LAST line pass B parsed before
    the decode failure is fully validated by pass B (base buffered and
    applied it) -- the boundary bound must be inclusive (ln <= lineno)."""

    def test_record_on_last_parsed_line_applies(self):
        import shutil
        import contextlib
        tmp = tempfile.mkdtemp(prefix="zmem-revr2-lastline-")
        self.addCleanup(shutil.rmtree, tmp, True)
        mod, conn = _load(tmp)
        mid = mod.add_memory(conn, namespace=NS, type_="state",
                             content="r2 lastline probe: pool size is 3",
                             expiry_class="setting")
        conn.execute("DELETE FROM memory_expiry WHERE memory_id=?", (mid,))
        mod._commit(conn)

        def row(obj_id, kind="memory", **kw):
            base = {"kind": kind, "id": obj_id, "namespace": NS,
                    "type": "state", "content": "pad", "tags": "",
                    "source_ref": "", "confidence": 0.8, "signal": "test",
                    "valid_from": "2026-01-01T00:00:00Z",
                    "ingestion_ts": "2026-01-01T00:00:00Z",
                    "superseded_at": None, "supersede_reason": ""}
            base.update(kw)
            return base

        feed = os.path.join(tmp, "lastline.jsonl")
        with open(feed, "wb") as f:
            f.write((json.dumps(row("82000000-0000-4000-8000-000000000001",
                                    content="y" * 10_000,
                                    type="fact")) + "\n").encode())
            # The record on the last line pass B fully parses:
            f.write((json.dumps({"kind": "memory_expiry", "memory_id": mid,
                                 "inject_until": "2033-01-01T00:00:00Z",
                                 "expiry_class": "metric"}) + "\n").encode())
            # A long clean line so the corrupt chunk is pulled by line 3's read:
            f.write((json.dumps(row("82000000-0000-4000-8000-000000000002",
                                    content="z" * 10_000,
                                    type="fact")) + "\n").encode())
            f.write(b"\xff\xfe invalid utf8 \x80\n")

        from storelib import sync as sync_mod
        cap = os.path.join(tmp, "err.log")
        with open(cap, "w", encoding="utf-8") as ch:
            with contextlib.redirect_stderr(ch):
                rc = sync_mod.cmd_ingest_jsonl(conn, in_path=feed,
                                               source_ref=None)
        self.assertEqual(rc, 2)
        got = conn.execute(
            "select expiry_class from memory_expiry where memory_id=?",
            (mid,)).fetchone()
        self.assertIsNotNone(
            got, "a record on the last line pass B parsed must apply "
                 "(base buffered and applied it; the bound is inclusive)")
        self.assertEqual(got[0], "metric")


class ReviewR2PairAcrossBoundaryTest(unittest.TestCase):
    """Reviewer r2 Minor (disclosed semantics): a live state row BEFORE the
    decode failure may pair with its record indexed AFTER it. The pair
    commits atomically inside the row's transaction and keeps the
    row+record invariant base violated in this corner (base landed the row
    bare); the record is rebuilt from three ASCII-only validated fields so
    replacement characters cannot ride through. This leg PINS that choice."""

    def test_parent_before_failure_pairs_with_postfailure_record(self):
        import shutil
        import contextlib
        tmp = tempfile.mkdtemp(prefix="zmem-revr2-pair-")
        self.addCleanup(shutil.rmtree, tmp, True)
        mod, conn = _load(tmp)

        def row(obj_id, **kw):
            base = {"kind": "memory", "id": obj_id, "namespace": NS,
                    "type": "state", "content": "pad", "tags": "",
                    "source_ref": "", "confidence": 0.8, "signal": "test",
                    "valid_from": "2026-01-01T00:00:00Z",
                    "ingestion_ts": "2026-01-01T00:00:00Z",
                    "superseded_at": None, "supersede_reason": ""}
            base.update(kw)
            return base

        pid = "83000000-0000-4000-8000-000000000001"
        feed = os.path.join(tmp, "pair.jsonl")
        with open(feed, "wb") as f:
            f.write((json.dumps(row(pid,
                                    content="r2 pair probe: pool size is 4"))
                     + "\n").encode())
            f.write((json.dumps(row("83000000-0000-4000-8000-000000000002",
                                    content="y" * 10_000,
                                    type="fact")) + "\n").encode())
            f.write((json.dumps({"kind": "memory_expiry", "memory_id": pid,
                                 "inject_until": "2034-01-01T00:00:00Z",
                                 "expiry_class": "setting"}) + "\n").encode())
            f.write(b"\xff\xfe invalid utf8 \x80\n")

        from storelib import sync as sync_mod
        cap = os.path.join(tmp, "err.log")
        with open(cap, "w", encoding="utf-8") as ch:
            with contextlib.redirect_stderr(ch):
                rc = sync_mod.cmd_ingest_jsonl(conn, in_path=feed,
                                               source_ref=None)
        self.assertEqual(rc, 2, "the corrupt file still reports unreadable")
        got = conn.execute(
            "select expiry_class from memory_expiry where memory_id=?",
            (pid,)).fetchone()
        self.assertIsNotNone(
            got, "the DELIBERATE pair-across-boundary semantics: the row "
                 "landed live and its record paired atomically (the "
                 "invariant-preserving outcome; see the sync.py comment)")
        self.assertEqual(got[0], "setting")



if __name__ == "__main__":
    unittest.main(verbosity=2)
