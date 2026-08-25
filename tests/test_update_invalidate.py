#!/usr/bin/env python3
"""Issue #59, tasks 4.2 / 4.3 / 4.4 / 4.7: append-only ``update``, ``invalidate``,
complete ``--as-of`` against ``valid_until``, and worst-of taint lineage.

Behaviors pinned (each verified against the real CLI first, then encoded here):
  - ``update`` tombstones the target (superseded_at == valid_until == now,
    supersede_reason='updated') and creates a NEW live row carrying the new
    content, ``valid_from=now``, ``valid_until=''``, ``update_of=<target>``.
    The target row's content is NEVER mutated (append-only history).
  - ``update --as-of`` is temporal: an as_of BEFORE the update returns the OLD
    content; as_of AT the update instant returns ONLY the NEW row (valid_until
    is the EXCLUSIVE end — a row is valid while ``valid_until > as_of``, never
    at equality).
  - ``update`` refusals: unknown / already-superseded id -> exit 2, nothing
    written; oversize content -> exit 1; auto-mode capture refusal -> exit 2.
  - Taint is WORST-OF through lineage: an update with no ``--taint`` inherits
    the replaced row's taint; an explicit ``--taint`` is applied; an unknown
    taint value is refused at argparse (there is deliberately no fourth rank),
    and the same applies to ``add --taint``.
  - A dedup-fold update (new content already live elsewhere) tombstones the
    target, does NOT create a second row, and the surviving row's taint is the
    worst of the two sources.
  - ``invalidate`` == ``supersede`` with a REQUIRED reason: it tombstones with
    valid_until=now and stores the reason; missing reason -> argparse exit 2.
    An invalidated row is gone from live recall and cannot be updated again.
  - ``decision`` / ``constraint`` are first-class shipped types (v9).

Drives the REAL store.py CLI against throwaway temp stores (model forced
absent for determinism) — the isolation-fixture pattern from
 tests/test_jsonl_sync.py — plus direct sqlite reads for DB-level assertions.

Run: python tests/test_update_invalidate.py   (no pytest -- repo convention)
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
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

# Only used by the in-process cap-boundary test (
# test_update_oversize_content_exits_1); harmless to expose at import.
sys.path.insert(0, str(SCRIPTS_DIR))


class Store:
    """One throwaway store + its pinned subprocess env (model absent)."""

    ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    def __init__(self, tmp: str):
        self.tmp = tmp
        self.path = os.path.join(tmp, "store.sqlite")
        self.env = {**os.environ}
        self.env["ZMEM_STORE"] = self.path
        self.env.pop("ZMEM_DATA", None)
        self.env.pop("ZMEM_BACKUP_DIR", None)
        self.env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
        self.env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

    def run(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=60,
        )

    def init(self):
        r = self.run("init")
        assert r.returncode == 0, r.stderr

    def add(self, namespace: str, content: str, *, type_: str = "fact",
            signal: str = "test", confidence: float | None = None,
            taint: str | None = None) -> str:
        args = ["add", "--namespace", namespace, "--type", type_,
                "--content", content, "--signal", signal]
        if confidence is not None:
            args += ["--confidence", str(confidence)]
        if taint is not None:
            args += ["--taint", taint]
        r = self.run(*args)
        assert r.returncode == 0, r.stderr
        return self._extract_id(r.stdout, "added memory")

    @classmethod
    def _extract_id(cls, stdout: str, prefix: str) -> str:
        m = re.search(re.escape(prefix) + r" (" + cls.ID_RE.pattern + r")", stdout)
        assert m, f"{prefix!r} id not found in stdout: {stdout!r}"
        return m.group(1)

    @classmethod
    def _extract_update_pair(cls, stdout: str) -> tuple[str, str]:
        """update prints `updated memory <OLD> -> <NEW>`; return (old, new)."""
        m = re.search(
            r"updated memory (" + cls.ID_RE.pattern + r") -> (" +
            cls.ID_RE.pattern + r")", stdout)
        assert m, f"update id pair not found in stdout: {stdout!r}"
        return m.group(1), m.group(2)

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        # sqlite3.Row so update_memory's column-name indexing works when a test
        # drives the write path in-process; numeric indexing still works too.
        c.row_factory = sqlite3.Row
        return c

    def row(self, mid: str) -> sqlite3.Row | None:
        c = self.conn()
        try:
            c.row_factory = sqlite3.Row
            return c.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone()
        finally:
            c.close()

    def recall(self, query: str, namespace: str, *, as_of: str | None = None):
        args = ["recall", "--query", query, "--namespace", namespace, "--json"]
        if as_of is not None:
            args += ["--as-of", as_of]
        r = self.run(*args)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    def recent(self, namespace: str, *, as_of: str | None = None):
        args = ["recent", "--namespace", namespace, "--json"]
        if as_of is not None:
            args += ["--as-of", as_of]
        r = self.run(*args)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-update-inv-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = Store(self.tmp)
        self.store.init()

    def ns(self):
        return f"project:update-inv-{id(self)}"


# ---------------------------------------------------------------------------
# 4.2 update: append-only history + inheritance + refusals
# ---------------------------------------------------------------------------
class UpdateBasicFlowTest(_StoreCase):
    def test_update_tombstones_old_and_creates_linked_new_row(self):
        ns = self.ns()
        mid = self.store.add(ns, "original alpha content to be revised",
                             taint="trusted_internal")
        old = self.store.row(mid)
        self.assertIsNone(old["superseded_at"])
        self.assertEqual(old["valid_until"], "")
        self.assertEqual(old["update_of"], "")

        r = self.store.run("update", "--id", mid, "--content",
                           "revised beta content", "--taint", "untrusted_tool")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, new_id = self.store._extract_update_pair(r.stdout)

        # Old row tombstoned with valid_until == superseded_at, reason fixed.
        old_after = self.store.row(mid)
        self.assertIsNotNone(old_after["superseded_at"])
        self.assertEqual(old_after["valid_until"], old_after["superseded_at"],
                         "tombstoned update carrier must close its validity at "
                         "the supersede instant (exclusive end)")
        self.assertEqual(old_after["supersede_reason"], "updated")
        # Content NEVER mutated (append-only).
        self.assertEqual(old_after["content"], "original alpha content to be revised")

        # New row is live with lineage back to the target.
        new = self.store.row(new_id)
        self.assertIsNone(new["superseded_at"])
        self.assertEqual(new["valid_until"], "")
        self.assertEqual(new["update_of"], mid)
        self.assertEqual(new["content"], "revised beta content")
        self.assertEqual(new["namespace"], ns)

    def test_update_inherits_metadata_unless_overridden(self):
        ns = self.ns()
        mid = self.store.add(ns, "carrier row for metadata inheritance",
                             confidence=0.7, taint="trusted_internal")
        # Override only content: everything else must be inherited.
        r = self.store.run("update", "--id", mid, "--content", "inherited metadata row")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, new_id = self.store._extract_update_pair(r.stdout)
        # We used signal=test (0.9 default confidence), NOT the 0.7 provided,
        # because we did not pass --confidence: the effective confidence is
        # inherited from the old row.
        new = self.store.row(new_id)
        self.assertEqual(new["signal"], "test")
        self.assertEqual(new["confidence"], 0.7)
        self.assertEqual(new["taint"], "trusted_internal",
                         "inherited lineage: replacing a trusted_internal row "
                         "without an explicit --taint keeps trusted_internal")

    def test_update_unknown_id_exits_2_nothing_written(self):
        ns = self.ns()
        before = self.store.conn().execute("SELECT count(*) FROM memory").fetchone()[0]
        r = self.store.run("update", "--id", "00000000-0000-0000-0000-000000000000",
                           "--content", "orphan content")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("no memory with id", r.stderr)
        after = self.store.conn().execute("SELECT count(*) FROM memory").fetchone()[0]
        self.assertEqual(before, after, "refused update must write nothing")

    def test_update_already_superseded_id_exits_2(self):
        ns = self.ns()
        mid = self.store.add(ns, "row updated once already")
        r1 = self.store.run("update", "--id", mid, "--content", "first revision")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.store.run("update", "--id", mid, "--content", "second revision")
        self.assertEqual(r2.returncode, 2, r2.stderr)
        self.assertIn("already superseded", r2.stderr)
        # Nothing new was written by the refused call.
        rows = self.store.conn().execute(
            "SELECT count(*) FROM memory WHERE content=?", ("second revision",)
        ).fetchone()[0]
        self.assertEqual(rows, 0)

    def test_update_oversize_content_exits_1(self):
        """Content-cap boundary via the write path. A ~65-KB arg can't even
        launch store.py on Windows (argv capped ~32K — WinError 206), so drive
        update_memory in-process against the SAME store file: it must raise
        ContentTooLarge and write nothing, while exactly-at-cap content
        succeeds. ContentTooLarge is a ValueError subclass BY DESIGN (write.py
        line ~109); what routes it to CLI exit 1 is the dedicated dispatch
        branch ordered BEFORE the generic ValueError exit-2 branch — pinned
        here on the dispatch source so the mapping can't silently regress."""
        from storelib.write import ContentTooLarge, update_memory
        ns = self.ns()
        mid = self.store.add(ns, "small row")
        conn = self.store.conn()
        try:
            with self.assertRaises(ContentTooLarge):
                update_memory(conn, mid=mid, content="X" * 65537)
            self.assertIsNone(self.store.row(mid)["superseded_at"],
                              "an oversize update must not tombstone the target")
            # Boundary: exactly at the cap succeeds and creates the new row.
            new_id, created = update_memory(conn, mid=mid, content="Y" * 65536)
            self.assertTrue(created)
            self.assertIsNotNone(self.store.row(mid)["superseded_at"])
            self.assertEqual(self.store.row(new_id)["update_of"], mid)
        finally:
            conn.close()
        # Exit-code mapping: the update dispatch catches ContentTooLarge with a
        # dedicated branch (exit 1) before the generic ValueError branch (exit
        # 2). This ordering is the ONLY thing separating oversize-refusal (1)
        # from id-refusal (2), so pin it.
        cli_src = (SCRIPTS_DIR / "storelib" / "cli.py").read_text(encoding="utf-8")
        upd_block = cli_src[cli_src.index('elif args.cmd == "update"'):]
        self.assertIn("except ContentTooLarge", upd_block)
        self.assertIn("except ValueError", upd_block)
        self.assertLess(
            upd_block.index("except ContentTooLarge"),
            upd_block.index("except ValueError"),
            "the dedicated ContentTooLarge -> exit 1 branch must precede the "
            "generic ValueError -> exit 2 branch, or oversize updates would be "
            "mis-reported as exit 2")

    def test_update_capture_refusal_exits_2(self):
        ns = self.ns()
        mid = self.store.add(ns, "benign target row")
        r = self.store.run("update", "--id", mid, "--content", "clean content",
                           "--source-ref", "creds ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
                           "--capture-mode", "auto")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("refusing automatic capture", r.stderr)
        self.assertIsNone(self.store.row(mid)["superseded_at"],
                          "a capture-refused update must not tombstone the target")


