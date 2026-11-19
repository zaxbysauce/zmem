"""Supplementary (unfrozen) guard for the PR #491 feedback round
(parallel-F-003): a store already holding NULL content_norm rows written
by the PRE-0.162.0 materialization writers must be repaired on its next
open by the one-time stamped _repair_null_content_norm_162 pass.

Complements tests/test_content_norm_backfill.py (frozen C1-C7, which test
FRESH materializations): this file proves the ALREADY-HOLLOW store class
heals, that the repair only normalizes the derived column, and that the
meta stamp makes the second open a no-op.

Run: python -X utf8 tests/test_content_norm_repair_on_open.py
"""

from __future__ import annotations
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Hermeticity floor before the #244 isolation guard import (same pattern
# as tests/test_content_norm_backfill.py).
if not (os.environ.get("ZMEM_STORE") or os.environ.get("ZMEM_DATA")):
    os.environ["ZMEM_DATA"] = tempfile.mkdtemp(prefix="zmem-repair162-")
import _store_guard  # noqa: F401  (issue #244 isolation guard)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"

_AMBIENT_PINS = (
    "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_INJECT", "ZMEM_MODELS_DIR",
    "ZMEM_REMOTE_URL",
)


def _store_env(store: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_PINS}
    env.update({
        "ZMEM_STORE": str(store),
        "ZMEM_DATA": str(store.parent),
        "ZMEM_MODELS_DIR": str(store.parent / "models-absent"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_INJECT": "0",
    })
    return env


class RepairOnOpenTest(unittest.TestCase):
    def test_hollow_store_repairs_on_next_open(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-repair162-leg-"))
        self.addCleanup(shutil_rmtree, tmp, True)
        store = tmp / "store.sqlite"
        env = _store_env(store)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "init"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Plant the hollow shape the way a REAL upgraded store looks:
        # a pre-0.162.0 store reaching the new client for the first time
        # carries NULL content_norm rows AND has no repair stamp (the
        # stamp only exists once a 0.162.0 client has opened the store).
        # The `init` above models the store's own history under the old
        # client; clearing the stamp afterwards restores the exact
        # pre-upgrade state the repair targets.
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(memory)")}
        self.assertIn("content_norm", cols,
                      "precondition: the fresh store carries content_norm")
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, "
            "source_ref, content_norm, superseded_at, ingestion_ts) "
            "VALUES ('repair162-proof', 'project:repair162', 'fact', "
            "'hollow row body', '', 'src:x', NULL, NULL, "
            "'2026-11-18T00:00:00Z')")
        conn.execute("DELETE FROM meta WHERE key='content_norm_repair_162'")
        conn.commit()
        conn.close()
        # The repair fires on the next production open (any store.py
        # command runs init+..., the same _prepare_store path).
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "stats"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=300)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, namespace, type, content, content_norm, source_ref, "
            "ingestion_ts FROM memory WHERE id='repair162-proof'"
        ).fetchone()
        self.assertIsNotNone(row, "the planted row must stay live")
        self.assertIsNotNone(
            row["content_norm"],
            "a store already holding NULL content_norm rows from the "
            "pre-0.162.0 writers must be repaired on its next open "
            "(the one-time stamped _repair_null_content_norm_162 pass)")
        # Exact value (parallel-F-003 BLOCKED clearance): the repair must
        # write the CANONICAL normalization of the row's own content, not
        # merely any non-NULL filler — and it must touch the derived
        # column only.
        sys.path.insert(0, str(SCRIPTS))
        from storelib.schema import _normalize_content
        self.assertEqual(
            row["content_norm"], _normalize_content("hollow row body"),
            "the repair must write the canonical normalize_content form "
            "of the row's own content, identical to what the live "
            "writers materialize")
        self.assertEqual(row["id"], "repair162-proof")
        self.assertEqual(row["namespace"], "project:repair162")
        self.assertEqual(row["type"], "fact")
        self.assertEqual(row["content"], "hollow row body")
        self.assertEqual(row["source_ref"], "src:x")
        self.assertEqual(row["ingestion_ts"], "2026-11-18T00:00:00Z")
        stamp = conn.execute(
            "SELECT value FROM meta WHERE key='content_norm_repair_162'"
        ).fetchone()
        self.assertIsNotNone(stamp, "the one-time repair stamp must be "
                             "written after the backfill")
        conn.close()

    def test_repair_is_one_time_and_content_only(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-repair162-idem-"))
        self.addCleanup(shutil_rmtree, tmp, True)
        store = tmp / "store.sqlite"
        env = _store_env(store)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "init"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(store))
        before = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
        stamp = conn.execute(
            "SELECT value FROM meta WHERE key='content_norm_repair_162'"
        ).fetchone()
        self.assertIsNotNone(stamp,
                             "the stamp is written on the first open even "
                             "when no NULL rows exist (steady state = one "
                             "meta read)")
        conn.close()
        # Idempotent: a second open neither changes rows nor re-fails.
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "stats"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=300)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        conn = sqlite3.connect(str(store))
        after = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
        self.assertEqual(before, after,
                         "the one-time repair must not add or drop rows")


