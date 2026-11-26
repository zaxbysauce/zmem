"""Issue #506 audit: restartable initialization, search-index recovery and
writer contention on the maintenance probe.

AC1 -- WHEN creation resumes after an interrupted migration or the search table is lost, THE
SYSTEM SHALL recover a schema equivalent to a fresh store, rebuild existing search entries and
diagnose missing index rows.  RED at base, GREEN after the fix.

AC2 -- WHEN 32 lexical CLI writers start together against one isolated Windows store, THE SYSTEM
SHALL recover all 32 distinct writes without a spurious restore refusal.  RED at base, GREEN
after the fix.  (The AC2 storm TestCase lives in the companion module
tests/test_audit_schema_recovery_storm.py; this file keeps the published module-level wrapper
``test_concurrent_writers`` that runs it, so ``pytest -q
tests/test_audit_schema_recovery.py::test_concurrent_writers`` still selects the storm.  Run the
companion file directly for AC2 alone; run this file directly for AC1 alone.)

AC1 drives everything through public entry points.  An interrupted migration is emulated by a
child that opens the store the way every first open does (schema.connect() then
schema._prepare_store()) and dies with os._exit(137) at the trace point of the version-stamp
UPDATE; the ONLY fixture claim after that kill is that the recorded stamp is still below the
version the interrupted block writes -- that is the proof the interrupted window was hit.  The
fixture deliberately does NOT claim the block's columns survived the kill: a correct atomic
migration rolls them back, and both shapes must resume.  The resumed store is then compared
against a FRESH store built by the same code in the same test run: the full sqlite_master
(type, name) object set (full-text table and shadow tables included), the ordered memory
columns, meta.schema_version (also pinned to storelib.schema.SUPPORTED_SCHEMA_VERSION) and
PRAGMA application_id / user_version.  The search-table-lost legs drop memory_fts the way
SQLite does (DROP TABLE, which removes the virtual table and its shadow tables) on a populated
store and require every old row to be lexical-searchable again and present in
memory_fts_docsize -- after a plain reopen, after a kill BEFORE the rebuild statement, and
after a kill WHILE the rebuild statement runs (progress handler).  A missing index entry is
created exactly the way the repo's own FTS triggers do (the 'delete' INSERT) and the doctor's
report must diagnose THAT condition specifically.

Hermetic: every subprocess pins a fresh throwaway store (ZMEM_STORE, ZMEM_DATA, ZMEM_BACKUP_DIR,
ZMEM_MODELS_DIR nonexistent, autodownload off, UTF-8); the operator store is never opened.  All
store reads inside assertions go through read-only SQLite URIs (file:...?mode=ro) so the
comparison itself writes nothing.

Run: python tests/test_audit_schema_recovery.py   (repo convention, no pytest)
"""

from __future__ import annotations
import atexit
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
DOCTOR_PY = SCRIPTS_DIR / "doctor.py"
PY = sys.executable

# ---------------------------------------------------------------------------
# Module-level scratch + env pin BEFORE the _store_guard import (issue #244):
# the guard judges the ambient env at ITS import, so the pin must precede it.
# Children re-pin per-run against their own fresh stores.
# ---------------------------------------------------------------------------
_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-506-recovery-"))


@atexit.register
def _cleanup_scratch():
    shutil.rmtree(_SCRATCH, ignore_errors=True)


os.environ["ZMEM_STORE"] = str(_SCRATCH / "guard-pin.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)
os.environ["ZMEM_BACKUP_DIR"] = str(_SCRATCH / "backups")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

NS = "project:audit506"

# AC2 contention knobs: PUBLIC configuration only.  The tightened wait window
# is ample for an uncontended writer (the single-writer control) but the
# writers' own exclusive maintenance-lock probes collide with each other.
# The storm module imports these so the two files cannot drift.
_TIGHT_WAIT = "0.05"
_TIGHT_POLL = "0.005"

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_BACKUP_DIR", "ZMEM_HOME", "ZMEM_NAMESPACE",
    "ZMEM_HOST", "ZMEM_PROJECT_DIR", "CLAUDE_PROJECT_DIR", "ZCODE_PROJECT_DIR",
    "ZMEM_SESSION", "ZMEM_SESSION_ID", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_MODEL_AUTODOWNLOAD",
    "ZMEM_MODELS_DIR", "ZMEM_EMBED_PROFILE", "ZMEM_QUERY_CONTEXT",
    "ZMEM_TRANSCRIPT", "ZMEM_TIER0", "ZMEM_SKILLS_DIRS", "ZMEM_PYTHON",
    "ZMEM_CROSS_PROJECT", "ZMEM_REMOTE_URL", "ZMEM_CROSS_ENCODER",
    "ZMEM_MAINTENANCE_WAIT_SECONDS", "ZMEM_MAINTENANCE_POLL_SECONDS",
)

