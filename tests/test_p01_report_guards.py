"""Guardrails and supplementary pins for `store.py report` (issue #249).

This file is deliberately NOT part of the frozen checkpoint manifest. The
frozen contract is `tests/test_p01_decision_report.py`, which stays
byte-identical to its pinned blob `2ab8696f…` through Phase 5.

It carries five jobs the frozen file structurally cannot:

  * Guardrail 1 — the `report` dispatch in `storelib/cli.py` must sit BEFORE
    the shared `connect()` lifecycle. A report reached after
    `connect()`/`_prepare_store()` cannot honour its read-only contract,
    because `_prepare_store` runs `PRAGMA journal_mode=WAL` and
    `_auto_near_miss_rekey` writes. Proven RED by mutating a COPY of cli.py in
    a scratch dir and re-running the structural check.
  * Guardrail 2 — the WAL-residue path. The frozen checks never reach that
    state (their setUp ends with writer `add` calls whose clean close removes
    the sidecars), and it is the state that broke the first design.
  * Supplementary pins for three weaknesses the frozen checks cannot close
    without weakening them: a global-vs-per-row `sessions` bug, an unasserted
    sanitized-bucket COUNT, and an unasserted moment key under CRLF.

A fourth guardrail drives a LIVE WRITER (committing AND checkpointing) while
the report runs, to pin exactly what the disclosed snapshot caveat permits and
forbids.
  * `StoreErrorContractTest` (seven cases) - every store-open failure class
    must refuse with the SAME shape: exit 2, a `[zmem] report:` prefix, empty
    stdout, no traceback, and no leaked staging directory. The corrupt,
    zero-byte, staging-OSError, torn-with-residue and schema-less-with-residue
    cases pin the staged path; the exclusive-lock case pins the EAGER direct
    path, where `sqlite3.connect` itself raises before any read check can run.
    Each half was added because a review round proved the corresponding fix was
    UNPINNED: reverting it left every suite green.
  * A `caveats` presence pin - the disclosure array that the plan, the
    CHANGELOG and SKILL.md all point operators at must exist and be non-empty
    on a trivially happy run, so the disclosure cannot vanish silently.

Run: python tests/test_p01_report_guards.py   (no pytest required)
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
CLI_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "storelib" / "cli.py"
PY = sys.executable

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_CONVENTION_INTERVAL",
    "ZMEM_SESSION", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW", "ZMEM_AUTO_REKEY",
    "ZMEM_BG_LOG_MAX_BYTES", "ZMEM_LOG_ROTATIONS",
)


def _clean_env(tmp: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
    })
    return env


def _ids_literal(ids) -> str:
    return "[" + ", ".join(repr(str(i)) for i in ids) + "]"


def _decision_line(ts: int, ids, all_ids, sid: str, moment: str) -> str:
    return (f"[{ts}] zmem-hook status=injected reason=injected "
            f"ids={_ids_literal(ids)} all={_ids_literal(all_ids)} "
            f"sid={sid} moment={moment}")


def _event_counts_from_log(log_path: str) -> tuple:
    """Independent oracle for the tier-summed totals, computed straight from
    the log text without importing storelib.

    Uses the SAME two bases the report uses, so a divergence fails loudly:
      * delivered = OCCURRENCES — every entry of every line's `ids=[...]` list,
        counted one by one, so a repeat within a line repeats a delivery and a
        repeat across lines counts again. This is why `delivered += len(ids)`
        over the raw list and NOT `len(set(ids))`.
      * withheld = DISTINCT CANDIDATES — `set(all) - set(ids)` per line, so the
        same id offered twice is one candidate.
    Returns (delivered, withheld).
    """
    import ast
    import re
    line_re = re.compile(r"ids=(\[[^\]]*\]) all=(\[[^\]]*\])")
    delivered = withheld = 0
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if "zmem-hook" not in raw:
                continue
            match = line_re.search(raw)
            if not match:
                continue
            try:
                ids = ast.literal_eval(match.group(1))
                pool = ast.literal_eval(match.group(2))
            except (ValueError, SyntaxError):
                continue
            delivered += len([i for i in ids if i])
            withheld += len({i for i in pool if i} - {i for i in ids if i})
    return delivered, withheld


class _ReportCase(unittest.TestCase):
    """Throwaway store seeded through the real CLI, driven via subprocess only.
    storelib is never imported in this process."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-p01-guard-")
        self.env = _clean_env(self.tmp)
        self.log = os.path.join(self.tmp, "zmem-decisions.log")
        self.pid = self._add_row("project:p01guard", "guard project canary row")
        self.gid = self._add_row("user:global", "guard global canary row")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_cli(self, *args, timeout=120):
        return subprocess.run(
            [PY, str(STORE_PY), *args], cwd=str(REPO_ROOT), env=self.env,
            capture_output=True, text=True, timeout=timeout)

    def _add_row(self, namespace: str, content: str) -> str:
        r = self._run_cli("add", "--namespace", namespace, "--type", "lesson",
                          "--content", content, "--signal", "test",
                          "--confidence", "0.9", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["id"]

    def _write_log(self, lines, newline="\n", name=None) -> None:
        with open(name or self.log, "wb") as fh:
            fh.write("".join(ln + newline for ln in lines).encode("utf-8"))

    def _run_report(self):
        return subprocess.run(
            [PY, str(STORE_PY), "report", "--decision-log", self.log, "--json"],
            cwd=str(REPO_ROOT), env=self.env,
            capture_output=True, text=True, timeout=120)

    def _snapshot_dir(self) -> dict:
        snap = {}
        for dirpath, _sub, filenames in os.walk(self.tmp):
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.tmp).replace(os.sep, "/")
                with open(full, "rb") as fh:
                    snap[rel] = fh.read()
        return snap


