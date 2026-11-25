"""Issue #504 audit: restore must validate a snapshot's identity and schema
BEFORE replacing the live store, and must preserve the live store when it
fails after staging the replacement.  Both tests are RED at base, GREEN
after the fix.

AC1 -- test_staged_validation (marker ``AC3-504:``).  Every invalid input
below is offered to ``storelib.backup.cmd_restore(from_path=..., force=True)``
IN-PROCESS against a live store holding three control rows, with an
``os.replace`` spy that records (and performs) every replacement whose
destination is the live store path.  Each must be refused BEFORE any
replacement: zero spied replacements, a non-zero int return, no exception
escaping, the destination main file byte-identical, no ``prerestore*``
safety copy taken, the control rows intact, and a later public ``add``
landing.  Invalid inputs: a foreign SQLite database; a wrong-shape
``memory`` table stamped 1; the same stamped as fully current; a file that
is not a SQLite database at all; core-column stores stamped with a
non-integer, ``0``, a negative, and no ``schema_version`` row; a falsely
current store (a real CLI-built store whose ``memory`` table is rebuilt
down to the fourteen v1 core columns while ``meta`` still carries the
supported stamp -- a shape the running client cannot write); a falsely
current side table (a real CLI-built store whose ``memory_expiry`` table
is rebuilt down to the ``memory_id`` column alone, its index recreated on
that column so every table and index name a fresh store has still exists,
stamp still current -- a required table the running client cannot write);
an FTS impostor (``memory_fts`` swapped for a standalone fts5 table with
the same three columns and NO external-content binding -- every name,
column and row count a fresh store has, integrity ok, and the existing
rows silently unindexed); a same-name index with different SQL
(``idx_memory_live`` recreated without its partial-index WHERE clause);
a same-name trigger with different SQL (``memory_ai`` recreated with a
body that does not insert into ``memory_fts``); a tainté impostor
(``memory`` rebuilt with the same columns except ``taint`` renamed to the
unquoted non-ASCII ``tainté`` -- a rename an ASCII-word tokenizer silently
reads as ``taint``); a Kelvin CHECK impostor (``entity`` rebuilt with its
``kind`` CHECK reference in the quoted Kelvin form ``"Kind"`` (U+212A) --
Python's casefold() equates U+212A with ASCII ``k``, SQLite does not, so
every valid entity insert fails); and the two quoted long-s mutations
(``memory`` rebuilt with ``signal`` renamed to the quoted long-s
``"ſignal"`` (U+017F), or with an EXTRA quoted ``"ſignal"`` column -- a
casefold-based token scanner reads ``ſ`` as ``s``, so only the
``PRAGMA table_info`` cross-check can catch either); the memory_au U+00A0
impostor (the genuine update trigger re-created verbatim except for ONE
no-break space (U+00A0) inserted after ``new.content`` -- an identifier
character to SQLite, so the insert/delete round trip passes but every
UPDATE of memory fails); and the U+3000 meta impostor (``meta`` rebuilt
with ``PRIMARY KEY`` written with an ideographic space, so the PRIMARY KEY
vanishes and duplicate meta keys are accepted).  An ACCEPT-with-heal
leg: a two-row store whose definitions are all genuine but whose derived
search index is stale (a rewording applied while the update trigger was
dropped, then the genuine trigger restored; the original rows' first
eligible term is ``sharedtoken``, the reworded row's is ``zurich``) must
be ACCEPTED, its index healed (``MATCH`` finds the reworded row under its
new term and no longer under the removed old one), the control row kept,
and a later public ``add`` landing.  An ACCEPT guard leg: a store whose
``memory`` definition writes every column key in single quotes is
semantically identical to the genuine definition and must be ACCEPTED.
Controls:
a
real backup one version older is accepted and upgraded with its rows
intact; a real backup stamped newer than supported is refused with the
live store untouched.  Genuine legacy fixtures (v1 DDL verbatim from the
initial commit, v2/v3 per ``migrate()``'s blocks, and a store really killed
inside the v3 migration window by a child ``os._exit(137)`` from a
``sqlite3`` trace callback) are accepted, upgraded to the supported
version, carry every v1 core column and every ``sqlite_master`` object a
fresh store has, and accept a later public ``add``.

AC2 -- test_recovery_preserves_later_write (marker ``AC4-504:``).  The
restore policy specified (rollback policy decided 2026-11-23; the earlier
checkpoint-only rollback target these legs encoded was CHECK_WRONG -- the
acceptance criterion never stated it): a failed restore reports failure in
a controlled way (``cmd_restore`` RETURNS a non-zero int,
``restore FAILED: <cause>`` on stderr, then a ``rolled back`` line naming
the existing ``prerestore*`` safety copy -- the store as SQLite read it:
it opens alone, with no sidecars, and shows those rows; the rollback does
not consume it) and puts the live store back to exactly what SQLite read
from it just before the restore -- the main file plus whatever a SQLite
open applies from the ``-wal`` or ``-journal``.  The on-disk files as they
were before the restore (the main file if present, and every present
``-wal``/``-shm``/``-journal``, names unchanged) are kept byte-for-byte in
a ``restore-forensic-*`` directory under the backup directory; each
sidecar taken away prints a line that contains ``removed stale <name>``
and ``set aside`` and names that directory; and after the rollback no
``-wal`` byte-identical to the planted log sits beside the live store.
Restore never refuses because of a sidecar, and after a rolled-back (or
failed-with-no-safety-copy) restore NO sidecar, staged copy or stash is
left beside the store (judged immediately after the restore returns,
before anything opens the store).  Legs: S1 (this file) stale ``-wal`` +
failure right after the swap; S2-S6 live in
tests/test_audit_restore_identity_recovery.py and are run by this wrapper
too: S2 a ``-wal``-only write (its committed frame IS part of the rollback
target, and the on-disk bytes survive in the forensic directory); S3
failures BEFORE the swap (faults on the first/second/third
destination-sidecar file operation, on the live-store install itself, a
pinned put-back double fault, and a two-sidecar leg pinning that put-backs
run in REVERSE order and a failed put-back does not stop the remaining
ones) leave every file byte-identical or stash the sidecar beside the
store with the output saying so; S4 no safety copy (destination missing /
an empty 0-byte file); S5 a SUCCESS leg -- a quiesced destination with no
sidecars restores cleanly without manufacturing any; S6 a failed restore
onto a quiesced NON-EMPTY destination rolls back with the ruled stderr
line and leaves no set-aside or forensic artifacts.  Existing output
phrases that must keep working: ``restore FAILED``, ``restore: OK``,
``pre-restore backup skipped``, ``removed stale``.

Hermetic: every subprocess and the in-process leg pin a fresh throwaway
store (ZMEM_STORE/ZMEM_DATA/ZMEM_BACKUP_DIR under a system-temp scratch
dir, ZMEM_MODELS_DIR nonexistent, autodownload off, UTF-8 IO); ambient
zmem/plugin env is stripped from children; the operator store is never
opened.  Run: C:/Python311/python.exe -X utf8 <this file>.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import io
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PY = sys.executable

# Module-level scratch + env pin BEFORE the _store_guard import (issue #244)
# and BEFORE any storelib import: the guard judges the ambient env at ITS
# import, and storelib freezes env-derived paths at import.
_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-504-audit-"))


@atexit.register
def _cleanup_scratch():
    shutil.rmtree(_SCRATCH, ignore_errors=True)


os.environ.update({
    "ZMEM_STORE": str(_SCRATCH / "guard-pin.sqlite"),
    "ZMEM_DATA": str(_SCRATCH),
    "ZMEM_BACKUP_DIR": str(_SCRATCH / "backups"),
    "ZMEM_MODELS_DIR": str(_SCRATCH / "no-models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
})

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

_SAVED_MODULES = {}
for _name in [k for k in list(sys.modules)
              if k == "storelib" or k.startswith("storelib.") or k == "schema_meta"]:
    _SAVED_MODULES[_name] = sys.modules.pop(_name)
_PATH_INSERTED = str(SCRIPTS_DIR) not in sys.path
if _PATH_INSERTED:
    sys.path.insert(0, str(SCRIPTS_DIR))
_PINNED_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_BACKUP_DIR", "ZMEM_MODELS_DIR",
                "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING")
_SAVED_ENV = {k: os.environ.get(k) for k in _PINNED_KEYS}

import storelib  # noqa: E402
from schema_meta import SCHEMA_VERSION_KEY  # noqa: E402
from storelib.schema import SUPPORTED_SCHEMA_VERSION  # noqa: E402
from storelib.backup import cmd_restore as _cmd_restore  # noqa: E402

# ---------------------------------------------------------------------------
# Genuine legacy fixture DDL, embedded as literals per the issue-#504 spec.
#
# _LEGACY_V1_DDL is the VERBATIM schema of the initial commit
# ca1f89d2e16498141f894221095b5a93126e2904, file
# skills/memory/scripts/store.py (memory with its 14 columns, the
# memory_fts table and its three triggers, meta stamped '1').
#
# _LEGACY_V2_DELTA / _LEGACY_V3_DELTA are exactly what the v2 and v3
# migration blocks in skills/memory/scripts/storelib/schema.py migrate()
# add (their tables, indexes and columns).  The v3 block's vec0 virtual
# table memory_vec is created by migrate() only inside a try/except when
# the sqlite-vec extension loads, so a plain-SQLite fixture cannot (and
# need not) express it; the durable v3 shape is the three columns below.
# ---------------------------------------------------------------------------
_LEGACY_V1_DDL = """
CREATE TABLE memory (
    id              TEXT PRIMARY KEY,
    namespace       TEXT NOT NULL,
    type            TEXT NOT NULL,
    content         TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '',
    source_ref      TEXT NOT NULL DEFAULT '',
    source_hash     TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 0.5,
    signal          TEXT NOT NULL DEFAULT 'none',
    valid_from      TEXT NOT NULL DEFAULT '',
    superseded_at   TEXT,
    ingestion_ts    TEXT NOT NULL,
    retrieval_count INTEGER NOT NULL DEFAULT 0,
    last_retrieved  TEXT
);
CREATE INDEX idx_memory_namespace ON memory(namespace);
CREATE INDEX idx_memory_live ON memory(superseded_at) WHERE superseded_at IS NULL;
CREATE INDEX idx_memory_type ON memory(type);

CREATE VIRTUAL TABLE memory_fts USING fts5(
    content, tags, namespace,
    content='memory', content_rowid='rowid',
    tokenize='unicode61'
);
CREATE TRIGGER memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, content, tags, namespace)
    VALUES (new.rowid, new.content, new.tags, new.namespace);
END;
CREATE TRIGGER memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, tags, namespace)
    VALUES ('delete', old.rowid, old.content, old.tags, old.namespace);
END;
CREATE TRIGGER memory_au AFTER UPDATE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, tags, namespace)
    VALUES ('delete', old.rowid, old.content, old.tags, old.namespace);
    INSERT INTO memory_fts(rowid, content, tags, namespace)
    VALUES (new.rowid, new.content, new.tags, new.namespace);
END;

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO meta(key, value) VALUES ('schema_version', '1');
"""

_LEGACY_V2_DELTA = """
CREATE INDEX IF NOT EXISTS idx_memory_confidence ON memory(confidence);
CREATE INDEX IF NOT EXISTS idx_memory_ingestion ON memory(ingestion_ts);
CREATE INDEX IF NOT EXISTS idx_memory_retrieval ON memory(retrieval_count);
DROP TRIGGER IF EXISTS memory_au;
CREATE TRIGGER memory_au AFTER UPDATE ON memory
WHEN old.content IS NOT new.content
  OR old.tags IS NOT new.tags
  OR old.namespace IS NOT new.namespace
BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, tags, namespace)
    VALUES ('delete', old.rowid, old.content, old.tags, old.namespace);
    INSERT INTO memory_fts(rowid, content, tags, namespace)
    VALUES (new.rowid, new.content, new.tags, new.namespace);
END;
UPDATE meta SET value='2' WHERE key='schema_version';
"""

_LEGACY_V3_DELTA = """
ALTER TABLE memory ADD COLUMN embedding BLOB;
ALTER TABLE memory ADD COLUMN embedding_model TEXT DEFAULT '';
ALTER TABLE memory ADD COLUMN embedded_at TEXT;
UPDATE meta SET value='3' WHERE key='schema_version';
"""

_V1_CORE_COLUMNS = (
    "id", "namespace", "type", "content", "tags", "source_ref", "source_hash",
    "confidence", "signal", "valid_from", "superseded_at", "ingestion_ts",
    "retrieval_count", "last_retrieved",
)

# The 14 v1 core columns plus `meta`, without any stamp -- the chassis for
# the badly-stamped invalid inputs (same column set as _LEGACY_V1_DDL).
_CORE_MEMORY_META_DDL = """
CREATE TABLE memory (
    id              TEXT PRIMARY KEY,
    namespace       TEXT NOT NULL,
    type            TEXT NOT NULL,
    content         TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '',
    source_ref      TEXT NOT NULL DEFAULT '',
    source_hash     TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 0.5,
    signal          TEXT NOT NULL DEFAULT 'none',
    valid_from      TEXT NOT NULL DEFAULT '',
    superseded_at   TEXT,
    ingestion_ts    TEXT NOT NULL,
    retrieval_count INTEGER NOT NULL DEFAULT 0,
    last_retrieved  TEXT
);
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# The memory-table rebuild DDL for the falsely-current fixture: exactly the
# fourteen v1 core columns under the staging name memory_v1 (renamed to
# memory once the old table -- its triggers and indexes go with it -- is
# dropped).
_MEMORY_V1_REBUILD_DDL = """
CREATE TABLE memory_v1 (
    id              TEXT PRIMARY KEY,
    namespace       TEXT NOT NULL,
    type            TEXT NOT NULL,
    content         TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '',
    source_ref      TEXT NOT NULL DEFAULT '',
    source_hash     TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 0.5,
    signal          TEXT NOT NULL DEFAULT 'none',
    valid_from      TEXT NOT NULL DEFAULT '',
    superseded_at   TEXT,
    ingestion_ts    TEXT NOT NULL,
    retrieval_count INTEGER NOT NULL DEFAULT 0,
    last_retrieved  TEXT
);
"""

