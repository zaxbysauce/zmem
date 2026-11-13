"""Knowledge reserved-ref guards (PR1): ordinary writer surfaces, sync
ingest, import-store, and consolidation must never forge, mutate, or remove
authoritative pointer state.

Covers, per the approved plan ("Required critic corrections"):
  - ordinary forged add: add_memory refuses a knowledge: source_ref; the
    exemption is an IDENTITY-compared private capability object, not a flag
  - ordinary update: neither claiming a knowledge ref nor editing a live
    pointer row in place
  - ordinary supersede/invalidate: a pointer row's tombstone is a pointer
    state transition — refused outside the handler's CAS
  - sync import (export-jsonl -> ingest-jsonl roundtrip): an imported ref is
    PRESERVED but demoted to verified=false (never newly verified), and a
    remote tombstone for a pointer row is refused even with
    --allow-tombstones (the "remote handled parent" case)
  - import-store direct sqlite path: same demotion semantics
  - consolidate protects refs: pointer rows are never seeds/keepers/neighbors

Run: python tests/test_knowledge_guards.py   (no pytest -- repo convention)
"""

from __future__ import annotations
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
IMPORT_STORE_PY = SCRIPTS_DIR / "import-store.py"

_TMP = tempfile.mkdtemp(prefix="zmem-knguards-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_TMP, "no-such-models")
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)
sys.path.insert(0, str(SCRIPTS_DIR))

import storelib  # noqa: E402
from storelib import schema as _schema  # noqa: E402
from storelib import sync as _sync  # noqa: E402
from storelib import write as _write  # noqa: E402
from storelib import knowledge as _knowledge  # noqa: E402
from storelib.knowledge import (  # noqa: E402
    KnowledgePointerRefusal,
    build_payload,
    demote_imported_payload,
    get_pointer,
    get_pointer_rows,
    link_pointer,
    make_source_ref,
    parse_payload,
)
from storelib.consolidate import consolidate  # noqa: E402

TARGET = "fleet-kb"
KEY = "runbooks/deploy-window"


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _ns() -> str:
    return "project:kn-guards-" + uuid.uuid4().hex[:10]


def _fresh_store_conn(path: Path) -> sqlite3.Connection:
    """Open a SECOND store file in-process: storelib resolves STORE_PATH at
    import/_refresh_env_state time, so swap the env, refresh, connect,
    then restore (the store.py shim does exactly this dance)."""
    old = os.environ.get("ZMEM_STORE")
    os.environ["ZMEM_STORE"] = str(path)
    try:
        storelib._refresh_env_state()
        conn = _schema.connect()
        _schema.init_db(conn)
        _schema.migrate(conn)
    finally:
        if old is None:
            os.environ.pop("ZMEM_STORE", None)
        else:
            os.environ["ZMEM_STORE"] = old
        storelib._refresh_env_state()
    return conn


class _StreamGrab:
    """A stdout/stderr stand-in that records printed lines."""

    def __init__(self, sink: list):
        self._sink = sink

    def write(self, s):
        if s.strip():
            self._sink.append(s.rstrip("\n"))
        return len(s)

    def flush(self):
        pass


class _Capture:
    """Collect stdout/stderr lines printed by CLI-level functions."""

    def __init__(self):
        self.out: list[str] = []
        self.err: list[str] = []

    def __enter__(self):
        import contextlib
        self._ctx = contextlib.ExitStack()
        self._ctx.enter_context(
            contextlib.redirect_stdout(_StreamGrab(self.out)))
        self._ctx.enter_context(
            contextlib.redirect_stderr(_StreamGrab(self.err)))
        return self

    def __exit__(self, *exc):
        self._ctx.close()
        return False


def capture():
    return _Capture()


def _ingest(conn, path: Path, *, allow_tombstones=False) -> tuple[int, list]:
    with capture() as cap:
        rc = _sync.cmd_ingest_jsonl(
            conn, in_path=str(path), source_ref=None,
            allow_tombstones=allow_tombstones)
    return rc, cap.err + cap.out


class _StoreTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = _schema.connect()
        _schema.init_db(cls.conn)
        _schema.migrate(cls.conn)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.close()
        except Exception:
            pass


def _mint_pointer(conn, ns: str, *, version=1, seed="v1"):
    return link_pointer(
        conn, namespace=ns, target=TARGET, key=KEY, version=version,
        full_sha256=_digest(ns + seed), summary="deploy window runbook",
        expected_row_id=None)


class ForgedAddGuardTest(_StoreTestCase):
    def test_add_memory_refuses_knowledge_ref(self):
        ns = _ns()
        with self.assertRaises(ValueError) as ctx:
            _write.add_memory(
                self.conn, namespace=ns, type_="fact",
                content="forged pointer payload",
                source_ref=make_source_ref(TARGET, KEY), signal="test")
        self.assertIn("knowledge:", str(ctx.exception))
        n = self.conn.execute(
            "SELECT count(*) AS c FROM memory WHERE namespace=?", (ns,)
        ).fetchone()["c"]
        self.assertEqual(n, 0)  # refused BEFORE any mutation

    def test_capability_is_identity_compared_not_truthy(self):
        # a serialized surface can at best pass a truthy value; the guard
        # identity-compares against the registered OBJECT, so anything else
        # (True, a string, a lookalike object) is still an ordinary writer
        ns = _ns()
        for fake in (True, "capability", object()):
            with self.assertRaises(ValueError):
                _write.add_memory(
                    self.conn, namespace=ns, type_="fact",
                    content="forged", signal="test",
                    source_ref=make_source_ref(TARGET, KEY),
                    _knowledge_cap=fake)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) AS c FROM memory WHERE namespace=?", (ns,)
            ).fetchone()["c"], 0)