class ReportDispatchGuardrailTest(unittest.TestCase):
    """Guardrail 1: `report` must dispatch before connect()."""

    @staticmethod
    def _report_block_line(src: str) -> int:
        """1-based line of the `if args.cmd == "report":` dispatch."""
        for i, line in enumerate(src.splitlines(), 1):
            if 'if args.cmd == "report":' in line:
                return i
        raise AssertionError("no `report` dispatch found in cli.py")

    @staticmethod
    def _connect_line(src: str) -> int:
        for i, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("conn = _connect_existing_store()") and \
                    "existing_only" in stripped:
                return i
        raise AssertionError("no shared connect() call found in cli.py")

    def test_report_dispatches_before_the_shared_connect(self):
        src = CLI_PY.read_text(encoding="utf-8")
        report_line = self._report_block_line(src)
        connect_line = self._connect_line(src)
        self.assertLess(
            report_line, connect_line,
            "store.py report must early-dispatch before connect(): a report "
            "reached after connect()/_prepare_store() cannot honour its "
            "read-only contract, because _prepare_store runs PRAGMA "
            "journal_mode=WAL and _auto_near_miss_rekey writes. "
            "report=%d connect=%d" % (report_line, connect_line))

    def test_report_guardrail_is_red_when_dispatch_is_moved_after_connect(self):
        """Proves the guardrail is discriminating: mutate a COPY of cli.py so
        the dispatch follows connect(), and re-run the same structural rule.
        The live cli.py is never modified."""
        src = CLI_PY.read_text(encoding="utf-8")
        block = (
            '    if args.cmd == "report":\n'
            '        from storelib.decision_report import main as _report_main\n'
            '\n'
            '        sys.exit(_report_main([\n'
            '            "--decision-log", args.decision_log,\n'
            '        ] + (["--json"] if args.as_json else [])))\n'
            '\n')
        if block not in src:
            self.skipTest("dispatch block text drifted; update the mutation")
        mutated = src.replace(block, "")
        connect_line = self._connect_line(mutated)
        mutated_lines = mutated.splitlines()
        # Re-insert the dispatch well after connect(), as a regression would.
        mutated_lines.insert(connect_line, block.rstrip("\n"))
        mutated = "\n".join(mutated_lines) + "\n"

        self.assertGreater(
            self._report_block_line(mutated), connect_line,
            "mutation must place the dispatch after connect()")
        self.assertFalse(
            self._report_block_line(mutated) < connect_line,
            "the structural rule must FAIL on the mutated source, otherwise "
            "the guardrail is vacuous")