# ---------------------------------------------------------------------------
# 4.4 complete --as-of against valid_until
# ---------------------------------------------------------------------------
class UpdateAsOfTemporalTest(_StoreCase):
    def _seed_backdated(self, content: str, *, taint: str):
        """Add a row, then backdate its valid_from far into the past so the
        as-of windows below are fully controllable and non-vacuous."""
        mid = self.store.add(self.ns(), content, taint=taint)
        c = self.store.conn()
        try:
            c.execute("UPDATE memory SET valid_from=? WHERE id=?",
                      ("2020-01-01T00:00:00Z", mid))
            c.commit()
        finally:
            c.close()
        return mid

    def test_as_of_before_update_returns_old_content(self):
        mid = self._seed_backdated("old alpha pre-revision content",
                                   taint="trusted_internal")
        r = self.store.run("update", "--id", mid, "--content", "new beta revision")
        self.assertEqual(r.returncode, 0, r.stderr)

        # as_of AT the old row's valid_from (inclusive lower bound): the old
        # row was valid at that instant because its valid_until (the update
        # time) is still in the future.
        results = self.store.recent(self.ns(), as_of="2020-01-01T00:00:00Z")
        contents = [x["content"] for x in results]
        self.assertEqual(contents, ["old alpha pre-revision content"],
                         "as_of before the update must return only the OLD content")

    def test_as_of_at_update_instant_returns_only_new_row(self):
        """The exclusivity boundary: as_of == the update instant must return
        ONLY the new row. The old row's valid_until == that same instant, and
        valid_until is EXCLUSIVE (a row is valid while valid_until > as_of,
        never at equality) — so the old row is not valid at its own end."""
        mid = self._seed_backdated("old omega pre-update text",
                                   taint="trusted_internal")
        r = self.store.run("update", "--id", mid, "--content", "new psi replacement")
        self.assertEqual(r.returncode, 0, r.stderr)

        update_ts = self.store.row(mid)["valid_until"]
        self.assertIsNotNone(update_ts)

        results = self.store.recent(self.ns(), as_of=update_ts)
        contents = [x["content"] for x in results]
        self.assertEqual(contents, ["new psi replacement"],
                         "as_of == the update instant must return ONLY the new "
                         "row (old valid_until is exclusive at its own instant): "
                         f"got {contents}")

    def test_as_of_after_update_returns_only_new_row(self):
        mid = self._seed_backdated("old gamma superseded row",
                                   taint="trusted_internal")
        r = self.store.run("update", "--id", mid, "--content", "new delta row")
        self.assertEqual(r.returncode, 0, r.stderr)
        results = self.store.recent(self.ns(), as_of="2099-01-01T00:00:00Z")
        contents = [x["content"] for x in results]
        self.assertEqual(contents, ["new delta row"],
                         "as_of after the update must return only the NEW row")

    def test_recall_as_of_before_update_returns_old_content(self):
        """The same temporal guarantee through `recall` (FTS path) as through
        `recent`, with a query term that only matches the old row."""
        mid = self._seed_backdated("quokka old content still valid early",
                                   taint="trusted_internal")
        r = self.store.run("update", "--id", mid, "--content", "replacement ninja content")
        self.assertEqual(r.returncode, 0, r.stderr)
        results = self.store.recall("quokka", self.ns(), as_of="2020-01-01T00:00:00Z")
        contents = [x["content"] for x in results]
        self.assertIn("quokka old content still valid early", contents,
                      "as_of-before recall must still surface the old row")
        self.assertNotIn("replacement ninja content", contents)