class StagedRefusalCleanupTest(unittest.TestCase):
    """Stage-B v3 reviewer required corrections: (1) a refusal INSIDE
    materialization (non-text content row) must surface as the DatasetError
    with the staged temp file removed — close-before-unlink, so the Windows
    sharing violation never masks the refusal; (2) an init_db/migrate
    failure must propagate as the ORIGINAL error (never an UnboundLocalError
    from the finally's flag read) with the staged file cleaned too."""

    def _fresh_store_and_export(self, tmp: Path) -> Path:
        store = tmp / "store.sqlite"
        env = _store_env(store)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "init"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(store))
        try:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, "
                "ingestion_ts) VALUES "
                "('00000000-0000-4000-8000-000000000001', "
                "'project:staged162', 'fact', 'seed body', "
                "'2026-11-18T00:00:00Z')")
            conn.commit()
        finally:
            conn.close()
        out_dir = tmp / "ds-export"
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY),
             "export-dataset", str(out_dir), "--namespace",
             "project:staged162"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        return out_dir

    def _rewrite_families(self, out_dir: Path, bad_content) -> None:
        sys.path.insert(0, str(SCRIPTS))
        from storelib import dataset as ds
        manifest = json.loads(
            (out_dir / "manifest.json").read_text(encoding="utf-8"))
        families = {}
        for family in ("memories", "episodes", "episode_members",
                       "links", "memory_expiry"):
            families[family] = ds._read_family(out_dir, family,
                                               manifest["format"])
        bad = families["memories"][0]
        bad["content"] = bad_content  # the N-05 non-text shape
        bad["row_checksum"] = ds.row_checksum(bad)
        # Craft the whole directory as a self-consistent JSONL dataset:
        # per-row checksums and the snapshot hash are re-stamped from the
        # SAME mutated families, so every import-side integrity gate passes
        # and the refusal actually fires inside materialization. (The
        # export's parquet files are left in place; the manifest flip makes
        # the reader ignore them.)
        manifest["format"] = "jsonl"
        for family in ("memories", "episodes", "episode_members", "links",
                       "memory_expiry"):
            stem = ds._FAMILY_FILES[family]
            with open(out_dir / "data" / f"{stem}.jsonl", "w",
                      encoding="utf-8", newline="\n") as fh:
                for row in families[family]:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        manifest["source_snapshot_hash"] = ds._compute_snapshot_hash(
            families, True)
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")

    def test_staged_refusal_cleans_tmp_and_preserves_dataseterror(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-staged162-"))
        self.addCleanup(shutil_rmtree, tmp, True)
        out_dir = self._fresh_store_and_export(tmp)
        self._rewrite_families(out_dir, 42)  # the N-05 non-text shape
        sys.path.insert(0, str(SCRIPTS))
        from storelib import dataset as ds
        manifest = json.loads(
            (out_dir / "manifest.json").read_text(encoding="utf-8"))
        dest = tmp / "dest"
        with self.assertRaisesRegex(ds.DatasetError, "non-text content"):
            ds.import_dataset(
                str(out_dir), revision=manifest["source_snapshot_hash"],
                dest_dir=str(dest), namespace="project:staged162")
        self.assertEqual(list(dest.iterdir()), [],
                         "a refusal inside materialization must leave no "
                         "staged temp file in the destination")

    def test_overcap_content_row_refused(self):
        # Stage-B v5 (UR-001): the writer path's MAX_CONTENT_CHARS admission
        # cap is a store invariant — a checksum-consistent crafted dataset
        # carrying an over-cap content row must be refused whole-snapshot
        # BEFORE any destination work (the #353 law), leaving no staged
        # file.
        tmp = Path(tempfile.mkdtemp(prefix="zmem-cap162-"))
        self.addCleanup(shutil_rmtree, tmp, True)
        out_dir = self._fresh_store_and_export(tmp)
        sys.path.insert(0, str(SCRIPTS))
        from storelib import dataset as ds
        from storelib.schema import MAX_CONTENT_CHARS
        self._rewrite_families(out_dir, "x" * (MAX_CONTENT_CHARS + 1))
        manifest = json.loads(
            (out_dir / "manifest.json").read_text(encoding="utf-8"))
        dest = tmp / "dest"
        with self.assertRaisesRegex(ds.DatasetError, "MAX_CONTENT_CHARS"):
            ds.import_dataset(
                str(out_dir), revision=manifest["source_snapshot_hash"],
                dest_dir=str(dest), namespace="project:staged162")
        self.assertFalse(
            dest.exists(),
            "an over-cap refusal is a pre-connection admission gate: the "
            "destination directory is never created (the #353 whole-snapshot "
            "refusal law)")

    def test_init_failure_preserves_original_error_and_cleans(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-staged162-init-"))
        self.addCleanup(shutil_rmtree, tmp, True)
        out_dir = self._fresh_store_and_export(tmp)
        sys.path.insert(0, str(SCRIPTS))
        from storelib import dataset as ds
        manifest = json.loads(
            (out_dir / "manifest.json").read_text(encoding="utf-8"))
        dest = tmp / "dest"
        from unittest import mock
        with mock.patch.object(ds, "init_db",
                               side_effect=RuntimeError("schema boom")):
            with self.assertRaisesRegex(RuntimeError, "schema boom"):
                ds.import_dataset(
                    str(out_dir),
                    revision=manifest["source_snapshot_hash"],
                    dest_dir=str(dest), namespace="project:staged162")
        self.assertEqual(list(dest.iterdir()), [],
                         "an init failure must not litter the staged "
                         "snapshot either")
        self.assertFalse((dest / "snapshot.sqlite").exists())


class ImportStoreStaleNormVerifyTest(unittest.TestCase):
    """Stage-B v4 (ST-001/UI-002 mechanism fix): sanitization leaving
    content/tags byte-identical must NOT certify a source-borne non-NULL
    content_norm. The sqlite import lane verifies every retained row's norm
    against the canonical normalize of the accepted content and overwrites
    mismatches; a CORRECT norm survives byte-identical (the verify is not a
    blanket rewrite)."""

    def _import_source_with_norm(self, tmp: Path, stored_norm) -> Path:
        store = tmp / "src.sqlite"
        env = _store_env(store)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "init"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        sys.path.insert(0, str(SCRIPTS))
        from storelib.schema import _normalize_content
        conn = sqlite3.connect(str(store))
        try:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, "
                "content_norm, ingestion_ts) VALUES "
                "('00000000-0000-4000-8000-00000000000a', "
                "'project:stale162', 'fact', 'stale verify body', ?, "
                "'2026-11-18T00:00:00Z')", (stored_norm,))
            conn.commit()
            # Import-store stages the source via the online-backup API and
            # then switches the staged file out of WAL inside its sanitize
            # transaction — leave the fixture in a clean DELETE-journal state
            # so the staged copy does not inherit WAL.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()
        dest_dir = tmp / "dest"
        dest_dir.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPTS / "import-store.py"),
             "--source", str(store), "--dest-dir", str(dest_dir), "--force"],
            capture_output=True, text=True,
            env={k: v for k, v in os.environ.items()
                 if k not in _AMBIENT_PINS} | {"ZMEM_INJECT": "0",
                                               "PYTHONIOENCODING": "utf-8"},
            cwd=str(REPO_ROOT), timeout=600)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        sys.path.insert(0, str(SCRIPTS))
        return dest_dir / "store.sqlite"

    def test_stale_nonnull_norm_corrected_on_import(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-stale162-"))
        self.addCleanup(shutil_rmtree, tmp, True)
        sys.path.insert(0, str(SCRIPTS))
        from storelib.schema import _normalize_content
        dest = self._import_source_with_norm(tmp, "STALE WRONG NORM")
        conn = sqlite3.connect(str(dest))
        try:
            row = conn.execute(
                "SELECT content_norm FROM memory "
                "WHERE id='00000000-0000-4000-8000-00000000000a'").fetchone()
        finally:
            conn.close()
        self.assertEqual(
            row[0], _normalize_content("stale verify body"),
            "a retained non-NULL content_norm that disagrees with the "
            "accepted content must be recomputed on import (ST-001/UI-002 "
            "mechanism fix)")

    def test_correct_norm_survives_unchanged(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-stale162-ok-"))
        self.addCleanup(shutil_rmtree, tmp, True)
        sys.path.insert(0, str(SCRIPTS))
        from storelib.schema import _normalize_content
        canonical = _normalize_content("stale verify body")
        dest = self._import_source_with_norm(tmp, canonical)
        conn = sqlite3.connect(str(dest))
        try:
            row = conn.execute(
                "SELECT content_norm FROM memory "
                "WHERE id='00000000-0000-4000-8000-00000000000a'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], canonical,
                         "a CORRECT non-NULL content_norm must survive the "
                         "import byte-identical (the verify leg rewrites "
                         "mismatches only, never a blanket rewrite)")


class WindowSiteExplainTest(unittest.TestCase):
    """Reviewer v6 required correction (supersedes the whole-file count
    pin): per-SITE binding. Each production window SQL is EXTRACTED from
    the writer modules via AST — implicit string-literal concatenation is
    merged at parse time, so every collected Constant that carries the
    window markers (content_norm=? + the knowledge-ref exclusion) IS one
    executable production query, not a comment or unrelated text. Each
    extracted site must carry the full window shape (namespace=?,
    content_norm=?, superseded_at IS NULL, the exclusion clause) — the
    same shape whose EXPLAIN the frozen C7 perf leg asserts is
    index-served. A site losing its exclusion fails THIS test with the
    site's SQL named; unrelated matching text cannot satisfy it (comments
    are not AST constants; docstrings lack both markers)."""

    _SITE_MARKERS = ("content_norm=?", "knowledge:%")
    # The canonical predicate BLOCK (boolean logic + parenthesization
    # pinned): a site flipping OR/AND, reordering the predicate, or
    # splicing an index hint inside the block fails this test. ORDER BY /
    # LIMIT tails and additional predicates appended AFTER the block
    # (e.g. the routed window's id<>?) stay free — disclosed in the PR
    # body amendment.
    _CANON_BLOCK = ("WHERE namespace=? AND content_norm=? "
                    "AND superseded_at IS NULL "
                    "AND (source_ref IS NULL OR source_ref NOT LIKE "
                    "'knowledge:%')")

    def _extract_sites(self, rel: str) -> list:
        import ast
        path = (REPO_ROOT / "skills" / "memory" / "scripts" / "storelib"
                / rel)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sites = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = " ".join(node.value.split())
                if all(m in sql for m in self._SITE_MARKERS) \
                        and sql.upper().lstrip().startswith("SELECT"):
                    sites.append(sql)
        return sites

    def test_every_production_window_site_carries_the_shape(self):
        # Pinned to the 0.162.0 shapes (AST-probed): write.py 3 full window
        # sites (add / update / routed windows), sync.py 1 (the ingest
        # re-rank window), mine.py 1 (the promote dry-run fold window). The
        # two clause-only prefix-recheck sites (write.py's cache-hit fetch,
        # sync.py's exact-match cache fetch) deliberately lack
        # content_norm=? — they re-check the knowledge prefix only and are
        # not window queries.
        expectations = {"write.py": 3, "sync.py": 1, "mine.py": 1}
        for rel, minimum in expectations.items():
            sites = self._extract_sites(rel)
            self.assertGreaterEqual(
                len(sites), minimum,
                f"{rel}: expected >= {minimum} production window sites "
                f"(SELECT constants carrying both window markers), found "
                f"{len(sites)} — a site's SQL changed shape or the "
                "exclusion clause was dropped")
            for i, sql in enumerate(sites):
                # Reviewer v7 required correction: boolean-logic binding —
                # the canonical predicate BLOCK must appear as a contiguous
                # normalized span (subsumes the four loose shape parts: an
                # OR/AND flip, a predicate reorder, a dropped clause, or an
                # index directive spliced inside all break the block and
                # fail here, with the site's SQL named). The ONE disclosed
                # interposition is the routed window's `AND id<>?`
                # (self-exclusion of the acting row — plan-neutral), which
                # is stripped before the block comparison; any OTHER
                # interposition still fails.
                check_sql = " ".join(
                    sql.replace("AND id<>?", "").split())
                # No index directive anywhere in the site SQL (SQLite's only
                # directive syntax): a spliced INDEXED BY forcing a wrong
                # index keeps the predicate block intact but would break
                # index-servedness — rejected here.
                self.assertNotIn(
                    "INDEXED BY", sql.upper(),
                    f"{rel} window site #{i + 1} carries an INDEXED BY "
                    f"directive (would force a possibly-wrong index and "
                    f"break the asserted plan oracle): {sql!r}")
                self.assertIn(
                    self._CANON_BLOCK, check_sql,
                    f"{rel} window site #{i + 1} does not carry the "
                    f"canonical predicate block verbatim (plan-relevant "
                    f"shape changed): {sql!r}")
                # Tail grammar pin (reviewer v8: appending ` OR 1=1` after
                # the block preserves every marker but defeats the partial
                # index — a disjunction at the top level makes the planner
                # post-filter). After the block, ONLY these tails are
                # legal: end-of-SQL, ORDER BY <cols>, LIMIT <n|?> — nothing
                # else (no OR, no AND, no extra WHERE terms).
                import re as _re
                tail = check_sql.split(self._CANON_BLOCK, 1)[1]
                if not _re.fullmatch(
                        r"( ORDER BY [A-Za-z0-9_,.?() ]+)?"
                        r"( LIMIT [0-9?]+)?", tail or ""):
                    self.fail(
                        f"{rel} window site #{i + 1} carries a "
                        f"plan-breaking tail after the canonical block "
                        f"(only ORDER BY/LIMIT continuations are legal — "
                        f"a disjunction or extra predicate here defeats "
                        f"the partial index): tail={tail!r} sql={sql!r}")


def shutil_rmtree(path, ignore_errors=False):
    import shutil
    shutil.rmtree(str(path), ignore_errors=ignore_errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