class UpdateGuardTest(_StoreTestCase):
    def test_update_cannot_claim_knowledge_ref(self):
        ns = _ns()
        row = _write.add_memory(
            self.conn, namespace=ns, type_="fact",
            content="ordinary fact", signal="test")
        with self.assertRaises(ValueError) as ctx:
            _write.update_memory(
                self.conn, mid=str(row), content="ordinary fact v2",
                namespace=ns, source_ref=make_source_ref(TARGET, KEY))
        self.assertIn("knowledge", str(ctx.exception))

    def test_pointer_row_cannot_be_edited_in_place(self):
        ns = _ns()
        ptr = _mint_pointer(self.conn, ns)
        with self.assertRaises(ValueError):
            _write.update_memory(
                self.conn, mid=ptr["row_id"], content="tampered payload",
                namespace=ns)
        content = self.conn.execute(
            "SELECT content FROM memory WHERE id=?", (ptr["row_id"],)
        ).fetchone()["content"]
        # byte-identical: the refusal fired before any mutation
        self.assertIsNotNone(parse_payload(content))
        self.assertTrue(parse_payload(content)["verified"])
        self.assertEqual(
            get_pointer(self.conn, ns, TARGET, KEY).row_id, ptr["row_id"])


class SupersedeGuardTest(_StoreTestCase):
    def test_supersede_refuses_pointer_row(self):
        ns = _ns()
        ptr = _mint_pointer(self.conn, ns)
        with self.assertRaises(KnowledgePointerRefusal):
            _write.supersede_memory(self.conn, ptr["row_id"])
        live = self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?", (ptr["row_id"],)
        ).fetchone()["superseded_at"]
        self.assertIsNone(live)
        # the authoritative pointer is untouched
        self.assertEqual(
            get_pointer(self.conn, ns, TARGET, KEY).row_id, ptr["row_id"])

    def test_supersede_refusal_not_a_cross_namespace_oracle_bypass(self):
        # even a correctly-scoped caller cannot tombstone a pointer
        ns = _ns()
        ptr = _mint_pointer(self.conn, ns)
        with self.assertRaises(KnowledgePointerRefusal):
            _write.supersede_memory(
                self.conn, ptr["row_id"], expected_namespace=ns)
        self.assertIsNone(self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?",
            (ptr["row_id"],)).fetchone()["superseded_at"])