class SupplementaryPinTest(_ReportCase):
    """Pins for the three weaknesses the frozen checks cannot close."""

    def test_sessions_and_max_per_session_are_per_row_not_global(self):
        # A whole-log distinct-sid bug yields sessions=2 for BOTH rows, and a
        # hardcoded max would yield 3 for both. The frozen C1 asserts neither.
        self._write_log([
            _decision_line(1740001000 + i, [self.pid], [self.pid],
                           "sess-a", "user_prompt") for i in range(3)
        ] + [
            _decision_line(1740001010, [self.pid], [self.pid],
                           "sess-b", "user_prompt"),
            _decision_line(1740001011, [self.gid], [self.gid],
                           "sess-b", "user_prompt"),
        ])
        r = self._run_report()
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(r.stdout)["rows"]
        self.assertEqual(rows[self.gid]["sessions"], 1,
                         "sessions is per-row: G was delivered in one session")
        self.assertEqual(rows[self.gid]["max_per_session"], 1,
                         "max_per_session is per-row: G's busiest session "
                         "delivered it once, not 3")
        self.assertEqual(rows[self.pid]["max_per_session"], 3)

    def test_sanitized_bucket_carries_a_real_count(self):
        # The frozen C4 only pins key presence/absence; a populated key with
        # zero counts would pass it.
        self._write_log([
            _decision_line(1740001100, [self.pid], [self.pid],
                           "sess-h", "evil(+)status=silent"),
        ])
        r = self._run_report()
        self.assertEqual(r.returncode, 0, r.stderr)
        btm = json.loads(r.stdout)["by_tier_moment"]
        self.assertNotIn("evil(+)status=silent", btm["project"])
        self.assertEqual(btm["project"]["evil___status_silent"]["delivered"], 1,
                         "the sanitized bucket must carry the real count")

    def test_report_always_carries_its_disclosure_caveats(self):
        # The `caveats` array is the operator-facing disclosure channel the
        # plan, the CHANGELOG and SKILL.md all point at. Pin that it exists and
        # is non-empty even on a trivially happy run, so the disclosure cannot
        # vanish without a test going red.
        self._write_log([
            _decision_line(1740001600, [self.pid], [self.pid],
                           "sess-cav", "pretool"),
        ])
        r = self._run_report()
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertIn("caveats", data,
                      "every report must carry a caveats disclosure array")
        self.assertTrue(data["caveats"],
                        "caveats must be non-empty: the counting basis, the "
                        "tier vocabulary, and the snapshot window are all "
                        "things an operator must be told")

    def test_tier_summed_delivered_reconciles_with_rows_delivered(self):
        # Closes the gap the implementation reviewer found: a distinct-per-
        # moment counting base would make this fail, because the same row
        # delivered on two lines at the same moment would count once in
        # by_tier_moment and twice in rows[*].delivered. The per-LINE basis
        # the approved plan specifies makes the two reconcile exactly.
        lines = [
            _decision_line(1740001300 + i, [self.pid], [self.pid],
                           "sess-r", "pretool") for i in range(3)
        ]
        lines.append(_decision_line(1740001310, [self.gid], [self.gid],
                                    "sess-r", "pretool"))
        lines.append(_decision_line(
            1740001320, [], [self.pid, self.gid], "sess-r", "pretool"))
        self._write_log(lines)

        r = self._run_report()
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        rows_sum = sum(a["delivered"] for a in data["rows"].values())
        tier_sum = sum(c["delivered"]
                       for tier in data["by_tier_moment"].values()
                       for c in tier.values())
        self.assertEqual(
            rows_sum, 4, "four delivery events carry an id (3x P, 1x G)")
        self.assertEqual(
            tier_sum, rows_sum,
            "the tier-summed delivered total must equal the sum of "
            "rows[*].delivered: both count delivery EVENTS, so the "
            "composition table is a partition of the repeat table")
        withheld_sum = sum(c["withheld"]
                           for tier in data["by_tier_moment"].values()
                           for c in tier.values())
        self.assertEqual(withheld_sum, 2,
                         "the third line offers P and G as withheld candidates")

    def test_intra_line_duplicates_keep_the_reconciliation_invariant(self):
        # Round-2 reviewer finding. `ids` is a delivery LIST and `all` is a
        # candidate LIST, so the two need different bases:
        #   delivered -> OCCURRENCES (a line repeating an id repeats a
        #                delivery, so rows[] and the tier table must agree)
        #   withheld  -> DISTINCT CANDIDATES (the same id offered twice is one
        #                candidate that was not chosen)
        # Before this, delivered deduped while withheld double-counted, which
        # falsified the docstring's reconciliation invariant.
        dup_ids = [self.pid, self.pid]          # same delivery, twice
        dup_pool = [self.gid, self.gid]         # same candidate, twice
        self._write_log([
            _decision_line(1740001400, dup_ids, dup_ids, "sess-d", "pretool"),
            _decision_line(1740001401, [self.pid], [self.pid, self.gid,
                                                    self.gid],
                           "sess-d", "pretool"),
        ])

        r = self._run_report()
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        btm = data["by_tier_moment"]

        rows_sum = sum(a["delivered"] for a in data["rows"].values())
        tier_sum = sum(c["delivered"] for t in btm.values() for c in t.values())
        self.assertEqual(
            rows_sum, 3,
            "the repeated id on line 1 is two delivery occurrences, plus one "
            "on line 2")
        self.assertEqual(
            tier_sum, rows_sum,
            "delivered counts occurrences on BOTH sides, so the invariant "
            "holds even when one line repeats an id")
        self.assertEqual(
            btm["global"]["pretool"]["withheld"], 1,
            "all=[G, G] with ids=[P] offers ONE distinct withheld candidate, "
            "not two")

    def test_moment_key_survives_crlf_in_the_active_log(self):
        # The frozen C3 pins the delivered total across rotation+CRLF but not
        # the moment key's integrity under \r\n.
        self._write_log(
            [_decision_line(1740001200, [self.pid], [self.pid],
                            "sess-c", "pretool")], newline="\r\n")
        r = self._run_report()
        self.assertEqual(r.returncode, 0, r.stderr)
        btm = json.loads(r.stdout)["by_tier_moment"]
        self.assertIn("pretool", btm["project"],
                      "a CRLF-terminated line must bucket under the clean "
                      "moment key, not one carrying a stray carriage return")
        self.assertEqual(
            [k for k in btm["project"] if "\r" in k], [],
            "no moment key may carry a carriage return")