# ---------------------------------------------------------------------------
# 4.7 worst-of taint lineage through update
# ---------------------------------------------------------------------------
class UpdateTaintLineageTest(_StoreCase):
    def test_unknown_taint_refused_at_argparse(self):
        ns = self.ns()
        r = self.store.run("add", "--namespace", ns, "--type", "fact",
                           "--content", "row with a bogus taint", "--signal", "test",
                           "--taint", "banana")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("banana", r.stdout + r.stderr)

    def test_update_unknown_taint_exits_2(self):
        ns = self.ns()
        mid = self.store.add(ns, "taint override target")
        r = self.store.run("update", "--id", mid, "--content", "x", "--taint", "banana")
        self.assertEqual(r.returncode, 2, r.stderr)
        # Nothing written: target still live, no new row.
        self.assertIsNone(self.store.row(mid)["superseded_at"])
        self.assertEqual(self.store.conn().execute(
            "SELECT count(*) FROM memory WHERE content=?", ("x",)).fetchone()[0], 0)

    def test_update_inherits_worst_of_taint_without_explicit_flag(self):
        """Replacing an untrusted_web row WITHOUT --taint must keep the new row
        at untrusted_web (the lineage is worst-of, so a caller that forgets to
        re-declare trust cannot silently upgrade it)."""
        ns = self.ns()
        mid = self.store.add(ns, "web sourced claim to revise", taint="untrusted_web")
        r = self.store.run("update", "--id", mid, "--content", "revised web claim")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, new_id = self.store._extract_update_pair(r.stdout)
        self.assertEqual(self.store.row(new_id)["taint"], "untrusted_web")

    def test_update_explicit_taint_applied_and_dominates(self):
        """An explicit untrusted_tool override on a trusted_internal target
        must land on the new row (the caller's origin participates in the
        worst-of)."""
        ns = self.ns()
        mid = self.store.add(ns, "trusted claim being replaced", taint="trusted_internal")
        r = self.store.run("update", "--id", mid, "--content", "replaced claim",
                           "--taint", "untrusted_tool")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, new_id = self.store._extract_update_pair(r.stdout)
        self.assertEqual(self.store.row(new_id)["taint"], "untrusted_tool")


