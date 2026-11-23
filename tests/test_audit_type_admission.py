"""Issue #510 audit: type admission at the apply-ops boundary and the
legacy-pool recall boundary.

AC1 -- when apply-ops receives an operation carrying an unknown memory
type, the system shall return an invalid_type refusal for that operation
and commit no row for it.  Driven through the real ``store.py apply-ops``
CLI with an add op whose args.type is ``factcard`` (not a member of the
canonical ALLOWED_TYPES vocabulary).  This test fails at base, passes at
head after the fix (at base the receipt says committed and a row lands).

AC2 -- when a historical global row carries an unfamiliar type beside a
healthy control row, the system shall keep the healthy row retrievable
through public recall and SessionStart.  Both rows are seeded by direct
sqlite INSERT into a disposable store (a subprocess ``add`` stores
taint=untrusted_tool which the selector filters out).  The recall leg and
the session-start recent leg are driven with the exact hook-lane argvs.
This test fails at base, passes at head after the fix (at base both legs
return the silent empty-pool envelope with zero rendered characters
because one unfamiliar-typed row poisons the whole selection pool).

Hermetic contract: every subprocess pins a fresh throwaway store
(ZMEM_STORE, ZMEM_DATA, ZMEM_MODELS_DIR at a nonexistent dir, model
autodownload off, UTF-8 IO); ambient namespace/session/budget variables
are stripped from the child env; the operator store is never opened.

Run: python tests/test_audit_type_admission.py   (repo convention, no pytest)
"""

from __future__ import annotations
import atexit
import json
import os
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

# ---------------------------------------------------------------------------
# Module-level scratch + env pin BEFORE the _store_guard import (issue #244):
# the guard judges the ambient env at ITS import, so the pin must precede it.
# Children re-pin per-run against their own fresh stores.
# ---------------------------------------------------------------------------
_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-510-type-"))


@atexit.register
def _cleanup_scratch():
    shutil.rmtree(_SCRATCH, ignore_errors=True)


os.environ["ZMEM_STORE"] = str(_SCRATCH / "guard-pin.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

NS = "project:audit510"

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_PROJECT_DIR", "CLAUDE_PROJECT_DIR", "ZCODE_PROJECT_DIR",
    "ZMEM_SESSION", "ZMEM_SESSION_ID", "CLAUDE_SESSION_ID",
    "ZCODE_SESSION_ID", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_EMBED_PROFILE",
    "ZMEM_QUERY_CONTEXT", "ZMEM_TRANSCRIPT", "ZMEM_TIER0",
    "ZMEM_SKILLS_DIRS", "ZMEM_PYTHON", "ZMEM_CROSS_PROJECT",
    "ZMEM_REMOTE_URL", "ZMEM_CROSS_ENCODER",
)