class WalResidueTest(_ReportCase):
    """Guardrail 2: the residue path the frozen checks never reach."""

    def _make_residue(self):
        """Leave -wal/-shm beside the store, exactly as the shipped
        `store.py source-exists` read-only command does."""
        r = self._run_cli("source-exists", "--namespace", "project:p01guard",
                          "--source-ref", "guard")
        self.assertEqual(r.returncode, 0, r.stderr)
        store = Path(self.tmp, "store.sqlite")
        self.assertTrue(Path(str(store) + "-wal").exists(),
                        "fixture precondition: source-exists must leave "
                        "-wal residue, else this test proves nothing")

    def test_residue_path_is_read_only_and_correctly_tiered(self):
        self._write_log([
            _decision_line(1740001300, [self.pid], [self.pid],
                           "sess-r", "pretool"),
            _decision_line(1740001301, [self.gid], [self.gid],
                           "sess-r", "pretool"),
        ])
        self._make_residue()
        before = self._snapshot_dir()

        r = self._run_report()
        self.assertEqual(r.returncode, 0, r.stderr)
        after = self._snapshot_dir()

        data = json.loads(r.stdout)
        self.assertEqual(
            data["rows"][self.pid]["tier"], "project",
            "the staged copy must classify the project row correctly through "
            "WAL residue")
        self.assertEqual(data["rows"][self.gid]["tier"], "global")
        # The operator's files must be untouched: residue is READ, never
        # mutated, and no new sidecar may appear.
        self.assertEqual(
            sorted(after), sorted(before),
            "the residue path must create no file (diff: added=%s removed=%s)"
            % (sorted(set(after) - set(before)), sorted(set(before) - set(after))))
        self.assertEqual(
            after.get("store.sqlite-shm"), before.get("store.sqlite-shm"),
            "the -shm bytes must be unchanged: a mode=ro read mutates them in "
            "place via WAL read-marks, which is why the residue path stages a "
            "private copy instead of opening the operator's store")