# ---------------------------------------------------------------------------
# 4.2 dedup-fold update: old tombstoned, no second row, worst-of on survivor
# ---------------------------------------------------------------------------
class UpdateDedupFoldTest(_StoreCase):
    def test_update_folds_into_existing_live_row_with_worst_of_taint(self):
        # PR-review PRR-S: the survivor must start at the BEST rank and the
        # update lineage at the WORST — seeding the survivor at untrusted_web
        # made the worst-of assertion vacuous (worse-of is idempotent at the
        # top rank; deleting the merge in write.py left it passing).
        ns = self.ns()
        future = self.store.add(ns, "identical destination content lives here",
                                taint="trusted_internal")
        mid = self.store.add(ns, "row about to be revised into a duplicate",
                             taint="untrusted_web")

        r = self.store.run("update", "--id", mid, "--content",
                           "identical destination content lives here")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("merged into existing", r.stdout, r.stdout)

        # The target was tombstoned (append-only history still holds).
        old = self.store.row(mid)
        self.assertIsNotNone(old["superseded_at"])
        self.assertEqual(old["valid_until"], old["superseded_at"])
        self.assertEqual(old["supersede_reason"], "updated")
        # The fold did NOT create a new row (existing id wins).
        live = [x["id"] for x in self.store.recall("identical destination", ns)]
        self.assertEqual(live, [future], "update fold must not create a second row")
        # The surviving row's taint is the WORST of the two sources
        # (trusted_internal survivor vs untrusted_web lineage -> untrusted_web).
        self.assertEqual(self.store.row(future)["taint"], "untrusted_web",
                         "dedup fold must apply worst-of taint to the survivor")


