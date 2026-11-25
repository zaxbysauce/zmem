"""Acceptance checks for issue #507 (Workstream X PR 3 of 6): revalidate
consolidation and update candidates inside the write transaction.
RED at base 5a5560ba, GREEN after the fix. (The branch was fast-forwarded
to 46e48662 after round 2; the writer modules are byte-identical there, and
every RED leg below was re-verified still RED on that tree.)

test_invalidated_neighbor (AC1 / trace AC5-507): a second connection
tombstones a selected consolidation neighbour (or the keeper) between
selection and the cluster transaction; the invalidation must survive (the
row keeps the second writer's tombstone, the keeper does not grow, no merge
is reported, no transaction is left open). Amended legs: the stale decision
is also refused when the neighbour's CONTENT or TAGS change after selection
without being invalidated, and the run's bookkeeping (consolidated_at,
report counts, merge-claiming entries) must reflect that nothing was
absorbed. Round-2 legs: the refusal also covers EACH other neighbour field
drifting alone (namespace, a reserved knowledge: pointer source_ref, type,
confidence, signal, taint, embedding, embedding_model), a keeper whose
content is rewritten after selection, and a mixed cluster where only the
changed neighbour is refused while the untouched one still merges.
Round-3 legs: the neighbour table gains ingestion_ts, and the KEEPER drift
coverage extends to every NON-content field changed alone (type, tags,
namespace, a reserved knowledge: pointer source_ref, confidence, signal,
taint, embedding, embedding_model, ingestion_ts) — each refuses the WHOLE
cluster and leaves the neighbour untouched.
Control: an uncontested pair still merges normally.
test_metadata_interleave (AC2 / trace AC6-507): a second connection commits
tags/confidence/signal — and, in the amended legs, type, source_ref, the
derived source_hash, and the derived taint (both directions: untrusted->
trusted and trusted->untrusted) — between update_memory's pre-lock read and
its write; the caller updates only content, and the surviving successor
must carry the concurrently committed values for every field the caller did
not pass, while an explicit caller value still wins, the reserved card:
source_ref guard still refuses, and a row that moved onto a knowledge:
pointer reference is refused untouched. Controls: inheritance without
competition, and the concurrent-supersede refusal mentioning "superseded".
The public-command legs (store.py update / store.py consolidate driven
in-process) live in tests/test_audit_write_revalidation_cli.py — including
the round-2 default-intent capture-policy leg over the FINAL tags; the
module-level wrappers below invoke them, so the published pytest node ids
still run every leg.

Deterministic in-process interleave: two connections to one throwaway store,
one-shot hooks (consolidate's _admitted_neighbors, write's
_validate_namespace), no sleeps, no network, no models. Runs under pytest
(published argvs name the module-level wrappers) and standalone (CI):
python -X utf8 tests/test_audit_write_revalidation.py. Env pinned to a temp
store BEFORE any storelib import (tests/test_update_liveness_toctou.py house
pattern); fresh store per case.
"""

from __future__ import annotations
import importlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

A_NOW = "2030-01-01T00:00:00Z"      # second writer's tombstone instant
B_NOW = "2031-06-15T12:00:00Z"      # caller's update instant
SEED_TS = "2029-01-01T00:00:00Z"    # deterministic direct-insert stamps
INVALIDATE_REASON = "invalidated by a second writer"
NS = "project:audit-507"
# Near-duplicate pair (lexical Jaccard 5/7 ~ 0.71 > 0.60), direct-inserted so
# write-time dedup cannot fold them (tests/test_consolidate_contested.py
# house pattern); retrieval_count ranks row 1 as the deterministic keeper.
KEEPER_TEXT = "always run migrations before deploy"
NEIGHBOR_TEXT = "always run migrations before deploy every time"
# Content the second connection writes over the selected neighbour. No
# negators: a contested (mixed-polarity) park is a different refusal path,
# not the stale-decision defect these legs pin.
DRIFTED_TEXT = "always run migrations before deploy every single time"
DRIFTED_TAGS = "deploy-extra"
# Round-2 drift texts: the keeper rewrite and the mixed cluster's second
# neighbour (lexical Jaccard vs KEEPER_TEXT 5/6 ~ 0.83 > 0.60, so the trio
# provably merges clean — the mixed-cluster leg proves it in a scenario-
# setup step before injecting the drift).
NEIGHBOR_B_TEXT = "always run migrations before every deploy"
KEEPER_DRIFT_TEXT = "always run migrations before every deploy window"
NEIGHBOR_B_DRIFT_TEXT = \
    "always run migrations before every deploy revised edition"
# Round 3: the ingestion_ts drift value (the direct-insert seeds stamp
# SEED_TS, so this is a genuinely different instant).
DRIFTED_TS = "2029-06-15T00:00:00Z"

# Round-2/3 C5: each field of the SELECTED NEIGHBOUR that a second connection
# rewrites alone (row kept live) after selection. The pointer source_ref
# uses the reserved `knowledge:` prefix (PR1: pointer rows are never
# consolidation INPUT). The embedding columns get a scenario-setup preset so
# the concurrent write is a DIFFERENT non-null BLOB / model id, not merely a
# first write into NULL.
_EMBED_BLOB_BEFORE = b"\x11" * 16
_EMBED_BLOB_AFTER = b"\x22" * 16
_NEIGHBOR_FIELD_DRIFTS = (
    ("namespace", "UPDATE memory SET namespace=? WHERE id=?",
     (NS + "-drift",), "namespace", NS + "-drift", None),
    ("pointer source_ref", "UPDATE memory SET source_ref=? WHERE id=?",
     ("knowledge:audit-507-pointer",), "source_ref",
     "knowledge:audit-507-pointer", None),
    ("type", "UPDATE memory SET type=? WHERE id=?",
     ("lesson",), "type", "lesson", None),
    ("confidence", "UPDATE memory SET confidence=? WHERE id=?",
     (0.35,), "confidence", 0.35, None),
    ("signal", "UPDATE memory SET signal=? WHERE id=?",
     ("none",), "signal", "none", None),
    ("taint", "UPDATE memory SET taint=? WHERE id=?",
     ("untrusted_web",), "taint", "untrusted_web", None),
    ("embedding", "UPDATE memory SET embedding=? WHERE id=?",
     (_EMBED_BLOB_AFTER,), "embedding", _EMBED_BLOB_AFTER,
     ("UPDATE memory SET embedding=? WHERE id=?", (_EMBED_BLOB_BEFORE,))),
    ("embedding_model", "UPDATE memory SET embedding_model=? WHERE id=?",
     ("audit507-embedder-v2",), "embedding_model", "audit507-embedder-v2",
     ("UPDATE memory SET embedding_model=? WHERE id=?",
      ("audit507-embedder-v1",))),
    ("ingestion_ts", "UPDATE memory SET ingestion_ts=? WHERE id=?",
     (DRIFTED_TS,), "ingestion_ts", DRIFTED_TS, None),
)