class StoreErrorContractTest(_ReportCase):
    """Every store-open failure class must refuse with the SAME contracted
    shape: exit code 2, a `[zmem] report:` prefix on stderr, no traceback, and
    no leaked staging directory.

    These exist because two review rounds found the error contract implemented
    but UNPINNED: neutering `_verify_readable` to `pass`, or re-raising the
    staging `OSError` raw, both left every shipped suite green while restoring
    the defect verbatim. A contract with no assertion is not a contract.
    """

    def _assert_refusal(self, label, expect_fragment):
        before = set(_stage_dirs())
        r = self._run_report()
        leaked = sorted(set(_stage_dirs()) - before)
        self.assertEqual(
            r.returncode, 2,
            "%s must exit 2, got %d (stderr: %s)"
            % (label, r.returncode, r.stderr.strip()[:300]))
        self.assertIn("[zmem] report:", r.stderr,
                      "%s must use the contracted stderr prefix" % label)
        self.assertNotIn("Traceback", r.stderr,
                         "%s must not leak a traceback" % label)
        self.assertIn(expect_fragment, r.stderr,
                      "%s must name the cause" % label)
        self.assertEqual(r.stdout, "",
                         "%s must leave stdout empty on a refusal" % label)
        self.assertEqual(leaked, [],
                         "%s leaked a staging directory holding a copy of the "
                         "operator's store" % label)

    def _make_residue(self):
        r = self._run_cli("source-exists", "--namespace", "project:p01guard",
                          "--source-ref", "guard")
        self.assertEqual(r.returncode, 0, r.stderr)

    def _write_one_line(self):
        self._write_log([
            _decision_line(1740001500, [self.pid], [self.pid],
                           "sess-err", "pretool"),
        ])

    def test_corrupt_store_refuses_with_the_contracted_shape(self):
        self._write_one_line()
        Path(self.tmp, "store.sqlite").write_bytes(
            b"definitely not a sqlite database" * 400)
        self._assert_refusal("corrupt store", "file is not a database")

    def test_zero_byte_store_refuses(self):
        self._write_one_line()
        Path(self.tmp, "store.sqlite").write_bytes(b"")
        self._assert_refusal("zero-byte store", "no such table: memory")

    def test_store_without_a_memory_table_refuses(self):
        self._write_one_line()
        store = Path(self.tmp, "store.sqlite")
        for suffix in ("-wal", "-shm"):
            side = Path(str(store) + suffix)
            if side.exists():
                side.unlink()
        conn = sqlite3.connect(store)
        try:
            conn.execute("CREATE TABLE unrelated (x INT)")
            conn.commit()
        finally:
            conn.close()
        # The store still HAS a memory table (add created it), so drop it to
        # build the real condition rather than assuming it.
        conn = sqlite3.connect(store)
        try:
            conn.execute("DROP TABLE memory")
            conn.commit()
        finally:
            conn.close()
        self._assert_refusal("store without a memory table",
                             "no such table: memory")

    def test_staging_oserror_refuses_and_cleans_up(self):
        # A -wal that is a DIRECTORY makes shutil.copy2 raise OSError, which
        # is the same exception family as an antivirus lock or a TOCTOU delete.
        self._write_one_line()
        Path(str(Path(self.tmp, "store.sqlite")) + "-wal").mkdir()
        self._assert_refusal("staging OSError", "cannot stage the store")

    def test_table_less_store_with_residue_refuses_on_the_staged_path(self):
        self._write_one_line()
        # `source-exists` itself queries the memory table, so the residue it
        # leaves must be created BEFORE the schema is broken. Then drop the
        # table through a writer connection that is deliberately LEFT OPEN:
        # a clean close checkpoints the -wal away, and the residue must still
        # be present when the report reads.
        self._make_residue()
        store = Path(self.tmp, "store.sqlite")
        conn = sqlite3.connect(store)
        self.addCleanup(lambda: conn.close())
        conn.execute("DROP TABLE memory")
        conn.execute("CREATE TABLE unrelated (x INT)")
        conn.executemany("INSERT INTO unrelated VALUES (?)", [(1,), (2,)])
        conn.commit()
        self.assertTrue(Path(str(store) + "-wal").exists(),
                        "fixture precondition: residue must be present when "
                        "the report reads")
        self._assert_refusal(
            "schema-less store with residue",
            "cannot read the store snapshot")


    @unittest.skipUnless(os.name == "nt",
                         "exclusive-open lock is Windows-specific; POSIX uses "
                         "chmod and is covered by the same contract there")
    def test_eager_open_failure_refuses_with_the_contracted_shape(self):
        # Pins the EAGER half of the direct-path contract. sqlite3.connect
        # normally succeeds lazily, but an EXCLUSIVE open makes it fail right
        # there, before _verify_readable can run -- so without the
        # `except sqlite3.Error` wrapper this path raised a raw traceback at
        # exit 1 instead of the contracted refusal.
        import ctypes
        from ctypes import wintypes
        self._write_one_line()
        store = os.path.join(self.tmp, "store.sqlite")
        GENERIC_READ = 0x80000000
        FILE_SHARE_NONE = 0x0
        OPEN_EXISTING = 3
        handle = wintypes.HANDLE()
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            store, GENERIC_READ, FILE_SHARE_NONE, None, OPEN_EXISTING,
            0x80, None)  # 0x80 = FILE_ATTRIBUTE_NORMAL
        if handle in (None, -1, wintypes.HANDLE(-1).value):
            self.skipTest("could not take an exclusive lock on this store")
        try:
            self._assert_refusal("eager open failure", "cannot read the store")
        finally:
            kernel32.CloseHandle(handle)

    def test_corrupt_store_with_residue_refuses_without_leaking(self):
        # Exercises the integrity-failure branch of the staging path, which
        # round 1 found removed the staging dir while the connection was open.
        self._write_one_line()
        self._make_residue()
        Path(self.tmp, "store.sqlite").write_bytes(
            b"torn and definitely not a database" * 400)
        self._assert_refusal("corrupt store with residue",
                             "cannot read the store snapshot")