class SyncDemoteTest(_StoreTestCase):
    """export-jsonl -> ingest-jsonl roundtrip: preserved but never newly
    verified; remote tombstones refused even when permitted."""

    def _setup_pair(self, tmpdir: Path):
        ns = _ns()
        src = _fresh_store_conn(tmpdir / "src.sqlite")
        dst = _fresh_store_conn(tmpdir / "dst.sqlite")
        return ns, src, dst

    def test_roundtrip_demotes_verified_and_preserves_ref(self):
        with tempfile.TemporaryDirectory(prefix="zmem-knsync-") as td:
            tdp = Path(td)
            ns, src, dst = self._setup_pair(tdp)
            try:
                ptr = link_pointer(
                    src, namespace=ns, target=TARGET, key=KEY, version=1,
                    full_sha256=_digest("sync-v1"), summary="sync runbook",
                    expected_row_id=None)
                f1 = tdp / "export1.jsonl"
                with capture():
                    self.assertEqual(_sync.cmd_export_jsonl(src, out=str(f1)), 0)
                rc, _logs = _ingest(dst, f1)
                self.assertEqual(rc, 0)

                # the ref survived...
                row = dst.execute(
                    "SELECT id, content FROM memory WHERE namespace=? "
                    "AND source_ref=? AND superseded_at IS NULL",
                    (ns, make_source_ref(TARGET, KEY)),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["id"], ptr["row_id"])
                # ...but ONLY as an unverified copy
                parsed = parse_payload(row["content"])
                self.assertIsNotNone(parsed)
                self.assertFalse(parsed["verified"])
                # so the destination has NO authoritative pointer
                self.assertIsNone(get_pointer(dst, ns, TARGET, KEY))
                rows = get_pointer_rows(dst, ns, TARGET, KEY)
                self.assertEqual(len(rows), 1)
                self.assertFalse(rows[0]["verified"])
            finally:
                src.close()
                dst.close()

    def test_ingest_keeps_ref_against_payload_identical_ordinary_keeper(self):
        """Generic subject dedup must not fold an imported pointer row into
        an ORDINARY keeper whose content is byte-identical to the demoted
        (verified=false) payload — pre-fix the row vanished into the keeper
        via _merge_on_dedup and the reserved ref never landed in the store."""
        with tempfile.TemporaryDirectory(prefix="zmem-knsync3-") as td:
            tdp = Path(td)
            ns, src, dst = self._setup_pair(tdp)
            try:
                ref = make_source_ref(TARGET, KEY)
                link_pointer(
                    src, namespace=ns, target=TARGET, key=KEY, version=1,
                    full_sha256=_digest("sync3-v1"),
                    summary="sync runbook three", expected_row_id=None)
                f1 = tdp / "live.jsonl"
                with capture():
                    self.assertEqual(
                        _sync.cmd_export_jsonl(src, out=str(f1)), 0)
                exported = [
                    json.loads(ln)
                    for ln in f1.read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
                payload_row = next(
                    r for r in exported if r["source_ref"] == ref)
                # The destination already holds an ordinary row whose
                # content is EXACTLY the demoted payload form the incoming
                # row will carry after the verified=false demote.
                demoted = demote_imported_payload(payload_row["content"])
                self.assertIsNotNone(demoted)
                keeper = str(_write.add_memory(
                    dst, namespace=ns, type_="fact", content=demoted,
                    signal="none", source_ref="peer:keeper-note"))

                rc, _logs = _ingest(dst, f1)  # no preserve_ids anywhere
                self.assertEqual(rc, 0)

                # The pointer row landed as its OWN live row: the ref
                # survived the ingest (pre-fix: 'deduped', no such row).
                live = dst.execute(
                    "SELECT id, content FROM memory WHERE namespace=? "
                    "AND source_ref=? AND superseded_at IS NULL",
                    (ns, ref)).fetchall()
                self.assertEqual(len(live), 1)
                self.assertNotEqual(live[0]["id"], keeper)
                parsed = parse_payload(live[0]["content"])
                self.assertIsNotNone(parsed)
                self.assertFalse(parsed["verified"])
                # The ordinary keeper was never merged into: content
                # untouched, retrieval counter not bumped (a dedup fold
                # reads the keeper and writes to it), tags not unioned.
                kept = dst.execute(
                    "SELECT content, retrieval_count, tags FROM memory "
                    "WHERE id=?", (keeper,)).fetchone()
                self.assertEqual(kept["content"], demoted)
                self.assertEqual(kept["retrieval_count"], 0)
                self.assertNotIn("knowledge", (kept["tags"] or ""))
                rows = get_pointer_rows(dst, ns, TARGET, KEY)
                self.assertEqual(len(rows), 1)
                self.assertFalse(rows[0]["verified"])
            finally:
                src.close()
                dst.close()

    def test_remote_tombstone_refused_even_with_allow_tombstones(self):
        with tempfile.TemporaryDirectory(prefix="zmem-knsync2-") as td:
            tdp = Path(td)
            ns, src, dst = self._setup_pair(tdp)
            ref = make_source_ref(TARGET, KEY)
            p1 = link_pointer(
                src, namespace=ns, target=TARGET, key=KEY, version=1,
                full_sha256=_digest("sync2-v1"), summary="sync runbook",
                expected_row_id=None)
            f1 = tdp / "live.jsonl"
            with capture():
                self.assertEqual(_sync.cmd_export_jsonl(src, out=str(f1)), 0)
            rc, _ = _ingest(dst, f1)
            self.assertEqual(rc, 0)

            # advance the SOURCE pointer (old row tombstoned there), export
            # everything including the tombstone, and let the remote file
            # ask the destination to kill its copy
            link_pointer(
                src, namespace=ns, target=TARGET, key=KEY, version=2,
                full_sha256=_digest("sync2-v2"), summary="sync runbook v2",
                expected_row_id=p1["row_id"])
            f2 = tdp / "with-tombstone.jsonl"
            with capture():
                self.assertEqual(
                    _sync.cmd_export_jsonl(
                        src, out=str(f2), include_superseded=True), 0)
            rc, logs = _ingest(dst, f2, allow_tombstones=True)
            self.assertEqual(rc, 0)
            self.assertTrue(
                any("refused tombstone" in ln for ln in logs),
                f"expected a tombstone refusal line, got: {logs}")

            try:
                # the tombstone TARGET is still live — no remote tombstone
                # landed on it (this is the assertion the guard owns)
                r1row = dst.execute(
                    "SELECT superseded_at FROM memory WHERE id=?",
                    (p1["row_id"],),
                ).fetchone()
                self.assertIsNotNone(r1row)
                self.assertIsNone(r1row["superseded_at"])
                # the new version landed as another INERT copy: the ref has
                # only unverified live rows and no authoritative pointer
                live = dst.execute(
                    "SELECT id, content FROM memory WHERE namespace=? "
                    "AND source_ref=? AND superseded_at IS NULL",
                    (ns, ref),
                ).fetchall()
                self.assertGreaterEqual(len(live), 1)
                self.assertIn(p1["row_id"], [r["id"] for r in live])
                for r in live:
                    parsed = parse_payload(r["content"])
                    self.assertIsNotNone(parsed)
                    self.assertFalse(parsed["verified"])
                self.assertIsNone(get_pointer(dst, ns, TARGET, KEY))
            finally:
                src.close()
                dst.close()

    def test_tombstone_refused_for_live_verified_pointer_same_store(self):
        # a hostile sync file carrying the row's own id cannot remove the
        # AUTHORITATIVE pointer from the store that minted it
        ns = _ns()
        ptr = _mint_pointer(self.conn, ns)
        row = self.conn.execute(
            "SELECT * FROM memory WHERE id=?", (ptr["row_id"],)).fetchone()
        obj = {
            "id": row["id"], "namespace": row["namespace"],
            "type": row["type"], "content": row["content"],
            "tags": row["tags"], "source_ref": row["source_ref"],
            "signal": row["signal"], "confidence": row["confidence"],
            "valid_from": row["valid_from"],
            "ingestion_ts": row["ingestion_ts"],
            "superseded_at": _schema.now_iso(),  # the hostile flip
            "supersede_reason": "remote says gone",
        }
        with tempfile.TemporaryDirectory(prefix="zmem-knhost-") as td:
            f = Path(td) / "hostile.jsonl"
            f.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            rc, logs = _ingest(self.conn, f, allow_tombstones=True)
            self.assertEqual(rc, 0)
            self.assertTrue(
                any("refused tombstone" in ln for ln in logs),
                f"expected a tombstone refusal line, got: {logs}")
        # the verified pointer row is untouched
        self.assertIsNone(self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?",
            (ptr["row_id"],)).fetchone()["superseded_at"])
        still = get_pointer(self.conn, ns, TARGET, KEY)
        self.assertEqual(still.row_id, ptr["row_id"])
        self.assertEqual(still.version, 1)


