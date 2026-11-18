"""Issue #489 frozen acceptance checks (collated 489+490 trace): the three
store-materialization writers (dataset export->import, fleet-snapshot payload
restore, import-store copies) create live rows with content_norm NULL, and the
one-shot v8 backfill never repairs them -- so every content_norm-keyed re-rank
window (add/update/ingest) silently matches nothing on those stores. The
second half pins the idx_memory_content_norm partial-index predicate: the
knowledge-ref exclusion must be index-served, not a post-filter group walk.

Contract legs (AC numbering per the collated trace; the DISCRIMINATING legs
are RED at the base tree and GREEN after the repair; PRESERVING legs are
GREEN at both ends by design):

- AC1 DatasetNullNormFoldTest: a live row with content_norm NULL on a store
  built by the shipped dataset export->import round-trip folds a
  normalized-equal `store.py add` onto that row (result deduped, live-row
  count stays 1, keeper retrieval_count incremented) instead of creating a
  duplicate live row.
- AC2 FleetSnapshotNullNormFoldTest: the same observable contract on a
  fleet-snapshot payload-restored store (_build_from_payload).
- AC3 ImportStoreNullNormFoldTest: the same observable contract on an
  import-store.py copy of a NULL-norm source.
- AC4 WriterUniformityTest (NEW-SURFACE, structural): content_norm is
  populated uniformly at all three writer sites (or one shared
  backfill-on-open choke point in storelib/schema.py wired from all three).
- AC5 PreservationTest (PRESERVING): only the content_norm column changes on
  repaired rows -- every other column byte-identical before/after.
- AC6 IngestNullStoreRerankSeamTest: the #486 contract on the NULL-norm
  store class -- with a forced colliding detector top-1 the ingest fold
  retargets onto the normalized-equal keeper and prints the dedup
  re-ranked notice (detector-stub seam mirroring
  tests/test_sync_ingest_exact_rerank.py DirectIngestRowSeamTest).
- AC7 IndexBoundWindowTest (NEW-SURFACE): the shipped idx_memory_content_norm
  DDL carries the knowledge-ref exclusion in its partial-index predicate,
  and the sync.py window query stays bounded (< 1.0 ms per query at a
  planted 25k knowledge-ref group; pre-change reference ~8.6-30 ms).

Isolation: every store.py / import-store.py invocation is a subprocess with
env pins per store (ZMEM_STORE / ZMEM_DATA / ZMEM_MODELS_DIR=<absent> /
ZMEM_MODEL_AUTODOWNLOAD=0 / ZMEM_INJECT=0); every in-process storelib import
happens only inside the AC6 seam leg with the sibling file's module-purge
discipline; scratch under per-test tempfile.mkdtemp(); ~/.zmem is never
touched (the _store_guard import below fails closed on that). Runs on
Windows and linux: sys.executable, pathlib, no shell-isms.

Run: python -X utf8 tests/test_content_norm_backfill.py
     python -X utf8 tests/test_content_norm_backfill.py DatasetNullNormFoldTest
     python -X utf8 tests/test_content_norm_backfill.py IndexBoundWindowTest
"""

from __future__ import annotations
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock  # noqa: F401  (detector-stub seam)
import uuid
from pathlib import Path

# Hermeticity floor BEFORE the #244 isolation guard import: on a developer
# box with a real default store the guard fails closed when no scratch pin
# is ambient (CI has no ~/.zmem so it is unaffected). Pin a module-lifetime
# scratch ZMEM_DATA so the guard's refusal can never mask a leg's own
# assertion signature; per-test subprocess pins below are stricter still.
if not (os.environ.get("ZMEM_STORE") or os.environ.get("ZMEM_DATA")):
    _GUARD_SCRATCH = tempfile.mkdtemp(prefix="zmem-backfill-guard-")
    os.environ["ZMEM_DATA"] = _GUARD_SCRATCH
import _store_guard  # noqa: F401  (issue #244 isolation guard)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
IMPORT_STORE_PY = SCRIPTS / "import-store.py"
PYTHON = sys.executable

# Ambient env keys scrubbed from EVERY subprocess here so a developer shell
# cannot bleed a real store, an embed profile, or a dedup threshold into a
# hermetic leg (the fake-profile collision seam is deliberately NOT used for
# the CLI add legs: in degraded mode the add path dedups ONLY through the
# content_norm exact window, which is exactly the surface under test).
_AMBIENT_PINS = (
    "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_EMBED_PROFILE", "ZMEM_DEDUP_THRESHOLD", "ZMEM_UPDATE_FOLD_THRESHOLD",
    "ZMEM_REMOTE_URL", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_INJECT",
    "ZMEM_MODELS_DIR", "ZMEM_CAPTURE_MODE",
)