def _stage_dirs():
    """Staging directories the report may have left in the OS temp dir."""
    return [p.name for p in Path(tempfile.gettempdir()).glob(
        "zmem-report-stage-*")]


class _LiveWriter(threading.Thread):
    """Commits rows AND runs wal_checkpoint(TRUNCATE) continuously.

    It deliberately does NOT append to the decision log: the log must stay
    STATIC during the report run so the test's independent oracle can read it
    deterministically. The store is the thing under concurrent mutation, which
    is exactly the surface the snapshot caveat governs."""

    daemon = True

    def __init__(self, store: Path):
        super().__init__()
        self.store = str(store)
        self.stop = threading.Event()
        self.inserted = 0
        self.err = None

    def run(self):
        try:
            conn = sqlite3.connect(self.store, timeout=15.0)
            conn.execute("PRAGMA busy_timeout=15000")
            try:
                while not self.stop.is_set():
                    for _ in range(5):
                        self.inserted += 1
                        conn.execute(
                            "INSERT INTO memory (id, namespace, type, content,"
                            " signal, confidence, ingestion_ts, source_ref)"
                            " VALUES (?,?,?,?,?,?,?,?)",
                            ("lw%06d-0000-4000-8000-000000000000"
                             % self.inserted, "project:p01guard", "lesson",
                             "live writer row %d padpadpadpad" % self.inserted,
                             "test", 0.9, "2026-10-09T00:00:00Z", "livewriter"))
                    conn.commit()
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    except sqlite3.Error:
                        pass
                    time.sleep(0.01)
            finally:
                conn.close()
        except Exception as exc:  # pragma: no cover - diagnostic
            self.err = exc