class ImportStoreDirectTest(unittest.TestCase):
    """The direct sqlite -> sqlite import path demotes pointer payloads with
    the same semantics as sync ingest."""

    def test_import_store_preserves_ref_demotes_verified(self):
        with tempfile.TemporaryDirectory(prefix="zmem-knimp-") as td:
            tdp = Path(td)
            ns = _ns()
            src_store = tdp / "legacy" / "store.sqlite"
            src_store.parent.mkdir(parents=True, exist_ok=True)
            src = _fresh_store_conn(src_store)
            ptr = link_pointer(
                src, namespace=ns, target=TARGET, key=KEY, version=4,
                full_sha256=_digest("import-v4"),
                summary="imported runbook", expected_row_id=None)
            ordinary = _write.add_memory(
                src, namespace=ns, type_="fact",
                content="ordinary row survives import", signal="test")
            src.close()

            dest_dir = tdp / "dest"
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run(
                [sys.executable, str(IMPORT_STORE_PY),
                 "--source", str(src_store),
                 "--dest-dir", str(dest_dir)],
                capture_output=True, text=True, env=env, timeout=180,
                cwd=str(REPO_ROOT))
            self.assertEqual(
                proc.returncode, 0,
                f"import-store failed:\n{proc.stdout}\n{proc.stderr}")
            self.assertIn("knowledge ref preserved unverified", proc.stdout)

            dest = sqlite3.connect(str(dest_dir / "store.sqlite"))
            dest.row_factory = sqlite3.Row
            try:
                row = dest.execute(
                    "SELECT id, content, superseded_at FROM memory "
                    "WHERE source_ref=? AND superseded_at IS NULL",
                    (make_source_ref(TARGET, KEY),),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["id"], ptr["row_id"])
                parsed = parse_payload(row["content"])
                self.assertIsNotNone(parsed)
                self.assertFalse(parsed["verified"])  # demoted
                self.assertEqual(parsed["version"], 4)  # payload intact
                # ordinary rows are untouched by the knowledge rule
                kept = dest.execute(
                    "SELECT count(*) AS c FROM memory WHERE namespace=? "
                    "AND superseded_at IS NULL", (ns,)).fetchone()["c"]
                self.assertEqual(kept, 2)
            finally:
                dest.close()


