#!/usr/bin/env python
"""Issue #63, 8.3: reembed --all / --profile / --batch / --dry-run contracts.

Run:  python tests/test_reembed.py   (no pytest; house convention)

Model-absent by design: every vector-producing scenario injects a
deterministic variable-dimension stub through the SAME module-global seam the
existing suite uses (`recall_mod._embeddings`, cf. tests/test_hybrid_default.py).
Guarantees pinned here:
- flagless backfill keeps its legacy stdout contract;
- --all is idempotent (second run changes 0 rows), never touches
  retrieval_count / surfaced_count / content, and records meta.profile;
- --dry-run writes NOTHING at the file level (byte digest comparison);
- dimension conversion recreates memory_vec atomically and KNN stays healthy
  immediately AND after a fresh reopen (skipUnless sqlite-vec);
- an embedder failing MID-RUN rolls back to the pristine pre-state (the
  issue's half-dim-index prohibition);
- --batch paces stderr progress (display-only);
- refusals are fail-closed exit-2s with remediation hints.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import shutil
import struct
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import embed_profiles as ep  # noqa: E402


def _sqlite_vec_available() -> bool:
    try:
        import sqlite_vec  # noqa: F401
        return True
    except ImportError:
        return False


class _StubEmbeddings:
    """Deterministic N-dim embedder keyed on normalized text length+content."""

    def __init__(self, available=True, dim=384, fail_after=None):
        self._available = available
        self._dim = dim
        self.calls = 0
        self.fail_after = fail_after  # raise on the (N+1)-th embed call

    def is_available(self):
        return self._available

    def availability_status(self):
        return {
            "available": self._available,
            "reason": "ok" if self._available else "model_file_missing",
            "missing_imports": [],
            "models_dir": "", "interpreter": "", "model_file": False,
            "tokenizer_file": False, "checksum_ok": None,
            "load_failed": False, "profile": "minilm", "dim": self._dim,
        }

    def warn_fake_active(self):
        # the production `embeddings` module owns this one-time banner; stubs
        # mirror the seam contract so profile=fake rebuild paths exercise it
        self.fake_warned = True

    def embed_text(self, text):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("stub embedder blew up mid-rebuild")
        seed = abs(hash("stable")) * 0  # determinism across runs below
        h = hashlib.sha256(text.encode("utf-8")).digest()
        base = h[0] / 255.0 or 0.5
        vals = [base] * self._dim
        return struct.pack(f"<{self._dim}f", *vals)


class ReembedBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-reembed-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store_path = Path(self.tmp) / "store.sqlite"
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("ZMEM_STORE", "ZMEM_DATA", ep.PROFILE_ENV,
                      "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
                      "ZMEM_CROSS_ENCODER")
        }
        os.environ["ZMEM_STORE"] = str(self.store_path)
        os.environ.pop("ZMEM_DATA", None)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_MODELS_DIR"] = str(Path(self.tmp) / "no-models")
        os.environ.pop(ep.PROFILE_ENV, None)
        os.environ.pop("ZMEM_CROSS_ENCODER", None)
        self.addCleanup(self._restore_env)
        # fresh interpreter-visible modules per test
        for m in list(sys.modules):
            if m == "store" or m.startswith("storelib"):
                del sys.modules[m]
        import storelib.recall as recall_mod
        import storelib.schema as schema_mod
        self.R = recall_mod
        self.S = schema_mod
        self.addCleanup(self._unpatch_embeddings)
        self._conn = self.S.connect()
        self.S._prepare_store(self._conn)
        self._orig_embeddings = recall_mod._embeddings

    def tearDown(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def _restore_env(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _unpatch_embeddings(self):
        self.R._embeddings = self._orig_embeddings
        from storelib import write as write_mod
        write_mod._embeddings = self._orig_embeddings

    def patch_stub(self, **kw):
        stub = _StubEmbeddings(**kw)
        # both owners matter: recall embeds queries/backfills; write embeds adds
        self.R._embeddings = stub
        from storelib import write as write_mod
        write_mod._embeddings = stub
        return stub

    def seed(self, n=4, content_fmt="seed content {} unique"):
        ids = []
        for i in range(n):
            r = self._conn.execute(
                "INSERT INTO memory(id,namespace,type,content,tags,source_ref,"
                "source_hash,confidence,signal,valid_from,superseded_at,"
                "ingestion_ts,retrieval_count,last_retrieved,embedding,"
                "embedding_model,embedded_at,content_norm,valid_until,"
                "update_of,taint) VALUES (?, 'user:t','fact',?, '', '', '', "
                "0.9,'test','2026-01-01T00:00:00Z',NULL,'2026-01-01T00:00:00Z',"
                f"{i}, NULL, NULL, '', NULL, ?, '', '', 'trusted_internal')",
                (f"row{i}", content_fmt.format(i),
                 content_fmt.format(i).strip().lower()),
            )
            ids.append(f"row{i}")
        self._conn.commit()
        return ids

    def state_fingerprint(self):
        """Everything the rebuild contract promises to leave untouched."""
        rows = self._conn.execute(
            "SELECT id, content, retrieval_count, surfaced_count FROM memory "
            "ORDER BY id").fetchall()
        return [
            (r["id"], r["content"], r["retrieval_count"], r["surfaced_count"])
            for r in rows
        ]

    def run_reembed(self, *args, argv_only=False):
        """Drive through the real argparse/main dispatch when flags matter."""
        old = sys.argv
        err, out = io.StringIO(), io.StringIO()
        sys.argv = ["store.py", "reembed", *args]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                from storelib.cli import main as cli_main
                try:
                    cli_main()
                    rc = 0
                except SystemExit as e:
                    rc = e.code or 0
        finally:
            sys.argv = old
        return rc, out.getvalue(), err.getvalue()


class LegacyFlaglessContract(ReembedBase):
    def test_backfill_message_and_counts(self):
        self.patch_stub(dim=384)
        self.seed(3)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.R.reembed_embeddings(self._conn)
        self.assertEqual(rc, 0)
        self.assertIn(
            "[zmem] embedded 3 memories", out.getvalue(),
            "legacy summary line shape must survive verbatim-ish",
        )

    def test_graceful_degrade_without_runtime(self):
        stub = self.patch_stub(available=False)
        self.seed(1)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.R.reembed_embeddings(self._conn)
        self.assertEqual(rc, 0, "flagless degrade is exit-0, not refusal")
        self.assertIn("embeddings unavailable", out.getvalue())
        left = self._conn.execute(
            "SELECT COUNT(*) FROM memory WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        self.assertEqual(left, 0)


class AllRebuildSemantics(ReembedBase):
    @unittest.skipUnless(_sqlite_vec_available(), "sqlite-vec required")
    def test_all_idempotent_and_metadata_recorded(self):
        self.patch_stub(dim=384)
        self.seed(3)
        self.assertEqual(
            self.run_reembed("--all")[0], 0, "seed pass must succeed")
        before_blob = self._blob_map()
        rc, out, err = self.run_reembed("--all")
        self.assertEqual(rc, 0)
        self.assertIn("rebuilt 3 memories", out)
        second_blobs = self._blob_map()
        self.assertEqual(before_blob, second_blobs,
                         "--all is idempotent: identical bytes reapplied")
        declared = self.R._declared_vec0_dim(self._conn)
        self.assertEqual(declared, 384)
        meta = self.S.get_meta(self._conn, "embedding_profile")
        self.assertEqual(meta, "minilm")

    def _blob_map(self):
        rows = self._conn.execute(
            "SELECT id, embedding FROM memory ORDER BY id").fetchall()
        return {r["id"]: bytes(r["embedding"]) for r in rows}

    def test_telemetry_and_content_untouched(self):
        self.patch_stub(dim=384)
        self.seed(2)
        before = self.state_fingerprint()
        self.assertEqual(self.run_reembed("--all")[0], 0)
        self.assertEqual(self.state_fingerprint(), before)

    def test_dry_run_writes_nothing_at_file_level(self):
        self.patch_stub(dim=384)
        self.seed(5)
        self._conn.execute(
            "UPDATE memory SET retrieval_count=retrieval_count WHERE id='row0'"
        )
        self._conn.commit()
        self._conn.close()
        self._conn = self.S.connect()
        self._prepare = self.S._prepare_store(self._conn)

        def file_digest():
            acc = hashlib.sha256()
            for suffix in ("", "-wal", "-shm"):
                pth = Path(str(self.store_path) + suffix)
                if pth.exists():
                    acc.update(pth.read_bytes())
            return acc.hexdigest()

        pre = file_digest()
        rc, out, _ = self.run_reembed("--all", "--dry-run")
        post = file_digest()
        self.assertEqual(rc, 0)
        self.assertEqual(pre, post, "--dry-run must be byte-level inert")
        self.assertIn("would change", out)


@unittest.skipUnless(_sqlite_vec_available(),
                     "dimension conversion exercises sqlite-vec virtual tables")
class DimensionConversion(ReembedBase):
    def test_384_to_16_swap_knn_and_meta(self):
        self.patch_stub(dim=384)
        self.seed(3)
        self.assertEqual(self.run_reembed("--all")[0], 0)
        self.assertEqual(self.R._declared_vec0_dim(self._conn), 384)

        rc, out, err = self.run_reembed("--all", "--profile", "fake")
        self.assertEqual(rc, 0)
        self.assertIn("profile 'fake', dim 16", out)
        self.assertEqual(self.R._declared_vec0_dim(self._conn), 16)
        self.assertEqual(self.S.get_meta(self._conn, "embedding_profile"),
                         "fake")
        rows = self._conn.execute(
            "SELECT DISTINCT length(embedding)/4 AS d FROM memory "
            "WHERE embedding IS NOT NULL").fetchall()
        self.assertEqual([r["d"] for r in rows], [16])

    def test_post_swap_knn_and_reopen_health(self):
        self.patch_stub(dim=384)
        self.seed(6)
        self.run_reembed("--all")

        class FakeOnly(_StubEmbeddings):
            pass

        self.R._embeddings = FakeOnly(dim=384)  # KNN query source irrelevant
        self.run_reembed("--all", "--profile", "fake")
        q = ep.fake_embed("seed content 1 unique")
        knn = self.S._vec_knn_in_namespace(
            self._conn, q, namespaces=["user:t"], k=3, overfetch=8, k_cap=50)
        self.assertTrue(knn, "vec lane must serve queries after conversion")
        best = knn[0][0]
        self.assertEqual(best, "row1", "nearest neighbor tracks canonical match")
        self._conn.close()
        fresh = self.S.connect()
        try:
            knn2 = self.S._vec_knn_in_namespace(
                fresh, q, namespaces=["user:t"], k=3, overfetch=8, k_cap=50)
            self.assertEqual(knn2[0][0], "row1",
                             "health must persist to a brand-new connection")
        finally:
            fresh.close()

    def test_midrun_failure_rolls_back_to_pristine(self):
        self.patch_stub(dim=384)
        self.seed(4)
        self.run_reembed("--all")
        pre_declared = self.R._declared_vec0_dim(self._conn)
        pre_blobs = self._blob_map_all()

        boom = _StubEmbeddings(dim=384, fail_after=1)  # fails on 2nd row
        self.assertFalse(getattr(boom, "fake_warned", False))
        self.R._embeddings = boom
        rc, out, err = self.run_reembed("--all")
        self.assertEqual(rc, 1)
        self.assertIn("Rolled back", err)
        self.assertEqual(self.R._declared_vec0_dim(self._conn), pre_declared)
        self.assertEqual(self._blob_map_all(), pre_blobs)
        self.assertEqual(
            self.S.get_meta(self._conn, "embedding_profile"), "minilm",
            "rolled-back conversion must leave the PRIOR committed marker",
        )

        # Health after rollback: fresh connection serves correct-dim KNN.
        self._conn.close()
        self._conn = self.S.connect()
        q = self._rebuild_query_vector(pre_blobs)
        knn = self.S._vec_knn_in_namespace(
            self._conn, q, namespaces=["user:t"], k=2, overfetch=8, k_cap=50)
        self.assertTrue(knn)

    def _rebuild_query_vector(self, blob_map):
        vals = list(struct.unpack("<384f", blob_map["row0"]))
        return struct.pack("<384f", *vals)

    def _blob_map_all(self):
        rows = self._conn.execute(
            "SELECT id, embedding FROM memory ORDER BY id").fetchall()
        return {r["id"]: bytes(r["embedding"]) for r in rows}


class BatchProgressPacing(ReembedBase):
    def test_progress_lines_equal_batches(self):
        self.patch_stub(dim=384)
        self.seed(7)
        err = io.StringIO()
        old = sys.argv
        sys.argv = ["store.py", "reembed", "--all", "--batch", "3"]
        buf_out = io.StringIO()
        try:
            with redirect_stdout(buf_out), redirect_stderr(err):
                from storelib.cli import main as cli_main
                try:
                    cli_main()
                except SystemExit as e:
                    self.assertIn(e.code, (0, None))
        finally:
            sys.argv = old
        lines = [ln for ln in err.getvalue().splitlines()
                 if ln.startswith("[zmem] reembed:")]
        self.assertEqual(len(lines), 3, "ceil(7/3)=3 progress ticks")

class ReviewRoundFixes(unittest.TestCase):
    """Direct coverage for the independent-review round (issue #63)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-revfix-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store_path = Path(self.tmp) / "store.sqlite"
        self._saved_autodl = os.environ.get("ZMEM_MODEL_AUTODOWNLOAD")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        self.addCleanup(self._restore_autodl)

    def _restore_autodl(self):
        if self._saved_autodl is None:
            os.environ.pop("ZMEM_MODEL_AUTODOWNLOAD", None)
        else:
            os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = self._saved_autodl

    def test_profile_without_all_refuses_exit_2(self):
        env = dict(os.environ)
        env["ZMEM_STORE"] = str(self.store_path)
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "reembed",
             "--profile", "fake"],
            capture_output=True, text=True, env=env, cwd=str(SCRIPTS),
            timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--profile only takes effect with --all",
                      r.stderr + r.stdout)

    def test_ingest_jsonl_guarded_by_dim_mismatch(self):
        """fake-profile ingest on a 384-d store: exit 2, zero rows."""
        import struct as _s
        # build a legacy minilm-dim store first
        os.environ["ZMEM_STORE"] = str(self.store_path)
        os.environ.pop("ZMEM_EMBED_PROFILE", None)
        sys.path.insert(0, str(SCRIPTS))
        for m in list(sys.modules):
            if m.startswith("storelib"):
                del sys.modules[m]
        from storelib.schema import connect as _c, _prepare_store as _p
        conn = _c(); _p(conn)
        conn.execute(
            "INSERT INTO memory(id,namespace,type,content,confidence,signal,"
            "valid_from,ingestion_ts,taint,embedding,embedding_model) VALUES"
            "('old1','user:t','fact','legacy row',0.9,'test',"
            "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','trusted_internal',"
            "?, 'minilm-onnx')",
            (_s.pack("<384f", *([0.3] * 384)),))
        conn.commit(); conn.close()

        jsonl = Path(self.tmp) / "in.jsonl"
        payload = {"id": "new1", "namespace": "user:t", "type": "fact",
                   "content": "fresh imported row", "signal": "none",
                   "ingestion_ts": "2026-02-02T00:00:00Z"}
        jsonl.write_text(json.dumps(payload), encoding="utf-8")

        env = dict(os.environ)
        env["ZMEM_STORE"] = str(self.store_path)
        env["ZMEM_EMBED_PROFILE"] = "fake"
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "ingest-jsonl",
             "--in", str(jsonl)],
            capture_output=True, text=True, env=env, cwd=str(SCRIPTS),
            timeout=60)
        self.assertEqual(
            r.returncode, 2,
            f"mismatched ingest must refuse; got rc={r.returncode} "
            f"stderr={r.stderr[-300:]!r}")
        self.assertIn(
            "expects", r.stderr,
            "rc-2 must come from the embedding-compat GUARD message, not an "
            "incidental validation path")
        cnt = sqlite3.connect(str(self.store_path)).execute(
            "SELECT COUNT(*) FROM memory").fetchone()[0]
        self.assertEqual(cnt, 1, "zero partial rows may land")