# ---------------------------------------------------------------------------
# PR-review PRR-A: the ADD dedup path must apply worst-of taint too — a
# re-observed untrusted duplicate may not leave the keeper's trust overstated.
# ---------------------------------------------------------------------------
class AddDedupTaintTest(_StoreCase):
    def test_add_dedup_upgrades_keeper_to_worst_of_taint(self):
        ns = self.ns()
        first = self.store.add(ns, "exact duplicate content for taint fold",
                               taint="trusted_internal")
        # A second add of the SAME content with a worse taint dedups into the
        # first row; the keeper must absorb the untrusted lineage.
        r = self.store.run("add", "--namespace", ns, "--type", "fact",
                           "--content", "exact duplicate content for taint fold",
                           "--signal", "test", "--taint", "untrusted_web")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dedup", r.stdout, r.stdout)
        self.assertEqual(self.store.row(first)["taint"], "untrusted_web",
                         "add-dedup must apply worst-of taint to the keeper")


# ---------------------------------------------------------------------------
# PR-review PRR-P: `--content -` reads content from stdin, so large-but-valid
# payloads can be delivered on Windows where argv caps far below the store cap.
# ---------------------------------------------------------------------------
class StdinContentTest(_StoreCase):
    def _run_stdin(self, *args, text: str):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=self.store.env, capture_output=True, text=True,
            input=text, timeout=60,
        )

    def test_add_content_dash_reads_stdin(self):
        ns = self.ns()
        r = self._run_stdin("add", "--namespace", ns, "--type", "fact",
                            "--content", "-", "--signal", "test",
                            text="memory body delivered over stdin")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("added memory", r.stdout)
        self.assertEqual(len(self.store.recent(ns)), 1)
        self.assertEqual(self.store.recent(ns)[0]["content"],
                         "memory body delivered over stdin")

    def test_update_content_dash_reads_stdin(self):
        ns = self.ns()
        mid = self.store.add(ns, "row to revise over stdin")
        r = self._run_stdin("update", "--id", mid, "--content", "-",
                            text="revised body delivered over stdin")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, new_id = self.store._extract_update_pair(r.stdout)
        self.assertEqual(self.store.row(new_id)["content"],
                         "revised body delivered over stdin")
        self.assertIsNotNone(self.store.row(mid)["superseded_at"])