class ConsolidateProtectsRefsTest(_StoreTestCase):
    def test_pointer_rows_never_seeds_keepers_or_neighbors(self):
        ns = _ns()
        base = ("pytest xdist workers share a tmpdir causing a race "
                "condition under flaky quarantine windows")
        ptr = link_pointer(
            self.conn, namespace=ns, target=TARGET, key=KEY, version=1,
            full_sha256=_digest("consolidate-v1"),
            summary=base,  # SAME text as the ordinary near-dup pair
            expected_row_id=None)
        payload = self.conn.execute(
            "SELECT content FROM memory WHERE id=?", (ptr["row_id"],)
        ).fetchone()["content"]

        def raw(content):
            mid = str(uuid.uuid4())
            ts = _schema.now_iso()
            self.conn.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref,
                    source_hash, confidence, signal, valid_from,
                    superseded_at, ingestion_ts, retrieval_count,
                    last_retrieved)
                   VALUES (?,?,?,?,?,'','',0.5,'test',?,NULL,?,0,NULL)""",
                (mid, ns, "fact", content, "", ts, ts))
            return mid

        r1 = raw(base)
        r2 = raw(base + " and lane ordering must be deterministic")
        r3 = raw(payload)  # an ordinary row whose content IS the payload
        self.conn.commit()

        with capture():
            consolidate(self.conn, force=True, namespace=ns)

        # the pointer row is untouched: live, byte-identical, not absorbed
        row = self.conn.execute(
            "SELECT content, superseded_at FROM memory WHERE id=?",
            (ptr["row_id"],)).fetchone()
        self.assertIsNone(row["superseded_at"])
        self.assertEqual(row["content"], payload)
        self.assertEqual(
            get_pointer(self.conn, ns, TARGET, KEY).row_id, ptr["row_id"])

        # no summary/keeper absorbed the pointer id
        absorbed = self.conn.execute(
            "SELECT count(*) AS c FROM memory WHERE merged_from LIKE ?",
            ("%" + ptr["row_id"] + "%",)).fetchone()["c"]
        self.assertEqual(absorbed, 0)

        # control: consolidation really ran and merged the ordinary pair
        ordinary_dead = self.conn.execute(
            "SELECT count(*) AS c FROM memory WHERE id IN (?,?) "
            "AND superseded_at IS NOT NULL", (r1, r2)).fetchone()["c"]
        summaries = self.conn.execute(
            "SELECT count(*) AS c FROM memory WHERE namespace=? "
            "AND source_ref LIKE 'organize:%'", (ns,)).fetchone()["c"]
        self.assertTrue(
            ordinary_dead >= 1 or summaries >= 1,
            "control failed: consolidate merged nothing in this namespace "
            "(guard assertions above would be vacuous)")


def _days_ago_iso(days: int) -> str:
    """A canonical store timestamp exactly ``days`` in the past."""
    moment = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class ConsolidatePruneProtectsRefsTest(_StoreTestCase):
    """The unrecalled-prune pass must skip reserved pointer rows: a pointer
    is born signal=none/confidence=0.2/retrieval_count=0, so a 31-day-old
    one matches every NUMERIC prune predicate — but supersede_memory
    refuses pointer rows (KnowledgePointerRefusal), and pre-fix that
    uncaught refusal aborted the whole consolidate routine mid-run."""

    def test_prune_excludes_31day_pointer_prunes_ordinary_control(self):
        ns = _ns()
        ptr = _mint_pointer(self.conn, ns)
        # Age the pointer 31 days: past the 30-day default window, so the
        # numeric predicate alone would select it.
        self.conn.execute(
            "UPDATE memory SET ingestion_ts=? WHERE id=?",
            (_days_ago_iso(31), ptr["row_id"]))
        # Ordinary control rows on the same predicate, no reserved ref.
        old_row = str(_write.add_memory(
            self.conn, namespace=ns, type_="fact",
            content="stale ungrounded lesson never retrieved "
                    + str(uuid.uuid4()),
            signal="none"))
        self.conn.execute(
            "UPDATE memory SET ingestion_ts=? WHERE id=?",
            (_days_ago_iso(31), old_row))
        fresh_row = str(_write.add_memory(
            self.conn, namespace=ns, type_="fact",
            content="fresh ungrounded lesson " + str(uuid.uuid4()),
            signal="none"))
        self.conn.commit()

        # Pre-fix this call RAISED KnowledgePointerRefusal (prune selected
        # the pointer, supersede refused, the routine aborted).
        with capture():
            report = consolidate(
                self.conn, force=True, namespace=ns, prune=True)

        # The routine completed and pruned exactly the ordinary old row.
        self.assertEqual(report["pruned"], 1)
        dead = self.conn.execute(
            "SELECT superseded_at, supersede_reason FROM memory WHERE id=?",
            (old_row,)).fetchone()
        self.assertIsNotNone(dead["superseded_at"])
        self.assertIn("pruned", dead["supersede_reason"])
        # The fresh ordinary row stays live: the age gate is real.
        self.assertIsNone(self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?",
            (fresh_row,)).fetchone()["superseded_at"])
        # The pointer was never even selected: live and still authoritative.
        ptr_row = self.conn.execute(
            "SELECT superseded_at, content FROM memory WHERE id=?",
            (ptr["row_id"],)).fetchone()
        self.assertIsNone(ptr_row["superseded_at"])
        self.assertTrue(parse_payload(ptr_row["content"])["verified"])
        self.assertEqual(
            get_pointer(self.conn, ns, TARGET, KEY).row_id, ptr["row_id"])


class RekeyGuardTest(unittest.TestCase):
    """rekey-namespace must refuse — atomically, BEFORE any move — a map
    that would concentrate two live VERIFIED pointer rows for one exact
    reserved ref into a single namespace (get_pointer/link_pointer treat
    that shape as unrecoverable store corruption); a non-conflicting map
    still moves the pointer."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-knrekey-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store_path = self.tmp / "store.sqlite"
        self.conn = _fresh_store_conn(self.store_path)
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _rekey(self, entries):
        from storelib.rekey import apply_namespace_map
        with mock.patch.dict(os.environ, {
            "ZMEM_DATA": str(self.tmp / "data"),
            "ZMEM_BACKUP_DIR": str(self.tmp / "backups"),
        }, clear=False), capture():
            return apply_namespace_map(
                conn=self.conn, store_path=self.store_path, entries=entries)

    def test_map_concentrating_verified_ref_rows_refused_atomically(self):
        ns_a, ns_b = _ns(), _ns()
        key = "runbooks/rekey-" + uuid.uuid4().hex[:8]
        ref = make_source_ref(TARGET, key)
        pa = link_pointer(
            self.conn, namespace=ns_a, target=TARGET, key=key, version=1,
            full_sha256=_digest("rk-a"), summary="rekey probe a",
            expected_row_id=None)
        pb = link_pointer(
            self.conn, namespace=ns_b, target=TARGET, key=key, version=1,
            full_sha256=_digest("rk-b"), summary="rekey probe b",
            expected_row_id=None)
        self.conn.commit()

        rc = self._rekey([(ref, "project:kn-rekey-" + uuid.uuid4().hex[:8])])
        self.assertEqual(rc, 1)

        # Atomic refusal: NOTHING moved, both pointers stay resolvable in
        # their home namespaces (post-move they would share one namespace
        # and every get_pointer call would raise KnowledgePointerConflict).
        for ns, result in ((ns_a, pa), (ns_b, pb)):
            row = self.conn.execute(
                "SELECT namespace, superseded_at FROM memory WHERE id=?",
                (result["row_id"],)).fetchone()
            self.assertEqual(row["namespace"], ns)
            self.assertIsNone(row["superseded_at"])
            self.assertEqual(
                get_pointer(self.conn, ns, TARGET, key).row_id,
                result["row_id"])

    def test_nonconflict_map_moves_pointer_and_ordinary_rows(self):
        ns_a = _ns()
        key = "runbooks/rekey-" + uuid.uuid4().hex[:8]
        ref = make_source_ref(TARGET, key)
        prefix = "peer:rk-" + uuid.uuid4().hex[:8]
        ordinary = str(_write.add_memory(
            self.conn, namespace=ns_a, type_="fact",
            content="ordinary row riding the same map " + str(uuid.uuid4()),
            signal="test", source_ref=prefix + ":note-1"))
        ptr = link_pointer(
            self.conn, namespace=ns_a, target=TARGET, key=key, version=1,
            full_sha256=_digest("rk-c"), summary="rekey probe c",
            expected_row_id=None)
        self.conn.commit()
        target_ns = "project:kn-rekey2-" + uuid.uuid4().hex[:8]

        rc = self._rekey([(prefix, target_ns), (ref, target_ns)])
        self.assertEqual(rc, 0)

        moved = self.conn.execute(
            "SELECT namespace FROM memory WHERE id=?", (ordinary,)).fetchone()
        self.assertEqual(moved["namespace"], target_ns)
        # the pointer moved WITH its namespace and remains authoritative
        live = get_pointer(self.conn, target_ns, TARGET, key)
        self.assertIsNotNone(live)
        self.assertEqual(live.row_id, ptr["row_id"])
        leftovers = self.conn.execute(
            "SELECT count(*) AS c FROM memory WHERE namespace=? "
            "AND source_ref=?", (ns_a, ref)).fetchone()["c"]
        self.assertEqual(leftovers, 0)