# Emulates a killed first open: the child dies at the trace point of the
# version-stamp UPDATE of the block named by argv[2], so that block's earlier
# statements are interrupted mid-block while the stamp is still behind (a
# non-atomic block leaves its DDL committed; an atomic block rolls it back --
# both must resume, so the fixture below asserts only the stamp).
_KILL_CHILD_SOURCE = """\
import os, sys
sys.path.insert(0, sys.argv[1])
from storelib import schema  # noqa: E402

conn = schema.connect()

def _kill(sql):
    if (sql.startswith("UPDATE meta SET value='%s'" % sys.argv[2])
            and "schema_version" in sql):
        os._exit(137)

conn.set_trace_callback(_kill)
schema._prepare_store(conn)
print("completed", flush=True)
"""

# Emulates a first open killed around the FTS rebuild: the child arms a
# sqlite3 trace callback and dies the FIRST time an INSERT INTO memory_fts
# statement runs.  That regex matches the FTS5 'rebuild' command
# (INSERT INTO memory_fts(memory_fts) VALUES('rebuild')) and any equivalent
# bulk re-insert, and never the CREATE TRIGGER DDL.  mode "before" kills the
# instant the statement is traced (BEFORE it runs); mode "during" instead arms
# a progress handler (set_progress_handler(fn, 1)) whose first call kills --
# i.e. WHILE the statement is executing.  The marker file records whether the
# kill fired, so a child that completed without ever attempting the rebuild is
# distinguishable from a killed one.
_REBUILD_KILL_CHILD_SOURCE = """\
import os, re, sys

sys.path.insert(0, sys.argv[1])
mode, marker = sys.argv[2], sys.argv[3]
from storelib import schema  # noqa: E402

conn = schema.connect()
armed = {"fired": False, "progress_armed": False}


def _die():
    armed["fired"] = True
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("fired")
    os._exit(137)


def _progress():
    if not armed["progress_armed"]:
        armed["progress_armed"] = True
        _die()


def _trace(sql):
    if armed["fired"] or armed["progress_armed"]:
        return
    if re.match(r"^\\s*INSERT\\s+INTO\\s+memory_fts", sql, re.IGNORECASE):
        if mode == "before":
            _die()
        conn.set_progress_handler(_progress, 1)


conn.set_trace_callback(_trace)
schema._prepare_store(conn)
print("completed", flush=True)
"""

# The three populated-store rows shared by the lost-search-table legs; each
# row carries its own distinct lexical token (second tuple element) so every
# row can be searched for individually.
_LOST_FTS_ROWS = (
    ("alpha beacon cove", "cove"),
    ("delta ember frost", "frost"),
    ("gold harbor ink", "harbor"),
)