# ---------------------------------------------------------------------------
# 4.3 invalidate: required reason, tombstone, second-update refusal
# ---------------------------------------------------------------------------
class InvalidateCommandTest(_StoreCase):
    def test_invalidate_missing_reason_is_argparse_refused_exit_2(self):
        ns = self.ns()
        mid = self.store.add(ns, "row to invalidate")
        r = self.store.run("invalidate", "--id", mid)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("--reason", r.stdout + r.stderr)
        # Nothing was written by the refused call.
        self.assertIsNone(self.store.row(mid)["superseded_at"])

    def test_invalidate_tombstones_with_valid_until_and_reason(self):
        ns = self.ns()
        mid = self.store.add(ns, "this fact will be invalidated", taint="untrusted_tool")
        r = self.store.run("invalidate", "--id", mid, "--reason", "the API changed")
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self.store.row(mid)
        self.assertIsNotNone(row["superseded_at"])
        self.assertEqual(row["valid_until"], row["superseded_at"],
                         "invalidate must close validity at the supersede instant")
        self.assertEqual(row["supersede_reason"], "the API changed")
        # Taint preserved on the tombstone (a tombstone creates no new row).
        self.assertEqual(row["taint"], "untrusted_tool")

    def test_invalidate_unknown_id_exits_1(self):
        r = self.store.run("invalidate", "--id", "00000000-0000-0000-0000-000000000000",
                           "--reason", "gone")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no memory with id", r.stderr)

    def test_invalidated_row_is_not_live_and_cannot_be_updated(self):
        ns = self.ns()
        mid = self.store.add(ns, "row that gets invalidated then re-updated")
        r = self.store.run("invalidate", "--id", mid, "--reason", "false premise")
        self.assertEqual(r.returncode, 0, r.stderr)

        gone = self.store.recall("invalidated then re-updated", ns)
        self.assertNotIn(mid, [x["id"] for x in gone],
                         "invalidated row must be gone from live recall")

        r2 = self.store.run("update", "--id", mid, "--content", "should be refused")
        self.assertEqual(r2.returncode, 2, r2.stderr)
        self.assertIn("already superseded", r2.stderr)

    def test_second_invalidate_refused_appends_no_new_history(self):
        # PR-review PRR-B: re-tombstoning would MOVE valid_until forward (an
        # as-of query in (old_end, new_end] would resurrect the dead fact) and
        # replace the audit reason — a mutation of append-only history. The
        # second invalidate must refuse (exit 2) and leave the original
        # tombstone bytes untouched.
        ns = self.ns()
        mid = self.store.add(ns, "fact invalidated exactly once")
        r1 = self.store.run("invalidate", "--id", mid, "--reason", "premise gone")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        before = self.store.row(mid)
        r2 = self.store.run("invalidate", "--id", mid, "--reason", "again later")
        self.assertEqual(r2.returncode, 2, r2.stderr)
        self.assertIn("already superseded", r2.stderr)
        after = self.store.row(mid)
        self.assertEqual(
            (after["superseded_at"], after["valid_until"], after["supersede_reason"]),
            (before["superseded_at"], before["valid_until"], before["supersede_reason"]),
            "a refused second invalidate must not touch the tombstone")

    def test_second_supersede_refused_history_immutable(self):
        # PR-review PRR-B, plain-supersede surface: same guard as invalidate.
        ns = self.ns()
        mid = self.store.add(ns, "fact superseded exactly once")
        r1 = self.store.run("supersede", "--id", mid, "--reason", "stale")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        before = self.store.row(mid)["valid_until"]
        r2 = self.store.run("supersede", "--id", mid, "--reason", "again")
        self.assertEqual(r2.returncode, 2, r2.stderr)
        self.assertIn("already superseded", r2.stderr)
        self.assertEqual(self.store.row(mid)["valid_until"], before,
                         "valid_until must not move on a refused re-supersede")

    def test_invalidate_whitespace_reason_refused_exit_2(self):
        # PR-review PRR-I: argparse required=True checks PRESENCE, not content —
        # a whitespace-only reason must be refused so the audit trail can never
        # be blank (MCP/Hermes already strip-refuse at their boundaries).
        ns = self.ns()
        mid = self.store.add(ns, "row with a blank reason attempt")
        r = self.store.run("invalidate", "--id", mid, "--reason", "   ")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("non-empty", r.stderr)
        self.assertIsNone(self.store.row(mid)["superseded_at"],
                          "a blank-reason invalidate must write nothing")