# Round-3 C5: each NON-content field of the KEEPER that a second connection
# rewrites alone (row kept live) after selection — the whole cluster must be
# refused. The tags value is deliberately NOT a task-subject tag: the
# per-absorb #376 revalidation vetoes subject-incompatible keepers already,
# and this table pins the UN-guarded drift class. The keeper-side pointer
# source_ref mirrors the neighbour-side reserved `knowledge:` prefix. The
# embedding columns get a preset for the same reason as the neighbour table.
_KEEPER_FIELD_DRIFTS = (
    ("type", "UPDATE memory SET type=? WHERE id=?",
     ("lesson",), "type", "lesson", None),
    ("tags", "UPDATE memory SET tags=? WHERE id=?",
     ("keeper-extra",), "tags", "keeper-extra", None),
    ("namespace", "UPDATE memory SET namespace=? WHERE id=?",
     (NS + "-drift",), "namespace", NS + "-drift", None),
    ("pointer source_ref", "UPDATE memory SET source_ref=? WHERE id=?",
     ("knowledge:audit-507-keeper-pointer",), "source_ref",
     "knowledge:audit-507-keeper-pointer", None),
    ("confidence", "UPDATE memory SET confidence=? WHERE id=?",
     (0.35,), "confidence", 0.35, None),
    ("signal", "UPDATE memory SET signal=? WHERE id=?",
     ("none",), "signal", "none", None),
    ("taint", "UPDATE memory SET taint=? WHERE id=?",
     ("untrusted_web",), "taint", "untrusted_web", None),
    ("embedding", "UPDATE memory SET embedding=? WHERE id=?",
     (_EMBED_BLOB_AFTER,), "embedding", _EMBED_BLOB_AFTER,
     ("UPDATE memory SET embedding=? WHERE id=?", (_EMBED_BLOB_BEFORE,))),
    ("embedding_model", "UPDATE memory SET embedding_model=? WHERE id=?",
     ("audit507-embedder-v2",), "embedding_model", "audit507-embedder-v2",
     ("UPDATE memory SET embedding_model=? WHERE id=?",
      ("audit507-embedder-v1",))),
    ("ingestion_ts", "UPDATE memory SET ingestion_ts=? WHERE id=?",
     (DRIFTED_TS,), "ingestion_ts", DRIFTED_TS, None),
)