class Refusals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-refuse-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = dict(os.environ)
        self.env["ZMEM_STORE"] = str(Path(self.tmp) / "store.sqlite")
        self.env["ZMEM_DATA"] = ""  # neutralized below
        self.env.pop("ZMEM_DATA", None)
        self.env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

    def test_unknown_profile_flag_exit_2(self):
        env = dict(self.env)
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "reembed", "--all",
             "--profile", "bogus"],
            capture_output=True, text=True, env=env, cwd=str(SCRIPTS),
            timeout=60,
        )
        self.assertEqual(r.returncode, 2)

    def test_unparseable_ddl_refuses_unit_level(self):
        raw_db = Path(self.tmp) / "weird.sqlite"
        c = sqlite3.connect(raw_db)
        c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT INTO meta VALUES('schema_version','11')")
        c.execute("CREATE TABLE memory(id TEXT PRIMARY KEY)")
        c.execute("CREATE TABLE memory_vec(embedding BLOB)")  # not float[N]
        c.commit(); c.close()
        sys.path.insert(0, str(SCRIPTS))
        import storelib.recall as R
        c2 = sqlite3.connect(raw_db)
        with self.assertRaises(RuntimeError):
            R._declared_vec0_dim(c2)
        c2.close()


class FailClosedDimensionGuard(unittest.TestCase):
    """env profile vs committed dim mismatch refuses BEFORE any mutation."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-guard-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.saved = {k: os.environ.get(k) for k in
                      ("ZMEM_STORE", ep.PROFILE_ENV, "ZMEM_DATA",
                       "ZMEM_MODEL_AUTODOWNLOAD")}
        os.environ["ZMEM_STORE"] = str(Path(self.tmp) / "store.sqlite")
        os.environ.pop("ZMEM_DATA", None)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ[ep.PROFILE_ENV] = "fake"

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_add_refused_on_mismatch_exit_2_no_partial_write(self):
        sys.path.insert(0, str(SCRIPTS))
        for m in list(sys.modules):
            if m.startswith("storelib"):
                del sys.modules[m]
        import storelib.schema as S
        conn = S.connect(); S._prepare_store(conn)
        # commit a 384-dim era row directly
        conn.execute(
            "INSERT INTO memory(id,namespace,type,content,confidence,signal,"
            "valid_from,ingestion_ts,taint) VALUES ('g1','user:t','fact',"
            "'legacy',0.9,'test','2026-01-01T00:00:00Z',"
            "'2026-01-01T00:00:00Z','trusted_internal')")
        conn.execute(
            "UPDATE memory SET embedding=?, embedding_model='minilm-onnx' "
            "WHERE id='g1'",
            (struct.pack("<384f", *([0.25] * 384)),))
        conn.commit(); conn.close()

        # same-process dispatch attempt under minilm env flips the mismatch
        os.environ[ep.PROFILE_ENV] = "minilm"
        for m in list(sys.modules):
            if m.startswith("storelib"):
                del sys.modules[m]
        import storelib.cli as C
        old = sys.argv
        sys.argv = ["store.py", "add", "--type", "fact",
                    "--content", "post-switch row"]
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    C.main()
        finally:
            sys.argv = old
            os.environ[ep.PROFILE_ENV] = "fake"
        self.assertEqual(cm.exception.code, 2)
        cnt = sqlite3.connect(str(Path(self.tmp) / "store.sqlite")).execute(
            "SELECT COUNT(*) FROM memory").fetchone()[0]
        self.assertEqual(cnt, 1, "refusal must leave zero partial writes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