# The memory_expiry rebuild DDL for the falsely-current-side-table fixture:
# the staging table carries ONLY memory_id; after the old table (its index
# goes with it) is dropped and this one renamed into place, the index
# idx_memory_expiry_until is recreated on memory_id so every table and
# index name a fresh store has still exists.
_MEMORY_EXPIRY_REBUILD_DDL = """
CREATE TABLE memory_expiry_rebuild (
    memory_id TEXT PRIMARY KEY
);
"""

# Emulates a store killed inside the v3 migration window: the child opens the
# fixture the way every first open does (schema.connect() then
# schema._prepare_store()) and dies with os._exit(137) from a sqlite3 trace
# callback just before the v3 version-stamp UPDATE executes, so that block's
# earlier DDL is committed while the stamp is still behind.  Minimal
# technique copied from tests/test_audit_schema_recovery.py (not imported).
_KILL_CHILD_SOURCE = """\
import os, sys
sys.path.insert(0, sys.argv[1])
from storelib import schema  # noqa: E402

conn = schema.connect()

def _kill(sql):
    if sql.startswith("UPDATE meta SET value='3'") and "schema_version" in sql:
        os._exit(137)

conn.set_trace_callback(_kill)
schema._prepare_store(conn)
print("completed", flush=True)
"""

_STRIP_ENV = ("ZMEM_STORE ZMEM_DATA ZMEM_HOME ZMEM_BACKUP_DIR ZMEM_NAMESPACE ZMEM_HOST "
              "ZMEM_PROJECT_DIR CLAUDE_PROJECT_DIR ZCODE_PROJECT_DIR CLAUDE_PLUGIN_DATA "
              "ZCODE_PLUGIN_DATA ZMEM_SESSION ZMEM_SESSION_ID CLAUDE_SESSION_ID "
              "ZCODE_SESSION_ID ZMEM_INJECT ZMEM_INJECT_TOKEN_BUDGET ZMEM_MODEL_AUTODOWNLOAD "
              "ZMEM_MODELS_DIR ZMEM_EMBED_PROFILE ZMEM_QUERY_CONTEXT ZMEM_TRANSCRIPT ZMEM_TIER0 "
              "ZMEM_SKILLS_DIRS ZMEM_PYTHON ZMEM_CROSS_PROJECT ZMEM_REMOTE_URL "
              "ZMEM_CROSS_ENCODER ZMEM_ALLOW_NEWER_SCHEMA ZMEM_ALLOW_REAL_STORE").split()


def _apply_env(mapping):
    for key, value in mapping.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def tearDownModule():  # noqa: N802 - unittest hook
    """Restore the ambient env / module cache (pytest co-run citizenship)."""
    for key in [k for k in list(sys.modules)
                if k == "storelib" or k.startswith("storelib.") or k == "schema_meta"]:
        del sys.modules[key]
    sys.modules.update(_SAVED_MODULES)
    if _PATH_INSERTED:
        try:
            sys.path.remove(str(SCRIPTS_DIR))
        except ValueError:  # pragma: no cover - defensive
            pass
    _apply_env(_SAVED_ENV)
    shutil.rmtree(_SCRATCH, ignore_errors=True)


def _child_env(tmp: Path, store: Path, backup_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({"ZMEM_STORE": str(store), "ZMEM_DATA": str(tmp), "ZMEM_BACKUP_DIR": str(backup_dir),
                "ZMEM_MODELS_DIR": str(tmp / "no-models"), "ZMEM_MODEL_AUTODOWNLOAD": "0",
                "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    return env


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _first_eligible_term(content):
    """The first whitespace token of 4+ characters -- the term-selection
    rule the restore's search-index sampler is specified to use."""
    for token in content.split():
        if len(token) >= 4:
            return token
    return None


def _make_foreign_db(path):
    with contextlib.closing(sqlite3.connect(str(path))) as conn:
        conn.execute("CREATE TABLE unrelated_inventory (sku TEXT PRIMARY KEY, qty INTEGER)")
        conn.execute("INSERT INTO unrelated_inventory VALUES ('widget', 3)")
        conn.commit()


def _make_wrong_shape_db(path, stamp):
    with contextlib.closing(sqlite3.connect(str(path))) as conn:
        conn.execute("CREATE TABLE memory (id TEXT PRIMARY KEY, content TEXT)")
        conn.execute("INSERT INTO memory VALUES ('imposter', 'not a zmem row')")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES (?, ?)", (SCHEMA_VERSION_KEY, str(stamp)))
        conn.commit()


def _make_text_file(path):
    path.write_bytes(b"this is definitely not a sqlite database\n" * 3)


def _make_core_fixture(path, stamp):
    """`memory` with the 14 v1 core columns plus `meta`; the stamp argument
    is written verbatim (non-integer strings included), or omitted entirely
    when None -- the falsely/under/un-stamped shapes restore must refuse."""
    with contextlib.closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(_CORE_MEMORY_META_DDL)
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, source_ref, "
            "source_hash, confidence, signal, valid_from, superseded_at, "
            "ingestion_ts, retrieval_count, last_retrieved) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("core-" + uuid.uuid4().hex, "project:audit504", "fact",
             "plausible row in a badly stamped store", "", "", "", 0.9, "test",
             "", None, "2026-11-23T00:00:00Z", 0, None))
        if stamp is not None:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)",
                         (SCHEMA_VERSION_KEY, str(stamp)))
        conn.commit()


class _RestoreAuditBase(unittest.TestCase):
    """Fresh throwaway store per test; public-CLI driver helpers."""

    # Every assertion message starts with the acceptance marker; suppress
    # unittest's standard "x != y :" prefix so failure texts are
    # marker-first.
    longMessage = False

    def setUp(self):
        self.mk = "AC3-504"
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-504-leg-"))
        self.store = self.tmp / "store.sqlite"
        self.backup_dir = self.tmp / "backups"
        self.ns = "project:audit504"
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _cli(self, *args):
        return subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY)] + list(args), capture_output=True,
            text=True, env=_child_env(self.tmp, self.store, self.backup_dir),
            cwd=str(REPO_ROOT), timeout=180)

    def _cli_in(self, work_dir, store, *args):
        """Public CLI against a store OTHER than this leg's live store."""
        return subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY)] + list(args), capture_output=True,
            text=True, env=_child_env(work_dir, store, work_dir / "backups"),
            cwd=str(REPO_ROOT), timeout=180)

    def _add(self, content, *, setup=False, what="control row"):
        prefix = f"{self.mk}: " + ("scenario setup: " if setup else "")
        r = self._cli("add", "--namespace", self.ns, "--type", "fact", "--content", content,
                      "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, prefix + f"{what} add {content!r} failed: "
                         f"{(r.stderr or r.stdout).strip()[:300]}")

    def _restore_cli(self, snap):
        return self._cli("restore", "--from", str(snap), "--force")

    def _live_rows(self):
        """Live rows as any SQLite client sees the store; unreadable -> reason."""
        try:
            with contextlib.closing(sqlite3.connect(str(self.store))) as conn:
                return [r[0] for r in conn.execute(
                    "SELECT content FROM memory WHERE superseded_at IS NULL")]
        except sqlite3.Error as e:
            return [f"<store unreadable: {e}>"]

    def _rows_at(self, path):
        """Live rows a normal SQLite open reads from `path` (sidecars beside
        it apply); unreadable -> reason."""
        try:
            with contextlib.closing(sqlite3.connect(str(path))) as conn:
                return [r[0] for r in conn.execute(
                    "SELECT content FROM memory WHERE superseded_at IS NULL")]
        except sqlite3.Error as e:
            return [f"<store unreadable: {e}>"]

    def _immutable_rows(self, path):
        """Live rows a sidecar-free immutable read of `path`'s MAIN FILE
        alone yields (the -wal/-journal beside it are ignored)."""
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            return [r[0] for r in conn.execute(
                "SELECT content FROM memory WHERE superseded_at IS NULL")]

    def _find_wal_copy(self, lines, planted_wal):
        """The first existing file named by one of `lines` (absolute tokens,
        or tokens resolved against the backup dir / store dir / cwd; a named
        directory contributes its files one level deep) whose bytes equal
        `planted_wal`; None when no line names such a location."""
        wanted = _digest_bytes(planted_wal)
        roots = (self.backup_dir, self.store.parent, Path.cwd())

        def _candidates(line):
            found = []
            for token in re.split(r"[\s'\"\[\]()<>]+", line):
                token = token.strip(".,;:`")
                if not token:
                    continue
                stems = [Path(token)]
                for root in roots:
                    stems.append(root / token.lstrip("/\\"))
                for stem in stems:
                    if stem.is_file():
                        found.append(stem)
                    elif stem.is_dir():
                        try:
                            found.extend(p for p in stem.iterdir() if p.is_file())
                            for sub in stem.iterdir():
                                if sub.is_dir():
                                    found.extend(p for p in sub.iterdir() if p.is_file())
                        except OSError:
                            pass
            return found

        for line in lines:
            for candidate in _candidates(line):
                try:
                    if _digest_bytes(candidate.read_bytes()) == wanted:
                        return candidate
                except OSError:
                    continue
        return None

    def _reset_live(self, tokens):
        """Rebuild the live store from scratch: exactly `tokens` as rows."""
        for suffix in ("", "-wal", "-shm", "-journal"):
            victim = Path(str(self.store) + suffix)
            if victim.exists():
                victim.unlink()
        if self.backup_dir.exists():
            shutil.rmtree(self.backup_dir)
        for token in tokens:
            self._add(token, setup=True)

    def _take_backup(self):
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        r = self._cli("backup")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: control backup "
                         f"failed: {(r.stderr or r.stdout).strip()[:300]}")
        snaps = sorted(self.backup_dir.glob("store-*.sqlite"))
        self.assertEqual(len(snaps), 1, f"{self.mk}: scenario setup: expected exactly one "
                         f"fresh snapshot, found {[s.name for s in snaps]}")
        return snaps[0]

    def _restamp(self, db_path, version):
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute("UPDATE meta SET value=? WHERE key=?",
                         (str(version), SCHEMA_VERSION_KEY))
            conn.commit()

    def _read_live_stamp(self):
        with contextlib.closing(sqlite3.connect(str(self.store))) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?",
                               (SCHEMA_VERSION_KEY,)).fetchone()
        return row[0] if row else None

    def _digest(self, path):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @contextlib.contextmanager
    def _pinned_env(self):
        """Point the in-process storelib at THIS leg's throwaway store.

        Also re-arms the import ecosystem when a previous pytest node's
        suite-level tearDownModule popped ``storelib`` from sys.modules and
        dropped the scripts dir from sys.path: without this, later nodes'
        deferred imports inside cmd_restore would fail (or bind a stale
        instance) and masquerade as product refusals.  Every storelib
        instance alive in this process is refreshed under the pin, so
        deferred imports never see a previous leg's frozen paths."""

        def _refresh_alive_instances():
            alive = sys.modules.get("storelib")
            if alive is not None and alive is not storelib:
                alive._refresh_env_state()
            storelib._refresh_env_state()

        saved = {k: os.environ.get(k) for k in _PINNED_KEYS}
        try:
            os.environ.update({"ZMEM_STORE": str(self.store), "ZMEM_DATA": str(self.tmp),
                               "ZMEM_BACKUP_DIR": str(self.backup_dir),
                               "ZMEM_MODELS_DIR": str(self.tmp / "no-models"),
                               "ZMEM_MODEL_AUTODOWNLOAD": "0",
                               "PYTHONIOENCODING": "utf-8"})
            if str(SCRIPTS_DIR) not in sys.path:
                sys.path.insert(0, str(SCRIPTS_DIR))
            if "storelib" not in sys.modules:
                import storelib as _rearmed  # noqa: F401,E402  (fresh, under this pin)
            _refresh_alive_instances()
            yield
        finally:
            _apply_env(saved)
            _refresh_alive_instances()

    def _assert_no_sidecars_or_stashes(self, label, *, main_expected="present"):
        """Evaluated immediately after the restore returns, BEFORE any check
        opens the store (no sqlite open, no later add): a restore that was
        rolled back (or failed onto a missing/empty destination) leaves NO
        sidecar, staged copy or stash beside the store.  main_expected:
        "present" (not judged here), "absent", or "empty" (0 bytes)."""
        store_name = self.store.name
        names = set(os.listdir(self.store.parent))
        leftovers = sorted(
            name for name in names
            if name in (store_name + "-wal", store_name + "-shm",
                        store_name + "-journal")
            or name.startswith(store_name + ".restore-stage-")
            or name.startswith(store_name + ".restore-stash-"))
        self.assertEqual(leftovers, [], f"{self.mk}: [{label}] immediately after the "
                         f"restore returned, sidecars, staged copies or stashes are "
                         f"left beside the store: {leftovers!r} -- none may remain")
        if main_expected == "absent":
            size_note = (f" ({self.store.stat().st_size} bytes)"
                         if self.store.exists() else "")
            self.assertFalse(self.store.exists(), f"{self.mk}: [{label}] after the "
                            f"failed restore the destination main file must be absent; "
                            f"it exists{size_note}")
        elif main_expected == "empty":
            self.assertTrue(self.store.exists(), f"{self.mk}: [{label}] the destination "
                            f"main file must still exist after the failed restore")
            if self.store.exists():
                self.assertEqual(self.store.stat().st_size, 0, f"{self.mk}: [{label}] "
                                f"after the failed restore the empty destination must "
                                f"still be 0 bytes; it is now "
                                f"{self.store.stat().st_size} bytes")

    def _restore_inprocess_spy(self, snap_path):
        """Run cmd_restore in-process against this leg's live store with an
        os.replace spy that records (and performs) every replacement whose
        destination is the live store path.  Returns a dict with the return
        code, any exception that escaped, captured stdout/stderr, and the
        number of live-store replacements observed."""
        out, err = io.StringIO(), io.StringIO()
        state = {"replacements": 0}
        live_abs = os.path.abspath(str(self.store))
        real_replace = os.replace

        def _spy(src, dst, *args, **kwargs):
            try:
                if os.path.abspath(os.fspath(dst)) == live_abs:
                    state["replacements"] += 1
            except (TypeError, ValueError):  # pragma: no cover - defensive
                pass
            return real_replace(src, dst, *args, **kwargs)

        rc, raised = None, None
        with self._pinned_env():
            os.replace = _spy
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = _cmd_restore(from_path=str(snap_path), force=True)
            except Exception as e:  # an escaping exception is itself a failure
                raised, rc = e, None
            finally:
                os.replace = real_replace
        return {"rc": rc, "raised": raised, "out": out.getvalue(),
                "err": err.getvalue(), "replacements": state["replacements"]}