class LiveWriterTest(_ReportCase):
    """Guardrail 4: the report stays correct and read-only under a live writer.

    Pins exactly what the disclosed snapshot caveat permits and forbids:
      * rows[*].delivered/sessions/max_per_session stay EXACT (log-derived);
      * the tier-SUMMED delivered/withheld totals stay EXACT (also log-derived);
      * the seeded row, committed before the writer started, keeps its tier.
    rc may be 2: a staged copy that is structurally torn legitimately refuses.
    """

    def test_log_derived_figures_survive_a_concurrent_writer(self):
        # The log is STATIC and references ids the writer will create. Those
        # ids may or may not be present in whatever snapshot the report takes
        # — that is the documented caveat — but the log-derived figures must
        # be exact regardless.
        pending = ["lw%06d-0000-4000-8000-000000000000" % i
                   for i in range(1, 41)]
        lines = [_decision_line(1740002100, [self.pid], [self.pid],
                                "sess-live", "pretool")]
        for i, row_id in enumerate(pending):
            lines.append(_decision_line(1740002200 + i, [], [row_id],
                                        "sess-live", "pretool"))
        self._write_log(lines)

        writer = _LiveWriter(Path(self.tmp, "store.sqlite"))
        writer.start()
        try:
            time.sleep(0.35)
            r = self._run_report()
        finally:
            writer.stop.set()
            writer.join(timeout=10)
        self.assertIsNone(writer.err, "live writer failed: %r" % writer.err)
        self.assertGreater(writer.inserted, 0,
                           "the live writer must have committed rows, else "
                           "this test proves nothing about concurrency")

        if r.returncode == 2:
            # A torn staged copy legitimately refuses rather than reporting a
            # partial read. Verify the refusal is the documented contract.
            self.assertIn("[zmem] report:", r.stderr)
            return
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)

        # 1. The seeded row's own line is log-derived and must be exact.
        self.assertEqual(data["rows"][self.pid]["delivered"], 1)
        self.assertEqual(data["rows"][self.pid]["sessions"], 1)
        self.assertEqual(data["rows"][self.pid]["max_per_session"], 1)

        # 2. Tier-SUMMED totals are log-derived and must be exact. The oracle
        # is computed HERE, directly from the static log text, so it is
        # independent of the module under test.
        expected_delivered, expected_withheld = _event_counts_from_log(
            self.log)
        summed_delivered = sum(c["delivered"] for tier in
                               data["by_tier_moment"].values()
                               for c in tier.values())
        summed_withheld = sum(c["withheld"] for tier in
                              data["by_tier_moment"].values()
                              for c in tier.values())
        self.assertEqual(summed_delivered, expected_delivered,
                         "tier-summed delivered must equal the distinct "
                         "delivered id count computed from the log")
        self.assertEqual(summed_withheld, expected_withheld,
                         "tier-summed withheld must equal the distinct "
                         "withheld id count computed from the log")
        self.assertEqual(expected_withheld, len(pending),
                         "fixture precondition: every pending id is a withheld "
                         "candidate, so the oracle is non-trivial")

        # 3. The seeded row was committed long before the writer started, so
        #    it must keep its tier; degradation to unknown is only permitted
        #    for rows inside the snapshot window.
        self.assertEqual(data["rows"][self.pid]["tier"], "project",
                         "a row committed before the writer started must keep "
                         "its tier; degradation to unknown is only permitted "
                         "for rows inside the snapshot window")


if __name__ == "__main__":
    unittest.main(verbosity=2)