def _store_env(store: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_PINS}
    store = Path(store)
    env.update({
        "ZMEM_STORE": str(store),
        "ZMEM_DATA": str(store.parent),
        "ZMEM_MODELS_DIR": str(store.parent / "models-absent"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_INJECT": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    env.update(extra)
    return env


def _run_store(args, store: Path, timeout: int = 300):
    return subprocess.run(
        [PYTHON, "-X", "utf8", str(STORE_PY)] + [str(a) for a in args],
        capture_output=True, text=True, env=_store_env(store),
        cwd=str(REPO_ROOT), timeout=timeout)


def _json_result(stdout: str):
    """The last JSON object line store.py --json printed, else None."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _q(store: Path, sql: str, args=()):
    conn = sqlite3.connect(f"file:{Path(store).as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _row_dump(store: Path, row_id: str):
    conn = sqlite3.connect(f"file:{Path(store).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM memory WHERE id=?", (row_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _build_source_store(tmp: Path, ns: str, content: str, tag: str):
    """One store.py add -> (store_path, keeper_id). Writer-populated norm."""
    store = tmp / "a" / "store.sqlite"
    store.parent.mkdir(parents=True, exist_ok=True)
    args = ["add", "--namespace", ns, "--type", "fact", "--signal", "test",
            "--content", content, "--json"]
    if tag:
        args += ["--tags", tag]
    r = _run_store(args, store)
    assert r.returncode == 0, (r.stdout, r.stderr)
    payload = _json_result(r.stdout)
    assert payload and payload.get("ok"), r.stdout
    return store, payload["id"]


def _dataset_roundtrip(tmp: Path, ns: str, content: str, tag: str = ""):
    """export-dataset -> import-dataset -> the isolated snapshot store."""
    src, keeper_id = _build_source_store(tmp, ns, content, tag)
    ds = tmp / "ds"
    r = _run_store(["export-dataset", str(ds), "--namespace", ns], src)
    assert r.returncode == 0, (r.stdout, r.stderr)
    manifest = json.loads(
        (ds / "manifest.json").read_text(encoding="utf-8"))
    rev = (manifest.get("source_snapshot_hash")
           or manifest.get("revision") or manifest.get("source_revision"))
    assert rev, manifest
    dest = tmp / "b"
    r = _run_store(["import-dataset", str(ds), "--revision", rev,
                    "--dest", str(dest)], src)
    assert r.returncode == 0, (r.stdout, r.stderr)
    snap = dest / "snapshot.sqlite"
    assert snap.exists(), r.stdout + r.stderr
    return src, snap, keeper_id


_FLEET_RESTORE_CODE = (
    "import sys, sqlite3\n"
    "sys.path.insert(0, r'%(scripts)s')\n"
    "from fleet.snapshot import _SNAPSHOT_COLUMNS, _build_from_payload\n"
    "src = sqlite3.connect(r'file:%(src)s?mode=ro', uri=True)\n"
    "sel = 'SELECT ' + _SNAPSHOT_COLUMNS + ' FROM memory WHERE superseded_at IS NULL'\n"
    "rows = [list(t) for t in src.execute(sel).fetchall()]\n"
    "payload = {'rows': rows, 'payload_bytes': 1, 'snapshot_revision': 1}\n"
    "out = _build_from_payload(payload, {'ZMEM_STORE': r'%(out)s'})\n"
    "print('RESTORED', out)\n"
)


def _fleet_restore(tmp: Path, src_store: Path):
    """_build_from_payload materializes the broker pair under
    <parent-of-ZMEM_STORE>/broker/snapshot.sqlite (verified against
    fleet/snapshot.py:_data_dir -> host.resolve_store_path().parent)."""
    requested = tmp / "fleet" / "snapshot.sqlite"
    requested.parent.mkdir(parents=True, exist_ok=True)
    code = _FLEET_RESTORE_CODE % {
        "scripts": str(SCRIPTS), "src": Path(src_store).as_posix(),
        "out": str(requested)}
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_PINS}
    env.update({"ZMEM_INJECT": "0", "PYTHONIOENCODING": "utf-8"})
    r = subprocess.run([PYTHON, "-X", "utf8", "-c", code],
                       capture_output=True, text=True, env=env,
                       cwd=str(REPO_ROOT), timeout=300)
    broker_snap = requested.parent / "broker" / "snapshot.sqlite"
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert broker_snap.exists(), (
        f"fleet payload restore produced no store at {broker_snap}; "
        f"stdout={r.stdout[-300:]!r} stderr={r.stderr[-300:]!r}")
    return broker_snap


def _equal_add(store: Path, ns: str, content: str):
    return _run_store(
        ["add", "--namespace", ns, "--type", "fact", "--signal", "test",
         "--content", content, "--json"], store)


def _run_import_store(source: Path, dest_dir: Path):
    """import-store.py --source <source> --dest-dir <dest_dir> --force as a
    subprocess with a scrubbed env (never ambient pins)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_PINS}
    env.update({"ZMEM_INJECT": "0", "PYTHONIOENCODING": "utf-8"})
    return subprocess.run(
        [PYTHON, "-X", "utf8", str(IMPORT_STORE_PY),
         "--source", str(source), "--dest-dir", str(dest_dir), "--force"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        timeout=600)


def _assert_fold_contract(testcase, store, ns, lane):
    """The AC1 observable summary, run against `store` after an equal add.

    The FIRST failing assertion at the pre-repair base is the live-row
    count, raised via ``fail()`` so the failure line starts EXACTLY with
    ``AssertionError: added=1 duplicate row`` (assertTrue would prefix
    "False is not true : ") -- the base failure signature is stable across
    the three lanes.
    """
    live = _q(store,
              "SELECT id, content_norm, retrieval_count FROM memory "
              "WHERE namespace=? AND superseded_at IS NULL", (ns,))
    if len(live) != 1:
        testcase.fail(
            f"added=1 duplicate row ({lane}): the normalized-equal add "
            f"created a second live row instead of folding onto the "
            f"materialized row; live rows: "
            f"{[(r[0][:8], 'norm_null' if r[1] is None else 'norm_set', r[2]) for r in live]}")
    keeper_id, keeper_norm, keeper_rc = live[0]
    testcase.assertIsNotNone(
        keeper_norm,
        f"({lane}) the live row's content_norm is NULL after the equal add "
        "-- the materialization writer (or the open-time backfill the add "
        "triggers) must populate it")
    testcase.assertNotEqual(keeper_norm, "",
                            f"({lane}) content_norm must not be empty string")
    return keeper_id, keeper_norm, keeper_rc


class DatasetNullNormFoldTest(unittest.TestCase):
    """AC1: dataset export->import leaves live rows with content_norm NULL
    (MEMORY_COLUMNS omits the column and the INSERT runs after migrate()),
    so a normalized-equal add duplicates instead of folding."""

    def test_null_norm_imported_row_folds_equal_add(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-489ds-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        ns = "project:cnb-ac1"
        content = "ac1 dataset lane keeper alpha bravo charlie"
        src, snap, _keeper = _dataset_roundtrip(tmp, ns, content)
        r = _equal_add(snap, ns, content)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        keeper_id, keeper_norm, keeper_rc = _assert_fold_contract(
            self, snap, ns, "dataset import lane")
        payload = _json_result(r.stdout) or {}
        self.assertIn(
            payload.get("result"), ("deduped",),
            f"(dataset import lane) the equal add must report result "
            f"deduped, got {payload.get('result')!r}; stdout tail: "
            f"{r.stdout.strip()[-300:]!r}")
        self.assertIn("dedup:", r.stdout + r.stderr,
                      "(dataset import lane) human-readable dedup line")
        self.assertGreaterEqual(
            keeper_rc, 1,
            "(dataset import lane) the keeper's retrieval_count must be "
            "incremented by the fold")


class FleetSnapshotNullNormFoldTest(unittest.TestCase):
    """AC2: the fleet-snapshot payload restore (_SNAPSHOT_COLUMNS omits
    content_norm; the INSERT runs after init_db+migrate) leaves live rows
    NULL-norm; an equal add must fold per AC1's observable summary."""

    def test_null_norm_restored_row_folds_equal_add(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-489fleet-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        ns = "project:cnb-ac2"
        content = "ac2 fleet lane keeper delta echo foxtrot"
        src, keeper_id = _build_source_store(tmp, ns, content, "")
        broker_snap = _fleet_restore(tmp, src)
        self.assertTrue(_q(broker_snap,
                           "SELECT 1 FROM memory WHERE id=? "
                           "AND superseded_at IS NULL", (keeper_id,)),
                        "fleet-restored store must carry the live keeper row")
        r = _equal_add(broker_snap, ns, content)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        _k_id, _k_norm, _k_rc = _assert_fold_contract(
            self, broker_snap, ns, "fleet snapshot restore lane")
        payload = _json_result(r.stdout) or {}
        self.assertIn(
            payload.get("result"), ("deduped",),
            f"(fleet snapshot restore lane) the equal add must report "
            f"result deduped, got {payload.get('result')!r}; stdout tail: "
            f"{r.stdout.strip()[-300:]!r}")
        live = _q(broker_snap,
                  "SELECT retrieval_count FROM memory WHERE superseded_at "
                  "IS NULL AND namespace=?", (ns,))
        self.assertGreaterEqual(
            live[0][0], 1,
            "(fleet snapshot restore lane) the keeper's retrieval_count "
            "must be incremented by the fold")


class ImportStoreNullNormFoldTest(unittest.TestCase):
    """AC3: import-store.py copies rows verbatim (its content_norm recompute
    covers only redaction-changed rows), so a NULL-norm source stays NULL
    through the copy and an equal add duplicates."""

    def test_null_norm_copied_row_folds_equal_add(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-489imp-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        ns = "project:cnb-ac3"
        content = "ac3 import-store lane keeper golf hotel india"
        # NULL-norm source via dataset import (leg B pattern), then the
        # content_norm is PINNED back to NULL with a direct sqlite UPDATE:
        # the pin keeps the AC3 lane discriminating at the FIX HEAD under a
        # per-writer repair (where dataset import itself would already
        # populate the norm) -- the lane must bind import-store's OWN
        # recompute-or-backfill behavior, not the dataset writer's.
        src, snap, keeper_id = _dataset_roundtrip(tmp, ns, content)
        conn = sqlite3.connect(str(snap))
        conn.execute("UPDATE memory SET content_norm=NULL WHERE id=?",
                     (keeper_id,))
        conn.commit()
        conn.close()
        self.assertIsNone(
            _q(snap, "SELECT content_norm FROM memory WHERE id=?",
               (keeper_id,))[0][0],
            "precondition: the source row's content_norm is pinned NULL")
        dest_dir = tmp / "d" / "zmem"
        dest_dir.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_PINS}
        env.update({"ZMEM_INJECT": "0", "PYTHONIOENCODING": "utf-8"})
        r = subprocess.run(
            [PYTHON, "-X", "utf8", str(IMPORT_STORE_PY),
             "--source", str(snap), "--dest-dir", str(dest_dir), "--force"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=600)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        copied = dest_dir / "store.sqlite"
        self.assertTrue(copied.exists(),
                        f"import-store produced no store at {copied}: "
                        f"{r.stdout[-300:]!r} {r.stderr[-300:]!r}")
        self.assertTrue(
            _q(copied, "SELECT 1 FROM memory WHERE id=? "
            "AND superseded_at IS NULL", (keeper_id,)),
            "the copied store must carry the live source row")
        r = _equal_add(copied, ns, content)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        _k_id, _k_norm, _k_rc = _assert_fold_contract(
            self, copied, ns, "import-store lane")
        payload = _json_result(r.stdout) or {}
        self.assertIn(
            payload.get("result"), ("deduped",),
            f"(import-store lane) the equal add must report result deduped, "
            f"got {payload.get('result')!r}; stdout tail: "
            f"{r.stdout.strip()[-300:]!r}")

    def test_source_lacking_content_norm_column_survives_import(self):
        # Critic-round supplementary leg, round-2 STRENGTHENED to the
        # head-side regression: a source store whose memory table GENUINELY
        # LACKS the content_norm column (a legacy pre-v8 shape; built by
        # dropping the index that references the column and then
        # ALTER TABLE ... DROP COLUMN -- sqlite >= 3.35). The no-crash
        # contract below holds at base; the (a)/(b) assertions are the
        # repair contract (round-2: a no-crash-only leg would also pass an
        # implementation that backfills only when the column already
        # exists). At the frozen base this leg is RED on (a) -- the
        # destination is a verbatim schema copy and also lacks the column
        # -- which is the documented base behavior; the CLASS-level first
        # failure stays the frozen lane above (this method sorts after
        # test_null_norm_copied_row_folds_equal_add).
        tmp = Path(tempfile.mkdtemp(prefix="zmem-489imp2-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        ns = "project:cnb-ac3b"
        content = "ac3b column-absent lane keeper november oscar papa"
        src, snap, keeper_id = _dataset_roundtrip(tmp, ns, content)
        conn = sqlite3.connect(str(snap))
        conn.execute("DROP INDEX IF EXISTS idx_memory_content_norm")
        try:
            conn.execute("ALTER TABLE memory DROP COLUMN content_norm")
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.close()
            self.skipTest(
                f"this sqlite build cannot DROP COLUMN ({exc}); the legacy "
                "shape cannot be built here")
        cols = [row[1] for row in conn.execute("PRAGMA table_info(memory)")]
        conn.close()
        self.assertNotIn("content_norm", cols,
                         "precondition: the source column is verifiably "
                         "absent")
        dest_dir = tmp / "d" / "zmem"
        r = _run_import_store(snap, dest_dir)
        combined = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(
            r.returncode, 0,
            f"import-store must not crash on a column-absent source; "
            f"rc={r.returncode}, output tail: {combined[-400:]!r}")
        self.assertNotIn(
            "OperationalError", combined,
            "import-store surfaced an unhandled OperationalError on the "
            f"column-absent source; output tail: {combined[-400:]!r}")
        self.assertNotIn("no such column", combined.lower(),
                         "import-store hit an unhandled 'no such column' "
                         f"error; output tail: {combined[-400:]!r}")
        copied = dest_dir / "store.sqlite"
        self.assertTrue(copied.exists(), combined[-300:])
        live = _q(copied, "SELECT id FROM memory WHERE id=? "
                  "AND superseded_at IS NULL", (keeper_id,))
        self.assertEqual(live, [(keeper_id,)],
                         "the copied store must carry the live source row")
        # (a) head-side regression: the destination memory table HAS the
        # column (the repair adds it before backfilling; base leaves the
        # verbatim schema copy without it).
        dest_cols = [row[1] for row in _q(copied,
                                          "PRAGMA table_info(memory)")]
        self.assertIn(
            "content_norm", dest_cols,
            "import-store left the destination memory table WITHOUT the "
            "content_norm column (verbatim schema copy of the "
            "column-absent source) -- the repair must add the column "
            "before backfilling (critic round 2)")
        # (b) the copied live row's norm equals the canonical normalizer
        # output for its content.
        row = _row_dump(copied, keeper_id)
        self.assertEqual(
            row.get("content_norm"),
            _canonical_norm(content, snap),
            "the copied row's content_norm must equal the canonical "
            "normalizer output for its content once the column is added "
            "(critic round 2)")


# --- AC4: structural writer-uniformity check ---------------------------------
#
# Mechanical rule (kept deliberately simple so the fix head can satisfy it
# under EITHER accepted repair shape):
#
#   per-writer predicate -- a writer module is "covered" when its own code
#   populates content_norm for the rows it materializes:
#     * dataset.py: _normalize_content referenced in the module, OR
#       content_norm inside the stored-columns definition it INSERTs
#       (MEMORY_COLUMNS / _MEMORY_STORE_COLUMNS);
#     * fleet/snapshot.py: _normalize_content referenced, OR content_norm
#       present in non-comment code (e.g. added to _SNAPSHOT_COLUMNS or a
#       post-INSERT UPDATE);
#     * import-store.py: a NULL-targeted backfill (content_norm IS NULL in
#       non-comment code), OR a second _normalize_content CALL site beyond
#       the single redaction-gated one shipped at base, OR an explicit
#       backfill choke-point call.
#
#   choke-point alternative -- storelib/schema.py exposes a bounded
#   backfill-on-open (a def whose name contains 'backfill' operating on NULL
#   content_norm at connect/open time) AND each writer module references
#   that function name. A choke point counts for a writer ONLY when the
#   writer's own module wires through it (dataset import and snapshot
#   restore build their stores fresh via init_db+migrate in their own code,
#   so an unwired choke point would never run on those routes).

_WRITER_PATHS = (
    "skills/memory/scripts/storelib/dataset.py",
    "skills/memory/scripts/fleet/snapshot.py",
    "skills/memory/scripts/import-store.py",
)
_SCHEMA_PATH = "skills/memory/scripts/storelib/schema.py"


def _code_text(src: str) -> str:
    """Source with full-line comments stripped (code-context only)."""
    return "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith("#"))


def _dataset_writer_repaired(src: str) -> bool:
    if "_normalize_content" in _code_text(src):
        return True
    m = re.search(r"_MEMORY_STORE_COLUMNS\s*=[^\n]*", src)
    if m and "content_norm" in m.group(0):
        return True
    m = re.search(r"\bMEMORY_COLUMNS\s*=\s*\([^)]*\)", src, re.S)
    return bool(m and "content_norm" in m.group(0))


def _snapshot_writer_repaired(src: str) -> bool:
    # base snapshot.py carries NEITHER string anywhere, so any reference in
    # non-comment code (a _SNAPSHOT_COLUMNS extension, a post-INSERT
    # UPDATE, an _normalize_content call) marks the repair.
    code = _code_text(src)
    return "_normalize_content" in code or "content_norm" in code


def _import_store_writer_repaired(src: str) -> bool:
    code = _code_text(src)
    if re.search(r"content_norm\s+IS\s+NULL", code):
        return True
    if code.count("_normalize_content(") >= 2:
        # base ships exactly ONE call site (the redaction-gated branch)
        return True
    if re.search(r"\b\w*backfill\w*\s*\(", code):
        return True
    return False


_PER_WRITER_PREDICATES = {
    _WRITER_PATHS[0]: _dataset_writer_repaired,
    _WRITER_PATHS[1]: _snapshot_writer_repaired,
    _WRITER_PATHS[2]: _import_store_writer_repaired,
}


class WriterUniformityTest(unittest.TestCase):
    """AC4 (NEW-SURFACE): the repair must cover all three writers -- the
    defect shipped as three independent omissions, and a partial repair
    silently leaves one store class inert."""

    def test_all_three_writers_populate_content_norm(self):
        sources = {rel: (REPO_ROOT / rel).read_text(encoding="utf-8")
                   for rel in _WRITER_PATHS}
        schema_src = (REPO_ROOT / _SCHEMA_PATH).read_text(encoding="utf-8")
        choke_names = re.findall(r"def (\w*backfill\w*)\s*\(", schema_src)
        uncovered = []
        for rel in _WRITER_PATHS:
            covered = _PER_WRITER_PREDICATES[rel](sources[rel])
            if not covered and choke_names:
                # the choke point counts only when THIS writer wires it
                wired = any(re.search(rf"\b{re.escape(name)}\b",
                                      _code_text(sources[rel]))
                            for name in choke_names)
                covered = wired
            if not covered:
                uncovered.append(rel)
        self.assertTrue(
            not uncovered,
            f"uncovered writer: {', '.join(uncovered)} -- content_norm is "
            "not populated (nor backfilled via a schema choke point this "
            "writer references); accepted shapes: per-writer population "
            "near each materialization INSERT (_normalize_content reference "
            "or content_norm in the stored columns / a NULL-targeted "
            "backfill for import-store copies), OR one bounded "
            "backfill-on-open in storelib/schema.py (a def named *backfill* "
            "operating on NULL content_norm) referenced by every writer "
            "module; schema choke-point defs found: "
            f"{choke_names or 'none'}")

    def test_promote_store_dry_run_parity_with_knowledge_pointer_dest(self):
        # Critic-round supplementary leg (mine.py:2221-2227, the design
        # tension the localization log flagged): the promote-store DRY-RUN
        # fold prediction window lacks the knowledge-ref exclusion every
        # real landing lane carries. Shape: the destination's ONLY
        # normalized-equal match for the source state row is a knowledge
        # POINTER row (source_ref='knowledge:%'), so the real promote
        # lands the row under its OWN id and copies its expiry record,
        # while the dry-run window (no exclusion) predicts a fold and
        # projects the expiry record as an orphan skip -- a misprediction.
        # Executed base reference: dry-run summary
        # "memory_expiry_orphan_skipped=1, would_promote=1" vs real promote
        # "added=1, memory_expiry=1" with the knowledge row untouched.
        tmp = Path(tempfile.mkdtemp(prefix="zmem-489ac4c-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        ns = "project:cnb-ac4c"
        content = "ac4c dry-run parity keeper delta echo foxtrot"
        # destination: one live norm-equal row, made a knowledge pointer
        dest = tmp / "dest" / "store.sqlite"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _r = _run_store(
            ["add", "--namespace", ns, "--type", "fact", "--signal",
             "test", "--content", content, "--json"], dest)
        self.assertEqual(_r.returncode, 0, (_r.stdout, _r.stderr))
        kp_id = (_json_result(_r.stdout) or {}).get("id")
        self.assertTrue(kp_id, _r.stdout)
        conn = sqlite3.connect(str(dest))
        conn.execute("UPDATE memory SET source_ref=? WHERE id=?",
                     ("knowledge:ac4c:repo/run-1", kp_id))
        conn.commit()
        conn.close()
        # source: one live STATE row (own id, normalized-equal content)
        # carrying an expiry record -- the expiry projection is the
        # load-bearing observable of the dry-run fold prediction.
        src_store = tmp / "src" / "store.sqlite"
        src_store.parent.mkdir(parents=True, exist_ok=True)
        _r = _run_store(
            ["add", "--namespace", ns, "--type", "fact", "--signal",
             "test", "--content", content, "--json"], src_store)
        self.assertEqual(_r.returncode, 0, (_r.stdout, _r.stderr))
        state_id = (_json_result(_r.stdout) or {}).get("id")
        self.assertTrue(state_id, _r.stdout)
        conn = sqlite3.connect(str(src_store))
        conn.execute("UPDATE memory SET type='state' WHERE id=?",
                     (state_id,))
        conn.execute(
            "INSERT INTO memory_expiry (memory_id, inject_until, "
            "expiry_class) VALUES (?,?,?)",
            (state_id, "2026-12-01T00:00:00Z", "session"))
        conn.commit()
        conn.close()
        # (1) the dry-run prediction: the row WOULD land under its own id
        # (would_promote, expiry record projected as a would-copy), never
        # a fold-projected orphan skip.
        r = _run_store(["promote-store", "--from", str(src_store),
                        "--dry-run"], dest)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        summary = ""
        for line in (r.stdout or "").splitlines():
            if "promote-store (dry-run)" in line:
                summary = line.strip()
                break
        self.assertTrue(summary, f"no dry-run summary line; out={r.stdout!r}")
        self.assertIn("would_promote=1", summary)
        self.assertIn(
            "memory_expiry=1", summary,
            "promote-store dry-run fold prediction mispredicts on a "
            "knowledge-pointer destination: the only normalized-equal "
            "match carries source_ref='knowledge:%', which every real "
            "landing lane excludes, so the row lands under its own id "
            "and its expiry record projects as a would-copy -- but the "
            f"dry-run window (no knowledge-ref exclusion) predicted a "
            f"fold and projected an orphan skip; summary: {summary}")
        self.assertNotIn("memory_expiry_orphan_skipped=1", summary,
                         f"the fold-predicted orphan-skip projection is "
                         f"the misprediction; summary: {summary}")
        # (2) parity ground truth: the REAL promote on a pristine copy of
        # the destination lands the row under its own id and copies the
        # expiry record; the knowledge row never receives the merge.
        dest2 = tmp / "dest2" / "store.sqlite"
        dest2.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dest, dest2)
        r = _run_store(["promote-store", "--from", str(src_store)], dest2)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        self.assertTrue(
            _q(dest2, "SELECT 1 FROM memory WHERE id=? AND type='state' "
            "AND superseded_at IS NULL", (state_id,)),
            "the real promote lands the state row under its own id")
        kp = _q(dest2, "SELECT retrieval_count FROM memory WHERE id=?",
                (kp_id,))
        self.assertEqual(kp, [(0,)],
                         "the knowledge-pointer row must never receive "
                         "the merge (retrieval_count unchanged)")
        self.assertTrue(
            _q(dest2, "SELECT 1 FROM memory_expiry WHERE memory_id=?",
               (state_id,)),
            "the real promote copies the expiry record (the would-copy "
            "projection the dry run must match)")


# --- AC5: preserving row-dump comparison -------------------------------------


def _canonical_norm(content: str, env_store: Path) -> str:
    """The expected post-repair content_norm: the PRODUCTION normalizer's
    output for `content`. Computed in a pinned subprocess importing
    ``_normalize_content`` from storelib.schema exactly the way production
    writers do (e.g. import-store.py's ``from storelib.schema import
    _normalize_content``) -- storelib is never imported in-process by this
    file (isolation rule); env pins come from `_store_env(env_store)`."""
    code = ("import sys\n"
            "sys.path.insert(0, r'%s')\n"
            "from storelib.schema import _normalize_content\n"
            "print(_normalize_content("
            "sys.stdin.readline().rstrip('\\n')))" % str(SCRIPTS))
    r = subprocess.run([PYTHON, "-X", "utf8", "-c", code],
                       input=content + "\n", capture_output=True, text=True,
                       env=_store_env(env_store), cwd=str(REPO_ROOT),
                       timeout=120)
    assert r.returncode == 0, (r.stdout, r.stderr)
    return r.stdout.strip()


def _assert_preserved(testcase, src_row, dst_row, lane, columns=None,
                      env_store=None):
    testcase.assertIsNotNone(src_row, f"({lane}) source row missing")
    testcase.assertIsNotNone(dst_row, f"({lane}) materialized row missing")
    cols = columns if columns is not None else sorted(src_row)
    for col in cols:
        if col == "content_norm":
            continue
        testcase.assertEqual(
            dst_row.get(col), src_row.get(col),
            f"({lane}) column {col!r} changed across materialization -- "
            f"only content_norm may differ (AC5); source={src_row.get(col)!r} "
            f"materialized={dst_row.get(col)!r}")
    # content_norm is the ONE column excluded above. Critic-round fix: a
    # source row whose norm is NULL that is CORRECTLY repaired to the
    # canonical normalized value must PASS -- the old rule (NULL or
    # byte-equal) rejected exactly that repair. Rule now:
    #   * source norm non-NULL: the materialized side may be NULL (the
    #     pre-repair materialization, green at base) or byte-equal;
    #   * source norm NULL: the materialized side may be NULL (base) or
    #     equal the canonical normalizer output for the row's content
    #     (the head contract; the normalizer subprocess runs only when
    #     the column is actually populated, so base legs pay no cost).
    s_norm, d_norm = src_row.get("content_norm"), dst_row.get("content_norm")
    if s_norm is None:
        ok = d_norm is None
        if not ok:
            testcase.assertIsNotNone(
                env_store,
                f"({lane}) env_store is required to check a populated "
                "materialized norm against the canonical normalizer")
            ok = d_norm == _canonical_norm(
                src_row.get("content") or "", env_store)
        testcase.assertTrue(
            ok,
            f"({lane}) content_norm: source NULL -> materialized "
            f"{d_norm!r}; only NULL or the canonical normalizer output "
            "for the row's content is sanctioned by AC5")
    else:
        testcase.assertTrue(
            d_norm is None or d_norm == s_norm,
            f"({lane}) content_norm on the materialized row is {d_norm!r} "
            f"(source {s_norm!r}); only NULL or byte-equal is sanctioned "
            "by AC5")


def _snapshot_columns_from_source() -> list:
    """The payload column list parsed from fleet/snapshot.py's
    _SNAPSHOT_COLUMNS tuple literal (the columns the restore carries)."""
    src = (SCRIPTS / "fleet" / "snapshot.py").read_text(encoding="utf-8")
    m = re.search(r"_SNAPSHOT_COLUMNS\s*=\s*\((.*?)\)", src, re.S)
    assert m, "_SNAPSHOT_COLUMNS tuple not found in fleet/snapshot.py"
    joined = "".join(re.findall(r'"([^"]*)"', m.group(1)))
    return [c.strip() for c in joined.split(",") if c.strip()]


class PreservationTest(unittest.TestCase):
    """AC5 (PRESERVING, GREEN at base AND head): materialization changes
    only the content_norm column; ids, tombstones, and every other column
    stay byte-for-byte identical."""

    def test_dataset_import_preserves_every_column_but_norm(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-489pd-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        ns = "project:cnb-ac5d"
        content = "ac5 dataset preservation keeper quebec romeo sierra"
        src, snap, keeper_id = _dataset_roundtrip(tmp, ns, content,
                                                  tag="preserve:d")
        _assert_preserved(self, _row_dump(src, keeper_id),
                          _row_dump(snap, keeper_id),
                          "dataset import lane", env_store=src)

    def test_fleet_restore_preserves_carried_columns(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-489pf-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        ns = "project:cnb-ac5f"
        content = "ac5 fleet preservation keeper tango uniform victor"
        src, keeper_id = _build_source_store(tmp, ns, content,
                                             tag="preserve:f")
        broker_snap = _fleet_restore(tmp, src)
        carried = _snapshot_columns_from_source()
        _assert_preserved(self, _row_dump(src, keeper_id),
                          _row_dump(broker_snap, keeper_id),
                          "fleet restore lane", columns=carried,
                          env_store=src)

    def test_import_store_copy_preserves_every_column_but_norm(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-489pi-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        ns = "project:cnb-ac5i"
        content = "ac5 import-store preservation keeper whiskey xray yankee"
        src, snap, keeper_id = _dataset_roundtrip(tmp, ns, content,
                                                  tag="preserve:i")
        dest_dir = tmp / "d" / "zmem"
        r = _run_import_store(snap, dest_dir)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        copied = dest_dir / "store.sqlite"
        self.assertTrue(copied.exists(), r.stdout + r.stderr)
        _assert_preserved(self, _row_dump(snap, keeper_id),
                          _row_dump(copied, keeper_id),
                          "import-store lane", env_store=snap)

    def test_null_pinned_import_store_copy_preserves_and_can_repair_norm(self):
        # Critic-round NULL-pinned leg (mirrors AC3's re-pin): the source
        # row's content_norm is pinned NULL before import-store runs. At
        # BASE the copy is verbatim (dest norm NULL -> green); at HEAD a
        # correct repair populates the CANONICAL normalizer output while
        # every other column stays byte-identical (also green).
        tmp = Path(tempfile.mkdtemp(prefix="zmem-489pn-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        ns = "project:cnb-ac5n"
        content = "ac5 null-pinned preservation keeper zulu alpha bravo"
        src, snap, keeper_id = _dataset_roundtrip(tmp, ns, content,
                                                  tag="preserve:n")
        conn = sqlite3.connect(str(snap))
        conn.execute("UPDATE memory SET content_norm=NULL WHERE id=?",
                     (keeper_id,))
        conn.commit()
        conn.close()
        self.assertIsNone(
            _row_dump(snap, keeper_id)["content_norm"],
            "precondition: the source row's content_norm is pinned NULL")
        dest_dir = tmp / "d" / "zmem"
        r = _run_import_store(snap, dest_dir)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        copied = dest_dir / "store.sqlite"
        self.assertTrue(copied.exists(), r.stdout + r.stderr)
        _assert_preserved(self, _row_dump(snap, keeper_id),
                          _row_dump(copied, keeper_id),
                          "import-store NULL-pinned lane", env_store=snap)


# --- AC6: ingest re-rank seam on the NULL-norm store class --------------------
#
# Mirrors tests/test_sync_ingest_exact_rerank.py DirectIngestRowSeamTest
# (the PR #487 sibling guardrail): the store is built via the dataset-import
# NULL path, a collider row is added live, and the detector stub forces the
# collider as top-1. The real re-rank must retarget onto the normalized-
# equal keeper. At the pre-repair base the exact window matches nothing on
# the NULL-norm store, so the fold lands on the COLLIDER with no notice.

SEAM_NS = "project:cnb-ac6"
SEAM_KEEPER = "ac6 seam lane keeper juliet kilo lima"
SEAM_COLLIDER = "ac6 collider mike november oscar papa"
_PURGE_PREFIXES = ("storelib", "embeddings", "embed_profiles",
                   "schema_meta", "fleet")


class IngestNullStoreRerankSeamTest(unittest.TestCase):
    """AC6: the #486 contract end-to-end on the previously-inert store
    class (dataset-import NULL content_norm)."""

    def _fresh_sync(self, store_path: str):
        # Sibling-file isolation discipline (test_sync_ingest_exact_rerank
        # .DirectIngestRowSeamTest._fresh_sync): snapshot env, module cache
        # and sys.path; purge every storelib-bound module; import sync
        # against THIS scratch store; restore everything in cleanup.
        env_before = dict(os.environ)
        mods_before = {}
        for mod in list(sys.modules):
            if any(mod == p or mod.startswith(p + ".")
                   for p in _PURGE_PREFIXES):
                mods_before[mod] = sys.modules[mod]
        path_before = list(sys.path)

        def _restore():
            os.environ.clear()
            os.environ.update(env_before)
            for mod in list(sys.modules):
                if any(mod == p or mod.startswith(p + ".")
                       for p in _PURGE_PREFIXES):
                    del sys.modules[mod]
            sys.modules.update(mods_before)
            sys.path[:] = path_before
        self.addCleanup(_restore)

        store = store_path
        os.environ["ZMEM_STORE"] = store
        os.environ["ZMEM_DATA"] = os.path.dirname(store)
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(
            os.path.dirname(store), "models-absent")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        # NOTE (adaptation vs the sibling seam): the sibling builds its
        # store under ZMEM_EMBED_PROFILE=fake; THIS store is the dataset-
        # import snapshot, whose fresh schema records the 384-dim default,
        # so a fake (16-dim) profile makes every writer refuse with the
        # dimension-mix guard. Degraded (no-embeddings) mode is the probe-
        # proven configuration for these stores, and the detector is
        # stubbed below anyway -- the fold path never embeds.
        for mod in list(sys.modules):
            if any(mod == p or mod.startswith(p + ".")
                   for p in _PURGE_PREFIXES):
                del sys.modules[mod]
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import storelib.sync as sync_mod  # noqa: E404 (post-purge import)
        return sync_mod

    def test_ingest_rerank_folds_onto_keeper_on_null_norm_store(self):
        import contextlib
        import io

        tmp = tempfile.mkdtemp(prefix="zmem-489seam-")
        self.addCleanup(shutil.rmtree, tmp, True)
        # (1) NULL-norm store via the dataset-import path (AC1 lane shape).
        tmp_p = Path(tmp)
        _src, snap, keeper_id = _dataset_roundtrip(
            tmp_p, SEAM_NS, SEAM_KEEPER)
        # (2) a live collider row the detector will rank top-1.
        r = _run_store(
            ["add", "--namespace", SEAM_NS, "--type", "fact",
             "--content", SEAM_COLLIDER, "--signal", "test", "--no-dedup",
             "--json"], snap)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        collider_id = (_json_result(r.stdout) or {}).get("id")
        self.assertTrue(collider_id, r.stdout)
        # (3) drive the REAL ingest lane in-process with the detector stub
        # forcing the collider as top-1 (the sibling seam, unchanged).
        sync_mod = self._fresh_sync(str(snap))
        conn = sqlite3.connect(str(snap))
        conn.row_factory = sqlite3.Row
        obj = {
            "id": str(uuid.uuid4()), "namespace": SEAM_NS, "type": "fact",
            "content": SEAM_KEEPER, "tags": "fold:ac6", "signal": "test",
            "confidence": 0.9, "source_ref": "",
            "ingestion_ts": "2026-11-17T15:00:00Z",
            "valid_from": "2026-11-17T15:00:00Z", "valid_until": "",
        }
        row = sync_mod._validate_sync_row(obj)

        def _stub(conn_, content, ns, dedup_cache=None):
            return {"id": collider_id}, 1.0, None  # colliding top-1

        buf_out, buf_err = io.StringIO(), io.StringIO()
        try:
            with unittest.mock.patch.object(sync_mod, "_detect_duplicate",
                                            _stub), \
                    contextlib.redirect_stdout(buf_out), \
                    contextlib.redirect_stderr(buf_err):
                outcome = sync_mod._ingest_row(
                    conn, dict(row), allow_tombstones=False)
            conn.commit()
            combined = buf_out.getvalue() + buf_err.getvalue()
            notice_present = "dedup re-ranked" in combined
            target = [r_["id"] for r_ in conn.execute(
                "SELECT id, tags FROM memory WHERE namespace=? "
                "AND superseded_at IS NULL", (SEAM_NS,)).fetchall()
                if "fold:ac6" in (r_["tags"] or "")]
            self.assertEqual(outcome, "deduped",
                             "the ingest fold must fire (stubbed detector "
                             f"top-1 at sim 1.000); outcome={outcome!r}")
            self.assertTrue(
                target == [keeper_id],
                f"FOLD TARGET -> COLLIDER (fold landed on "
                f"{[t[:8] for t in target]}, expected the normalized-equal "
                f"keeper {keeper_id[:8]}); on the pre-repair NULL-norm store "
                "the exact window matches nothing and the detector top-1 "
                f"collider receives the merge; notice present: "
                f"{notice_present}")
            self.assertTrue(
                notice_present,
                f"the dedup re-ranked notice must print when the window "
                f"retargets the fold onto the keeper; notice present: "
                f"{notice_present}; FOLD TARGET -> COLLIDER check passed "
                f"(target={[t[:8] for t in target]})")
        finally:
            conn.close()


# --- AC7: the shipped index predicate + bounded window -----------------------

_WINDOW_SQL = (
    "SELECT id, content, tags, type FROM memory "
    "WHERE namespace=? AND content_norm=? "
    "AND superseded_at IS NULL "
    "AND (source_ref IS NULL OR source_ref NOT LIKE 'knowledge:%') "
    "ORDER BY rowid LIMIT 33"
)


def _index_ddls_from_schema() -> list:
    """Every idx_memory_content_norm CREATE statement schema.py ships, with
    adjacent string literals concatenated (both the init_db site ~:1188 and
    the v8 migrate site ~:2539 use the split-literal form)."""
    src = (SCRIPTS / "storelib" / "schema.py").read_text(encoding="utf-8")
    ddls = []
    for m in re.finditer(
            r'"(CREATE INDEX[^"]*idx_memory_content_norm[^"]*)"', src):
        parts = [m.group(1)]
        pos = m.end()
        while True:
            m2 = re.match(r'\s*"([^"]*)"', src[pos:])
            if not m2:
                break
            parts.append(m2.group(1))
            pos += m2.end()
        ddls.append("".join(parts))
    return ddls


class IndexBoundWindowTest(unittest.TestCase):
    """AC7 (NEW-SURFACE): the knowledge-ref exclusion must be served by the
    idx_memory_content_norm partial-index predicate (the shipped DDL), and
    the window query must stay bounded on a planted 25k knowledge-ref
    group."""

    def test_index_predicate_and_window_cost(self):
        ddls = _index_ddls_from_schema()
        self.assertTrue(
            ddls,
            "no CREATE INDEX ... idx_memory_content_norm statement found in "
            "skills/memory/scripts/storelib/schema.py")
        # Structural half (deterministic; fails FIRST at base so the base
        # signature never depends on machine timing): EVERY shipped DDL for
        # this index must carry the knowledge-ref exclusion in its partial-
        # index WHERE predicate.
        lacking = [d for d in ddls
                   if "NOT LIKE" not in d.upper()
                   or "knowledge" not in d.lower()]
        self.assertTrue(
            not lacking,
            "index predicate missing knowledge-ref exclusion: the shipped "
            f"idx_memory_content_norm DDL(s) {lacking} do not carry the "
            "source_ref NOT LIKE 'knowledge:%' exclusion in the partial-"
            "index predicate, so all four window queries (write.py detector "
            "exact lane / add window / update gate window, sync.py ingest "
            "window) post-filter OUTSIDE the index and walk the whole "
            "(namespace, content_norm) group per query")
        # Behavioral half: plant the measured amplification shape (25k
        # knowledge-ref rows sharing one content_norm group) and require a
        # bounded per-query cost under the REAL shipped index shape.
        tmp = Path(tempfile.mkdtemp(prefix="zmem-489idx-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        store = tmp / "store.sqlite"
        conn = sqlite3.connect(str(store))
        conn.executescript("""
        CREATE TABLE memory (
          id TEXT PRIMARY KEY, namespace TEXT, content TEXT, tags TEXT,
          source_ref TEXT, superseded_at TEXT, content_norm TEXT, type TEXT
        );
        """)
        norm = "ac7 planted knowledge group norm"
        rows = [(str(uuid.uuid4()), "project:cnb-ac7", "payload " + str(i),
                 "", "knowledge:plant/%d" % i, None, norm, "fact")
                for i in range(25000)]
        # one ordinary live row in the same group: the window's result
        rows.append((str(uuid.uuid4()), "project:cnb-ac7", "payload ordinary",
                     "", "", None, norm, "fact"))
        conn.executemany(
            "INSERT INTO memory (id, namespace, content, tags, source_ref, "
            "superseded_at, content_norm, type) VALUES (?,?,?,?,?,?,?,?)",
            rows)
        conn.execute(ddls[0])  # the real shipped index shape
        conn.commit()
        plan = [p[3] for p in conn.execute(
            "EXPLAIN QUERY PLAN " + _WINDOW_SQL,
            ("project:cnb-ac7", norm)).fetchall()]
        n_queries = 120
        t0 = time.perf_counter()
        for _ in range(n_queries):
            conn.execute(_WINDOW_SQL,
                         ("project:cnb-ac7", norm)).fetchall()
        avg_ms = (time.perf_counter() - t0) * 1000.0 / n_queries
        conn.close()
        self.assertTrue(
            avg_ms < 1.0,
            f"the window query walks the planted 25k knowledge-ref group: "
            f"measured {avg_ms:.3f} ms per query (threshold: fails when "
            f"ms >= 1.0; pre-change reference ~8.6-30 ms) -- the knowledge-"
            f"ref exclusion is still a post-filter outside the index, not "
            f"index-served; EXPLAIN QUERY PLAN: {plan}")

    def test_migrate_rebuilds_stale_index_with_exclusion(self):
        # Critic-round supplementary leg: a store carrying the OLD index
        # shape (the frozen base DDL below -- exactly what schema.py ships
        # at the base commit) must be upgraded by the PRODUCTION open+
        # migrate path (any store.py command runs the same
        # _prepare_store/init+migrate pair), so already-created stores get
        # the knowledge-ref exclusion too, not only fresh ones. At base
        # no migration exists and the DDL stays stale -> RED with the
        # frozen signature phrase.
        old_ddl = ("CREATE INDEX idx_memory_content_norm "
                   "ON memory(namespace, content_norm) "
                   "WHERE superseded_at IS NULL")
        tmp = Path(tempfile.mkdtemp(prefix="zmem-489mig-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        store = tmp / "store.sqlite"
        r = _run_store(["init"], store)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        # force the legacy index shape (no-op net effect at base; at the
        # fix head init may already create the extended predicate, so the
        # stale shape is planted explicitly to exercise the upgrade path)
        conn = sqlite3.connect(str(store))
        conn.execute("DROP INDEX IF EXISTS idx_memory_content_norm")
        conn.execute(old_ddl)
        conn.commit()
        stale = conn.execute(
            "SELECT sql FROM sqlite_master WHERE "
            "name='idx_memory_content_norm'").fetchone()
        conn.close()
        self.assertEqual(
            (stale[0] or "").strip(), old_ddl,
            f"precondition: the stale index shape is planted verbatim; "
            f"got {stale!r}")
        # the production open+migrate path
        r = _run_store(["stats"], store)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        ddl_rows = _q(store, "SELECT sql FROM sqlite_master WHERE "
                      "name='idx_memory_content_norm'")
        self.assertTrue(
            ddl_rows and ddl_rows[0][0],
            "the idx_memory_content_norm index vanished from sqlite_master "
            "after the production open+migrate path ran")
        ddl = ddl_rows[0][0]
        self.assertTrue(
            "NOT LIKE" in ddl.upper() and "knowledge" in ddl.lower(),
            "index predicate missing knowledge-ref exclusion: opening a "
            "store that carries the stale (pre-fix) idx_memory_content_norm "
            "shape via the production migrate path did NOT rebuild the "
            "index with the source_ref NOT LIKE 'knowledge:%' exclusion "
            f"(sqlite_master DDL after store.py stats: {ddl!r}) -- every "
            "already-created store keeps post-filtering outside the index")
        # Round-2 addition (post-migration plan proof): the rebuilt index
        # must actually SERVE the real window query (the sync.py shape).
        # Unreachable at base -- the DDL assertion above fails first with
        # the frozen phrase -- so this is head-side evidence only.
        # Planner variance (CI-round lesson): SQLite builds differ on (a)
        # which index the unhinted planner prefers on this small fixture
        # (idx_memory_namespace on CI Linux) and (b) whether INDEXED BY on
        # a partial index with an OR-term predicate yields a plan at all
        # ("no query solution" on CI Linux). Accept a three-mode proof and
        # record which mode fired in the failure path: (1) INDEXED BY plan
        # names the index (strongest: the planner CAN force it); (2) the
        # unhinted plan names the index; (3) functional mode -- a planted
        # window-matching row round-trips through the real window query on
        # the migrated store. The DDL assertion above remains the primary
        # discrimination (the frozen phrase).
        proof_mode = None
        forced = _WINDOW_SQL.replace(
            "FROM memory ", "FROM memory INDEXED BY idx_memory_content_norm ")
        try:
            plan = [p[3] for p in _q(
                store, "EXPLAIN QUERY PLAN " + forced,
                ("project:cnb-ac7m", "nonexistent-norm-x"))]
            if any("idx_memory_content_norm" in line for line in plan):
                proof_mode = "INDEXED BY plan"
        except sqlite3.OperationalError:
            plan = []
        if proof_mode is None:
            plan2 = [p[3] for p in _q(
                store, "EXPLAIN QUERY PLAN " + _WINDOW_SQL,
                ("project:cnb-ac7m", "nonexistent-norm-x"))]
            if any("idx_memory_content_norm" in line for line in plan2):
                proof_mode = "unhinted plan"
            else:
                # functional mode: plant a live row the window must match
                # (ingestion_ts is NOT NULL without a default)
                wconn = sqlite3.connect(str(store))
                try:
                    wconn.execute(
                        "INSERT INTO memory (id, namespace, type, content, "
                        "tags, source_ref, content_norm, superseded_at, "
                        "ingestion_ts) "
                        "VALUES ('cnb-ac7m-proof', 'project:cnb-ac7m', "
                        "'fact', 'proof row', '', 'src:x', "
                        "'nonexistent-norm-x', NULL, '2026-11-18T00:00:00Z')")
                    wconn.commit()
                    hit = wconn.execute(
                        _WINDOW_SQL,
                        ("project:cnb-ac7m", "nonexistent-norm-x")).fetchall()
                finally:
                    wconn.close()
                self.assertTrue(
                    hit and hit[0][0] == "cnb-ac7m-proof",
                    f"functional mode failed: the migrated store's window "
                    f"query did not return the planted row (got {hit!r}); "
                    f"INDEXED BY plan: {plan!r}; unhinted plan: {plan2!r}")
                proof_mode = "functional (row round-trip)"
        # If we reach here a proof mode fired; keep an assertion on the
        # mode so a silent no-op cannot pass.
        self.assertIn(proof_mode, (
            "INDEXED BY plan", "unhinted plan",
            "functional (row round-trip)"))

    def test_migrate_failure_leaves_store_in_pre_migration_state(self):
        # Round-2 rollback-coverage leg, round-3 CORRECTED proxy
        # semantics: the proxy ALLOWS the DROP INDEX through (executes it)
        # and raises ONLY on the subsequent CREATE INDEX naming
        # idx_memory_content_norm (and on any executescript that would
        # CREATE it -- same allow-DROP / fail-CREATE carve-out). Under
        # the old reject-any-DDL semantics the injection always fired at
        # the DROP, so a deliberately NON-ATOMIC DROP/commit/CREATE
        # implementation passed (the critic's probe shape); now the DROP
        # runs, the CREATE fails, and the pre-migration-state assertions
        # become the real atomicity proof -- a committed DROP with a
        # failed CREATE leaves the index ABSENT, which the exactly-one-
        # old-index assertion kills. At BASE migrate() no-ops on a
        # current-version store, the injection never fires (the probe
        # prints INJECTION_SILENT -- documented) and the invariant holds
        # trivially -> GREEN. At HEAD the rebuild path exists (the
        # sibling migrate leg proves it), the probe must record
        # FAILED_STAGE=CREATE (the DROP was allowed through), and the
        # invariant proves the rebuild is atomic.
        old_ddl = ("CREATE INDEX idx_memory_content_norm "
                   "ON memory(namespace, content_norm) "
                   "WHERE superseded_at IS NULL")
        tmp = Path(tempfile.mkdtemp(prefix="zmem-489rb-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        store = tmp / "store.sqlite"
        r = _run_store(["init"], store)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        _r = _run_store(
            ["add", "--namespace", "project:cnb-ac7r", "--type", "fact",
             "--signal", "test", "--content",
             "ac7 rollback lane keeper alpha bravo", "--json"], store)
        self.assertEqual(_r.returncode, 0, (_r.stdout, _r.stderr))
        keeper_id = (_json_result(_r.stdout) or {}).get("id")
        conn = sqlite3.connect(str(store))
        conn.execute("DROP INDEX IF EXISTS idx_memory_content_norm")
        conn.execute(old_ddl)
        conn.commit()
        tables_before = sorted(
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"))
        conn.close()
        # the failure-injection probe (in-process storelib import lives
        # only inside this subprocess, per the file's isolation rule).
        # Proxy discipline: DROP INDEX naming the index PASSES (and is
        # recorded in drop_seen); CREATE INDEX naming it RAISES; an
        # executescript mentioning the index raises only when it also
        # contains a CREATE (a drop-only script passes, same carve-out).
        probe_code = (
            "import sqlite3, sys\n"
            "sys.path.insert(0, r'%s')\n"
            "from storelib.schema import migrate\n"
            "class FailOnIdxCreate:\n"
            "    def __init__(self, real):\n"
            "        object.__setattr__(self, '_real', real)\n"
            "        object.__setattr__(self, 'drop_seen', False)\n"
            "    def execute(self, sql, parameters=()):\n"
            "        s = str(sql).strip().lstrip('(').strip().upper()\n"
            "        if 'IDX_MEMORY_CONTENT_NORM' in s:\n"
            "            if s.startswith('DROP'):\n"
            "                object.__setattr__(self, 'drop_seen', True)\n"
            "                return object.__getattribute__(self, '_real')"
            ".execute(sql, parameters)\n"
            "            if s.startswith('CREATE'):\n"
            "                raise sqlite3.OperationalError("
            "'injected index-CREATE failure')\n"
            "        return object.__getattribute__(self, '_real').execute("
            "sql, parameters)\n"
            "    def executescript(self, sql):\n"
            "        t = str(sql).upper()\n"
            "        if ('IDX_MEMORY_CONTENT_NORM' in t and\n"
            "                'CREATE' in t):\n"
            "            raise sqlite3.OperationalError("
            "'injected index-CREATE failure')\n"
            "        if ('IDX_MEMORY_CONTENT_NORM' in t and\n"
            "                'DROP' in t):\n"
            "            object.__setattr__(self, 'drop_seen', True)\n"
            "        return object.__getattribute__(self, '_real')"
            ".executescript(sql)\n"
            "    def __getattr__(self, name):\n"
            "        return getattr(object.__getattribute__(self, '_real'),"
            " name)\n"
            "conn = sqlite3.connect(r'%s')\n"
            "proxy = FailOnIdxCreate(conn)\n"
            "fired, stage = False, 'NONE'\n"
            "try:\n"
            "    migrate(proxy)\n"
            "except Exception as exc:\n"
            "    fired = ('injected index-CREATE failure' in str(exc))\n"
            "    stage = 'CREATE' if fired else type(exc).__name__\n"
            "try:\n"
            "    conn.rollback()\n"
            "except Exception:\n"
            "    pass\n"
            "conn.close()\n"
            "print('INJECTION_FIRED' if fired else 'INJECTION_SILENT')\n"
            "print('FAILED_STAGE', stage)\n"
            "print('DROP_SEEN', proxy.drop_seen)\n"
            % (str(SCRIPTS), str(store)))
        env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_PINS}
        env.update({"ZMEM_STORE": str(store),
                    "ZMEM_DATA": str(store.parent),
                    "ZMEM_MODELS_DIR": str(store.parent / "models-absent"),
                    "ZMEM_MODEL_AUTODOWNLOAD": "0", "ZMEM_INJECT": "0",
                    "PYTHONIOENCODING": "utf-8"})
        probe = subprocess.run([PYTHON, "-X", "utf8", "-c", probe_code],
                               capture_output=True, text=True, env=env,
                               cwd=str(REPO_ROOT), timeout=300)
        self.assertEqual(probe.returncode, 0,
                         (probe.stdout, probe.stderr))
        lines = (probe.stdout or "").splitlines()
        marker = lines[0] if lines else ""
        stage_line = lines[1] if len(lines) > 1 else ""
        drop_line = lines[2] if len(lines) > 2 else ""
        # BASE: INJECTION_SILENT (migrate no-ops on a current-version
        # store -- documented); HEAD: INJECTION_FIRED with the failure
        # landed on the CREATE stage (the DROP was allowed through).
        self.assertIn(marker, ("INJECTION_FIRED", "INJECTION_SILENT"),
                      f"unexpected probe marker {marker!r}; probe output: "
                      f"{probe.stdout!r} {probe.stderr[-200:]!r}")
        if marker == "INJECTION_FIRED":
            self.assertEqual(
                stage_line, "FAILED_STAGE CREATE",
                "the injected failure must land on the CREATE stage (the "
                "DROP is allowed through by design); a FAILED_STAGE of "
                "DROP means the proxy semantics regressed to rejecting "
                "the first DDL, which cannot distinguish an atomic "
                "rebuild from a non-atomic DROP/commit/CREATE one "
                f"(probe: {probe.stdout!r})")
        idx_rows = _q(store, "SELECT name, sql FROM sqlite_master WHERE "
                      "name='idx_memory_content_norm'")
        self.assertEqual(
            len(idx_rows), 1,
            f"a failed index rebuild must not leave the store without (or "
            f"with duplicated) idx_memory_content_norm -- under the "
            f"corrected proxy the DROP executed, so a missing index here "
            f"is the non-atomic DROP/commit/CREATE shape; sqlite_master "
            f"rows: {idx_rows}; probe: {marker} {stage_line} {drop_line}")
        self.assertEqual(
            (idx_rows[0][1] or "").strip(), old_ddl,
            f"the old index shape must survive a failed rebuild attempt "
            f"(got {idx_rows[0][1]!r}; probe: {marker} {stage_line})")
        tables_after = sorted(
            row[0] for row in _q(
                store, "SELECT name FROM sqlite_master WHERE type='table'"))
        self.assertEqual(tables_after, tables_before,
                         "the table set must be intact after a failed "
                         "rebuild attempt")
        self.assertTrue(
            _q(store, "SELECT 1 FROM memory WHERE id=? "
            "AND superseded_at IS NULL", (keeper_id,)),
            "the seeded row must survive a failed rebuild attempt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