class _RestoreStagedValidationCase(_RestoreAuditBase):
    """AC1 (#504): invalid snapshots are refused before the live store moves;
    genuine legacy fixtures are upgraded."""

    CONTROLS = ("ac3 control row alpha", "ac3 control row bravo", "ac3 control row charlie")

    def _assert_live_preserved(self, label, context, later):
        """The live control survives `label`: all control rows present,
        nothing gained or lost, and a later public add lands on top."""
        rows = self._live_rows()
        for control in self.CONTROLS:
            self.assertIn(control, rows, f"{self.mk}: {label} displaced the live store: "
                          f"{context}, live rows now {rows!r}")
        self.assertEqual(len(rows), len(self.CONTROLS), f"{self.mk}: {label} changed the "
                         f"live store's row set: {context}, live rows now {rows!r}")
        self._add(later, what="the later public write")
        rows = self._live_rows()
        self.assertEqual(sorted(rows), sorted(self.CONTROLS + (later,)),
                         f"{self.mk}: after {label} the live store did not accept a later "
                         f"write on top of the preserved control rows; live rows now {rows!r}")

    def _make_legacy_fixture(self, path, contents, version):
        """Plain-SQLite fixture at the embedded legacy DDL for `version`."""
        ddl = _LEGACY_V1_DDL
        if version >= 2:
            ddl += _LEGACY_V2_DELTA
        if version >= 3:
            ddl += _LEGACY_V3_DELTA
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            conn.executescript(ddl)
            for index, content in enumerate(contents):
                conn.execute(
                    "INSERT INTO memory (id, namespace, type, content, tags, "
                    "source_ref, source_hash, confidence, signal, valid_from, "
                    "superseded_at, ingestion_ts, retrieval_count, last_retrieved) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("legacy-%d-%s" % (version, uuid.uuid4().hex[:8]), self.ns, "fact",
                     content, "", "", "", 0.9, "test", "", None,
                     "2026-11-23T00:00:0%dZ" % index, 0, None))
            conn.commit()
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            stamp = conn.execute("SELECT value FROM meta WHERE key=?",
                                 (SCHEMA_VERSION_KEY,)).fetchone()[0]
        self.assertEqual(stamp, str(version), f"{self.mk}: scenario setup: the v{version} "
                         f"legacy fixture must be stamped {version}, found {stamp!r}")

    def _make_interrupted_v3_fixture(self, path, contents):
        """A real kill inside the v3 migration window: build the v2 fixture,
        then a child opens it and os._exit(137)s from a sqlite3 trace
        callback just before the v3 stamp statement executes.  Shape-tolerant
        (mirroring tests/test_audit_schema_recovery.py's _interrupted_store):
        a NON-atomic v3 block leaves its DDL committed while the stamp stays
        behind; a fix that makes the block ATOMIC rolls the DDL back -- both
        are valid interrupted shapes and both must resume, so the ONLY hard
        fixture claim is the pre-v3 stamp.  The v3 columns must be either
        ALL present or ALL absent (a partial subset would mean the fixture
        was fabricated, which this builder never does); the state is only
        ever observed read-only, never rewritten."""
        fixture_dir = path.parent
        self._make_legacy_fixture(path, contents, version=2)
        child = fixture_dir / ("_kill_child_v3_%s.py" % uuid.uuid4().hex[:8])
        child.write_text(_KILL_CHILD_SOURCE, encoding="utf-8")
        env = _child_env(fixture_dir, path, fixture_dir / "backups")
        kill = subprocess.run([PY, "-X", "utf8", str(child), str(SCRIPTS_DIR)],
                              capture_output=True, text=True, env=env,
                              cwd=str(REPO_ROOT), timeout=180)
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            stamp = conn.execute("SELECT value FROM meta WHERE key=?",
                                 (SCHEMA_VERSION_KEY,)).fetchone()
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
        stamp = stamp[0] if stamp else None
        v3_columns = ("embedding", "embedding_model", "embedded_at")
        present = [c for c in v3_columns if c in cols]
        columns_shape_ok = not present or len(present) == len(v3_columns)
        state_ok = stamp == "2" and columns_shape_ok
        if kill.returncode != 137 and not state_ok:
            self.fail(f"{self.mk}: scenario setup: the v3 kill child exited "
                      f"{kill.returncode} (stderr {kill.stderr.strip()[:200]!r}) and the "
                      f"fixture is not in the interrupted state (stamp {stamp!r})")
        self.assertEqual(stamp, "2", f"{self.mk}: scenario setup: the interrupted-migration "
                        f"fixture must carry the pre-v3 stamp '2' (the kill must land "
                        f"inside the v3 window, and the builder never fabricates the "
                        f"state); stamp {stamp!r}")
        self.assertTrue(columns_shape_ok, f"{self.mk}: scenario setup: the v3 columns "
                        f"after the kill must be either all committed (non-atomic block) "
                        f"or all rolled back (atomic block), not a partial subset; found "
                        f"{present!r} of {v3_columns!r}")

    def _make_falsely_current_fixture(self, path):
        """A falsely current store, the shape a sync tool leaves behind: a
        real store created by the PUBLIC CLI in a scratch directory is
        copied to `path`, then plain SQLite rebuilds its `memory` table
        with ONLY the fourteen v1 core columns (rows kept; the old table's
        triggers and indexes go with the drop), every other table and index
        is left alone, and `meta.schema_version` stays at the supported
        value -- a stamp claiming a shape the running client cannot write.
        The scenario-setup proof below runs read-only on the fixture BEFORE
        any restore attempt."""
        donor_dir = self.tmp / ("falsely-current-donor-" + uuid.uuid4().hex[:8])
        donor_dir.mkdir(parents=True)
        donor = donor_dir / "store.sqlite"
        r = self._cli_in(donor_dir, donor, "add", "--namespace", self.ns, "--type",
                         "fact", "--content", "donor row inside the falsely current "
                         "fixture", "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: the donor store "
                         f"for the falsely-current fixture could not be built with the "
                         f"public CLI: {(r.stderr or r.stdout).strip()[:300]}")
        shutil.copyfile(donor, path)
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            conn.executescript(_MEMORY_V1_REBUILD_DDL)
            conn.execute(
                "INSERT INTO memory_v1 (id, namespace, type, content, tags, "
                "source_ref, source_hash, confidence, signal, valid_from, "
                "superseded_at, ingestion_ts, retrieval_count, last_retrieved) "
                "SELECT id, namespace, type, content, tags, source_ref, "
                "source_hash, confidence, signal, valid_from, superseded_at, "
                "ingestion_ts, retrieval_count, last_retrieved FROM memory")
            conn.execute("DROP TABLE memory")
            conn.execute("ALTER TABLE memory_v1 RENAME TO memory")
            conn.commit()
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
            stamp = conn.execute("SELECT value FROM meta WHERE key=?",
                                 (SCHEMA_VERSION_KEY,)).fetchone()
        missing = [c for c in _V1_CORE_COLUMNS if c not in cols]
        self.assertEqual(missing, [], f"{self.mk}: scenario setup: the falsely-current "
                         f"fixture's memory table must carry all fourteen v1 core "
                         f"columns; missing {missing!r}")
        late = [c for c in ("embedding", "consolidated_at") if c in cols]
        self.assertEqual(late, [], f"{self.mk}: scenario setup: the falsely-current "
                         f"fixture's memory table must lack the columns later "
                         f"migrations add; found {late!r}")
        self.assertEqual(stamp[0] if stamp else None, str(SUPPORTED_SCHEMA_VERSION),
                         f"{self.mk}: scenario setup: the falsely-current fixture must "
                         f"be stamped with the supported version "
                         f"{SUPPORTED_SCHEMA_VERSION}; found "
                         f"{(stamp[0] if stamp else None)!r}")

    def _make_falsely_current_side_table_fixture(self, path):
        """A falsely current side table, the shape a partial sync leaves
        behind: a real store created by the PUBLIC CLI in a scratch
        directory is copied to `path`, then plain SQLite rebuilds the
        required `memory_expiry` table with ONLY the `memory_id` column
        (rows kept), drops the old table (its index goes with it) and
        recreates `idx_memory_expiry_until` on that column, so every table
        and index name a fresh store has still exists; the stamp stays at
        the supported value and every other table is left alone.  The
        scenario-setup proof below runs read-only on the fixture BEFORE any
        restore attempt."""
        donor_dir = self.tmp / ("falsely-current-side-donor-" + uuid.uuid4().hex[:8])
        donor_dir.mkdir(parents=True)
        donor = donor_dir / "store.sqlite"
        r = self._cli_in(donor_dir, donor, "add", "--namespace", self.ns, "--type",
                         "fact", "--content", "donor row inside the falsely current "
                         "side table fixture", "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: the donor store "
                         f"for the falsely-current-side-table fixture could not be built "
                         f"with the public CLI: {(r.stderr or r.stdout).strip()[:300]}")
        shutil.copyfile(donor, path)
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            conn.executescript(_MEMORY_EXPIRY_REBUILD_DDL)
            conn.execute("INSERT INTO memory_expiry_rebuild (memory_id) "
                         "SELECT memory_id FROM memory_expiry")
            conn.execute("DROP TABLE memory_expiry")
            conn.execute("ALTER TABLE memory_expiry_rebuild RENAME TO memory_expiry")
            conn.execute("CREATE INDEX idx_memory_expiry_until "
                         "ON memory_expiry(memory_id)")
            conn.commit()
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(memory_expiry)")]
            index = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_memory_expiry_until",)).fetchone()
            stamp = conn.execute("SELECT value FROM meta WHERE key=?",
                                 (SCHEMA_VERSION_KEY,)).fetchone()
        self.assertEqual(cols, ["memory_id"], f"{self.mk}: scenario setup: the "
                         f"falsely-current side table fixture's memory_expiry must have "
                         f"exactly the one column memory_id; found {cols!r}")
        self.assertNotEqual(index, None, f"{self.mk}: scenario setup: the "
                            f"falsely-current side table fixture must still carry the "
                            f"idx_memory_expiry_until index (recreated on memory_id)")
        self.assertEqual(stamp[0] if stamp else None, str(SUPPORTED_SCHEMA_VERSION),
                         f"{self.mk}: scenario setup: the falsely-current side table "
                         f"fixture must be stamped with the supported version "
                         f"{SUPPORTED_SCHEMA_VERSION}; found "
                         f"{(stamp[0] if stamp else None)!r}")

    def _make_fts_impostor_fixture(self, path):
        """The critic's FTS impostor (r3 probe): a real CLI-built store
        whose `memory_fts` virtual table is swapped for a STANDALONE FTS5
        table with the same three columns and NO external-content binding
        (`DROP TABLE memory_fts` then a plain fts5() CREATE).  The three
        triggers survive, every FTS object NAME a fresh store has is still
        present, columns and row counts are unchanged and integrity_check
        stays ok -- yet the existing rows are silently unindexed and lexical
        search finds nothing.  Scenario-setup proofs (read-only) run BEFORE
        the restore."""
        donor_dir = self.tmp / ("fts-impostor-donor-" + uuid.uuid4().hex[:8])
        donor_dir.mkdir(parents=True)
        donor = donor_dir / "store.sqlite"
        r = self._cli_in(donor_dir, donor, "add", "--namespace", self.ns, "--type",
                         "fact", "--content", "ac3 fts impostor donor row uniquetokenfts",
                         "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: the donor store "
                         f"for the FTS-impostor fixture could not be built with the "
                         f"public CLI: {(r.stderr or r.stdout).strip()[:300]}")
        shutil.copyfile(donor, path)
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            conn.execute("DROP TABLE memory_fts")
            conn.execute("CREATE VIRTUAL TABLE memory_fts USING fts5("
                         "content, tags, namespace, tokenize='unicode61')")
            conn.commit()
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            stamp = conn.execute("SELECT value FROM meta WHERE key=?",
                                 (SCHEMA_VERSION_KEY,)).fetchone()
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
            memory_rows = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
            docsize_rows = conn.execute(
                "SELECT COUNT(*) FROM memory_fts_docsize").fetchone()[0]
            hits = conn.execute(
                "SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?",
                ("uniquetokenfts",)).fetchall()
        self.assertEqual(stamp[0] if stamp else None, str(SUPPORTED_SCHEMA_VERSION),
                         f"{self.mk}: scenario setup: the FTS-impostor fixture must be "
                         f"stamped with the supported version "
                         f"{SUPPORTED_SCHEMA_VERSION}; found "
                         f"{(stamp[0] if stamp else None)!r}")
        fts_family = {"memory_fts", "memory_fts_config", "memory_fts_data",
                      "memory_fts_docsize", "memory_fts_idx"}
        missing_names = sorted(fts_family - names)
        self.assertEqual(missing_names, [], f"{self.mk}: scenario setup: the FTS "
                         f"impostor must keep all five FTS object names a fresh store "
                         f"has; missing {missing_names!r}")
        triggers = {"memory_ai", "memory_ad", "memory_au"} & names
        self.assertEqual(triggers, {"memory_ai", "memory_ad", "memory_au"},
                         f"{self.mk}: scenario setup: the FTS impostor must keep all "
                         f"three memory triggers; found {sorted(triggers)!r}")
        self.assertGreater(memory_rows, docsize_rows, f"{self.mk}: scenario setup: "
                         f"the FTS impostor's existing rows must be unindexed (memory "
                         f"rows {memory_rows} vs docsize rows {docsize_rows})")
        self.assertEqual(hits, [], f"{self.mk}: scenario setup: a lexical MATCH for a "
                         f"word of an existing row must return nothing on the FTS "
                         f"impostor; got {hits!r}")

    def _make_impostor_index_fixture(self, path):
        """A same-name index with different SQL: a real CLI-built store whose
        `idx_memory_live` is dropped and recreated WITHOUT the partial-index
        WHERE clause (`ON memory(superseded_at)` instead of
        `ON memory(superseded_at) WHERE superseded_at IS NULL`).  Same name,
        same table, different semantics.  Scenario-setup proofs (read-only)
        run BEFORE the restore."""
        donor_dir = self.tmp / ("impostor-index-donor-" + uuid.uuid4().hex[:8])
        donor_dir.mkdir(parents=True)
        donor = donor_dir / "store.sqlite"
        r = self._cli_in(donor_dir, donor, "add", "--namespace", self.ns, "--type",
                         "fact", "--content", "ac3 impostor index donor row",
                         "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: the donor store "
                         f"for the impostor-index fixture could not be built with the "
                         f"public CLI: {(r.stderr or r.stdout).strip()[:300]}")
        shutil.copyfile(donor, path)
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            conn.execute("DROP INDEX idx_memory_live")
            conn.execute("CREATE INDEX idx_memory_live ON memory(superseded_at)")
            conn.commit()
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' "
                               "AND name='idx_memory_live'").fetchone()
        self.assertNotEqual(row, None, f"{self.mk}: scenario setup: the impostor-index "
                            f"fixture must still carry an index named idx_memory_live")
        fixture_sql = re.sub(r"\s+", " ", row[0] or "").strip().casefold() if row else None
        fresh_sql = self._fresh_control_sql("idx_memory_live")
        self.assertNotEqual(fixture_sql, fresh_sql, f"{self.mk}: scenario setup: the "
                            f"impostor index's stored SQL must differ from a fresh "
                            f"store's (fixture {fixture_sql!r} vs fresh {fresh_sql!r})")

    def _make_impostor_trigger_fixture(self, path):
        """A same-name trigger with different SQL: a real CLI-built store
        whose `memory_ai` is dropped and recreated under the same name with
        a body that does NOT insert into memory_fts.  Same name, different
        behavior: rows inserted after the swap are never indexed.
        Scenario-setup proofs (read-only, on a scratch copy for the insert)
        run BEFORE the restore."""
        donor_dir = self.tmp / ("impostor-trigger-donor-" + uuid.uuid4().hex[:8])
        donor_dir.mkdir(parents=True)
        donor = donor_dir / "store.sqlite"
        r = self._cli_in(donor_dir, donor, "add", "--namespace", self.ns, "--type",
                         "fact", "--content", "ac3 impostor trigger donor row",
                         "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: the donor store "
                         f"for the impostor-trigger fixture could not be built with the "
                         f"public CLI: {(r.stderr or r.stdout).strip()[:300]}")
        shutil.copyfile(donor, path)
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            conn.execute("DROP TRIGGER memory_ai")
            conn.execute("CREATE TRIGGER memory_ai AFTER INSERT ON memory "
                         "BEGIN SELECT new.rowid; END")
            conn.commit()
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                               "AND name='memory_ai'").fetchone()
        self.assertNotEqual(row, None, f"{self.mk}: scenario setup: the "
                            f"impostor-trigger fixture must still carry a trigger named "
                            f"memory_ai")
        fixture_sql = re.sub(r"\s+", " ", row[0] or "").strip().casefold() if row else None
        fresh_sql = self._fresh_control_sql("memory_ai")
        self.assertNotEqual(fixture_sql, fresh_sql, f"{self.mk}: scenario setup: the "
                            f"impostor trigger's stored SQL must differ from a fresh "
                            f"store's (fixture {fixture_sql!r} vs fresh {fresh_sql!r})")
        proof_dir = self.tmp / ("impostor-trigger-proof-" + uuid.uuid4().hex[:8])
        proof_dir.mkdir(parents=True)
        proof = proof_dir / "proof.sqlite"
        shutil.copyfile(path, proof)
        with contextlib.closing(sqlite3.connect(str(proof))) as conn:
            conn.execute("INSERT INTO memory (id, namespace, type, content, "
                         "ingestion_ts, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                         ("impostor-trigger-proof", self.ns, "fact",
                          "ac3 row that the impostor trigger must leave unindexed",
                          "2026-11-24T00:00:00Z", 0.9))
            conn.commit()
            hits = conn.execute("SELECT rowid FROM memory_fts WHERE memory_fts "
                                "MATCH ?", ("unindexed",)).fetchall()
        self.assertEqual(hits, [], f"{self.mk}: scenario setup: a row inserted "
                         f"directly through SQL must NOT be findable by FTS on the "
                         f"impostor-trigger fixture (the impostor memory_ai does not "
                         f"index); got {hits!r}")

    def _make_stale_index_fixture(self, path, row_one, row_two_original,
                                  row_two_new):
        """The two-row stale-index store (critic r4 finding 1, reworked per
        critic r5 finding 1): a real CLI-built store with two rows whose
        FIRST ELIGIBLE search term (first whitespace token of 4+
        characters) is `sharedtoken`; then, with plain SQLite, `DROP TRIGGER
        memory_au`, UPDATE the second row's content to a reworded value
        whose first eligible term is `zurich` (a short lead word the term
        rule skips), and re-CREATE the GENUINE memory_au trigger from the
        donor's stored SQL.  Result: every object name present, docsize
        rowids equal memory rowids, integrity ok -- but the search index is
        stale (`sharedtoken` still finds the reworded row; `zurich` finds
        nothing, so a sampler following the term rule MUST rebuild).
        Scenario-setup proofs (read-only) run BEFORE the restore."""
        donor_dir = self.tmp / ("stale-index-donor-" + uuid.uuid4().hex[:8])
        donor_dir.mkdir(parents=True)
        donor = donor_dir / "store.sqlite"
        for content in (row_one, row_two_original):
            r = self._cli_in(donor_dir, donor, "add", "--namespace", self.ns,
                             "--type", "fact", "--content", content,
                             "--signal", "test", "--json")
            self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: the "
                             f"donor store for the stale-index fixture could not be "
                             f"built with the public CLI: "
                             f"{(r.stderr or r.stdout).strip()[:300]}")
        with contextlib.closing(sqlite3.connect(str(donor))) as conn:
            genuine_au = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND "
                "name='memory_au'").fetchone()
        self.assertTrue(genuine_au and genuine_au[0], f"{self.mk}: scenario "
                        f"setup: the donor store must carry the genuine memory_au "
                        f"trigger to copy")
        shutil.copyfile(donor, path)
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            conn.execute("DROP TRIGGER memory_au")
            conn.execute("UPDATE memory SET content=? WHERE content=?",
                         (row_two_new, row_two_original))
            changed = conn.execute("SELECT changes()").fetchone()[0]
            conn.execute(genuine_au[0])
            conn.commit()
        self.assertEqual(changed, 1, f"{self.mk}: scenario setup: exactly one "
                         f"donor row must match the second row's original content "
                         f"for the stale-index update (changed {changed})")
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            fixture_master = {(row[0], row[1]) for row in conn.execute(
                "SELECT type, name FROM sqlite_master")}
            memory_ids = {row[0] for row in conn.execute("SELECT rowid FROM memory")}
            docsize_ids = {row[0] for row in conn.execute(
                "SELECT rowid FROM memory_fts_docsize")}
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            zurich = conn.execute("SELECT rowid FROM memory_fts WHERE "
                                  "memory_fts MATCH 'zurich'").fetchall()
            reworded_id = conn.execute("SELECT rowid FROM memory WHERE "
                                       "content=?", (row_two_new,)).fetchone()
            sharedtoken_ids = {row[0] for row in conn.execute(
                "SELECT rowid FROM memory_fts WHERE "
                "memory_fts MATCH 'sharedtoken'")}
        self.assertEqual(_first_eligible_term(row_one), "sharedtoken",
                         f"{self.mk}: scenario setup: the first eligible term of "
                         f"the first row must be sharedtoken")
        self.assertEqual(_first_eligible_term(row_two_original), "sharedtoken",
                         f"{self.mk}: scenario setup: the first eligible term of "
                         f"the second row's ORIGINAL text must be sharedtoken")
        self.assertEqual(_first_eligible_term(row_two_new), "zurich",
                         f"{self.mk}: scenario setup: the first eligible term of "
                         f"the reworded second row must be zurich (its lead word "
                         f"is shorter than the 4-character term rule), so a "
                         f"sampler following that rule must rebuild the index")
        self.assertNotEqual(reworded_id, None, f"{self.mk}: scenario setup: the "
                            f"reworded second row must exist in the fixture")
        self.assertIn(reworded_id[0], sharedtoken_ids, f"{self.mk}: scenario "
                            f"setup: the stale index must still find the reworded "
                            f"row under its OLD term sharedtoken "
                            f"(hits {sorted(sharedtoken_ids)!r})")
        missing = sorted(self._fresh_control_master() - fixture_master)
        self.assertEqual(missing, [], f"{self.mk}: scenario setup: the stale-index "
                         f"fixture must keep every object name a fresh store has; "
                         f"missing {missing!r}")
        self.assertEqual(docsize_ids, memory_ids, f"{self.mk}: scenario setup: the "
                         f"stale-index fixture's memory_fts_docsize rowids must "
                         f"equal memory's rowids")
        self.assertEqual(integrity, "ok", f"{self.mk}: scenario setup: the "
                         f"stale-index fixture must pass integrity_check "
                         f"({integrity!r})")
        self.assertEqual(zurich, [], f"{self.mk}: scenario setup: the stale index "
                         f"must hide the reworded row before the restore "
                         f"(MATCH 'zurich' got {zurich!r})")

    def _make_taint_impostor_fixture(self, path):
        """The tainté impostor (critic r4, finding 2): a real CLI-built
        store whose `memory` table is rebuilt the way the falsely-current
        fixture rebuilds it (new table, rows copied, triggers and indexes
        recreated from the donor's stored SQL) with the SAME columns EXCEPT
        `taint` renamed to the unquoted non-ASCII `tainté` -- a rename an
        ASCII-word tokenizer silently reads as `taint`.  Stamp untouched.
        Scenario-setup proofs (read-only) run BEFORE the restore."""
        donor_dir = self.tmp / ("taint-impostor-donor-" + uuid.uuid4().hex[:8])
        donor_dir.mkdir(parents=True)
        donor = donor_dir / "store.sqlite"
        r = self._cli_in(donor_dir, donor, "add", "--namespace", self.ns, "--type",
                         "fact", "--content", "ac3 taint impostor donor row",
                         "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: the donor "
                         f"store for the tainté impostor could not be built with "
                         f"the public CLI: {(r.stderr or r.stdout).strip()[:300]}")
        shutil.copyfile(donor, path)
        with contextlib.closing(sqlite3.connect(str(donor))) as conn:
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                               "AND name='memory'").fetchone()[0]
            dependents = [row[2] for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='memory' "
                "AND type IN ('index','trigger') AND sql IS NOT NULL")]
        self.assertIn("taint", ddl.split(), f"{self.mk}: scenario setup: the donor "
                      f"memory DDL must name the taint column to rename")
        renamed = re.sub(r"\btaint\b", "tainté", ddl)
        rebuild_ddl = re.sub(r"CREATE TABLE (IF NOT EXISTS )?memory\b",
                             lambda m: "CREATE TABLE " + (m.group(1) or "")
                             + "memory_rebuild",
                             renamed, count=1)
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(memory)")]
            self.assertIn("taint", cols, f"{self.mk}: scenario setup: the donor "
                          f"memory table must carry the taint column")
            conn.execute(rebuild_ddl)
            source_list = ", ".join(cols)
            target_list = ", ".join("tainté" if col == "taint" else col
                                    for col in cols)
            conn.execute(f"INSERT INTO memory_rebuild ({target_list}) "
                         f"SELECT {source_list} FROM memory")
            conn.execute("DROP TABLE memory")
            conn.execute("ALTER TABLE memory_rebuild RENAME TO memory")
            for sql_text in dependents:
                conn.execute(sql_text)
            conn.commit()
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            stored_ddl = conn.execute("SELECT sql FROM sqlite_master WHERE "
                                      "type='table' AND name='memory'").fetchone()[0]
            cols = [row[1] for row in conn.execute("PRAGMA table_info(memory)")]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertIn("tainté", stored_ddl, f"{self.mk}: scenario setup: the "
                      f"tainté impostor's stored memory DDL must contain the "
                      f"renamed column")
        self.assertNotIn("taint", cols, f"{self.mk}: scenario setup: the tainté "
                         f"impostor must have NO column named taint; found it "
                         f"among {cols!r}")
        self.assertIn("tainté", cols, f"{self.mk}: scenario setup: the tainté "
                      f"impostor must carry the renamed tainté column")
        self.assertEqual(integrity, "ok", f"{self.mk}: scenario setup: the tainté "
                         f"impostor must pass integrity_check ({integrity!r})")

    def _make_kelvin_check_impostor_fixture(self, path):
        """The Kelvin CHECK impostor (critic r5, finding 2): a real
        CLI-built store whose `entity` table is rebuilt with the SAME
        columns (and values -- the entity rows are emptied first, mirroring
        the critic's own fixture, because the broken CHECK rejects every
        insert) but the CHECK's `kind` reference replaced by the QUOTED
        Kelvin form \"Kind\" (U+212A before `ind`).  Python's casefold()
        equates U+212A with ASCII `k`, so a casefold-based token scanner
        accepts the store, while every valid entity insert fails with
        `CHECK constraint failed`.  Scenario-setup proofs (read-only) run
        BEFORE the restore."""
        donor_dir = self.tmp / ("kelvin-donor-" + uuid.uuid4().hex[:8])
        donor_dir.mkdir(parents=True)
        donor = donor_dir / "store.sqlite"
        r = self._cli_in(donor_dir, donor, "add", "--namespace", self.ns, "--type",
                         "fact", "--content", "ac3 kelvin check impostor donor row",
                         "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: the donor "
                         f"store for the Kelvin CHECK impostor could not be built "
                         f"with the public CLI: {(r.stderr or r.stdout).strip()[:300]}")
        with contextlib.closing(sqlite3.connect(str(donor))) as conn:
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                               "AND name='entity'").fetchone()[0]
        self.assertIn("CHECK (kind IN (", ddl, f"{self.mk}: scenario setup: the "
                      f"donor entity DDL must carry the genuine kind CHECK to "
                      f"replace")
        shutil.copyfile(donor, path)
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DELETE FROM entity")
            changed = ddl.replace("CHECK (kind IN (",
                                  'CHECK ("\u212aind" IN (', 1)
            rebuild_ddl = re.sub(r"CREATE TABLE (IF NOT EXISTS )?entity\b",
                                 lambda m: "CREATE TABLE " + (m.group(1) or "")
                                 + "entity_rebuild", changed, count=1)
            cols = [row[1] for row in conn.execute("PRAGMA table_info(entity)")]
            quoted = ", ".join('"%s"' % c for c in cols)
            conn.execute(rebuild_ddl)
            conn.execute(f"INSERT INTO entity_rebuild ({quoted}) "
                         f"SELECT {quoted} FROM entity")
            conn.execute("DROP TABLE entity")
            conn.execute("ALTER TABLE entity_rebuild RENAME TO entity")
            conn.commit()
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            stored_ddl = conn.execute("SELECT sql FROM sqlite_master WHERE "
                                      "type='table' AND name='entity'").fetchone()[0]
            cols = [row[1] for row in conn.execute("PRAGMA table_info(entity)")]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertIn("\u212a", stored_ddl, f"{self.mk}: scenario setup: the Kelvin "
                      f"CHECK impostor's stored entity DDL must contain the Kelvin "
                      f"sign U+212A")
        self.assertEqual(cols, self._fresh_control_columns("entity"), f"{self.mk}: "
                         f"scenario setup: the Kelvin CHECK impostor's entity "
                         f"columns must be unchanged; found {cols!r}")
        self.assertEqual(integrity, "ok", f"{self.mk}: scenario setup: the Kelvin "
                         f"CHECK impostor must pass integrity_check "
                         f"({integrity!r})")

    def _make_long_s_missing_fixture(self, path):
        """The quoted long-s missing-column mutation (critic r5, finding 2):
        a real CLI-built store whose `memory` table is rebuilt with the
        `signal` column renamed to the QUOTED long-s form \"ſignal\"
        (U+017F).  A casefold-based token scanner reads ſ as `s`, so only
        the PRAGMA table_info cross-check can catch the rename.  Scenario
        proofs (read-only) run BEFORE the restore."""
        donor_dir = self.tmp / ("long-s-missing-donor-" + uuid.uuid4().hex[:8])
        donor_dir.mkdir(parents=True)
        donor = donor_dir / "store.sqlite"
        r = self._cli_in(donor_dir, donor, "add", "--namespace", self.ns, "--type",
                         "fact", "--content", "ac3 long-s missing donor row",
                         "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: the donor "
                         f"store for the long-s missing mutation could not be built "
                         f"with the public CLI: "
                         f"{(r.stderr or r.stdout).strip()[:300]}")
        with contextlib.closing(sqlite3.connect(str(donor))) as conn:
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                               "AND name='memory'").fetchone()[0]
            dependents = [row[2] for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='memory' "
                "AND type IN ('index','trigger') AND sql IS NOT NULL")]
        shutil.copyfile(donor, path)
        renamed = re.sub(r"\bsignal\b", '"\u017fignal"', ddl)
        rebuild_ddl = re.sub(r"CREATE TABLE (IF NOT EXISTS )?memory\b",
                             lambda m: "CREATE TABLE " + (m.group(1) or "")
                             + "memory_rebuild", renamed, count=1)
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(memory)")]
            self.assertIn("signal", cols, f"{self.mk}: scenario setup: the donor "
                          f"memory table must carry the signal column")
            conn.execute(rebuild_ddl)
            target = ", ".join('"\u017fignal"' if c == "signal" else c
                               for c in cols)
            conn.execute(f"INSERT INTO memory_rebuild ({target}) "
                         f"SELECT {', '.join(cols)} FROM memory")
            conn.execute("DROP TABLE memory")
            conn.execute("ALTER TABLE memory_rebuild RENAME TO memory")
            for sql_text in dependents:
                conn.execute(sql_text)
            conn.commit()
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            stored_ddl = conn.execute("SELECT sql FROM sqlite_master WHERE "
                                      "type='table' AND name='memory'").fetchone()[0]
            cols = [row[1] for row in conn.execute("PRAGMA table_info(memory)")]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertIn("\u017fignal", stored_ddl, f"{self.mk}: scenario setup: the "
                      f"long-s missing mutation's stored DDL must contain the "
                      f"quoted long-s column")
        self.assertNotIn("signal", cols, f"{self.mk}: scenario setup: the long-s "
                         f"missing mutation must have NO column named signal; "
                         f"found it among {cols!r}")
        self.assertIn("\u017fignal", cols, f"{self.mk}: scenario setup: the long-s "
                      f"missing mutation must carry the long-s column")
        self.assertEqual(integrity, "ok", f"{self.mk}: scenario setup: the long-s "
                         f"missing mutation must pass integrity_check "
                         f"({integrity!r})")

    def _make_long_s_extra_fixture(self, path):
        """The quoted long-s EXTRA-column mutation (critic r5, finding 2):
        a real CLI-built store whose `memory` table is rebuilt with all
        genuine columns PLUS an extra column named with the quoted long-s
        form \"ſignal\" (U+017F) -- a token scanner casefolds it onto the
        genuine `signal` and sees only genuine columns.  Scenario proofs
        (read-only) run BEFORE the restore."""
        donor_dir = self.tmp / ("long-s-extra-donor-" + uuid.uuid4().hex[:8])
        donor_dir.mkdir(parents=True)
        donor = donor_dir / "store.sqlite"
        r = self._cli_in(donor_dir, donor, "add", "--namespace", self.ns, "--type",
                         "fact", "--content", "ac3 long-s extra donor row",
                         "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: the donor "
                         f"store for the long-s extra mutation could not be built "
                         f"with the public CLI: "
                         f"{(r.stderr or r.stdout).strip()[:300]}")
        with contextlib.closing(sqlite3.connect(str(donor))) as conn:
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                               "AND name='memory'").fetchone()[0]
            dependents = [row[2] for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='memory' "
                "AND type IN ('index','trigger') AND sql IS NOT NULL")]
        shutil.copyfile(donor, path)
        close = ddl.rfind(")")
        extra_ddl = (ddl[:close].rstrip()
                     + ",\n    \"\u017fignal\" TEXT NOT NULL DEFAULT ''\n)")
        rebuild_ddl = re.sub(r"CREATE TABLE (IF NOT EXISTS )?memory\b",
                             lambda m: "CREATE TABLE " + (m.group(1) or "")
                             + "memory_rebuild", extra_ddl, count=1)
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(memory)")]
            conn.execute(rebuild_ddl)
            both = ", ".join(cols)
            conn.execute(f"INSERT INTO memory_rebuild ({both}) "
                         f"SELECT {both} FROM memory")
            conn.execute("DROP TABLE memory")
            conn.execute("ALTER TABLE memory_rebuild RENAME TO memory")
            for sql_text in dependents:
                conn.execute(sql_text)
            conn.commit()
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            stored_ddl = conn.execute("SELECT sql FROM sqlite_master WHERE "
                                      "type='table' AND name='memory'").fetchone()[0]
            cols = [row[1] for row in conn.execute("PRAGMA table_info(memory)")]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertIn('"\u017fignal"', stored_ddl, f"{self.mk}: scenario setup: "
                      f"the long-s extra mutation's stored DDL must contain the "
                      f"quoted long-s column")
        extra = sorted(set(cols) - set(self._fresh_control_columns("memory")))
        self.assertEqual(extra, ["\u017fignal"], f"{self.mk}: scenario setup: the "
                         f"long-s extra mutation must list exactly one column no "
                         f"fresh store has (the long-s column); found {extra!r}")
        self.assertIn("signal", cols, f"{self.mk}: scenario setup: the long-s "
                      f"extra mutation must keep the genuine signal column too")
        self.assertEqual(integrity, "ok", f"{self.mk}: scenario setup: the long-s "
                         f"extra mutation must pass integrity_check "
                         f"({integrity!r})")

    def _make_single_quoted_key_fixture(self, path):
        """The single-quoted-key store (critic r5, finding C): a real
        CLI-built store whose `memory` table is rebuilt with every column
        definition's KEY written in single quotes ('id', 'namespace', ...)
        -- SQLite resolves a single-quoted name in a column-key position as
        the identifier, so the store is semantically identical to the
        genuine definition and must be ACCEPTED.  Rows copied, genuine
        triggers and indexes recreated, stamp untouched.  Scenario proofs
        (read-only, plus a scratch-copy insert) run BEFORE the restore."""
        donor_dir = self.tmp / ("quoted-key-donor-" + uuid.uuid4().hex[:8])
        donor_dir.mkdir(parents=True)
        donor = donor_dir / "store.sqlite"
        r = self._cli_in(donor_dir, donor, "add", "--namespace", self.ns, "--type",
                         "fact", "--content", "ac3 single quoted key donor row",
                         "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: the donor "
                         f"store for the single-quoted-key fixture could not be "
                         f"built with the public CLI: "
                         f"{(r.stderr or r.stdout).strip()[:300]}")
        with contextlib.closing(sqlite3.connect(str(donor))) as conn:
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                               "AND name='memory'").fetchone()[0]
            dependents = [row[2] for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='memory' "
                "AND type IN ('index','trigger') AND sql IS NOT NULL")]
        keywords = {"check", "primary", "unique", "foreign", "constraint",
                    "not", "default", "references", "create", "table"}
        quoted_lines = []
        for line in ddl.splitlines(True):
            stripped = line.lstrip()
            if stripped.startswith("--"):
                quoted_lines.append(line)
                continue
            match = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\b", line)
            if match and match.group(2).lower() not in keywords:
                line = (match.group(1) + "'" + match.group(2) + "'"
                        + line[len(match.group(1)) + len(match.group(2)):])
            quoted_lines.append(line)
        quoted_ddl = "".join(quoted_lines)
        rebuild_ddl = re.sub(r"CREATE TABLE (IF NOT EXISTS )?memory\b",
                             lambda m: "CREATE TABLE " + (m.group(1) or "")
                             + "memory_rebuild", quoted_ddl, count=1)
        shutil.copyfile(donor, path)
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(memory)")]
            conn.execute(rebuild_ddl)
            both = ", ".join(cols)
            conn.execute(f"INSERT INTO memory_rebuild ({both}) "
                         f"SELECT {both} FROM memory")
            conn.execute("DROP TABLE memory")
            conn.execute("ALTER TABLE memory_rebuild RENAME TO memory")
            for sql_text in dependents:
                conn.execute(sql_text)
            conn.commit()
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            stored_ddl = conn.execute("SELECT sql FROM sqlite_master WHERE "
                                      "type='table' AND name='memory'").fetchone()[0]
            cols = [row[1] for row in conn.execute("PRAGMA table_info(memory)")]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertIn("'namespace'", stored_ddl, f"{self.mk}: scenario setup: the "
                      f"single-quoted-key store's DDL must write the column keys "
                      f"in single quotes")
        self.assertIn("'id'", stored_ddl, f"{self.mk}: scenario setup: the "
                      f"single-quoted-key store's DDL must quote every column key")
        self.assertEqual(cols, self._fresh_control_columns("memory"), f"{self.mk}: "
                         f"scenario setup: the single-quoted-key store's columns "
                         f"must be exactly the fresh set; found {cols!r}")
        self.assertEqual(integrity, "ok", f"{self.mk}: scenario setup: the "
                         f"single-quoted-key store must pass integrity_check "
                         f"({integrity!r})")
        proof_dir = self.tmp / ("quoted-key-proof-" + uuid.uuid4().hex[:8])
        proof_dir.mkdir(parents=True)
        proof = proof_dir / "proof.sqlite"
        shutil.copyfile(path, proof)
        with contextlib.closing(sqlite3.connect(str(proof))) as conn:
            conn.execute("INSERT INTO memory (id, namespace, type, content, "
                         "ingestion_ts, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                         ("quoted-key-proof", self.ns, "fact",
                          "ac3 direct insert into the quoted-key table",
                          "2026-11-25T00:00:00Z", 0.9))
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        self.assertEqual(count, 2, f"{self.mk}: scenario setup: a direct SQL "
                         f"insert into the single-quoted-key store must work "
                         f"(found {count} rows after the insert)")

    def _make_nbsp_trigger_fixture(self, path):
        """The memory_au U+00A0 impostor (final-gate finding D1): a real
        CLI-built store whose genuine memory_au trigger is re-created from
        the donor's stored SQL VERBATIM except for exactly ONE insertion of
        U+00A0 (no-break space) immediately after `new.content` (the
        critic's probe's occurrence: the comma-terminated one in the INSERT
        list).  SQLite treats U+00A0 as an IDENTIFIER character, so the
        poisoned reference reads as the column `new.content\\xa0`: the
        insert/delete round trip (which never fires memory_au) passes, but
        every UPDATE of memory fails.  Scenario proofs (read-only, plus a
        scratch-copy round trip) run BEFORE the restore."""
        donor_dir = self.tmp / ("nbsp-trigger-donor-" + uuid.uuid4().hex[:8])
        donor_dir.mkdir(parents=True)
        donor = donor_dir / "store.sqlite"
        r = self._cli_in(donor_dir, donor, "add", "--namespace", self.ns, "--type",
                         "fact", "--content", "ac3 nbsp trigger donor row",
                         "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: the donor "
                         f"store for the U+00A0 trigger impostor could not be built "
                         f"with the public CLI: {(r.stderr or r.stdout).strip()[:300]}")
        with contextlib.closing(sqlite3.connect(str(donor))) as conn:
            genuine_au = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND "
                "name='memory_au'").fetchone()
        self.assertTrue(genuine_au and genuine_au[0], f"{self.mk}: scenario "
                        f"setup: the donor store must carry the genuine memory_au "
                        f"trigger to poison")
        poisoned = genuine_au[0].replace("new.content,", "new.content\u00a0,", 1)
        self.assertNotEqual(poisoned, genuine_au[0], f"{self.mk}: scenario setup: "
                            f"the donor memory_au SQL must contain the comma-"
                            f"terminated new.content reference to poison")
        shutil.copyfile(donor, path)
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            conn.execute("DROP TRIGGER memory_au")
            conn.execute(poisoned)
            conn.commit()
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            stored_au = conn.execute("SELECT sql FROM sqlite_master WHERE "
                                     "type='trigger' AND "
                                     "name='memory_au'").fetchone()[0]
            fixture_master = {(row[0], row[1]) for row in conn.execute(
                "SELECT type, name FROM sqlite_master")}
            memory_ids = {row[0] for row in conn.execute("SELECT rowid FROM memory")}
            docsize_ids = {row[0] for row in conn.execute(
                "SELECT rowid FROM memory_fts_docsize")}
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertIn("\u00a0", stored_au, f"{self.mk}: scenario setup: the "
                      f"poisoned memory_au SQL must contain the U+00A0 character")
        missing = sorted(self._fresh_control_master() - fixture_master)
        self.assertEqual(missing, [], f"{self.mk}: scenario setup: the U+00A0 "
                         f"trigger impostor must keep every object name a fresh "
                         f"store has; missing {missing!r}")
        self.assertEqual(docsize_ids, memory_ids, f"{self.mk}: scenario setup: "
                         f"the U+00A0 trigger impostor's memory_fts_docsize "
                         f"rowids must equal memory's rowids")
        self.assertEqual(integrity, "ok", f"{self.mk}: scenario setup: the "
                         f"U+00A0 trigger impostor must pass integrity_check "
                         f"({integrity!r})")
        proof_dir = self.tmp / ("nbsp-roundtrip-" + uuid.uuid4().hex[:8])
        proof_dir.mkdir(parents=True)
        proof = proof_dir / "proof.sqlite"
        shutil.copyfile(path, proof)
        with contextlib.closing(sqlite3.connect(str(proof))) as conn:
            conn.execute("BEGIN")
            conn.execute("INSERT INTO memory (id, namespace, type, content, "
                         "ingestion_ts, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                         ("nbsp-roundtrip", self.ns, "fact",
                          "nbsproundtriptoken", "2026-11-25T00:00:00Z", 0.9))
            found = conn.execute("SELECT rowid FROM memory_fts WHERE "
                                 "memory_fts MATCH 'nbsproundtriptoken'"
                                 ).fetchall()
            conn.execute("DELETE FROM memory WHERE id='nbsp-roundtrip'")
            gone = conn.execute("SELECT rowid FROM memory_fts WHERE "
                                "memory_fts MATCH 'nbsproundtriptoken'"
                                ).fetchall()
            conn.execute("ROLLBACK")
        self.assertTrue(found and not gone, f"{self.mk}: scenario setup: the "
                         f"insert-then-delete round trip through the triggers "
                         f"must SUCCEED on the U+00A0 impostor (found {found!r}, "
                         f"gone {gone!r}) -- it never fires memory_au, which is "
                         f"exactly why the round trip cannot catch this impostor")

    def _make_ideographic_meta_fixture(self, path):
        """The U+3000 meta PRIMARY KEY impostor (final-gate finding D1,
        sibling the critic also proved): a real CLI-built store whose
        `meta` table is rebuilt with `PRIMARY KEY` written as
        `PRIMARY\\u3000KEY` (ideographic space -- an identifier character to
        SQLite, whitespace to a Unicode-aware scanner).  The PRIMARY KEY
        vanishes (the fragment becomes part of the declared type), so
        duplicate meta keys are accepted and the production UPSERT fails.
        Scenario proofs (read-only, plus a scratch-copy duplicate insert)
        run BEFORE the restore."""
        donor_dir = self.tmp / ("ideographic-meta-donor-" + uuid.uuid4().hex[:8])
        donor_dir.mkdir(parents=True)
        donor = donor_dir / "store.sqlite"
        r = self._cli_in(donor_dir, donor, "add", "--namespace", self.ns, "--type",
                         "fact", "--content", "ac3 ideographic meta donor row",
                         "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: the donor "
                         f"store for the U+3000 meta impostor could not be built "
                         f"with the public CLI: {(r.stderr or r.stdout).strip()[:300]}")
        with contextlib.closing(sqlite3.connect(str(donor))) as conn:
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                               "AND name='meta'").fetchone()[0]
        self.assertIn("PRIMARY KEY", ddl, f"{self.mk}: scenario setup: the donor "
                      f"meta DDL must carry the PRIMARY KEY constraint to poison")
        poisoned = ddl.replace("PRIMARY KEY", "PRIMARY\u3000KEY", 1)
        rebuild_ddl = re.sub(r"CREATE TABLE (IF NOT EXISTS )?meta\b",
                             lambda m: "CREATE TABLE " + (m.group(1) or "")
                             + "meta_rebuild", poisoned, count=1)
        shutil.copyfile(donor, path)
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(meta)")]
            quoted = ", ".join('"%s"' % c for c in cols)
            conn.execute(rebuild_ddl)
            conn.execute(f"INSERT INTO meta_rebuild ({quoted}) "
                         f"SELECT {quoted} FROM meta")
            conn.execute("DROP TABLE meta")
            conn.execute("ALTER TABLE meta_rebuild RENAME TO meta")
            conn.commit()
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            stored_ddl = conn.execute("SELECT sql FROM sqlite_master WHERE "
                                      "type='table' AND name='meta'").fetchone()[0]
            key_pk = [row[5] for row in conn.execute("PRAGMA table_info(meta)")
                      if row[1] == "key"]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertIn("\u3000", stored_ddl, f"{self.mk}: scenario setup: the "
                      f"U+3000 meta impostor's stored DDL must contain the "
                      f"ideographic space")
        self.assertEqual(key_pk, [0], f"{self.mk}: scenario setup: the U+3000 "
                         f"meta impostor's key column must NO LONGER be the "
                         f"PRIMARY KEY (pk flag {key_pk!r}) -- the constraint "
                         f"was absorbed into the declared type")
        self.assertEqual(integrity, "ok", f"{self.mk}: scenario setup: the "
                         f"U+3000 meta impostor must pass integrity_check "
                         f"({integrity!r})")
        proof_dir = self.tmp / ("ideographic-dup-" + uuid.uuid4().hex[:8])
        proof_dir.mkdir(parents=True)
        proof = proof_dir / "proof.sqlite"
        shutil.copyfile(path, proof)
        with contextlib.closing(sqlite3.connect(str(proof))) as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES "
                         "('schema_version', 'duplicate')")
            conn.commit()
            duplicates = conn.execute(
                "SELECT COUNT(*) FROM meta WHERE "
                "key='schema_version'").fetchone()[0]
        self.assertEqual(duplicates, 2, f"{self.mk}: scenario setup: a duplicate "
                         f"meta key must be ACCEPTED by the U+3000 meta impostor "
                         f"(found {duplicates} schema_version rows) -- the "
                         f"PRIMARY KEY is gone, which is the trap")

    def _master_objects(self, path):
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            return {(row[0], row[1]) for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}

    def _fresh_control_store(self):
        """A store built by the public CLI in this same environment, built
        once per test and cached (the comparison oracle for upgraded stores
        and for same-name impostor objects)."""
        if getattr(self, "_fresh_store_path", None) is None:
            fresh_dir = self.tmp / "fresh-control"
            fresh_dir.mkdir(parents=True, exist_ok=True)
            fresh_store = fresh_dir / "store.sqlite"
            r = self._cli_in(fresh_dir, fresh_store, "add", "--namespace", self.ns,
                             "--type", "fact", "--content", "fresh control store row",
                             "--signal", "test", "--json")
            self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: building the "
                             f"fresh control store failed: "
                             f"{(r.stderr or r.stdout).strip()[:300]}")
            self._fresh_store_path = fresh_store
        return self._fresh_store_path

    def _fresh_control_master(self):
        """sqlite_master objects of the fresh control store."""
        return self._master_objects(self._fresh_control_store())

    def _fresh_control_sql(self, name):
        """The whitespace-collapsed, case-folded sqlite_master SQL text the
        fresh control store carries for `name` (None when absent)."""
        with contextlib.closing(sqlite3.connect(str(self._fresh_control_store()))) as conn:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE name=?",
                               (name,)).fetchone()
        return re.sub(r"\s+", " ", row[0] or "").strip().casefold() if row else None

    def _fresh_control_columns(self, table):
        """The column-name list the fresh control store's `table` carries
        (the comparison oracle for impostor column sets)."""
        with contextlib.closing(sqlite3.connect(str(self._fresh_control_store()))) as conn:
            return [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]

    def _leg_legacy(self, tag, version, interrupted, fresh_master):
        rows_pair = (f"ac3 legacy {tag} row one", f"ac3 legacy {tag} row two")
        self._reset_live(self.CONTROLS)
        fixture = self.tmp / f"legacy-{tag}.sqlite"
        if interrupted:
            self._make_interrupted_v3_fixture(fixture, rows_pair)
        else:
            self._make_legacy_fixture(fixture, rows_pair, version)
        r = self._restore_cli(fixture)
        self.assertEqual(r.returncode, 0, f"{self.mk} [legacy {tag}]: a genuine legacy "
                         f"fixture must be accepted and upgraded, but restore exited "
                         f"{r.returncode}; stderr {r.stderr.strip()[:300]!r}")
        rows = self._live_rows()
        self.assertEqual(sorted(rows), sorted(rows_pair), f"{self.mk} [legacy {tag}]: the "
                         f"upgraded store must hold exactly the fixture's two rows; live "
                         f"rows now {rows!r}")
        stamp = self._read_live_stamp()
        self.assertEqual(str(stamp), str(SUPPORTED_SCHEMA_VERSION),
                         f"{self.mk} [legacy {tag}]: the fixture was not upgraded to "
                         f"schema {SUPPORTED_SCHEMA_VERSION}; stamp now {stamp!r}")
        with contextlib.closing(sqlite3.connect(str(self.store))) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
        missing = [c for c in _V1_CORE_COLUMNS if c not in cols]
        self.assertEqual(missing, [], f"{self.mk} [legacy {tag}]: the upgraded memory "
                         f"table lacks the v1 core columns {missing}")
        restored_master = self._master_objects(self.store)
        absent = fresh_master - restored_master
        self.assertEqual(absent, set(), f"{self.mk} [legacy {tag}]: the upgraded store's "
                         f"sqlite_master is missing objects a fresh store has: "
                         f"{sorted(absent)!r}")
        self._add(f"ac3 post-{tag} upgrade write", what=f"the write after the {tag} upgrade")
        rows = self._live_rows()
        self.assertEqual(sorted(rows), sorted(rows_pair + (f"ac3 post-{tag} upgrade write",)),
                         f"{self.mk} [legacy {tag}]: a write after the upgrade did not land "
                         f"beside the upgraded rows; live rows now {rows!r}")

    def test_staged_validation(self):
        """RED at base, GREEN after the fix: foreign SQLite, falsely stamped
        schemas, non-databases, bad stamps, a falsely current store (all
        fourteen v1 columns, stamped current, but missing the columns later
        migrations add), a falsely current side table (memory_expiry rebuilt
        down to memory_id alone, stamped current), an FTS impostor
        (memory_fts swapped for a standalone fts5 table with no
        external-content binding), a same-name index with different SQL
        (idx_memory_live without its partial WHERE clause), a same-name
        trigger with different SQL (memory_ai that does not index), the
        tainté impostor (memory rebuilt with its taint column renamed to
        the non-ASCII tainté), the Kelvin CHECK impostor (entity's kind
        CHECK reference in the quoted Kelvin form) and the two quoted
        long-s mutations (signal renamed to the quoted long-s form, or an
        extra quoted long-s column), the memory_au U+00A0 impostor (one
        no-break space after new.content) and the U+3000 meta impostor
        (PRIMARY KEY written with an ideographic space) are
        refused before any replacement (an os.replace spy on the live store
        path records zero calls), while legitimate older backups and genuine
        legacy fixtures (v1/v2/v3 and a store killed inside the v3
        migration window) are upgraded, a two-row stale-index store is
        ACCEPTED with its search index healed, a single-quoted-key store is
        ACCEPTED as-is, and a newer-than-supported
        store is refused with the live store kept."""
        self.mk = "AC3-504"
        with self.subTest(control="legitimate older backup is accepted and upgraded"):
            self._reset_live(self.CONTROLS)
            snap = self._take_backup()
            self._restamp(snap, SUPPORTED_SCHEMA_VERSION - 1)
            r = self._restore_cli(snap)
            self.assertEqual(r.returncode, 0, f"{self.mk} [control]: a legitimate backup "
                             f"one schema version older was refused: exit {r.returncode}, "
                             f"stderr {r.stderr.strip()[:200]}")
            rows = self._live_rows()
            for control in self.CONTROLS:
                self.assertIn(control, rows, f"{self.mk} [control]: upgrading the older "
                              f"backup lost row {control!r}; live rows {rows!r}")
            stamp = self._read_live_stamp()
            self.assertEqual(str(stamp), str(SUPPORTED_SCHEMA_VERSION),
                             f"{self.mk} [control]: the older backup was not upgraded to "
                             f"schema {SUPPORTED_SCHEMA_VERSION}; stamp now {stamp!r}")
            self._add("ac3 post-upgrade write", what="the write after the upgrade")
            self.assertEqual(len(self._live_rows()), len(self.CONTROLS) + 1,
                             f"{self.mk} [control]: a write after the upgrade did not land "
                             f"beside the upgraded rows; live rows now {self._live_rows()!r}")
        with self.subTest(control="newer-than-supported stamp is refused"):
            self._reset_live(self.CONTROLS)
            snap = self._take_backup()
            self._restamp(snap, SUPPORTED_SCHEMA_VERSION + 100)
            r = self._restore_cli(snap)
            self.assertNotEqual(r.returncode, 0, f"{self.mk} [control]: a store stamped "
                                f"newer than this client supports was accepted (exit 0)")
            self._assert_live_preserved("refusing the newer-than-supported store",
                                        f"restore exit {r.returncode}", "ac3 post-refusal write")
        cases = (
            ("a foreign SQLite database", _make_foreign_db),
            ("a wrong-shape memory table stamped 1",
             lambda p: _make_wrong_shape_db(p, 1)),
            ("the same wrong-shape table stamped as fully current",
             lambda p: _make_wrong_shape_db(p, SUPPORTED_SCHEMA_VERSION)),
            ("a plain text file that is not a SQLite database", _make_text_file),
            ("core columns with a non-integer schema_version",
             lambda p: _make_core_fixture(p, "not-a-number")),
            ("core columns with schema_version 0", lambda p: _make_core_fixture(p, "0")),
            ("core columns with a negative schema_version",
             lambda p: _make_core_fixture(p, "-2")),
            ("core columns with no schema_version row at all",
             lambda p: _make_core_fixture(p, None)),
            ("a falsely current store: all fourteen v1 columns, stamped "
             "current, but missing the columns later migrations add",
             self._make_falsely_current_fixture),
            ("a falsely current side table: memory_expiry rebuilt down to "
             "memory_id alone, stamped current",
             self._make_falsely_current_side_table_fixture),
            ("an FTS impostor: memory_fts swapped for a standalone fts5 "
             "table with no external-content binding",
             self._make_fts_impostor_fixture),
            ("a same-name index with different SQL: idx_memory_live without "
             "its partial-index WHERE clause",
             self._make_impostor_index_fixture),
            ("a same-name trigger with different SQL: memory_ai that does "
             "not insert into memory_fts",
             self._make_impostor_trigger_fixture),
            ("the tainté impostor: memory rebuilt with its taint column "
             "renamed to the non-ASCII tainté",
             self._make_taint_impostor_fixture),
            ("the Kelvin CHECK impostor: entity rebuilt with its kind CHECK "
             "reference in the quoted Kelvin form",
             self._make_kelvin_check_impostor_fixture),
            ("the quoted long-s missing-column mutation: memory rebuilt "
             "with signal renamed to the quoted long-s form",
             self._make_long_s_missing_fixture),
            ("the quoted long-s extra-column mutation: memory rebuilt with "
             "an extra quoted long-s column",
             self._make_long_s_extra_fixture),
            ("the memory_au U+00A0 impostor: the genuine update trigger "
             "re-created with one no-break space after new.content",
             self._make_nbsp_trigger_fixture),
            ("the U+3000 meta impostor: meta rebuilt with PRIMARY KEY "
             "written with an ideographic space",
             self._make_ideographic_meta_fixture),
        )
        for index, (name, build) in enumerate(cases):
            with self.subTest(invalid_input=name):
                self._reset_live(self.CONTROLS)
                bad = self.tmp / ("bad-input-%d.sqlite" % index)
                build(bad)
                before = self._digest(self.store)
                result = self._restore_inprocess_spy(bad)
                self.assertIsNone(result["raised"], f"{self.mk}: restore let {name} crash "
                                 f"cmd_restore with {result['raised']!r} instead of "
                                 f"refusing it with a controlled non-zero exit")
                self.assertEqual(result["replacements"], 0, f"{self.mk}: restore replaced "
                                 f"the live store with {name} before refusing it: the "
                                 f"os.replace spy recorded {result['replacements']} call(s) "
                                 f"whose destination is the live store path")
                self.assertIsInstance(result["rc"], int, f"{self.mk}: cmd_restore returned "
                                 f"{result['rc']!r} instead of an int exit code for {name}")
                self.assertNotEqual(result["rc"], 0, f"{self.mk}: restore accepted {name} "
                                    f"(exit 0)")
                self.assertEqual(self._digest(self.store), before, f"{self.mk}: refusing "
                                 f"{name} changed the destination main file's bytes")
                leftovers = (sorted(p.name for p in self.backup_dir.glob("prerestore*"))
                             if self.backup_dir.is_dir() else [])
                self.assertEqual(leftovers, [], f"{self.mk}: refusing {name} still left "
                                 f"pre-restore safety copies behind: {leftovers!r}")
                self._assert_live_preserved(name, f"restore exit {result['rc']}",
                                            "ac3 later write")
        fresh_master = self._fresh_control_master()
        with self.subTest(legacy="v1 fixture of the initial commit is upgraded"):
            self._leg_legacy("v1", 1, False, fresh_master)
        with self.subTest(legacy="v2 fixture is upgraded"):
            self._leg_legacy("v2", 2, False, fresh_master)
        with self.subTest(legacy="v3 fixture is upgraded"):
            self._leg_legacy("v3", 3, False, fresh_master)
        with self.subTest(legacy="store killed inside the v3 migration window is upgraded"):
            self._leg_legacy("interrupted-v3", None, True, fresh_master)
        with self.subTest(heal="a two-row stale-index store is accepted and its "
                              "search index healed"):
            self._leg_stale_index_heal()
        with self.subTest(accept="a single-quoted-key store is accepted as-is"):
            self._leg_single_quoted_key_accept()

    def _leg_single_quoted_key_accept(self):
        """ACCEPT guard leg (critic r5, finding C): a store whose memory
        definition writes every column key in single quotes is
        SEMANTICALLY IDENTICAL to the genuine definition (SQLite resolves a
        single-quoted name in a column-key position as the identifier), so
        the restore must ACCEPT it, preserve the rows, and leave the store
        usable -- refusing it would be a false positive."""
        row_one = "ac3 single quoted key donor row"
        self._reset_live(self.CONTROLS)
        fixture = self.tmp / "single-quoted-key.sqlite"
        self._make_single_quoted_key_fixture(fixture)
        r = self._restore_cli(fixture)
        self.assertEqual(r.returncode, 0, f"{self.mk} [quoted-key]: a store whose "
                         f"column keys are written in single quotes is semantically "
                         f"identical to the genuine definition and must be "
                         f"ACCEPTED, not refused; restore exited {r.returncode}; "
                         f"stderr {r.stderr.strip()[:300]!r}")
        rows = self._live_rows()
        self.assertEqual(sorted(rows), sorted([row_one]), f"{self.mk} "
                         f"[quoted-key] the accepted store must preserve its rows; "
                         f"live rows now {rows!r}")
        self._add("ac3 post-quoted-key write",
                  what="the write after the quoted-key restore")
        rows = self._live_rows()
        self.assertEqual(sorted(rows),
                         sorted([row_one, "ac3 post-quoted-key write"]), f"{self.mk} "
                         f"[quoted-key] a write after the accepted restore did not "
                         f"land beside the preserved rows; live rows now {rows!r}")

    def _leg_stale_index_heal(self):
        """ACCEPT-with-heal leg (critic r4 finding 1, reworked per critic r5
        finding 1): a store whose definitions are all genuine but whose
        derived search-index data is stale (a rewording applied while the
        update trigger was dropped, then the genuine trigger restored) must
        be ACCEPTED and its index healed -- not refused, and not installed
        verbatim with the stale row still unfindable.  The number of
        rebuilds is not honestly observable from index state (a rebuild is
        idempotent), so the leg asserts the healed END STATE that only a
        re-index from current contents produces: the reworded row's new
        first eligible term finds it, and its OLD term no longer does."""
        row_one = "sharedtoken control row one"
        row_two_original = "sharedtoken second row two"
        row_two_new = "x zurich reworded second row"
        self._reset_live(self.CONTROLS)
        fixture = self.tmp / "stale-index.sqlite"
        self._make_stale_index_fixture(fixture, row_one, row_two_original,
                                       row_two_new)
        r = self._restore_cli(fixture)
        self.assertEqual(r.returncode, 0, f"{self.mk} [stale-index]: a store with "
                         f"genuine definitions and only stale derived index data "
                         f"must be ACCEPTED and healed, not refused; restore exited "
                         f"{r.returncode}; stderr {r.stderr.strip()[:300]!r}")
        with contextlib.closing(sqlite3.connect(str(self.store))) as conn:
            zurich_hits = {row[0] for row in conn.execute(
                "SELECT rowid FROM memory_fts WHERE memory_fts MATCH 'zurich'")}
            shared_hits = {row[0] for row in conn.execute(
                "SELECT rowid FROM memory_fts WHERE "
                "memory_fts MATCH 'sharedtoken'")}
            by_content = {row[1]: row[0] for row in conn.execute(
                "SELECT rowid, content FROM memory")}
        self.assertEqual(zurich_hits, {by_content[row_two_new]}, f"{self.mk} "
                         f"[stale-index]: the restore installed the store without "
                         f"healing its stale search index: MATCH 'zurich' "
                         f"{sorted(zurich_hits)!r} instead of the reworded second "
                         f"row -- a restore of a store with genuine definitions "
                         f"must re-index derived data so the current content is "
                         f"findable")
        self.assertEqual(shared_hits, {by_content[row_one]}, f"{self.mk} "
                         f"[stale-index]: the heal left the reworded row's OLD "
                         f"term in the index: MATCH 'sharedtoken' "
                         f"{sorted(shared_hits)!r} must find only the first row -- "
                         f"the reworded second row no longer contains that term, "
                         f"so only stale index data could still match it")
        rows = self._live_rows()
        self.assertEqual(sorted(rows), sorted([row_one, row_two_new]), f"{self.mk} "
                         f"[stale-index] the accepted stale-index store must keep "
                         f"both rows (the control row survives); live rows now "
                         f"{rows!r}")
        self._add("ac3 post-stale-index write",
                  what="the write after the stale-index heal")
        rows = self._live_rows()
        self.assertEqual(sorted(rows),
                         sorted([row_one, row_two_new, "ac3 post-stale-index write"]),
                         f"{self.mk} [stale-index] a write after the healed restore "
                         f"did not land beside the healed rows; live rows now "
                         f"{rows!r}")