def _supported_schema_version() -> int:
    """storelib.schema.SUPPORTED_SCHEMA_VERSION, read from the live code (the
    fresh store and the resumed store must both land on it)."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from storelib import schema as _schema  # noqa: E402
    return int(_schema.SUPPORTED_SCHEMA_VERSION)


def _names_search_index(check) -> bool:
    """True when a doctor check's ID (or, more specifically worded, its
    summary) identifies it as a full-text / search-index check.  Bare 'index'
    counts only in the ID; the summary must say fts / full-text / fulltext /
    search index so ambient hermetic-mode warns (embeddings unavailable, vec
    overfetch) cannot masquerade as index checks."""
    cid = str(check.get("id") or "").lower()
    if any(t in cid for t in ("fts", "index", "search")):
        return True
    summary = str(check.get("summary") or "").lower()
    return any(t in summary for t in ("fts", "full-text", "fulltext",
                                      "full text", "search index",
                                      "search-index"))


def _mentions_missing_rows(check) -> bool:
    """True when a check's summary/details mention the missing index rows:
    the repair hint 'rebuild-fts', or a COUNT coupled to index/row vocabulary
    ('2 rows missing', 'missing rows: 1', '1 stale index entry').  Path-shaped
    tokens are scrubbed first: interpreter/store paths carry digits and
    hyphen-separated words, and 'FTS5' alone or a model_file_missing reason
    must never satisfy this."""
    summary = str(check.get("summary") or "")
    details = json.dumps(check.get("details") or {}, ensure_ascii=False,
                         default=str)
    blob = (summary + " " + details).lower().replace("fts5", "fts")
    blob = re.sub(r"\S*[/\\]\S*", " ", blob)  # scrub path-shaped tokens
    if "rebuild-fts" in blob:
        return True
    vocab = (r"missing|stale|unindexed|out.of.sync|desync|not indexed"
             r"|rows?|entries|documents?|records?|index")
    coupled = (r"\b\d+(?:\.\d+)?\b[^0-9\n]{0,40}\b(?:" + vocab + r")\b"
               r"|\b(?:" + vocab + r")\b[^0-9\n]{0,40}\b\d+(?:\.\d+)?\b")
    return bool(re.search(coupled, blob))


def _names_rebuild_fts_repair(check) -> bool:
    """True when a check's summary/details name the repair `rebuild-fts`
    (path-shaped tokens scrubbed first, same rule as the missing-rows
    predicate, so an ambient path can never satisfy this)."""
    summary = str(check.get("summary") or "")
    details = json.dumps(check.get("details") or {}, ensure_ascii=False,
                         default=str)
    blob = re.sub(r"\S*[/\\]\S*", " ",
                  (summary + " " + details).lower())
    return "rebuild-fts" in blob


class AuditSchemaRecoveryBase(unittest.TestCase):
    """Fresh throwaway store dir per test; pinned env for every subprocess."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-506-case-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store_dir(self, name):
        d = self.tmp / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _child_env(self, store_dir, extra=None):
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env.update({
            "ZMEM_STORE": str(store_dir / "store.sqlite"),
            "ZMEM_DATA": str(store_dir),
            "ZMEM_BACKUP_DIR": str(store_dir / "backups"),
            "ZMEM_MODELS_DIR": str(store_dir / "no-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        })
        if extra:
            env.update({k: str(v) for k, v in extra.items()})
        return env

    def _cli(self, store_dir, *args, extra_env=None):
        return subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY)] + list(args), capture_output=True,
            text=True, env=self._child_env(store_dir, extra_env),
            cwd=str(REPO_ROOT), timeout=180)

    def _add(self, store_dir, content, namespace=NS):
        return self._cli(store_dir, "add", "--namespace", namespace, "--type",
                         "fact", "--content", content, "--signal", "test",
                         "--json")

    def _doctor(self, store_dir, store_path):
        return subprocess.run(
            [PY, "-X", "utf8", str(DOCTOR_PY), "--store", str(store_path),
             "--format", "json"], capture_output=True, text=True,
            env=self._child_env(store_dir), cwd=str(REPO_ROOT), timeout=300)

    def _run_kill_child(self, store_dir, stamp):
        child = store_dir / "_kill_child.py"
        child.write_text(_KILL_CHILD_SOURCE, encoding="utf-8")
        return subprocess.run(
            [PY, "-X", "utf8", str(child), str(SCRIPTS_DIR), stamp],
            capture_output=True, text=True, env=self._child_env(store_dir),
            cwd=str(REPO_ROOT), timeout=120)

    def _sql(self, store_path, sql, params=(), write=False):
        conn = sqlite3.connect(str(store_path))
        try:
            rows = conn.execute(sql, params).fetchall()
            if write:
                conn.commit()
            return rows
        finally:
            conn.close()

    def _ro_sql(self, store_path, sql, params=()):
        """Read a store through a read-only SQLite URI so the comparison
        itself writes nothing (works with a leftover -wal file too)."""
        uri = "file:%s?mode=ro" % str(store_path).replace("\\", "/")
        conn = sqlite3.connect(uri, uri=True)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _stamp_ro(self, store_path):
        row = self._ro_sql(store_path,
                           "SELECT value FROM meta WHERE key='schema_version'")
        return row[0][0] if row else None

    def _populate(self, store_dir, contents):
        for words in contents:
            r = self._add(store_dir, words)
            self.assertEqual(r.returncode, 0,
                "AC1-506: scenario setup: populated-store add %r failed: exit"
                " %s stderr %r" % (words, r.returncode, r.stderr))

    def _interrupted_store(self, name, stamp, prev_stamp):
        """Store whose first open died mid-migration block ``stamp``: the
        version stamp is still below that block's target.  The ONLY fixture
        claim is the stamp (a non-atomic block also leaves its DDL committed;
        an atomic one rolls it back -- both are valid interrupted shapes and
        both must resume)."""
        d = self._store_dir(name)
        kill = self._run_kill_child(d, stamp)
        self.assertIn(kill.returncode, (0, 137),
            "AC1-506: scenario setup: the v%s kill child exited unexpectedly:"
            " rc=%s stderr %r" % (stamp, kill.returncode, kill.stderr[-300:]))
        if kill.returncode != 137:
            # The migration's statement shapes changed; recreate the same
            # interrupted precondition (stamp behind, block's work present).
            self._sql(d / "store.sqlite",
                      "UPDATE meta SET value=? WHERE key='schema_version'",
                      (prev_stamp,), write=True)
        value = self._stamp_ro(d / "store.sqlite")
        try:
            below = value is not None and int(value) < int(stamp)
        except ValueError:
            below = False
        self.assertTrue(below,
            "AC1-506: scenario setup: the kill inside the v%s block must leave"
            " the recorded stamp still below %s -- that is the proof the"
            " interrupted window was hit (read-only read found %r)"
            % (stamp, stamp, value))
        return d

    def _loads(self, r, what):
        try:
            return json.loads(r.stdout)
        except (json.JSONDecodeError, TypeError):
            self.fail("%s: stdout is not JSON: %r" % (what, r.stdout[:300]))