# ---------------------------------------------------------------------------
# v9 types: decision / constraint are first-class shipped types
# ---------------------------------------------------------------------------
class DecisionConstraintTypeTest(_StoreCase):
    def test_add_accepts_decision_and_constraint_types(self):
        ns = self.ns()
        for type_ in ("decision", "constraint"):
            with self.subTest(type=type_):
                mid = self.store.add(ns, f"a {type_} memory", type_=type_)
                self.assertEqual(self.store.row(mid)["type"], type_)

    def test_recall_surfaces_type_and_taint_on_results(self):
        """The as-json recall payload must carry the new provenance columns so
        consumers can reason about lineage and trust (issue #59, 4.7 pin)."""
        ns = self.ns()
        mid = self.store.add(ns, "payload shape row quokka", taint="untrusted_tool")
        results = self.store.recall("quokka", ns)
        self.assertGreater(len(results), 0)
        item = next(x for x in results if x["id"] == mid)
        for key in ("valid_from", "valid_until", "update_of", "taint"):
            self.assertIn(key, item, f"recall payload missing {key}")
        self.assertEqual(item["taint"], "untrusted_tool")
        self.assertEqual(item["update_of"], "")


class V9MigrationAndSupersedeTest(_StoreCase):
    """R1-M7 + R1-S4: the v8->v9 migration must add the lineage/provenance
    columns and temporal index, backfill ``valid_until`` for tombstones and
    ``taint`` from signal, stay idempotent on re-run — and the PLAIN
    ``supersede`` command (not just invalidate/update) must also write
    ``valid_until == superseded_at``."""

    def test_plain_supersede_sets_valid_until(self):
        ns = self.ns()
        mid = self.store.add(ns, "row to supersede with plain supersede")
        r = self.store.run("supersede", "--id", mid, "--reason", "general tombstone")
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self.store.row(mid)
        self.assertIsNotNone(row["superseded_at"])
        self.assertEqual(
            row["valid_until"], row["superseded_at"],
            "plain supersede must close validity at the tombstone instant "
            "(else as-of would resurrect it)")

    def test_v8_store_migrates_to_v9_with_backfills(self):
        """Build a synthetic v8 store (real v9 schema, v9-only columns/index
        dropped, version rewound to 8), seed live + tombstoned rows, then run
        any command to trigger migrate(). It must re-add the columns and the
        temporal index, backfill tombstone valid_until == superseded_at and
        taint by signal (grounded -> trusted_internal, none -> untrusted_tool),
        and stay at v9 on a second run."""
        ns = "project:mig-v9-x"
        live_test = self.store.add(ns, "v8 live grounded row", signal="test")
        live_none = self.store.add(ns, "v8 live self-opinion row", signal="none")
        dead = self.store.add(ns, "v8 row later superseded", signal="test")
        r = self.store.run("supersede", "--id", dead, "--reason", "pre-v9 tombstone")
        self.assertEqual(r.returncode, 0, r.stderr)

        # Rewind to v8: drop the v9-only surface and the version stamp.
        c = self.store.conn()
        try:
            c.execute("DROP INDEX IF EXISTS idx_memory_time")
            for col in ("valid_until", "update_of", "taint"):
                c.execute(f"ALTER TABLE memory DROP COLUMN {col}")
            c.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
            c.commit()
        finally:
            c.close()

        # Any command triggers migrate() -> v9.
        r = self.store.run("stats")
        self.assertEqual(r.returncode, 0, r.stderr)

        c = self.store.conn()
        try:
            ver = c.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            self.assertEqual(ver, "9", "migrate must land on the supported version")
            cols = {rr["name"] for rr in c.execute("PRAGMA table_info(memory)")}
            for col in ("valid_until", "update_of", "taint"):
                self.assertIn(col, cols, f"migration did not re-add {col}")
            idx = c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_memory_time'").fetchone()
            self.assertIsNotNone(idx, "temporal index must be created by migration")
            # Backfills.
            drow = c.execute(
                "SELECT valid_until, superseded_at FROM memory WHERE id=?",
                (dead,)).fetchone()
            self.assertEqual(
                drow["valid_until"], drow["superseded_at"],
                "tombstone valid_until must be backfilled from superseded_at")
            t_test = c.execute(
                "SELECT taint FROM memory WHERE id=?", (live_test,)).fetchone()[0]
            t_none = c.execute(
                "SELECT taint FROM memory WHERE id=?", (live_none,)).fetchone()[0]
            self.assertEqual(t_test, "trusted_internal",
                             "grounded signal must backfill trusted_internal")
            self.assertEqual(t_none, "untrusted_tool",
                             "'none' signal must backfill untrusted_tool")
        finally:
            c.close()

        # Idempotent re-run stays at v9.
        r = self.store.run("stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.store.conn()
        try:
            ver = c.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            self.assertEqual(ver, "9")
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