class _RestoreFailureRecoveryCase(_RestoreAuditBase):
    """AC2 (#504): a post-staging failure rolls the live store back to what
    SQLite read from the destination before the restore (main file plus the
    planted sidecar's committed frames) and preserves the on-disk bytes
    forensically; the companion legs (wal-only write, pre-swap faults,
    missing/empty destination) run from here too."""

    def test_recovery_preserves_later_write(self):
        """RED at base, GREEN after the fix: when restore fails after
        staging the replacement, the failure is reported as a controlled
        non-zero return with a 'rolled back' line naming the pre-restore
        safety copy, the live store is put back to exactly what SQLite read
        from the destination before the restore (the older and newer rows
        plus the planted stale -wal's committed ghost row), no sidecar,
        staged copy or stash is left beside the store (checked before
        anything opens the store), the on-disk files survive byte-for-byte
        in a restore-forensic-* directory named by a 'removed stale ...
        set aside' line, the safety copy opens alone and shows those rows,
        the operator's later write lands on top, and the companion-file
        legs S2-S6 hold as well.

        Amended 2026-11-23 to the decided rollback policy; the earlier
        checkpoint-only target these legs encoded (the ghost row must not
        be live) was CHECK_WRONG -- the acceptance criterion never stated
        it."""
        self.mk = "AC4-504"
        with self.subTest(leg="S1: stale -wal and a failure right after the swap"):
            older = "ac4 older row present in the backup"
            newer = "ac4 newer row written after the backup"
            third = "ac4 third row written after the failed restore"
            ghost = "ac4 ghost row that only a replayed stale -wal could add"
            self._add(older, setup=True)
            snap = self._take_backup()
            self._add(newer, setup=True)
            stale_wal = self._build_stale_wal(ghost)
            rows = self._live_rows()
            self.assertEqual(sorted(rows), sorted([older, newer]), f"{self.mk}: scenario "
                             f"setup: the live control before the failed restore must hold "
                             f"exactly the older and newer rows; live rows {rows!r}")
            # Plant the stale WAL before the restore so it spans the failed
            # restore and the later write (planting it afterwards would be
            # unsatisfiable: SQLite replays a valid WAL on the very next open).
            Path(str(self.store) + "-wal").write_bytes(stale_wal)
            # Scenario-setup proof of what SQLite reads from this state: a
            # normal open of a scratch copy of (main + planted -wal) sees the
            # ghost row too; an immutable open of the main file alone does not.
            proof_dir = self.tmp / ("s1-proof-" + uuid.uuid4().hex[:8])
            proof_dir.mkdir(parents=True)
            pair = proof_dir / "pair.sqlite"
            shutil.copyfile(self.store, pair)
            Path(str(pair) + "-wal").write_bytes(stale_wal)
            self.assertEqual(sorted(self._rows_at(pair)),
                             sorted([older, newer, ghost]), f"{self.mk}: scenario setup: "
                             f"a normal SQLite open of the main file plus the planted "
                             f"stale -wal must read exactly the older, newer and ghost "
                             f"rows; rows {self._rows_at(pair)!r}")
            solo = proof_dir / "solo.sqlite"
            shutil.copyfile(self.store, solo)
            self.assertEqual(sorted(self._immutable_rows(solo)),
                             sorted([older, newer]), f"{self.mk}: scenario setup: an "
                             f"immutable open of the main file alone must read exactly "
                             f"the older and newer rows; rows "
                             f"{self._immutable_rows(solo)!r}")

            result = self._fail_restore_after_staging(str(snap))
            self._assert_no_sidecars_or_stashes("S1")
            self.assertIn("restore FAILED", result["err"], f"{self.mk}: a restore that "
                            f"failed after staging did not print 'restore FAILED' on "
                            f"stderr; stderr tail {result['err'][-300:]!r}")
            safety_copy = self._require_rollback_line(result)
            rows = self._live_rows()
            self.assertEqual(sorted(rows), sorted([older, newer, ghost]), f"{self.mk}: "
                             f"the failed restore did not put the live store back to "
                             f"exactly what SQLite read from the destination before the "
                             f"restore (main file plus the planted -wal's committed "
                             f"frames); live rows now {rows!r}")
            live_wal = Path(str(self.store) + "-wal")
            wal_clash = (live_wal.exists()
                         and _digest_bytes(live_wal.read_bytes())
                         == _digest_bytes(stale_wal))
            self.assertFalse(wal_clash, f"{self.mk}: after the rollback a -wal "
                            f"byte-identical to the planted stale log still sits beside "
                            f"the live store; the rollback target already carries its "
                            f"committed frames, so the planted log must be gone from "
                            f"beside the store")
            alone_dir = self.tmp / ("s1-alone-" + uuid.uuid4().hex[:8])
            alone_dir.mkdir(parents=True)
            alone = alone_dir / "safety-copy-alone.sqlite"
            shutil.copyfile(safety_copy, alone)
            self.assertEqual(sorted(self._rows_at(alone)),
                             sorted([older, newer, ghost]), f"{self.mk}: the "
                             f"pre-restore safety copy must be the store as SQLite read "
                             f"it: a scratch copy opened alone, with no sidecars, must "
                             f"read exactly the older, newer and ghost rows; rows "
                             f"{self._rows_at(alone)!r}")
            set_aside = self._require_set_aside_copy(result, stale_wal)
            forensic_main = set_aside.parent / self.store.name
            self.assertTrue(forensic_main.is_file(), f"{self.mk}: the forensic "
                            f"directory must keep the pre-restore main file under its "
                            f"unchanged name {self.store.name!r} beside its sidecar "
                            f"copies; {forensic_main} is missing")
            self.assertEqual(sorted(self._immutable_rows(forensic_main)),
                             sorted([older, newer]), f"{self.mk}: the forensic main "
                             f"file (the destination main file as it was on disk before "
                             f"the restore) must read exactly the older and newer rows "
                             f"under an immutable open; rows "
                             f"{self._immutable_rows(forensic_main)!r}")
            self.assertTrue(safety_copy.is_file(), f"{self.mk}: the pre-restore safety "
                            f"copy {safety_copy.name} did not survive the automatic "
                            f"rollback -- the rollback must not consume it")
            self._add(third, what="write after the failed restore")
            rows = self._live_rows()
            self.assertEqual(sorted(rows), sorted([older, newer, ghost, third]),
                             f"{self.mk}: recovery lost or gained rows across the failed "
                             f"restore and the later write; live rows now {rows!r}")
        # S2-S4 live in the companion file; the published wrapper runs them too.
        self._run_companion_legs()

    def _require_set_aside_copy(self, result, planted_wal):
        """The sidecar taken away from beside the live store must be
        reported on ONE line that contains 'removed stale <name>' and
        'set aside' and names the forensic location holding a byte-identical
        copy of the planted -wal; that location must be a
        restore-forensic-* directory under the backup directory.  Returns
        the byte-identical copy's path."""
        text = result["out"] + "\n" + result["err"]
        wal_name = self.store.name + "-wal"
        lines = [line for line in text.splitlines()
                 if "set aside" in line and "removed stale" in line
                 and wal_name in line]
        self.assertTrue(lines, f"{self.mk}: the sidecar taken away from beside the "
                        f"live store must be reported on one line containing 'removed "
                        f"stale {wal_name}' and 'set aside' and naming the forensic "
                        f"location holding its byte copy; output tail "
                        f"{text[-400:]!r}")
        found = self._find_wal_copy(lines, planted_wal)
        self.assertIsNotNone(found, f"{self.mk}: the 'removed stale'/'set aside' line "
                        f"does not name any file or directory holding a "
                        f"byte-identical copy of the planted -wal; lines {lines!r}")
        forensic_dir = found.parent
        self.assertTrue(forensic_dir.name.startswith("restore-forensic-"), f"{self.mk}: "
                        f"the on-disk files from before the restore must be kept "
                        f"byte-for-byte in a directory whose name starts with "
                        f"'restore-forensic-' (found {forensic_dir.name!r})")
        try:
            forensic_dir.resolve().relative_to(self.backup_dir.resolve())
        except ValueError:
            self.fail(f"{self.mk}: the forensic directory must live under the backup "
                      f"directory {self.backup_dir}; found {forensic_dir}")
        return found

    def _require_rollback_line(self, result):
        """The failure output must contain a 'rolled back' line naming an
        existing pre-restore safety copy; returns that copy's path."""
        text = result["out"] + "\n" + result["err"]
        lines = [line for line in text.splitlines() if "rolled back" in line]
        self.assertTrue(lines, f"{self.mk}: the failed restore printed no 'rolled back' "
                        f"line naming the pre-restore safety copy it restored; output "
                        f"tail {text[-400:]!r}")
        prerestore = (sorted(self.backup_dir.glob("prerestore*"))
                      if self.backup_dir.is_dir() else [])
        self.assertTrue(prerestore, f"{self.mk}: the failed restore created no pre-restore "
                        f"safety copy (no prerestore* file in the backup directory) that "
                        f"an automatic rollback could name")
        named = [p for p in prerestore
                 if any(p.name in line or str(p) in line for line in lines)]
        self.assertTrue(named, f"{self.mk}: no 'rolled back' line names the pre-restore "
                        f"safety copy; rollback lines {lines!r}, safety copies on disk "
                        f"{[p.name for p in prerestore]!r}")
        return named[0]

    def _run_companion_legs(self):
        """Run the C4 legs that live in the companion file (imported lazily
        so both the direct run and pytest see exactly one copy)."""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_audit_restore_identity_recovery as companion  # noqa: E402
        companion.run_legs()

    def _build_stale_wal(self, ghost_content):
        """Capture the -wal frames of one committed ghost row against THIS
        store, then delete the row and let the close-time checkpoint apply
        both frames, leaving a clean main file. The captured bytes are a
        stale WAL (valid checksums against this very database, holding one
        committed row the main file no longer contains); a second
        connection stays open so no close checkpoints the WAL early."""
        ghost_id = "ghost-" + uuid.uuid4().hex
        wal_path = Path(str(self.store) + "-wal")
        holder = sqlite3.connect(str(self.store))
        writer = sqlite3.connect(str(self.store))
        try:
            writer.execute("INSERT INTO memory (id, namespace, type, content, "
                           "ingestion_ts, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                           (ghost_id, self.ns, "fact", ghost_content,
                            "2026-11-23T00:00:00Z", 0.9))
            writer.commit()
            captured = wal_path.read_bytes()
            writer.execute("DELETE FROM memory WHERE id=?", (ghost_id,))
            writer.commit()
        finally:
            writer.close()
            holder.close()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.store) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        self.assertTrue(captured, f"{self.mk}: scenario setup: no -wal frames were "
                        f"captured, the stale-WAL leg would be vacuous")
        return captured

    def _fail_restore_after_staging(self, snap_path):
        """Run the restore entry point in-process; a one-shot wrapper on
        os.replace performs the real replacement of the live store and then
        raises, failing the step right after the staged install. Returns a
        dict {rc, out, err}; fails the leg when an exception escapes
        cmd_restore, when the fault never fired, or when the call does not
        return a non-zero int."""
        class _PostStagingFault(RuntimeError):
            pass

        state = {"fired": False}
        live_abs = os.path.abspath(str(self.store))
        real_replace = os.replace

        def _replace_then_fail(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            if not state["fired"] and os.path.abspath(os.fspath(dst)) == live_abs:
                state["fired"] = True
                raise _PostStagingFault("AC4-504 injected failure: the staged "
                                        "replacement of the live store completed; "
                                        "the step after it failed")
            return None

        out, err = io.StringIO(), io.StringIO()
        rc, escaped = None, None
        with self._pinned_env():
            os.replace = _replace_then_fail
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = _cmd_restore(from_path=str(snap_path), force=True)
            except Exception as e:  # escaping is itself the failure being specified
                escaped, rc = e, None
            finally:
                os.replace = real_replace
        self.assertTrue(state["fired"], f"{self.mk}: the injected post-replacement fault "
                        f"never fired, so the 'restore fails after staging' scenario was "
                        f"not exercised; restore reported rc={rc!r} raised={escaped!r} "
                        f"stderr tail {err.getvalue()[-200:]!r}")
        self.assertIsNone(escaped, f"{self.mk}: cmd_restore let the staged-replacement "
                        f"failure escape as {escaped!r} instead of returning a controlled "
                        f"non-zero exit code -- a failed restore must be reported, not "
                        f"raised")
        self.assertIsInstance(rc, int, f"{self.mk}: cmd_restore returned {rc!r} instead "
                        f"of an int exit code after the staged-replacement failure")
        self.assertNotEqual(rc, 0, f"{self.mk}: a restore that failed after staging "
                        f"returned {rc!r} instead of a non-zero exit code")
        return {"rc": rc, "out": out.getvalue(), "err": err.getvalue()}


def _run_case(case_cls, method):
    """Run one TestCase method with full setUp/tearDown and surface failures
    as a plain AssertionError for the module-level pytest entry points."""
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(
        unittest.TestSuite([case_cls(method)]))
    if result.wasSuccessful():
        return
    raise AssertionError(f"{case_cls.__name__}.{method} did not pass:\n{stream.getvalue()}")


def test_staged_validation():
    """AC1 (#504) pytest entry point; the spec lives on
    _RestoreStagedValidationCase.test_staged_validation.
    RED at base, GREEN after the fix."""
    _run_case(_RestoreStagedValidationCase, "test_staged_validation")


def test_recovery_preserves_later_write():
    """AC2 (#504) pytest entry point; the spec lives on
    _RestoreFailureRecoveryCase.test_recovery_preserves_later_write (which
    also runs the S2-S4 legs from the companion file).
    RED at base, GREEN after the fix."""
    _run_case(_RestoreFailureRecoveryCase, "test_recovery_preserves_later_write")


if __name__ == "__main__":
    unittest.main(verbosity=2)