class InterruptedSchemaAndFtsTests(AuditSchemaRecoveryBase):
    """AC1 (#506): interrupted migrations resume to a fresh-equivalent schema;
    a lost search table is re-indexed and the rebuild survives kills around
    it; the doctor diagnoses a missing index entry.  RED at base, GREEN after
    the fix."""

    def test_interrupted_schema_and_fts(self):
        """Eight legs, each in its own subTest so every leg reports its own
        result.  RED at base, GREEN after the fix."""
        # -- fresh, never-interrupted control store (the fresh-equivalence
        #    oracle for L1/L2 and the healthy store for L8) ----------------
        fresh = self._store_dir("fresh-control")
        r = self._add(fresh, "fresh control row one")
        self.assertEqual(r.returncode, 0,
            "AC1-506: a store that was never interrupted must be unaffected: add exit %s stderr %r"
            % (r.returncode, r.stderr))

        with self.subTest(leg="L1 v3-window resume equals a fresh store"):
            v3 = self._interrupted_store("v3-interrupted", "3", "2")
            r = self._add(v3, "resumed store post-v3 row")
            self.assertEqual(r.returncode, 0,
                "AC1-506: a first open killed inside the v3 migration did not resume: reopen exit %s"
                " stderr %r" % (r.returncode, r.stderr))
            self._assert_fresh_equivalent(v3, fresh, "v3")

        with self.subTest(leg="L2 v4-window resume equals a fresh store"):
            v4 = self._interrupted_store("v4-interrupted", "4", "3")
            r = self._add(v4, "resumed store post-v4 row")
            self.assertEqual(r.returncode, 0,
                "AC1-506: a first open killed inside the v4 migration did not resume: reopen exit %s"
                " stderr %r" % (r.returncode, r.stderr))
            self._assert_fresh_equivalent(v4, fresh, "v4")

        with self.subTest(leg="L3 v2-window resume control"):
            v2 = self._interrupted_store("v2-control", "2", "1")
            r = self._add(v2, "resumed store post-v2 row")
            self.assertEqual(r.returncode, 0,
                "AC1-506: control - a first open killed inside the v2 window must keep resuming:"
                " reopen exit %s stderr %r" % (r.returncode, r.stderr))

        with self.subTest(leg="L4 lost search table re-indexes every row"):
            self._leg_lost_search_table()

        with self.subTest(leg="L5 rebuild survives a kill before the rebuild statement"):
            self._leg_rebuild_kill("before")

        with self.subTest(leg="L6 rebuild survives a kill while the rebuild statement runs"):
            self._leg_rebuild_kill("during")

        with self.subTest(leg="L7 doctor diagnoses the missing index row"):
            self._leg_doctor_missing_row()

        with self.subTest(leg="L8 healthy-store doctor control"):
            self._leg_doctor_healthy(fresh)

    # -- leg helpers -------------------------------------------------------

    def _assert_fresh_equivalent(self, resumed_dir, fresh_dir, label):
        """The resumed store's schema must be equivalent to a fresh store's:
        full sqlite_master object set, ordered memory columns, application
        identity (schema_version, application_id, user_version)."""
        resumed = resumed_dir / "store.sqlite"
        fresh = fresh_dir / "store.sqlite"
        res_objs = self._ro_sql(resumed,
                                "SELECT type, name FROM sqlite_master"
                                " ORDER BY type, name")
        fresh_objs = self._ro_sql(fresh,
                                  "SELECT type, name FROM sqlite_master"
                                  " ORDER BY type, name")
        self.assertEqual(res_objs, fresh_objs,
            "AC1-506: the resumed %s store's full database-object surface must"
            " equal a fresh store's, including the full-text table and its"
            " shadow tables (objects only the resumed store has: %r; objects"
            " only a fresh store has: %r)"
            % (label, sorted(set(res_objs) - set(fresh_objs)),
               sorted(set(fresh_objs) - set(res_objs))))
        res_cols = [r[1] for r in self._ro_sql(resumed,
                                               "PRAGMA table_info(memory)")]
        fresh_cols = [r[1] for r in self._ro_sql(fresh,
                                                 "PRAGMA table_info(memory)")]
        self.assertEqual(res_cols, fresh_cols,
            "AC1-506: the resumed %s store's ordered memory columns must equal"
            " a fresh store's (resumed: %r; fresh: %r)"
            % (label, res_cols, fresh_cols))
        res_stamp = self._stamp_ro(resumed)
        fresh_stamp = self._stamp_ro(fresh)
        supported = _supported_schema_version()
        self.assertTrue(res_stamp is not None
                        and int(res_stamp) == int(fresh_stamp) == supported,
            "AC1-506: the resumed %s store's application identity must match a"
            " fresh store: meta.schema_version %r must equal the fresh store's"
            " %r and storelib.schema.SUPPORTED_SCHEMA_VERSION %d"
            % (label, res_stamp, fresh_stamp, supported))
        res_ident = tuple(self._ro_sql(resumed, q)[0][0] for q in
                          ("PRAGMA application_id", "PRAGMA user_version"))
        fresh_ident = tuple(self._ro_sql(fresh, q)[0][0] for q in
                            ("PRAGMA application_id", "PRAGMA user_version"))
        self.assertEqual(res_ident, fresh_ident,
            "AC1-506: the resumed %s store's PRAGMA application_id/user_version"
            " %r must equal the fresh store's %r"
            % (label, res_ident, fresh_ident))

    def _assert_all_searchable(self, store_dir, when):
        """The two L4 assertions, reused by L5/L6: every populated row is
        found by lexical search for its own token, and every memory rowid is
        present in memory_fts_docsize (the docsize table -- never
        SELECT COUNT(*) FROM memory_fts, which reads the content table)."""
        p = store_dir / "store.sqlite"
        for content, token in _LOST_FTS_ROWS:
            r = self._cli(store_dir, "search", "--text", token,
                          "--namespace", NS, "--json")
            self.assertEqual(r.returncode, 0,
                "AC1-506: %s, lexical search for token %r exited %s stderr %r"
                % (when, token, r.returncode, r.stderr))
            results = (self._loads(
                r, "AC1-506: %s lexical search report" % when)
                or {}).get("results") or []
            self.assertTrue(any((x.get("content") or "") == content
                                for x in results),
                "AC1-506: %s, EVERY existing row must be re-indexed so lexical"
                " search finds it again: token %r must surface row %r but the"
                " search returned %d rows (base signature: the recreated index"
                " is empty while the rows still exist)"
                % (when, token, content, len(results)))
        memory_ids = {r[0] for r in self._ro_sql(p, "SELECT rowid FROM memory")}
        doc_ids = {r[0] for r in self._ro_sql(
            p, "SELECT rowid FROM memory_fts_docsize")}
        self.assertTrue(memory_ids <= doc_ids,
            "AC1-506: %s, every memory rowid must be present in"
            " memory_fts_docsize (memory rowids with no index row: %r)"
            % (when, sorted(memory_ids - doc_ids)))

    def _leg_lost_search_table(self):
        fts = self._store_dir("fts-lost")
        self._populate(fts, [c for c, _ in _LOST_FTS_ROWS])
        r = self._cli(fts, "search", "--text", "delta ember",
                      "--namespace", NS, "--json")
        self.assertEqual(len((self._loads(
            r, "AC1-506: pre-drop lexical search") or {}).get("results") or []),
            1,
            "AC1-506: scenario setup: pre-drop control - lexical search must"
            " find the row while the index is intact")
        self._sql(fts / "store.sqlite", "DROP TABLE memory_fts", write=True)
        self.assertEqual(
            [r[0] for r in self._ro_sql(
                fts / "store.sqlite",
                "SELECT name FROM sqlite_master WHERE name LIKE 'memory_fts%'")],
            [],
            "AC1-506: scenario setup: DROP TABLE memory_fts must remove the"
            " virtual table and its shadow tables before the reopen")
        r = self._cli(fts, "list", "--namespace", NS, "--json")
        self.assertEqual(r.returncode, 0,
            "AC1-506: the next open after losing the search table must succeed: exit %s stderr %r"
            % (r.returncode, r.stderr))
        self._assert_all_searchable(fts, "after the search table is lost")
        r = self._cli(fts, "search", "--text", "delta ember frost", "--exact",
                      "--namespace", NS, "--json")
        self.assertTrue((self._loads(
            r, "AC1-506: post-drop exact control") or {}).get("results"),
            "AC1-506: control - exact search must keep seeing the surviving rows (the rows were"
            " never gone)")

    def _leg_rebuild_kill(self, mode):
        """L5 (mode 'before': killed the instant the rebuild statement is
        traced) and L6 (mode 'during': a progress handler kills on its first
        call, inside the running statement)."""
        d = self._store_dir("fts-rebuild-kill-%s" % mode)
        self._populate(d, [c for c, _ in _LOST_FTS_ROWS])
        self._sql(d / "store.sqlite", "DROP TABLE memory_fts", write=True)
        self.assertEqual(
            [r[0] for r in self._ro_sql(
                d / "store.sqlite",
                "SELECT name FROM sqlite_master WHERE name LIKE 'memory_fts%'")],
            [],
            "AC1-506: scenario setup: DROP TABLE memory_fts must remove the"
            " virtual table and its shadow tables before the killed open")
        marker = d / ("kill-fired-%s.marker" % mode)
        child = d / ("_rebuild_kill_child_%s.py" % mode)
        child.write_text(_REBUILD_KILL_CHILD_SOURCE, encoding="utf-8")
        kill = subprocess.run(
            [PY, "-X", "utf8", str(child), str(SCRIPTS_DIR), mode,
             str(marker)],
            capture_output=True, text=True, env=self._child_env(d),
            cwd=str(REPO_ROOT), timeout=120)
        self.assertIn(kill.returncode, (0, 137),
            "AC1-506: scenario setup: the %s-rebuild kill child exited"
            " unexpectedly: rc=%s stderr %r"
            % (mode, kill.returncode, kill.stderr[-300:]))
        if not marker.exists():
            if kill.returncode != 0:
                self.fail(
                    "AC1-506: scenario setup: the %s-rebuild kill child died"
                    " without recording the kill: rc=%s stderr %r"
                    % (mode, kill.returncode, kill.stderr[-300:]))
            self.fail(
                "AC1-506: the open after the search table was dropped never"
                " attempted to rebuild the index: no INSERT INTO memory_fts"
                " statement ran during the open (the kill never fired; the"
                " child completed cleanly) -- the reopen must rebuild the"
                " lost index")
        r = self._cli(d, "list", "--namespace", NS, "--json")
        self.assertEqual(r.returncode, 0,
            "AC1-506: the next public open after a kill %s the rebuild"
            " statement must succeed: exit %s stderr %r"
            % (mode, r.returncode, r.stderr))
        self._assert_all_searchable(
            d, "after a kill %s the rebuild statement" % mode)

    def _leg_doctor_missing_row(self):
        """L7: the doctor must diagnose the specific condition.  The doctor's
        EXIT CODE is not part of the contract (doctor.py's main ends with
        ``return 0 if report["ok"] else 1``, so both 0 and 1 are accepted
        below); the REPORT must (a) carry an index-named warn/fail check that
        mentions the missing index rows, (b) not grade the fts lane ok, (c)
        grade that check as degraded derived data (warn, fail also
        acceptable) and name the `rebuild-fts` repair, and (d) blame no
        unrelated failing check."""
        # NOTE: keep index/row vocabulary OUT of this dir name -- the doctor
        # embeds store paths in check summaries/details and the missing-rows
        # predicate below must not be satisfiable by the fixture's own path.
        miss = self._store_dir("doctor-fts-probe")
        self._populate(miss, ("alpha beacon cove marker",
                              "delta ember frost marker",
                              "gold harbor ink marker"))
        target = self._sql(miss / "store.sqlite",
                           "SELECT rowid, content, tags, namespace FROM memory"
                           " WHERE content LIKE ?", ("%delta%",))[0]
        # remove the entry the way the repo's own FTS triggers do
        self._sql(miss / "store.sqlite",
                  "INSERT INTO memory_fts(memory_fts, rowid, content, tags, namespace)"
                  " VALUES('delete', ?, ?, ?, ?)", target, write=True)
        self.assertNotIn(target[0],
            {r[0] for r in self._ro_sql(miss / "store.sqlite",
                                        "SELECT rowid FROM memory_fts_docsize")},
            "AC1-506: scenario setup: the trigger-style delete must have removed the row's index entry")
        self.assertIn(target[0],
            {r[0] for r in self._ro_sql(miss / "store.sqlite",
                                        "SELECT rowid FROM memory")},
            "AC1-506: scenario setup: the memory row itself must still exist")
        r = self._doctor(miss, miss / "store.sqlite")
        # The exit code follows the report's ok flag (0 if ok else 1) and is
        # NOT the signal: a store graded warn keeps ok true and exits 0, a
        # fail-graded one exits 1.  Accept both; any other code fails here,
        # and a crashed doctor (stdout that is not a JSON report, e.g. noise
        # from a traceback) fails at the _loads step right below.
        self.assertIn(r.returncode, (0, 1),
            "AC1-506: doctor exited %s on a store with a missing index entry"
            " (expected 0 or 1: the exit code mirrors the report's ok flag and"
            " is not the signal): stderr %r"
            % (r.returncode, r.stderr[-300:]))
        payload = self._loads(r, "AC1-506: doctor report")
        checks = payload.get("checks") or []
        flagged = [(str(c.get("id")), str(c.get("status")))
                   for c in checks if c.get("status") in ("warn", "fail")]
        candidates = [c for c in checks
                      if c.get("status") in ("warn", "fail")
                      and _names_search_index(c)]
        # (a) a check that names the full-text / search index, is fail/warn,
        #     and mentions the missing index rows (a count or rebuild-fts).
        self.assertTrue(any(_mentions_missing_rows(c) for c in candidates),
            "AC1-506: the doctor must carry a check that names the full-text /"
            " search index (id or summary), reports status fail or warn, and"
            " mentions the missing index rows (a count or the 'rebuild-fts'"
            " repair) -- no such check exists, so the report is blind to the"
            " missing index row; the fail/warn checks were: %r" % (flagged,))
        # (b) the fts lane in the report's lanes is not ok.
        lane = (payload.get("lanes") or {}).get("fts")
        self.assertTrue(lane is not None and lane.get("status") != "ok",
            "AC1-506: the report's fts lane must not be 'ok' on a store with a"
            " missing index entry (lanes.fts=%r; base signature: the lane"
            " probe only MATCHes the table, so the missing rows are invisible)"
            % (lane,))
        # (c) the check found in (a) grades the condition as degraded derived
        #     data (status warn -- the doctor's own convention for comparable
        #     derived-data checks; fail is also acceptable) and names the
        #     `rebuild-fts` repair in its summary or details.
        repairing = [c for c in candidates
                     if _mentions_missing_rows(c)
                     and _names_rebuild_fts_repair(c)]
        self.assertTrue(repairing,
            "AC1-506: the check that reports the missing index rows must carry"
            " a degraded derived-data grade (status warn, or fail) and name the"
            " 'rebuild-fts' repair in its summary or details; the index-named"
            " warn/fail checks were: %r"
            % [(str(c.get("id")), str(c.get("status")),
                (str(c.get("summary") or ""))[:80]) for c in candidates])
        # (d) every failing STORE-FACING check names the full-text / search
        #     index in its id, so an unrelated failure cannot satisfy this
        #     leg. Host-state checks (the runner's claude-native-memory, the
        #     served-install family) diagnose the MACHINE, not the store, and
        #     are out of this leg's scope — a CI runner with no Claude
        #     install fails them while the store is perfectly healthy (the
        #     ubuntu lane proved exactly that; AMENDED CHECK_WRONG after the
        #     first CI round).
        _HOST_STATE_MARKERS = ("claude", "native-memory", "served", "install",
                               "host-config")
        unrelated = [str(c.get("id")) for c in checks
                     if c.get("status") == "fail"
                     and not any(m in str(c.get("id") or "").lower()
                                 for m in _HOST_STATE_MARKERS)
                     and not any(t in str(c.get("id") or "").lower()
                                 for t in ("fts", "index", "search"))]
        self.assertEqual(unrelated, [],
            "AC1-506: every failing store-facing check must name the"
            " full-text / search"
            " index in its id so an unrelated failure cannot satisfy this leg;"
            " unrelated fail checks: %r" % (unrelated,))

    def _leg_doctor_healthy(self, fresh_dir):
        r = self._doctor(fresh_dir, fresh_dir / "store.sqlite")
        # The control pins STORE health, not machine health: doctor's global
        # exit code and ok flag include HOST-state checks (claude-native-
        # memory, served installs) that fail on a bare CI runner while the
        # store is healthy (AMENDED CHECK_WRONG after the first ubuntu CI
        # round tripped on exactly that). The store-facing assertions below
        # carry the leg's discriminating power.
        payload = self._loads(r, "AC1-506: doctor healthy control")
        _HOST_STATE_MARKERS = ("claude", "native-memory", "served", "install",
                               "host-config")
        store_fail = [str(c.get("id")) for c in payload.get("checks") or []
                      if c.get("status") == "fail"
                      and not any(m in str(c.get("id") or "").lower()
                                  for m in _HOST_STATE_MARKERS)]
        self.assertEqual(store_fail, [],
            "AC1-506: control - no store-facing check may fail on a healthy"
            " never-interrupted store; failing: %r" % (store_fail,))
        index_checks = [(str(c.get("id")), str(c.get("status")))
                        for c in payload.get("checks") or []
                        if _names_search_index(c)]
        self.assertTrue(any(status == "pass" for _cid, status in index_checks),
            "AC1-506: control - the doctor's search-index check(s) must report"
            " status 'pass' on a healthy store (index-named checks: %r)"
            % (index_checks,))