def _child_env(tmp: Path, extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": str(tmp / "store.sqlite"),
        "ZMEM_DATA": str(tmp),
        "ZMEM_MODELS_DIR": str(tmp / "no-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


class AuditTypeAdmissionBase(unittest.TestCase):
    """Fresh throwaway store per test; direct-sqlite seed helper."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-510-case-"))
        # One benign CLI add initializes the schema (its row is
        # taint-filtered by the selector and harmless to the assertions).
        r = self._cli("add", "--namespace", NS, "--type", "fact",
                      "--content", "benign schema init row about zebra apron",
                      "--signal", "test", "--confidence", "0.9", "--json")
        self.assertEqual(r.returncode, 0,
                         "schema-init add failed: %s" % r.stderr.strip()[:300])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cli(self, *args, extra_env=None, input_text=None):
        return subprocess.run(
            [PY, str(STORE_PY)] + list(args), capture_output=True,
            text=True, env=_child_env(self.tmp, extra_env),
            cwd=str(REPO_ROOT), input=input_text, timeout=180)

    def _seed_row(self, mid, ns, typ, content, conf=0.9):
        """Direct INSERT: trusted taint, FTS triggers index the row."""
        conn = sqlite3.connect(str(self.tmp / "store.sqlite"))
        try:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content,"
                " ingestion_ts, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                (mid, ns, typ, content, "2026-11-20T10:00:00Z", conf))
            conn.commit()
        finally:
            conn.close()

    def _count_rows(self, where_sql, params):
        conn = sqlite3.connect(str(self.tmp / "store.sqlite"))
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM memory WHERE " + where_sql,
                params).fetchone()[0]
        finally:
            conn.close()


class InvalidNewTypeAdmissionTests(AuditTypeAdmissionBase):
    """AC1 (#510): unknown op type must be refused, never committed."""

    def test_invalid_new_type(self):
        """apply-ops add with type=factcard is refused invalid_type with no
        committed row.  Fails at base, passes at head after the fix."""
        marker = "kobra delta factcard admission probe"
        op = {"op_id": "op510-" + uuid.uuid4().hex[:12],
              "op": "add",
              "args": {"namespace": NS, "type": "factcard",
                       "content": marker + " " + uuid.uuid4().hex[:8],
                       "signal": "test"}}
        r = self._cli("apply-ops", "--in", "-", "--json",
                      input_text=json.dumps(op))
        self.assertEqual(
            r.returncode, 0,
            "apply-ops exited %d; stderr=%s"
            % (r.returncode, r.stderr.strip()[:400]))
        try:
            payload = json.loads(r.stdout)
        except json.JSONDecodeError:
            self.fail("apply-ops stdout is not JSON: %r" % r.stdout[:300])
        receipts = payload.get("receipts") or []
        self.assertEqual(len(receipts), 1,
                         "expected exactly one receipt, got %r" % receipts)
        receipt = receipts[0]
        self.assertEqual(
            receipt.get("status"), "rejected",
            "AC1: an add op with the unknown type 'factcard' must be "
            "refused, but the receipt says status=%r (base signature: "
            "committed; head contract: rejected). Receipt: %r"
            % (receipt.get("status"), receipt))
        self.assertEqual(
            receipt.get("error"), "invalid_type",
            "AC1: the refusal must name error=invalid_type, got %r"
            % (receipt.get("error"),))
        self.assertFalse(
            receipt.get("canonical_id"),
            "AC1: a refused op must not resolve a canonical row id, got %r"
            % (receipt.get("canonical_id"),))
        committed = self._count_rows("content LIKE ?",
                                     ("%" + marker + "%",))
        self.assertEqual(
            committed, 0,
            "AC1: the refused op must commit NO row, but %d row(s) carrying "
            "the op content exist in the store" % committed)


class LegacyPoolAdmissionTests(AuditTypeAdmissionBase):
    """AC2 (#510): an unfamiliar-typed historical row must not poison the
    pool for a healthy neighbor."""

    def test_legacy_pool(self):
        """One user:global factcard row beside one user:global fact row
        (same grounding words): BOTH the recall leg and the SessionStart
        recent leg must keep delivering the healthy row.  Fails at base,
        passes at head after the fix."""
        grounding = "harbor quartz lantern"
        bad_id = "audit510-legacy-factcard"
        good_id = "audit510-healthy-fact"
        self._seed_row(bad_id, "user:global", "factcard",
                       "%s unfamiliar-type legacy row" % grounding)
        self._seed_row(good_id, "user:global", "fact",
                       "%s fact healthy control row" % grounding)

        # --- recall leg (the exact hook-lane argv) ---
        r = self._cli(
            "recall", "--query", grounding,
            "--namespace", NS, "--limit", "5",
            "--include-global", "--global-limit", "3", "--no-bump",
            "--for-injection", "--json",
            "--session-id=" + "audit510-recall-" + uuid.uuid4().hex[:10],
            "--moment", "pretool", "--lane", "codex")
        self.assertEqual(
            r.returncode, 0,
            "recall leg exited %d; stderr=%s"
            % (r.returncode, r.stderr.strip()[:400]))
        try:
            envelope = json.loads(r.stdout)
        except json.JSONDecodeError:
            self.fail("recall leg stdout is not JSON: %r" % r.stdout[:300])
        rendered = envelope.get("rendered") or ""
        self.assertNotEqual(
            envelope.get("reason"), "empty-pool",
            "AC2 recall leg: the pool must not collapse to the silent "
            "empty-pool envelope (base signature: status empty-pool with "
            "zero rendered chars because the factcard row poisons "
            "selection). Envelope reason=%r" % (envelope.get("reason"),))
        self.assertTrue(
            rendered.strip(),
            "AC2 recall leg: rendered fence text must be non-empty")
        self.assertIn(
            good_id, rendered,
            "AC2 recall leg: the healthy fact row id must stay retrievable "
            "through public recall beside the unfamiliar-typed row "
            "(rendered fence follows): %r" % rendered[:400])

        # --- session-start leg (the exact SessionStart recent argv) ---
        r = self._cli(
            "recent", "--namespace", NS, "--limit", "3",
            "--include-global", "--global-limit", "2", "--no-bump",
            "--for-injection", "--json",
            "--session-id", "audit510-start-" + uuid.uuid4().hex[:10],
            "--moment", "session_start", "--lane", "codex")
        self.assertEqual(
            r.returncode, 0,
            "session-start leg exited %d; stderr=%s"
            % (r.returncode, r.stderr.strip()[:400]))
        try:
            envelope = json.loads(r.stdout)
        except json.JSONDecodeError:
            self.fail("session-start leg stdout is not JSON: %r"
                      % r.stdout[:300])
        rendered = envelope.get("rendered") or ""
        self.assertNotEqual(
            envelope.get("reason"), "empty-pool",
            "AC2 session-start leg: the pool must not collapse to the "
            "silent empty-pool envelope. Envelope reason=%r"
            % (envelope.get("reason"),))
        self.assertTrue(
            rendered.strip(),
            "AC2 session-start leg: rendered fence text must be non-empty")
        self.assertIn(
            good_id, rendered,
            "AC2 session-start leg: the healthy fact row id must stay "
            "retrievable through SessionStart beside the unfamiliar-typed "
            "row (rendered fence follows): %r" % rendered[:400])


def _run_case(class_name: str, method_name: str) -> None:
    suite = unittest.TestSuite()
    suite.addTest(class_name(method_name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise AssertionError(
            "%s.%s failed on this tree (see the runner output above)"
            % (class_name.__name__, method_name))


def test_invalid_new_type():
    """Published-argv wrapper: runs the AC1 TestCase assertions."""
    _run_case(InvalidNewTypeAdmissionTests, "test_invalid_new_type")


def test_legacy_pool():
    """Published-argv wrapper: runs the AC2 TestCase assertions."""
    _run_case(LegacyPoolAdmissionTests, "test_legacy_pool")


if __name__ == "__main__":
    unittest.main()