class CapabilityRegistrationTest(_StoreTestCase):
    """The reserved-prefix capability must FAIL CLOSED in the unregistered
    (None) state: pre-fix, a module reload resetting _KNOWLEDGE_CAPABILITY
    to None made the guard's identity check degenerate (`None is not None`
    -> False), so an ordinary writer passing add_memory's None default
    could mint a knowledge: row. Registration itself rejects None."""

    def test_register_rejects_none(self):
        with self.assertRaises(ValueError):
            _write._register_knowledge_capability(None)

    def test_unregistered_state_fails_closed_for_every_caller(self):
        ns = _ns()
        with mock.patch.object(_write, "_KNOWLEDGE_CAPABILITY", None):
            # the ordinary writer's own default is None — still refused
            with self.assertRaises(ValueError):
                _write.add_memory(
                    self.conn, namespace=ns, type_="fact", content="forged",
                    signal="test",
                    source_ref=make_source_ref(TARGET, "runbooks/cap-none"))
            # and the (now unregistered) authoritative writer is refused too
            with self.assertRaises(ValueError):
                link_pointer(
                    self.conn, namespace=ns, target=TARGET,
                    key="runbooks/cap-none", version=1,
                    full_sha256=_digest("cap-none"),
                    summary="capability probe", expected_row_id=None)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) AS c FROM memory WHERE namespace=?",
                (ns,)).fetchone()["c"], 0)
        # state restored by mock: the real registration still works
        _mint_pointer(self.conn, _ns())

    def test_reload_fail_closed_then_re_register_restores_writer(self):
        """Isolated in a SUBPROCESS on purpose: reloading storelib.write in
        THIS process would rebind its exception classes (a reloaded
        module's KnowledgePointerRefusal is a NEW class object) and poison
        every later assertRaises in sibling classes. The probe itself:
        reloading write must not re-open the reserved prefix to the
        None-default writer (the pre-fix `None is not None` fail-open),
        the stale registration must fail closed for the handler too, and
        re-importing the knowledge module must re-register and restore the
        authoritative writer."""
        code = "\n".join([
            "import importlib, os, sys",
            "sys.path.insert(0, sys.argv[1])",
            "os.environ['ZMEM_MODEL_AUTODOWNLOAD'] = '0'",
            "from storelib import write as w, knowledge as k",
            "importlib.reload(w)",
            "from storelib.schema import connect, init_db, migrate",
            "conn = connect(); init_db(conn); migrate(conn)",
            "ns = 'project:kn-cap-reload'",
            "ref = k.make_source_ref('fleet-kb', 'runbooks/cap-reload')",
            "try:",
            "    w.add_memory(conn, namespace=ns, type_='fact',",
            "                 content='forged after reload', signal='test',",
            "                 source_ref=ref)",
            "except ValueError:",
            "    pass",
            "else:",
            "    raise SystemExit('FAILOPEN: forged add landed after reload')",
            "try:",
            "    k.link_pointer(conn, namespace=ns, target='fleet-kb',",
            "                   key='runbooks/cap-reload', version=1,",
            "                   full_sha256='a' * 64,",
            "                   summary='capability probe', expected_row_id=None)",
            "except ValueError:",
            "    pass",
            "else:",
            "    raise SystemExit('stale registration accepted after reload')",
            "importlib.reload(k)",
            "out = k.link_pointer(conn, namespace=ns, target='fleet-kb',",
            "                     key='runbooks/cap-reload', version=1,",
            "                     full_sha256='a' * 64,",
            "                     summary='capability probe', expected_row_id=None)",
            "assert out['status'] == 'created', out",
            "try:",
            "    w.add_memory(conn, namespace=ns, type_='fact',",
            "                 content='forged after re-register', signal='test',",
            "                 source_ref=ref + '-x')",
            "except ValueError:",
            "    print('RELOADPROBE-OK')",
            "else:",
            "    raise SystemExit('FAILOPEN: forged add landed after re-register')",
        ])
        with tempfile.TemporaryDirectory(prefix="zmem-kncap-") as td:
            env = dict(os.environ)
            env.update({
                "ZMEM_STORE": os.path.join(td, "probe.sqlite"),
                "ZMEM_MODELS_DIR": os.path.join(td, "no-models"),
                "PYTHONIOENCODING": "utf-8",
            })
            proc = subprocess.run(
                [sys.executable, "-c", code, str(SCRIPTS_DIR)],
                capture_output=True, text=True, env=env, timeout=180,
                cwd=str(REPO_ROOT))
        self.assertEqual(
            proc.returncode, 0,
            f"reload probe failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("RELOADPROBE-OK", proc.stdout)
        self.assertNotIn("FAILOPEN", proc.stdout + proc.stderr)


class WriteRekeyCollisionTest(unittest.TestCase):
    """write.rekey_namespace (--from / --near-miss, incl. its automatic CLI
    route) and schema's v5 _rekey_namespaces move namespaces too — they
    must carry the SAME exact-ref verified-pointer uniqueness guard, from
    the same single implementation in storelib.rekey."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-knrekey2-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store_path = self.tmp / "store.sqlite"
        self.conn = _fresh_store_conn(self.store_path)
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _two_verified_same_ref(self):
        ns_a, ns_b = _ns(), _ns()
        key = "runbooks/wrk-" + uuid.uuid4().hex[:8]
        pa = link_pointer(
            self.conn, namespace=ns_a, target=TARGET, key=key, version=1,
            full_sha256=_digest("wrk-a"), summary="writer rekey probe a",
            expected_row_id=None)
        pb = link_pointer(
            self.conn, namespace=ns_b, target=TARGET, key=key, version=1,
            full_sha256=_digest("wrk-b"), summary="writer rekey probe b",
            expected_row_id=None)
        return ns_a, ns_b, key, pa, pb

    def test_write_rekey_namespace_collision_refused_with_rollback(self):
        ns_a, ns_b, key, pa, pb = self._two_verified_same_ref()
        self.conn.commit()
        with capture():
            with self.assertRaises(RuntimeError):
                _write.rekey_namespace(
                    self.conn, from_namespace=ns_a, to_namespace=ns_b)
        # atomic refusal: NOTHING moved and both pointers stay resolvable
        self.assertFalse(self.conn.in_transaction)
        for ns, result in ((ns_a, pa), (ns_b, pb)):
            row = self.conn.execute(
                "SELECT namespace, superseded_at FROM memory WHERE id=?",
                (result["row_id"],)).fetchone()
            self.assertEqual(row["namespace"], ns)
            self.assertIsNone(row["superseded_at"])
            self.assertEqual(
                get_pointer(self.conn, ns, TARGET, key).row_id,
                result["row_id"])

    def test_write_rekey_namespace_nonconflict_still_moves(self):
        ns_a = _ns()
        key = "runbooks/wrk-ok-" + uuid.uuid4().hex[:8]
        pa = link_pointer(
            self.conn, namespace=ns_a, target=TARGET, key=key, version=1,
            full_sha256=_digest("wrk-ok"), summary="writer rekey probe ok",
            expected_row_id=None)
        self.conn.commit()
        target_ns = "project:kn-wrk-" + uuid.uuid4().hex[:8]
        with capture():
            moved = _write.rekey_namespace(
                self.conn, from_namespace=ns_a, to_namespace=target_ns)
        self.assertGreaterEqual(moved, 1)
        self.assertEqual(
            get_pointer(self.conn, target_ns, TARGET, key).row_id,
            pa["row_id"])

    def test_schema_v5_rekey_skips_colliding_namespace(self):
        import types
        _ns_a, target_ns, key, pa, pb = self._two_verified_same_ref()
        # strand pa under a legacy dead key (v5 shape: write-guard-invalid)
        legacy_ns = "legacy-" + uuid.uuid4().hex[:8]
        self.conn.execute(
            "UPDATE memory SET namespace=? WHERE id=?", (legacy_ns, pa["row_id"]))
        checkout = self.tmp / "checkout"
        checkout.mkdir()
        self.conn.commit()
        suffix = target_ns[len("project:"):]
        fake_host = types.SimpleNamespace(
            _get_git_remote_status=lambda p: (
                "https://example.invalid/org/repo.git", "remote"),
            _normalize_remote=lambda url: suffix,
        )
        env = {"ZMEM_NS_MIGRATION_MAP": json.dumps(
            {legacy_ns: str(checkout)})}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(_schema, "_host", fake_host), capture():
            mapping = _schema._rekey_namespaces(self.conn, [legacy_ns])
        # refuse-and-retry: the colliding namespace is left unmoved (and
        # recorded as unresolved, so the retry pass re-attempts it later)
        self.assertEqual(mapping, {})
        row = self.conn.execute(
            "SELECT namespace, superseded_at FROM memory WHERE id=?",
            (pa["row_id"],)).fetchone()
        self.assertEqual(row["namespace"], legacy_ns)
        self.assertIsNone(row["superseded_at"])
        self.assertEqual(
            get_pointer(self.conn, target_ns, TARGET, key).row_id,
            pb["row_id"])


class RescanPointerLaneTest(_StoreTestCase):
    """rescan-secrets must never route a reserved pointer row through
    update_memory (its guard refuses pointer edits, which pre-fix aborted
    the WHOLE maintenance batch). Secret-suspect pointers are quarantined
    and tombstoned individually via the knowledge handler's dedicated
    maintenance helper, with the reason recorded; clean pointers stay
    untouched; ordinary rows keep flowing through the normal batch; the
    report never hides the suspects."""

    def _raw_pointer_row(self, ns, ref, payload_json):
        mid = str(uuid.uuid4())
        ts = _schema.now_iso()
        self.conn.execute(
            """INSERT INTO memory
               (id, namespace, type, content, tags, source_ref, source_hash,
                confidence, signal, valid_from, superseded_at, ingestion_ts,
                retrieval_count, last_retrieved, taint)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, ns, "fact", payload_json, "knowledge", ref, "", 0.2,
             "none", ts, None, ts, 0, None, "untrusted_tool"))
        self.conn.commit()
        return mid

    def _payload_with_summary(self, key_suffix, summary):
        base = build_payload(
            target=TARGET, key="runbooks/" + key_suffix, version=1,
            full_sha256=_digest(key_suffix), summary="clean placeholder")
        obj = json.loads(base)
        obj["summary"] = summary
        return json.dumps(obj, sort_keys=True, separators=(",", ":"))

    def test_secret_pointer_quarantined_ordinary_redacted_clean_kept(self):
        ns = _ns()
        # 1) a clean, admission-screened verified pointer: not a suspect
        clean_key = "runbooks/scan-clean-" + uuid.uuid4().hex[:6]
        clean = link_pointer(
            self.conn, namespace=ns, target=TARGET, key=clean_key, version=1,
            full_sha256=_digest("scan-clean"), summary="clean runbook note",
            expected_row_id=None)
        clean_payload = self.conn.execute(
            "SELECT content FROM memory WHERE id=?", (clean["row_id"],)
        ).fetchone()["content"]
        # 2) an inert/unverified pointer row whose payload text carries a
        #    secret (surgery: admission would refuse this summary today)
        suspect_ref = make_source_ref(
            TARGET, "runbooks/scan-suspect-" + uuid.uuid4().hex[:6])
        suspect = self._raw_pointer_row(
            ns, suspect_ref, self._payload_with_summary(
                "scan-suspect", "api_key=abcdEFGH1234 leaked into summary"))
        # 3) an ordinary row the policy CAN remediate
        ordinary = str(_write.add_memory(
            self.conn, namespace=ns, type_="fact",
            content="ordinary note with token: supersecretvalue1 inside",
            signal="test", source_ref="fixture:rescan:ordinary"))
        self.conn.commit()

        # dry-run first: the suspect is REPORTED, never hidden
        dry = _write.rescan_secrets(self.conn, apply=False)
        self.assertIn(suspect, dry["ids"])
        self.assertEqual(dry["pointer_rows_quarantined"], 1)

        result = _write.rescan_secrets(self.conn, apply=True)

        # no abort: the routine completed for every row kind
        self.assertEqual(result["pointer_rows_quarantined"], 1)
        self.assertIn(suspect, result["ids"])
        self.assertIn(ordinary, result["ids"])
        self.assertNotIn(clean["row_id"], result["ids"])
        # the suspect pointer is dead with the quarantine reason recorded
        dead = self.conn.execute(
            "SELECT superseded_at, supersede_reason FROM memory WHERE id=?",
            (suspect,)).fetchone()
        self.assertIsNotNone(dead["superseded_at"])
        self.assertIn("quarantined", dead["supersede_reason"])
        self.assertIn("secret", dead["supersede_reason"])
        # and it is no longer authoritative/visible as pointer state
        rows = [r for r in get_pointer_rows(
            self.conn, ns, TARGET, suspect_ref.split(":", 2)[2])
            if not r["verified"]]
        self.assertEqual(rows, [])
        # the ordinary row was remediated: old row tombstoned, a live
        # redacted replacement exists
        self.assertIsNotNone(self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?",
            (ordinary,)).fetchone()["superseded_at"])
        redacted_live = self.conn.execute(
            "SELECT count(*) AS c FROM memory WHERE namespace=? "
            "AND superseded_at IS NULL AND content LIKE ?",
            (ns, "%[REDACTED_SECRET]%")).fetchone()["c"]
        self.assertGreaterEqual(redacted_live, 1)
        # the clean pointer is byte-identical and still authoritative
        kept = self.conn.execute(
            "SELECT content, superseded_at FROM memory WHERE id=?",
            (clean["row_id"],)).fetchone()
        self.assertIsNone(kept["superseded_at"])
        self.assertEqual(kept["content"], clean_payload)
        self.assertEqual(
            get_pointer(self.conn, ns, TARGET, clean_key).row_id,
            clean["row_id"])

    def test_pointer_capture_refusal_quarantines_not_aborts(self):
        ns = _ns()
        ref = make_source_ref(
            TARGET, "runbooks/scan-sudo-" + uuid.uuid4().hex[:6])
        suspect = self._raw_pointer_row(
            ns, ref, self._payload_with_summary(
                "scan-sudo", "runs sudo -S dnf install x for the update"))
        self.conn.commit()
        # pre-fix: apply_capture_policy raised unredactable_secret and the
        # whole rescan aborted; now the pointer lane quarantines it
        result = _write.rescan_secrets(self.conn, apply=True)
        self.assertEqual(result["pointer_rows_quarantined"], 1)
        dead = self.conn.execute(
            "SELECT superseded_at, supersede_reason FROM memory WHERE id=?",
            (suspect,)).fetchone()
        self.assertIsNotNone(dead["superseded_at"])
        self.assertIn("capture refused", dead["supersede_reason"])

    def test_future_pattern_pointer_suspect_and_ordinary_candidate(self):
        # a pattern admitted today but flagged tomorrow: patch BOTH the
        # detector list and the redaction seam the capture policy consults
        import re as _re
        future_pat = _re.compile(r"FUTUREWORD")

        def fake_redact(text):
            count = 1 if future_pat.search(text) else 0
            return (future_pat.sub("[REDACTED_SECRET]", text), count)

        ns = _ns()
        ref = make_source_ref(
            TARGET, "runbooks/scan-future-" + uuid.uuid4().hex[:6])
        suspect = self._raw_pointer_row(
            ns, ref, self._payload_with_summary(
                "scan-future", "rotate the FUTUREWORD before the window"))
        ordinary = str(_write.add_memory(
            self.conn, namespace=ns, type_="fact",
            content="ordinary FUTUREWORD note", signal="test",
            source_ref="fixture:rescan:future"))
        self.conn.commit()

        with mock.patch.object(_write, "SECRET_PATTERNS", [future_pat]), \
                mock.patch.object(_write, "_redact_secret_like_text",
                                  fake_redact):
            result = _write.rescan_secrets(self.conn, apply=True)

        self.assertEqual(result["pointer_rows_quarantined"], 1)
        self.assertIn(suspect, result["ids"])
        self.assertIsNotNone(self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?",
            (suspect,)).fetchone()["superseded_at"])
        # the ordinary future row was remediated through the normal lane
        redacted_live = self.conn.execute(
            "SELECT count(*) AS c FROM memory WHERE namespace=? "
            "AND superseded_at IS NULL AND content LIKE ?",
            (ns, "%[REDACTED_SECRET]%")).fetchone()["c"]
        self.assertGreaterEqual(redacted_live, 1)

    def test_quarantine_helper_refuses_ordinary_rows(self):
        ns = _ns()
        ordinary = str(_write.add_memory(
            self.conn, namespace=ns, type_="fact", content="plain row",
            signal="test", source_ref="fixture:rescan:plain"))
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(KnowledgePointerRefusal):
                _knowledge.quarantine_pointer_row(
                    self.conn, ordinary, reason="probe")
        finally:
            self.conn.rollback()


if __name__ == "__main__":
    unittest.main(verbosity=2)