def _run_case(case_class, method_name: str) -> None:
    suite = unittest.TestSuite()
    suite.addTest(case_class(method_name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        # Surface the FIRST failing leg's own text (it starts with the AC
        # marker) so the published node id's failure text names the defect.
        first = ""
        for bucket in (result.failures, result.errors):
            for _test, tb in bucket:
                lines = [ln.strip() for ln in tb.strip().splitlines()
                         if ln.strip()]
                if lines:
                    first = lines[-1]
                    break
            if first:
                break
        head, sep, rest = first.partition(": ")
        if sep and re.match(r"^[A-Za-z_][\w.]*$", head):
            first = rest  # strip the exception-class prefix ("AssertionError: ")
        raise AssertionError(first or "%s.%s failed on this tree (see the"
                                     " runner output above)"
                             % (case_class.__name__, method_name))


def test_interrupted_schema_and_fts():
    """Published-argv wrapper: runs the AC1 TestCase assertions."""
    _run_case(InterruptedSchemaAndFtsTests, "test_interrupted_schema_and_fts")


def test_concurrent_writers():
    """Published-argv wrapper: runs the AC2 storm TestCase imported from the
    companion module tests/test_audit_schema_recovery_storm.py (the import is
    deferred so each file also stands alone)."""
    from test_audit_schema_recovery_storm import ConcurrentWritersStormTests
    _run_case(ConcurrentWritersStormTests, "test_concurrent_writers")


if __name__ == "__main__":
    unittest.main(verbosity=2)