# Env pinned BEFORE any storelib import (and before the issue-#244 store
# isolation guard loads, which reads the ambient env at import time).
_TMP = tempfile.mkdtemp(prefix="zmem-audit507-")
os.environ.update({
    "ZMEM_HOME": str(REPO_ROOT),
    "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
    "ZMEM_DATA": _TMP,
    "ZMEM_MODELS_DIR": os.path.join(_TMP, "missing-models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
})
for _k in ("ZMEM_BACKUP_DIR", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
           "ZMEM_MCP_TOKENS_FILE", "ZMEM_MCP_TOKEN", "ZMEM_MCP_URL",
           "ZMEM_REMOTE_URL"):
    os.environ.pop(_k, None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

_SAVED_MODULES = {k: v for k, v in list(sys.modules.items())
                  if k == "storelib" or k.startswith("storelib.")}
for _name in list(_SAVED_MODULES):
    del sys.modules[_name]
_PATH_INSERTED = str(SCRIPTS_DIR) not in sys.path
if _PATH_INSERTED:
    sys.path.insert(0, str(SCRIPTS_DIR))
_PINNED_KEYS = ("ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING",
                "ZMEM_BACKUP_DIR", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
                "ZMEM_MCP_TOKENS_FILE", "ZMEM_MCP_TOKEN", "ZMEM_MCP_URL",
                "ZMEM_REMOTE_URL")
_SAVED_ENV = {key: os.environ.get(key) for key in _PINNED_KEYS}

import storelib  # noqa: E402,F401  (graph warm-up under the pin above)
from storelib.schema import _normalize_content  # noqa: E402  (pure helper)


def tearDownModule():  # noqa: N802 - unittest hook
    """Restore the ambient env / module cache (pytest co-run citizenship)."""
    for key in [k for k in list(sys.modules)
                if k == "storelib" or k.startswith("storelib.")]:
        del sys.modules[key]
    sys.modules.update(_SAVED_MODULES)
    if _PATH_INSERTED:
        try:
            sys.path.remove(str(SCRIPTS_DIR))
        except ValueError:  # pragma: no cover - defensive
            pass
    for key, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(_TMP, ignore_errors=True)


def _quiet(fn, *args, **kwargs):
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        return fn(*args, **kwargs)


def _one_shot(module, attr, fire):
    """One-shot seam: wrap ``module.attr`` so that AFTER the original has
    run (the decision under test is already computed from pre-hook state),
    ``fire()`` runs exactly once on the second connection and commits while
    the FIRST connection still holds no write lock — the file's existing
    interleave technique. Returns an undo callable."""
    original = getattr(module, attr)
    state = {"fired": False}

    def hooked(*args, **kwargs):
        result = original(*args, **kwargs)
        if not state["fired"]:
            state["fired"] = True
            fire()
        return result

    setattr(module, attr, hooked)
    return lambda: setattr(module, attr, original)


class _InterleaveCaseBase(unittest.TestCase):
    """Shared fixtures for the #507 interleave checks (this module and the
    CLI companion). No test methods, so pytest never collects it twice.

    storelib is resolved at RUN time inside the helpers: under a full-suite
    pytest run this module's tearDownModule purges the storelib module cache
    after its own tests finish, and the companion (which runs later) must
    hook and drive the graph _new_store just refreshed — never an object
    captured at import time."""

    def _new_store(self):
        """Fresh throwaway store; two connections (LIFO cleanup closes them
        before the temp dir is removed)."""
        if str(SCRIPTS_DIR) not in sys.path:
            # tearDownModule (this module) removes SCRIPTS_DIR after its own
            # tests; the CLI companion runs after that and must be able to
            # (re)import storelib for its own legs.
            sys.path.insert(0, str(SCRIPTS_DIR))
        import storelib  # runtime-resolved (see class docstring)
        tmp = tempfile.mkdtemp(prefix="zmem-audit507-case-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.environ["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
        os.environ["ZMEM_DATA"] = tmp
        storelib._refresh_env_state()
        conns = []
        for _ in range(2):
            conn = storelib.connect()
            storelib.init_db(conn)
            storelib.migrate(conn)
            self.addCleanup(conn.close)
            conns.append(conn)
        return conns[0], conns[1]

    def _update_memory(self, conn, **kwargs):
        """update_memory on the RUNTIME storelib.write module — the same
        object _hook_second_writer patches, whatever the run order did to
        sys.modules."""
        import storelib.write as writelib_now
        return _quiet(writelib_now.update_memory, conn, **kwargs)

    def _add_memory(self, conn, **kwargs):
        """add_memory on the runtime storelib.write module (the function the
        CLI's add command calls)."""
        import storelib.write as writelib_now
        return _quiet(writelib_now.add_memory, conn, **kwargs)

    def _insert_row(self, conn, content, retrieval_count):
        mid = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO memory
               (id, namespace, type, content, tags, source_ref, source_hash,
                confidence, signal, valid_from, superseded_at, ingestion_ts,
                retrieval_count, last_retrieved)
               VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,NULL)""",
            (mid, NS, "fact", content, "", "", "", 0.9, "test",
             SEED_TS, SEED_TS, retrieval_count))
        conn.commit()
        return mid

    def _seed_pair(self, conn):
        keeper = self._insert_row(conn, KEEPER_TEXT, retrieval_count=5)
        return keeper, self._insert_row(conn, NEIGHBOR_TEXT, retrieval_count=0)

    def _seed_trio(self, conn):
        """Keeper plus TWO neighbours, both lexically near-duplicate to the
        keeper (and admitted together in its cluster): the mixed-cluster
        leg's working set."""
        keeper = self._insert_row(conn, KEEPER_TEXT, retrieval_count=5)
        first = self._insert_row(conn, NEIGHBOR_TEXT, retrieval_count=0)
        return (keeper, first,
                self._insert_row(conn, NEIGHBOR_B_TEXT, retrieval_count=0))

    def _seed_one(self, conn, prefix):
        return str(self._add_memory(
            conn, namespace=NS, type_="fact",
            content="%s fact %s" % (prefix, uuid.uuid4().hex),
            tags="orig-tag", source_ref="audit", signal="test",
            confidence=0.9))

    def _row(self, conn, mid):
        return conn.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone()

    def _run_consolidate(self, conn):
        mod = importlib.import_module("storelib.consolidate")
        return _quiet(mod.consolidate, conn, force=True)

    def _invalidate(self, conn, mid):
        conn.execute(
            "UPDATE memory SET superseded_at=?, valid_until=?, "
            "supersede_reason=? WHERE id=?",
            (A_NOW, A_NOW, INVALIDATE_REASON, mid))
        conn.commit()

    def _live_successors(self, conn, mid):
        return [r["id"] for r in conn.execute(
            "SELECT id FROM memory WHERE update_of=? AND superseded_at IS NULL",
            (mid,))]

    def _hook_invalidate_during_admission(self, conn, other, victim):
        """One-shot seam (the frozen interleave point): while consolidation
        pauses inside _admitted_neighbors — AFTER the admitted list has been
        computed, BEFORE the per-cluster BEGIN — a second connection
        tombstones ``victim`` and commits. The consolidating connection
        holds no write lock at that moment, so the second writer's commit
        lands; what must change is what consolidate does with its now-stale
        decision."""
        mod = importlib.import_module("storelib.consolidate")
        undo = _one_shot(mod, "_admitted_neighbors",
                         lambda: self._invalidate(other, victim))
        try:
            return self._run_consolidate(conn)
        finally:
            undo()

    def _hook_rewrite_during_admission(self, conn, other, sql, params):
        """Same one-shot seam and interleave point as
        _hook_invalidate_during_admission, but the second connection
        REWRITES row state (content / tags) instead of tombstoning it: the
        row stays live while the admission decision consolidate already
        made goes stale."""
        mod = importlib.import_module("storelib.consolidate")

        def fire():
            other.execute(sql, params)
            other.commit()

        undo = _one_shot(mod, "_admitted_neighbors", fire)
        try:
            return self._run_consolidate(conn)
        finally:
            undo()

    def _hook_second_writer(self, fire):
        """One-shot seam on storelib.write._validate_namespace: after the
        original validation returns — update_memory has read the target row
        but has NOT yet taken its BEGIN IMMEDIATE write lock — ``fire()``
        commits on the second connection. That call sits between the
        pre-lock row read and the write transaction, which is exactly the
        window AC2 pins; the first connection holds no write lock there, so
        the second writer's commit lands."""
        import storelib.write as writelib_now
        return _one_shot(writelib_now, "_validate_namespace", fire)

    def _cli(self, argv):
        """Drive the PUBLIC command surface in-process: swap sys.argv, call
        storelib.cli.main() with redirected streams, and catch its
        SystemExit as the exit code (the house pattern of
        tests/test_reembed.py's run_reembed). The CLI opens its own
        connection from the env _new_store just pinned, so the second
        connection's interleave (injected through the same one-shot library
        seams) is visible to it. Returns (rc, stdout, stderr)."""
        from storelib.cli import main as cli_main
        old_argv = sys.argv
        sys.argv = list(argv)
        out_buf, err_buf = io.StringIO(), io.StringIO()
        rc = 0
        try:
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                try:
                    cli_main()
                except SystemExit as exc:
                    rc = exc.code or 0
        finally:
            sys.argv = old_argv
        return rc, out_buf.getvalue(), err_buf.getvalue()


class AuditWriteRevalidationTest(_InterleaveCaseBase):
    """Consolidation and update must not commit decisions made on stale rows."""

    def test_invalidated_neighbor(self):
        """AC5-507 (RED at base, GREEN after the fix): a consolidation
        candidate invalidated — or whose content/tags drift — after
        selection keeps that concurrent change; the stale merge is refused
        with truthful bookkeeping."""
        with self.subTest("control: uncontested pair merges"):
            conn, _other = self._new_store()
            keeper, neighbor = self._seed_pair(conn)
            report = self._run_consolidate(conn)
            self.assertEqual(report["merged"], 1, "AC5-507 control: the "
                             "uncontested pair must report exactly one merge "
                             "(got %r)" % (report["merged"],))
            self.assertEqual(self._row(conn, neighbor)["supersede_reason"],
                             "consolidated into %s" % keeper,
                             "AC5-507 control: the absorbed row must keep the "
                             "'consolidated into' tombstone (got %r)"
                             % (self._row(conn, neighbor)["supersede_reason"],))
            self.assertIn("--- merged from %s ---" % neighbor,
                          self._row(conn, keeper)["content"],
                          "AC5-507 control: the keeper must contain the "
                          "merged neighbour text")

        with self.subTest("neighbor invalidated mid-flight"):
            conn, other = self._new_store()
            keeper, neighbor = self._seed_pair(conn)
            consolidated_before = self._row(conn, keeper)["consolidated_at"]
            report = self._hook_invalidate_during_admission(
                conn, other, neighbor)
            nb = self._row(conn, neighbor)
            self.assertEqual(
                (nb["superseded_at"], nb["supersede_reason"]),
                (A_NOW, INVALIDATE_REASON),
                "AC5-507: consolidation overwrote a neighbour's concurrent "
                "invalidation: tombstone now (%r, %r), second writer wrote "
                "(%r, %r)" % (nb["superseded_at"], nb["supersede_reason"],
                              A_NOW, INVALIDATE_REASON))
            kp = self._row(conn, keeper)
            self.assertEqual(kp["content"], KEEPER_TEXT,
                             "AC5-507: the keeper's content grew with the "
                             "invalidated neighbour's text (now %r)"
                             % (kp["content"],))
            self.assertNotIn(neighbor, kp["merged_from"] or "",
                             "AC5-507: the keeper's merged_from lists the "
                             "invalidated neighbour (merged_from=%r)"
                             % (kp["merged_from"],))
            self.assertEqual(report["merged"], 0,
                             "AC5-507: the invalidated neighbour was reported "
                             "as merged (report['merged']=%r)"
                             % (report["merged"],))
            # Bookkeeping when nothing is absorbed: a skipped cluster must
            # not stamp the keeper, count a merge, or claim one anywhere in
            # the machine report.
            self.assertEqual(
                self._row(conn, keeper)["consolidated_at"], consolidated_before,
                "AC5-507: the keeper's consolidated_at changed although no "
                "neighbour was absorbed (before=%r, now=%r)"
                % (consolidated_before,
                   self._row(conn, keeper)["consolidated_at"]))
            for entry in report.get("contested_clusters", []):
                if entry.get("keeper") == keeper and entry.get("merged"):
                    self.fail(
                        "AC5-507: a contested-cluster report entry claims a "
                        "merge for the keeper whose cluster was skipped "
                        "(entry=%r)" % (entry,))
            for list_key in ("absorbed", "consolidated_ids"):
                claimed = report.get(list_key) or []
                self.assertNotIn(
                    neighbor, claimed,
                    "AC5-507: report[%r] lists the neighbour although no "
                    "absorb happened (%r)" % (list_key, claimed))

        with self.subTest("keeper invalidated mid-flight"):
            conn, other = self._new_store()
            keeper, neighbor = self._seed_pair(conn)
            report = self._hook_invalidate_during_admission(
                conn, other, keeper)
            self.assertEqual(self._row(conn, keeper)["content"], KEEPER_TEXT,
                             "AC5-507: the merge ran into an invalidated "
                             "keeper: its content grew with the neighbour's "
                             "text")
            self.assertEqual(
                self._row(conn, keeper)["supersede_reason"], INVALIDATE_REASON,
                "AC5-507: the invalidated keeper's own tombstone did not "
                "survive the consolidation run")
            self.assertIsNone(self._row(conn, neighbor)["superseded_at"],
                              "AC5-507: the live neighbour was tombstoned "
                              "into an invalidated keeper, removing its "
                              "content from live recall")
            self.assertEqual(report["merged"], 0,
                             "AC5-507: a merge into an invalidated keeper "
                             "was reported as done (report['merged']=%r)"
                             % (report["merged"],))
            # No open transaction left behind: a skipped cluster must return
            # the caller's connection to the auto-commit state, or the next
            # writer on that connection fails with "cannot start a
            # transaction within a transaction".
            self.assertFalse(
                conn.in_transaction,
                "AC5-507: consolidate returned with a transaction still open "
                "on the caller's connection after skipping the cluster")
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
            except sqlite3.Error as exc:
                self.fail("AC5-507: a fresh BEGIN IMMEDIATE on the caller's "
                          "connection failed after consolidate returned "
                          "(%s)" % (exc,))

        with self.subTest("neighbor content rewritten mid-flight"):
            conn, other = self._new_store()
            keeper, neighbor = self._seed_pair(conn)
            report = self._hook_rewrite_during_admission(
                conn, other,
                "UPDATE memory SET content=?, content_norm=NULL WHERE id=?",
                (DRIFTED_TEXT, neighbor))
            # content_norm is set to NULL by the second writer so no stale
            # normalised copy of the OLD content survives either.
            self.assertEqual(report["merged"], 0,
                             "AC5-507: consolidation merged a neighbour whose "
                             "content was rewritten after selection — the "
                             "stale decision must be refused, not committed "
                             "(report['merged']=%r)" % (report["merged"],))
            nb = self._row(conn, neighbor)
            self.assertIsNone(
                nb["superseded_at"],
                "AC5-507: the content-drifted neighbour was tombstoned by "
                "the stale merge, taking its NEW content out of live recall")
            self.assertEqual(nb["content"], DRIFTED_TEXT,
                             "AC5-507: the neighbour no longer holds the "
                             "concurrently committed content (content=%r)"
                             % (nb["content"],))
            self.assertEqual(
                self._row(conn, keeper)["content"], KEEPER_TEXT,
                "AC5-507: the keeper's content grew with the neighbour's "
                "pre-rewrite text (now %r)"
                % (self._row(conn, keeper)["content"],))
            self.assertNotIn(
                neighbor, self._row(conn, keeper)["merged_from"] or "",
                "AC5-507: the keeper's merged_from lists the content-drifted "
                "neighbour (merged_from=%r)"
                % (self._row(conn, keeper)["merged_from"],))
            # A recomputed normalised copy is fine; the stale normalisation
            # of the OLD text is not.
            if nb["content_norm"] is not None:
                self.assertEqual(
                    nb["content_norm"], _normalize_content(DRIFTED_TEXT),
                    "AC5-507: the neighbour kept a stale normalised copy of "
                    "its pre-rewrite content (content_norm=%r)"
                    % (nb["content_norm"],))

        with self.subTest("neighbor tags rewritten mid-flight"):
            conn, other = self._new_store()
            keeper, neighbor = self._seed_pair(conn)
            report = self._hook_rewrite_during_admission(
                conn, other, "UPDATE memory SET tags=? WHERE id=?",
                (DRIFTED_TAGS, neighbor))
            self.assertEqual(report["merged"], 0,
                             "AC5-507: consolidation merged a neighbour whose "
                             "tags were rewritten after selection — the stale "
                             "decision must be refused, not committed "
                             "(report['merged']=%r)" % (report["merged"],))
            nb = self._row(conn, neighbor)
            self.assertIsNone(
                nb["superseded_at"],
                "AC5-507: the tag-drifted neighbour was tombstoned by the "
                "stale merge although nothing about its admission still "
                "held")
            self.assertEqual(nb["tags"], DRIFTED_TAGS,
                             "AC5-507: the neighbour's live row does not "
                             "carry its concurrently committed tags "
                             "(tags=%r)" % (nb["tags"],))
            self.assertEqual(
                self._row(conn, keeper)["content"], KEEPER_TEXT,
                "AC5-507: the keeper's content grew with the neighbour's "
                "text committed under the pre-rewrite tags (now %r)"
                % (self._row(conn, keeper)["content"],))
            self.assertNotIn(
                neighbor, self._row(conn, keeper)["merged_from"] or "",
                "AC5-507: the keeper's merged_from lists the tag-drifted "
                "neighbour (merged_from=%r)"
                % (self._row(conn, keeper)["merged_from"],))

        # Round 2, critic leg 1: EACH field of the selected neighbour, changed
        # alone with the row kept live, must also refuse the stale decision —
        # not only content and tags.
        for (field_label, hook_sql, hook_params, column, expected,
             preset) in _NEIGHBOR_FIELD_DRIFTS:
            with self.subTest("neighbor %s rewritten mid-flight"
                              % (field_label,)):
                conn, other = self._new_store()
                keeper, neighbor = self._seed_pair(conn)
                if preset is not None:
                    # scenario setup: give the column a known starting value
                    # so the concurrent write lands as a DIFFERENT value.
                    conn.execute(preset[0], preset[1] + (neighbor,))
                    conn.commit()
                report = self._hook_rewrite_during_admission(
                    conn, other, hook_sql, hook_params + (neighbor,))
                self.assertEqual(
                    report["merged"], 0,
                    "AC5-507: consolidation merged a neighbour whose %s was "
                    "rewritten after selection — the stale decision must be "
                    "refused, not committed (report['merged']=%r)"
                    % (field_label, report["merged"],))
                nb = self._row(conn, neighbor)
                self.assertIsNone(
                    nb["superseded_at"],
                    "AC5-507: the %s-drifted neighbour was tombstoned by "
                    "the stale merge, taking its concurrently committed "
                    "value out of live recall" % (field_label,))
                self.assertEqual(
                    nb[column], expected,
                    "AC5-507: the neighbour's live row does not carry its "
                    "concurrently committed %s (%s=%r, expected %r)"
                    % (field_label, column, nb[column], expected))
                self.assertEqual(
                    self._row(conn, keeper)["content"], KEEPER_TEXT,
                    "AC5-507: the keeper's content grew with the "
                    "%s-drifted neighbour's pre-rewrite text (now %r)"
                    % (field_label, self._row(conn, keeper)["content"],))
                self.assertNotIn(
                    neighbor, self._row(conn, keeper)["merged_from"] or "",
                    "AC5-507: the keeper's merged_from lists the %s-drifted "
                    "neighbour (merged_from=%r)"
                    % (field_label, self._row(conn, keeper)["merged_from"],))

        # Round 2, critic leg 2: the KEEPER's own content rewritten after
        # selection refuses the whole cluster.
        with self.subTest("keeper content rewritten mid-flight"):
            conn, other = self._new_store()
            keeper, neighbor = self._seed_pair(conn)
            consolidated_before = self._row(conn, keeper)["consolidated_at"]
            report = self._hook_rewrite_during_admission(
                conn, other,
                "UPDATE memory SET content=?, content_norm=NULL WHERE id=?",
                (KEEPER_DRIFT_TEXT, keeper))
            self.assertEqual(report["merged"], 0,
                             "AC5-507: consolidation merged into a keeper "
                             "whose content was rewritten after selection — "
                             "the stale decision must be refused, not "
                             "committed (report['merged']=%r)"
                             % (report["merged"],))
            kp = self._row(conn, keeper)
            self.assertEqual(kp["content"], KEEPER_DRIFT_TEXT,
                             "AC5-507: the keeper no longer holds its "
                             "concurrently committed content — the stale "
                             "merge appended the neighbour's text onto it "
                             "(content=%r)" % (kp["content"],))
            self.assertEqual(
                kp["consolidated_at"], consolidated_before,
                "AC5-507: the skipped cluster still stamped the keeper's "
                "consolidated_at (before=%r, now=%r)"
                % (consolidated_before, kp["consolidated_at"]))
            self.assertIsNone(
                self._row(conn, neighbor)["superseded_at"],
                "AC5-507: the live neighbour was tombstoned into a keeper "
                "whose content had changed after selection")
            # A recomputed normalised copy is fine; the stale normalisation
            # of the OLD text is not.
            if kp["content_norm"] is not None:
                self.assertEqual(
                    kp["content_norm"],
                    _normalize_content(KEEPER_DRIFT_TEXT),
                    "AC5-507: the keeper kept a stale normalised copy of its "
                    "pre-rewrite content (content_norm=%r)"
                    % (kp["content_norm"],))

        # Round 3, critic legs: each NON-content field of the KEEPER, changed
        # alone with the row kept live after selection, refuses the WHOLE
        # cluster — not only content and invalidation.
        for (field_label, hook_sql, hook_params, column, expected,
             preset) in _KEEPER_FIELD_DRIFTS:
            with self.subTest("keeper %s rewritten mid-flight"
                              % (field_label,)):
                conn, other = self._new_store()
                keeper, neighbor = self._seed_pair(conn)
                if preset is not None:
                    # scenario setup: give the column a known starting value
                    # so the concurrent write lands as a DIFFERENT value.
                    conn.execute(preset[0], preset[1] + (keeper,))
                    conn.commit()
                consolidated_before = \
                    self._row(conn, keeper)["consolidated_at"]
                neighbor_before = dict(self._row(conn, neighbor))
                report = self._hook_rewrite_during_admission(
                    conn, other, hook_sql, hook_params + (keeper,))
                self.assertEqual(
                    report["merged"], 0,
                    "AC5-507: the cluster merged although the keeper's %s "
                    "was rewritten after selection — the stale decision must "
                    "be refused, not committed (report['merged']=%r)"
                    % (field_label, report["merged"],))
                kp = self._row(conn, keeper)
                self.assertEqual(
                    kp[column], expected,
                    "AC5-507: the keeper no longer holds its concurrently "
                    "committed %s (%s=%r, expected %r)"
                    % (field_label, column, kp[column], expected))
                self.assertEqual(
                    kp["consolidated_at"], consolidated_before,
                    "AC5-507: the refused cluster still stamped the keeper's "
                    "consolidated_at (before=%r, now=%r)"
                    % (consolidated_before, kp["consolidated_at"]))
                nb = self._row(conn, neighbor)
                self.assertIsNone(
                    nb["superseded_at"],
                    "AC5-507: the live neighbour was tombstoned into a "
                    "keeper whose %s had changed after selection, removing "
                    "its content from live recall" % (field_label,))
                neighbor_after = dict(nb)
                self.assertEqual(
                    neighbor_after, neighbor_before,
                    "AC5-507: the neighbour row was modified although the "
                    "cluster was refused (changed fields: %r)"
                    % (sorted(key for key in neighbor_before
                              if neighbor_after.get(key)
                              != neighbor_before[key]),))
                # No report entry claims a merge for the refused cluster.
                for entry in report.get("contested_clusters", []):
                    if entry.get("keeper") == keeper and entry.get("merged"):
                        self.fail(
                            "AC5-507: a contested-cluster report entry "
                            "claims a merge for the %s-drifted keeper "
                            "(entry=%r)" % (field_label, entry,))
                for list_key in ("absorbed", "consolidated_ids"):
                    claimed = report.get(list_key) or []
                    self.assertNotIn(
                        keeper, claimed,
                        "AC5-507: report[%r] lists the %s-drifted keeper "
                        "although the cluster was refused (%r)"
                        % (list_key, field_label, claimed))
                    self.assertNotIn(
                        neighbor, claimed,
                        "AC5-507: report[%r] lists the neighbour although "
                        "the cluster was refused (%r)"
                        % (list_key, claimed))

        # Round 2, critic leg 3: mixed cluster — refusing the changed
        # neighbour must not also refuse the untouched one.
        with self.subTest("mixed cluster: only the changed neighbour refused"):
            proof_conn, _proof_other = self._new_store()
            proof_keeper, proof_first, proof_second = \
                self._seed_trio(proof_conn)
            proof_report = self._run_consolidate(proof_conn)
            self.assertEqual(
                proof_report["merged"], 2,
                "AC5-507 scenario setup: without interference the keeper "
                "plus two neighbours must merge BOTH neighbours "
                "(report['merged']=%r); the mixed-cluster scenario below is "
                "not valid without that baseline" % (proof_report["merged"],))
            for proof_nb, proof_label in ((proof_first, "first"),
                                          (proof_second, "second")):
                self.assertEqual(
                    self._row(proof_conn, proof_nb)["supersede_reason"],
                    "consolidated into %s" % proof_keeper,
                    "AC5-507 scenario setup: the clean baseline must absorb "
                    "the %s neighbour into the keeper (supersede_reason=%r)"
                    % (proof_label,
                       self._row(proof_conn, proof_nb)["supersede_reason"]))

            conn, other = self._new_store()
            keeper, first, second = self._seed_trio(conn)
            report = self._hook_rewrite_during_admission(
                conn, other,
                "UPDATE memory SET content=?, content_norm=NULL WHERE id=?",
                (NEIGHBOR_B_DRIFT_TEXT, second))
            self.assertEqual(
                report["merged"], 1,
                "AC5-507: the cluster merged BOTH neighbours although the "
                "second one's content was rewritten after selection — only "
                "the untouched neighbour may merge (report['merged']=%r)"
                % (report["merged"],))
            self.assertEqual(
                self._row(conn, first)["supersede_reason"],
                "consolidated into %s" % keeper,
                "AC5-507: refusing the changed neighbour also refused the "
                "untouched one (supersede_reason=%r)"
                % (self._row(conn, first)["supersede_reason"],))
            self.assertIn(
                NEIGHBOR_TEXT, self._row(conn, keeper)["content"],
                "AC5-507: the keeper's content does not contain the "
                "untouched neighbour's text")
            nb = self._row(conn, second)
            self.assertIsNone(
                nb["superseded_at"],
                "AC5-507: the changed neighbour was tombstoned by the stale "
                "decision, taking its NEW content out of live recall")
            self.assertEqual(nb["content"], NEIGHBOR_B_DRIFT_TEXT,
                             "AC5-507: the changed neighbour no longer holds "
                             "its concurrently committed content "
                             "(content=%r)" % (nb["content"],))
            self.assertNotIn(
                second, self._row(conn, keeper)["merged_from"] or "",
                "AC5-507: the keeper's merged_from lists the changed "
                "neighbour (merged_from=%r)"
                % (self._row(conn, keeper)["merged_from"],))
            # No report entry claims the changed neighbour as merged.
            for entry in report.get("contested_clusters", []):
                member_ids = [m.get("id")
                              for m in entry.get("members", [])]
                if second in member_ids and entry.get("merged"):
                    self.fail(
                        "AC5-507: a contested-cluster report entry claims "
                        "the changed neighbour as merged (entry=%r)"
                        % (entry,))
            for list_key in ("absorbed", "consolidated_ids"):
                claimed = report.get(list_key) or []
                self.assertNotIn(
                    second, claimed,
                    "AC5-507: report[%r] lists the changed neighbour "
                    "although it was refused (%r)" % (list_key, claimed))

    def test_metadata_interleave(self):
        """AC6-507 (RED at base, GREEN after the fix): metadata another
        connection commits between update_memory's read and its write is
        preserved in the surviving successor — for every inherited field
        (type, tags, source_ref, confidence, signal) and the values derived
        from them (source_hash, taint) — while explicit caller values still
        win and the reserved-reference guard still refuses."""
        new_content = "corrected deployment fact " + uuid.uuid4().hex

        with self.subTest("concurrent metadata preserved"):
            conn, other = self._new_store()
            mid = self._seed_one(conn, "deployment")

            def fire():
                other.execute(
                    "UPDATE memory SET tags=?, confidence=?, signal=? "
                    "WHERE id=?", ("concurrent-tag", 0.45, "user", mid))
                other.commit()

            undo = self._hook_second_writer(fire)
            try:
                self._update_memory(conn, mid=mid, content=new_content,
                                    supersede_reason="updated", now=B_NOW)
            finally:
                undo()
            live = self._live_successors(conn, mid)
            self.assertEqual(len(live), 1,
                             "AC6-507: exactly one live successor must exist "
                             "after the update (found %r)" % (live,))
            succ = self._row(conn, live[0])
            self.assertEqual(succ["content"], new_content,
                             "AC6-507: the successor lost the caller's new "
                             "content (content=%r)" % (succ["content"],))
            self.assertEqual(succ["tags"], "concurrent-tag",
                             "AC6-507: the successor carries stale tags from "
                             "the pre-lock read instead of the concurrently "
                             "committed value (tags=%r)" % (succ["tags"],))
            self.assertAlmostEqual(float(succ["confidence"]), 0.45, places=6,
                                   msg="AC6-507: the successor carries a "
                                   "stale confidence from the pre-lock read "
                                   "instead of the concurrently committed "
                                   "value (confidence=%r)"
                                   % (succ["confidence"],))
            self.assertEqual(succ["signal"], "user",
                             "AC6-507: the successor carries a stale signal "
                             "from the pre-lock read instead of the "
                             "concurrently committed value (signal=%r)"
                             % (succ["signal"],))
            old = self._row(conn, mid)
            self.assertEqual((old["tags"], old["signal"]),
                             ("concurrent-tag", "user"),
                             "AC6-507: the old row no longer keeps the "
                             "concurrently committed values (tags=%r, "
                             "signal=%r)" % (old["tags"], old["signal"]))

        with self.subTest("concurrent type and source_ref preserved"):
            conn, other = self._new_store()
            mid = self._seed_one(conn, "identity")
            identity_content = "corrected identity fact " + uuid.uuid4().hex

            def fire():
                other.execute(
                    "UPDATE memory SET type=?, source_ref=? WHERE id=?",
                    ("lesson", "session:concurrent-xyz", mid))
                other.commit()

            undo = self._hook_second_writer(fire)
            try:
                self._update_memory(conn, mid=mid, content=identity_content,
                                    supersede_reason="updated", now=B_NOW)
            finally:
                undo()
            live = self._live_successors(conn, mid)
            self.assertEqual(len(live), 1,
                             "AC6-507: exactly one live successor must exist "
                             "after the update (found %r)" % (live,))
            succ = self._row(conn, live[0])
            self.assertEqual(succ["content"], identity_content,
                             "AC6-507: the successor lost the caller's new "
                             "content (content=%r)" % (succ["content"],))
            self.assertEqual(succ["type"], "lesson",
                             "AC6-507: the successor carries the stale type "
                             "from the pre-lock read instead of the "
                             "concurrently committed value (type=%r; "
                             "expected 'lesson')"
                             % (succ["type"],))
            self.assertEqual(succ["source_ref"], "session:concurrent-xyz",
                             "AC6-507: the successor carries the stale "
                             "source_ref from the pre-lock read instead of "
                             "the concurrently committed value "
                             "(source_ref=%r)" % (succ["source_ref"],))
            old = self._row(conn, mid)
            self.assertEqual((old["type"], old["source_ref"]),
                             ("lesson", "session:concurrent-xyz"),
                             "AC6-507: the old row no longer keeps the "
                             "concurrently committed identity (type=%r, "
                             "source_ref=%r)"
                             % (old["type"], old["source_ref"]))

        with self.subTest("derived source_hash follows the final source_ref"):
            # The expected hash is what the PUBLIC add path gives a row with
            # the same content and the same source_ref: a reference row is
            # created through add_memory (the function the CLI's add command
            # calls) in a SEPARATE throwaway store, so the update's dedup
            # cannot fold the two rows together.
            src_dir = tempfile.mkdtemp(prefix="zmem-audit507-src-")
            self.addCleanup(shutil.rmtree, src_dir, ignore_errors=True)
            src_path = os.path.join(src_dir, "concurrent_source.md")
            with open(src_path, "wb") as fh:
                fh.write(b"concurrent source bytes rev 1\n")
            file_ref = "file:" + src_path.replace(os.sep, "/")
            hash_content = "corrected sourced fact " + uuid.uuid4().hex
            ref_conn, _ref_other = self._new_store()
            ref_id = str(self._add_memory(
                ref_conn, namespace=NS, type_="fact",
                content=hash_content, tags="ref", source_ref=file_ref,
                signal="test", confidence=0.9))
            ref_hash = self._row(ref_conn, ref_id)["source_hash"]
            self.assertTrue(
                ref_hash,
                "AC6-507 scenario setup: the public add path must hash a "
                "file: source_ref (reference row source_hash=%r for ref=%r); "
                "without a non-empty reference hash this leg cannot "
                "discriminate" % (ref_hash, file_ref))

            conn, other = self._new_store()
            mid = self._seed_one(conn, "sourced")

            def fire():
                other.execute("UPDATE memory SET source_ref=? WHERE id=?",
                              (file_ref, mid))
                other.commit()

            undo = self._hook_second_writer(fire)
            try:
                self._update_memory(conn, mid=mid, content=hash_content,
                                    supersede_reason="updated", now=B_NOW)
            finally:
                undo()
            live = self._live_successors(conn, mid)
            self.assertEqual(len(live), 1,
                             "AC6-507: exactly one live successor must exist "
                             "after the update (found %r)" % (live,))
            succ = self._row(conn, live[0])
            self.assertEqual(succ["source_ref"], file_ref,
                             "AC6-507: the successor carries the stale "
                             "source_ref from the pre-lock read instead of "
                             "the concurrently committed file ref "
                             "(source_ref=%r)" % (succ["source_ref"],))
            self.assertEqual(succ["source_hash"], ref_hash,
                             "AC6-507: the successor's derived source_hash "
                             "does not match what the public add path hashes "
                             "for the FINAL source_ref (source_hash=%r, "
                             "expected %r) — it was derived from the stale "
                             "pre-lock ref"
                             % (succ["source_hash"], ref_hash))

        with self.subTest("derived taint follows the final signal"):
            # Per storelib.write._default_taint_for_signal, an add with no
            # explicit taint derives 'untrusted_tool' for signal 'none' and
            # 'trusted_internal' for the trusted signal 'user' — the
            # scenario-setup assertion below pins that difference (the thing
            # that makes this leg discriminate).
            ref_conn, _ref_other = self._new_store()
            taint_for = {}
            for ref_signal in ("none", "user"):
                rid = str(self._add_memory(
                    ref_conn, namespace=NS, type_="fact",
                    content="taint ref %s %s" % (ref_signal,
                                                 uuid.uuid4().hex),
                    tags="ref", source_ref="audit", signal=ref_signal,
                    confidence=0.9))
                taint_for[ref_signal] = self._row(ref_conn, rid)["taint"]
            self.assertNotEqual(
                taint_for["none"], taint_for["user"],
                "AC6-507 scenario setup: the public add path must derive "
                "different taints for signals 'none' and 'user' when no "
                "taint is passed (got %r for both); without a difference "
                "this leg cannot discriminate" % (taint_for,))

            conn, other = self._new_store()
            # Seed the row with signal 'none' but an EXPLICIT trusted taint:
            # the successor's taint is worst-of(the old lineage, the base
            # derived from the effective signal), so a row that already
            # carried the untrusted derivation would mask the drift this
            # leg must expose.
            mid = str(self._add_memory(
                conn, namespace=NS, type_="fact",
                content="taint fact " + uuid.uuid4().hex, tags="taint",
                source_ref="audit", signal="none",
                taint="trusted_internal", confidence=0.9))

            def fire():
                other.execute("UPDATE memory SET signal=? WHERE id=?",
                              ("user", mid))
                other.commit()

            undo = self._hook_second_writer(fire)
            try:
                self._update_memory(
                    conn, mid=mid,
                    content="taint fact corrected " + uuid.uuid4().hex,
                    supersede_reason="updated", now=B_NOW)
            finally:
                undo()
            live = self._live_successors(conn, mid)
            self.assertEqual(len(live), 1,
                             "AC6-507: exactly one live successor must exist "
                             "after the update (found %r)" % (live,))
            succ = self._row(conn, live[0])
            self.assertEqual(succ["signal"], "user",
                             "AC6-507: the successor carries the stale signal "
                             "from the pre-lock read instead of the "
                             "concurrently committed value (signal=%r)"
                             % (succ["signal"],))
            self.assertEqual(succ["taint"], taint_for["user"],
                             "AC6-507: the successor's derived taint was "
                             "computed from the pre-lock signal, not the "
                             "FINAL one (taint=%r, expected the %r that the "
                             "public add path derives for signal 'user')"
                             % (succ["taint"], taint_for["user"]))

        with self.subTest(
                "derived taint follows the final signal (trusted to untrusted)"):
            # Round 2, critic leg 4 — the mirror direction of the leg above:
            # the row starts on a TRUSTED signal ('user', so its own lineage
            # derives trusted_internal) and the second connection flips the
            # signal to 'none'. The expected taint is again established from
            # the PUBLIC add path, and the successor must carry the FINAL
            # signal's derivation, not the pre-lock one.
            ref_conn, _ref_other = self._new_store()
            rid = str(self._add_memory(
                ref_conn, namespace=NS, type_="fact",
                content="taint ref flip none %s" % uuid.uuid4().hex,
                tags="ref", source_ref="audit", signal="none",
                confidence=0.9))
            taint_for_none = self._row(ref_conn, rid)["taint"]
            self.assertTrue(
                taint_for_none,
                "AC6-507 scenario setup: the public add path must derive a "
                "non-empty taint for signal 'none' (got %r); without it this "
                "leg cannot discriminate" % (taint_for_none,))

            conn, other = self._new_store()
            mid = str(self._add_memory(
                conn, namespace=NS, type_="fact",
                content="taint flip fact " + uuid.uuid4().hex, tags="taint",
                source_ref="audit", signal="user", confidence=0.9))

            def fire():
                other.execute("UPDATE memory SET signal=? WHERE id=?",
                              ("none", mid))
                other.commit()

            undo = self._hook_second_writer(fire)
            try:
                self._update_memory(
                    conn, mid=mid,
                    content="taint flip fact v2 " + uuid.uuid4().hex,
                    supersede_reason="updated", now=B_NOW)
            finally:
                undo()
            live = self._live_successors(conn, mid)
            self.assertEqual(len(live), 1,
                             "AC6-507: exactly one live successor must exist "
                             "after the update (found %r)" % (live,))
            succ = self._row(conn, live[0])
            self.assertEqual(succ["signal"], "none",
                             "AC6-507: the successor carries the stale signal "
                             "from the pre-lock read instead of the "
                             "concurrently committed value (signal=%r)"
                             % (succ["signal"],))
            self.assertEqual(succ["taint"], taint_for_none,
                             "AC6-507: the successor's derived taint was "
                             "computed from the pre-lock signal ('user'), "
                             "not the FINAL one ('none') (taint=%r, expected "
                             "the %r that the public add path derives for "
                             "signal 'none')"
                             % (succ["taint"], taint_for_none))

        with self.subTest("explicit caller tags win over the concurrent value"):
            conn, other = self._new_store()
            mid = self._seed_one(conn, "explicit")

            def fire():
                other.execute("UPDATE memory SET tags=? WHERE id=?",
                              ("concurrent-tag", mid))
                other.commit()

            undo = self._hook_second_writer(fire)
            try:
                self._update_memory(
                    conn, mid=mid,
                    content="explicit tags v2 " + uuid.uuid4().hex,
                    tags="caller-tag", supersede_reason="updated",
                    now=B_NOW)
            finally:
                undo()
            live = self._live_successors(conn, mid)
            self.assertEqual(len(live), 1,
                             "AC6-507: exactly one live successor must exist "
                             "after the update (found %r)" % (live,))
            succ = self._row(conn, live[0])
            self.assertEqual(succ["tags"], "caller-tag",
                             "AC6-507: the successor dropped the caller's "
                             "EXPLICIT tags in favour of the concurrently "
                             "committed value (tags=%r; an explicit caller "
                             "value must win)" % (succ["tags"],))
            old = self._row(conn, mid)
            self.assertEqual(old["tags"], "concurrent-tag",
                             "AC6-507: the old row no longer keeps the "
                             "concurrently committed tags (tags=%r)"
                             % (old["tags"],))

        with self.subTest("control: inheritance without competition"):
            conn, _other = self._new_store()
            mid = self._seed_one(conn, "inherit")
            self._update_memory(conn, mid=mid,
                                content="inherited v2 " + uuid.uuid4().hex,
                                supersede_reason="updated", now=B_NOW)
            live = self._live_successors(conn, mid)
            self.assertEqual(len(live), 1,
                             "AC6-507 control: exactly one live successor "
                             "must exist (found %r)" % (live,))
            succ = self._row(conn, live[0])
            self.assertEqual(succ["tags"], "orig-tag",
                             "AC6-507 control: without competition, "
                             "unspecified fields must be inherited from the "
                             "old row (tags=%r)" % (succ["tags"],))
            self.assertEqual(succ["signal"], "test",
                             "AC6-507 control: without competition, "
                             "unspecified fields must be inherited from the "
                             "old row (signal=%r)" % (succ["signal"],))
            self.assertAlmostEqual(float(succ["confidence"]), 0.9, places=6,
                                   msg="AC6-507 control: without competition, "
                                   "unspecified fields must be inherited from "
                                   "the old row (confidence=%r)"
                                   % (succ["confidence"],))
            self.assertEqual(succ["type"], "fact",
                             "AC6-507 control: without competition, "
                             "unspecified fields must be inherited from the "
                             "old row (type=%r)" % (succ["type"],))
            self.assertEqual(succ["source_ref"], "audit",
                             "AC6-507 control: without competition, "
                             "unspecified fields must be inherited from the "
                             "old row (source_ref=%r)" % (succ["source_ref"],))

        with self.subTest("control: reserved card: source_ref still refused"):
            conn, _other = self._new_store()
            mid = self._seed_one(conn, "reserved")
            refused = None
            try:
                self._update_memory(
                    conn, mid=mid,
                    content="reserved ref probe " + uuid.uuid4().hex,
                    source_ref="card:forge-attempt-507",
                    supersede_reason="updated", now=B_NOW)
            except ValueError as exc:
                refused = str(exc)
            self.assertIsNotNone(
                refused,
                "AC6-507 control: an ordinary row claiming a card: "
                "source_ref must be refused")
            self.assertIn("card", (refused or "").lower(),
                          "AC6-507 control: the reserved-reference refusal "
                          "must mention 'card' or 'entity' (got %r)"
                          % (refused,))
            self.assertIsNone(
                self._row(conn, mid)["superseded_at"],
                "AC6-507 control: the refused update still tombstoned the "
                "old row")
            self.assertEqual(
                self._live_successors(conn, mid), [],
                "AC6-507 control: the refused update still minted a "
                "successor")

        with self.subTest("knowledge: pointer claim under the interleave refused"):
            # Round 2, critic leg 5: the reserved knowledge: reference guard
            # must fire on the row's FINAL state — the second connection
            # moves the row onto a `knowledge:` pointer reference between
            # the pre-lock read and the write, and the update must refuse
            # instead of superseding pointer state (PR1's CAS-only rule).
            conn, other = self._new_store()
            mid = self._seed_one(conn, "kptr")
            original_content = self._row(conn, mid)["content"]

            def fire():
                other.execute("UPDATE memory SET source_ref=? WHERE id=?",
                              ("knowledge:audit-507-pointer", mid))
                other.commit()

            undo = self._hook_second_writer(fire)
            refused = None
            try:
                self._update_memory(
                    conn, mid=mid,
                    content="pointer claim v2 " + uuid.uuid4().hex,
                    supersede_reason="updated", now=B_NOW)
            except ValueError as exc:
                refused = str(exc)
            finally:
                undo()
            self.assertIsNotNone(
                refused,
                "AC6-507: the update committed although the row had moved "
                "onto a reserved knowledge: pointer reference between the "
                "pre-lock read and the write — the guard must refuse")
            self.assertIn("knowledge", (refused or "").lower(),
                          "AC6-507: the knowledge-pointer refusal must "
                          "mention 'knowledge' (got %r)" % (refused,))
            old = self._row(conn, mid)
            self.assertIsNone(
                old["superseded_at"],
                "AC6-507: the refused knowledge-pointer update still "
                "tombstoned the old row")
            self.assertEqual(old["content"], original_content,
                             "AC6-507: the old row's content is no longer "
                             "the original (content=%r)" % (old["content"],))
            self.assertEqual(
                self._live_successors(conn, mid), [],
                "AC6-507: the refused knowledge-pointer update still minted "
                "a live successor")

        with self.subTest("control: concurrent supersede still refused"):
            conn, other = self._new_store()
            mid = self._seed_one(conn, "race")

            def fire():
                self._update_memory(
                    other, mid=mid,
                    content="writer A content " + uuid.uuid4().hex,
                    supersede_reason="updated", now=A_NOW)

            undo = self._hook_second_writer(fire)
            b_error = None
            try:
                self._update_memory(
                    conn, mid=mid,
                    content="writer B content " + uuid.uuid4().hex,
                    supersede_reason="updated", now=B_NOW)
            except ValueError as exc:
                b_error = str(exc)
            finally:
                undo()
            self.assertIsNotNone(b_error,
                                 "AC6-507 control: writer B committed although "
                                 "the competing connection had already "
                                 "superseded the row; the liveness guard must "
                                 "refuse with a reason mentioning 'superseded'")
            self.assertIn("superseded", (b_error or "").lower(),
                          "AC6-507 control: the refusal must mention "
                          "'superseded' (got %r)" % (b_error,))


def _run_case(class_name, method_name):
    """Published-argv runner (house pattern of tests/test_audit_delivery_ack.py):
    the frozen per-AC pytest node ids name the MODULE-LEVEL wrappers, which
    run the TestCase under a real unittest runner (subTest included).
    Returns True when every leg passed."""
    suite = unittest.TestSuite()
    suite.addTest(class_name(method_name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


def _require_cli_leg(which):
    """Run one public-command leg from the CLI companion so the published
    node ids exercise the issue's "through the public commands" wording too.
    Imported lazily: the companion imports THIS module for the shared
    fixtures, so it must only load after the env pin above has run. Returns
    True when the leg passed."""
    import test_audit_write_revalidation_cli as _cli_legs
    return (_cli_legs.run_cli_consolidate_leg() if which == "consolidate"
            else _cli_legs.run_cli_update_leg())


def test_invalidated_neighbor():
    """Published-argv wrapper (AC1 / AC5-507): runs the TestCase assertions
    plus the store.py consolidate public-command leg. Both legs always run —
    a failure in one must not hide the other's verdict."""
    ok = _run_case(AuditWriteRevalidationTest, "test_invalidated_neighbor")
    ok = _require_cli_leg("consolidate") and ok
    if not ok:
        raise AssertionError(
            "AC5-507 legs failed on this tree (see the runner output above)")


def test_metadata_interleave():
    """Published-argv wrapper (AC2 / AC6-507): runs the TestCase assertions
    plus the store.py update public-command leg. Both legs always run — a
    failure in one must not hide the other's verdict."""
    ok = _run_case(AuditWriteRevalidationTest, "test_metadata_interleave")
    ok = _require_cli_leg("update") and ok
    if not ok:
        raise AssertionError(
            "AC6-507 legs failed on this tree (see the runner output above)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
