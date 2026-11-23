"""Issue #501 audit: midrow budget cutoff retry and acknowledgement
exactly-once discipline.

AC5 -- when a byte cap cuts through a reserved row and the same session
retries, the system shall withhold the incomplete row and record exactly
one surface after the complete retry.  A user:global decision row of
about 6300 characters (trusted taint, direct sqlite INSERT) is recalled
on the ``--for-injection`` leg with ZMEM_INJECT_TOKEN_BUDGET=300 (the
row is clipped mid-content by the budget clip at base), then the SAME
session retries with a budget of 2500.  The contract-level assertions:
after the complete retry the full row reached the host (the tail content
marker is inside the rendered fence) and exactly one surface is recorded
(a third call on the same session reports reason already-delivered).
This test fails at base, passes at head after the fix (at base attempt 2
returns reason=already-delivered with zero rendered characters because
the clipped attempt 1 was recorded as delivered).

AC6 -- when two acknowledgements race or terminal receipt processing
times out, the system shall credit the matching store once and remove
terminal sidecars.  A decision row is reserved via
``prefetch --reserve``; TWO concurrent ``prefetch-ack`` subprocesses run
against the same reservation and exactly one must return the committed
row while the other reports the honest duplicate; a second reservation
under ZMEM_RESERVATION_TTL_S=2 acknowledged past its TTL must commit
nothing and report expired; ``delivery-clear`` must remove the session's
delivery ledger state from the data dir the store actually uses (the
parent of the store file); and the expired reservation sidecar must be
reaped by the next reserve.  This leg is a PIN expected to hold at base
already; any failing leg is reported honestly.

Hermetic contract: fresh throwaway store per run, model autodownload off,
UTF-8 IO, ambient namespace/session/budget variables stripped from every
child env; the operator store is never opened.

Run: python tests/test_audit_delivery_ack.py   (repo convention, no pytest)
"""

from __future__ import annotations
import atexit
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PY = sys.executable

AC5_GROUNDING = "meridian relay decision standing order"
AC5_TAIL_MARKER = "FINAL-TAIL-MARKER"
AC6_GROUNDING = "cedar fox marble reservation ledger"

# ---------------------------------------------------------------------------
# Module-level scratch + env pin BEFORE the _store_guard import (issue #244).
# ---------------------------------------------------------------------------
_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-501-ack-"))


@atexit.register
def _cleanup_scratch():
    shutil.rmtree(_SCRATCH, ignore_errors=True)


os.environ["ZMEM_STORE"] = str(_SCRATCH / "guard-pin.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

NS = "project:audit501"

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_PROJECT_DIR", "CLAUDE_PROJECT_DIR", "ZCODE_PROJECT_DIR",
    "ZMEM_SESSION", "ZMEM_SESSION_ID", "CLAUDE_SESSION_ID",
    "ZCODE_SESSION_ID", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_EMBED_PROFILE",
    "ZMEM_QUERY_CONTEXT", "ZMEM_TRANSCRIPT", "ZMEM_TIER0",
    "ZMEM_SKILLS_DIRS", "ZMEM_PYTHON", "ZMEM_CROSS_PROJECT",
    "ZMEM_REMOTE_URL", "ZMEM_CROSS_ENCODER", "ZMEM_RESERVATION_TTL_S",
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


class AuditDeliveryAckBase(unittest.TestCase):
    """Fresh throwaway store per test; direct-sqlite seed helper."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-501-case-"))
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

    def _seed_decision(self, mid, content):
        """Direct INSERT: trusted taint so the selector admits the row."""
        conn = sqlite3.connect(str(self.tmp / "store.sqlite"))
        try:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content,"
                " ingestion_ts, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                (mid, "user:global", "decision", content,
                 "2026-11-20T10:00:00Z", 0.9))
            conn.commit()
        finally:
            conn.close()

    def _recall_for_injection(self, session_id, budget):
        """The exact hook-lane recall argv under an explicit token budget."""
        r = self._cli(
            "recall", "--query", AC5_GROUNDING,
            "--namespace", NS, "--limit", "5",
            "--include-global", "--global-limit", "3", "--no-bump",
            "--for-injection", "--json", "--session-id=" + session_id,
            "--moment", "pretool", "--lane", "codex",
            extra_env={"ZMEM_INJECT_TOKEN_BUDGET": str(budget)})
        self.assertEqual(
            r.returncode, 0,
            "recall exited %d; stderr=%s"
            % (r.returncode, r.stderr.strip()[:400]))
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            self.fail("recall stdout is not JSON: %r" % r.stdout[:300])


class MidrowCutoffRetryTests(AuditDeliveryAckBase):
    """AC5 (#501): a budget-clipped row must not be recorded as delivered."""

    def test_midrow_cutoff_retry(self):
        """Attempt 1 clips the decision row under a 300-token budget;
        attempt 2 on the SAME session with a 2500-token budget must
        deliver the COMPLETE row, and a third call must report
        already-delivered (exactly one recorded surface).  Fails at base,
        passes at head after the fix."""
        filler = "orchard beacon cascade vertex "
        # ~6300 characters: grounded header, filler body, distinctive tail
        # marker that only the COMPLETE row can carry.
        content = ("Decision standing order for the %s: " % AC5_GROUNDING
                   + filler * 210
                   + " %s the %s ends here." % (AC5_TAIL_MARKER,
                                                AC5_GROUNDING))
        self.assertGreater(len(content), 6000)
        self.assertLess(len(content), 7000)
        row_id = "audit501-decision-row"
        self._seed_decision(row_id, content)

        session_id = "audit501-retry-" + uuid.uuid4().hex[:10]
        # Attempt 1: the byte cap cuts through the row (base clips the
        # protected decision row mid-content with the budget-truncated
        # marker; head withholds the incomplete row from the ledger).
        attempt1 = self._recall_for_injection(session_id, 300)
        # Attempt 2: SAME session retries with a budget the complete row
        # fits in (verified: a fresh session at 2500 renders the full row).
        attempt2 = self._recall_for_injection(session_id, 2500)
        rendered2 = attempt2.get("rendered") or ""
        self.assertNotEqual(
            attempt2.get("reason"), "already-delivered",
            "AC5: the same-session complete retry must not be suppressed "
            "by the clipped attempt 1 (base signature: attempt 2 returns "
            "already-delivered with zero rendered chars). reason=%r"
            % (attempt2.get("reason"),))
        self.assertIn(
            AC5_TAIL_MARKER, rendered2,
            "AC5: after the complete retry the FULL row must reach the "
            "host -- the tail content marker is missing from the rendered "
            "fence (rendered head: %r)" % rendered2[:300])
        self.assertIn(
            row_id, rendered2,
            "AC5: the retried decision row id must appear in the rendered "
            "fence (rendered head: %r)" % rendered2[:300])
        # Exactly one surface recorded: a third call on the same session
        # observes the delivery and suppresses.
        attempt3 = self._recall_for_injection(session_id, 2500)
        self.assertEqual(
            attempt3.get("reason"), "already-delivered",
            "AC5: a third call on the same session must report "
            "already-delivered (exactly one recorded surface); got %r"
            % (attempt3.get("reason"),))


class ConcurrentAckAndCleanupTests(AuditDeliveryAckBase):
    """AC6 (#501): exactly-once ack credit, TTL expiry, terminal cleanup."""

    def test_concurrent_ack_and_cleanup(self):
        """Two racing prefetch-ack subprocesses credit the store exactly
        once; an expired reservation commits nothing; delivery-clear
        removes the session ledger and the expired sidecar is reaped.
        PIN: expected to hold at base already."""
        row_id = "audit501-ack-row"
        self._seed_decision(row_id,
                            "%s decision row for the acknowledgement probe"
                            % AC6_GROUNDING)
        sid = "audit501-ack-" + uuid.uuid4().hex[:10]

        # --- reserve ---
        r = self._cli("prefetch", "--query", AC6_GROUNDING,
                      "--namespace", NS, "--session-id", sid,
                      "--moment", "user_prompt", "--lane", "codex",
                      "--json", "--reserve")
        self.assertEqual(r.returncode, 0,
                         "prefetch --reserve failed: %s" % r.stderr[:300])
        reservation = json.loads(r.stdout).get("reservation")
        self.assertTrue(
            reservation,
            "prefetch --reserve must mint a reservation id (envelope: %s)"
            % r.stdout[:300])

        # --- two concurrent acks on the SAME reservation ---
        procs = [subprocess.Popen(
            [PY, str(STORE_PY), "prefetch-ack", "--reservation", reservation],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_child_env(self.tmp), cwd=str(REPO_ROOT)) for _ in range(2)]
        acks = []
        for proc in procs:
            out, err = proc.communicate(timeout=180)
            self.assertEqual(
                proc.returncode, 0,
                "prefetch-ack exited %d; stderr=%s" % (proc.returncode,
                                                       err.strip()[:300]))
            acks.append(json.loads(out))
        winners = [ack for ack in acks if ack.get("committed")]
        duplicates = [ack for ack in acks if ack.get("duplicate")]
        self.assertEqual(
            len(winners), 1,
            "AC6: exactly ONE racing ack must credit the store; got %d "
            "winners out of %d acks: %r" % (len(winners), len(acks), acks))
        self.assertIn(
            row_id, winners[0]["committed"],
            "AC6: the winning ack must commit the reserved row id; got %r"
            % (winners[0]["committed"],))
        self.assertFalse(
            winners[0].get("expired"),
            "AC6: the winning ack must not be classified expired")
        self.assertEqual(
            len(duplicates), 1,
            "AC6: the losing ack must report the honest duplicate=true; "
            "got %d duplicates out of %d acks: %r"
            % (len(duplicates), len(acks), acks))
        self.assertEqual(
            duplicates[0].get("committed"), [],
            "AC6: the duplicate ack must commit nothing")

        # --- TTL expiry leg ---
        ttl_env = {"ZMEM_RESERVATION_TTL_S": "2"}
        sid_ttl = sid + "-ttl"
        r = self._cli("prefetch", "--query", AC6_GROUNDING,
                      "--namespace", NS, "--session-id", sid_ttl,
                      "--moment", "user_prompt", "--lane", "codex",
                      "--json", "--reserve", extra_env=ttl_env)
        self.assertEqual(r.returncode, 0, "ttl reserve failed: %s" % r.stderr)
        reservation_ttl = json.loads(r.stdout).get("reservation")
        self.assertTrue(reservation_ttl, "ttl reserve must mint an id")
        time.sleep(3.2)  # past the 2 s TTL
        r = self._cli("prefetch-ack", "--reservation", reservation_ttl,
                      extra_env=ttl_env)
        self.assertEqual(
            r.returncode, 0,
            "expired ack exited %d; stderr=%s" % (r.returncode,
                                                  r.stderr.strip()[:300]))
        expired_ack = json.loads(r.stdout)
        self.assertEqual(
            expired_ack.get("committed"), [],
            "AC6: an expired reservation must commit nothing")
        self.assertTrue(
            expired_ack.get("expired"),
            "AC6: the past-TTL ack must report expired=true; got %r"
            % (expired_ack,))

        # --- terminal cleanup: delivery-clear removes the session ledger ---
        # The store actually uses the PARENT of the store file as its data
        # dir (ZMEM_DATA == the parent of store.sqlite here).
        ops_dir = self.tmp / "ops"
        ledger_name = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:32] \
            + ".ledger"
        ledger_file = ops_dir / ledger_name
        self.assertTrue(
            ledger_file.exists(),
            "precondition: the session's delivery ledger must exist before "
            "delivery-clear (expected %s)" % ledger_file)
        r = self._cli("delivery-clear", "--session-id", sid)
        self.assertEqual(
            r.returncode, 0,
            "delivery-clear exited %d; stderr=%s"
            % (r.returncode, r.stderr.strip()[:300]))
        cleared = json.loads(r.stdout)
        self.assertTrue(
            cleared.get("ok") and cleared.get("cleared"),
            "delivery-clear must report ok cleared; got %r" % (cleared,))
        self.assertFalse(
            ledger_file.exists(),
            "AC6: delivery-clear must remove the session's delivery ledger "
            "from the data dir (%s)" % ledger_file)

        # --- terminal sidecar removal: the expired reservation is reaped
        # by the next reserve (the lazy TTL reap contract) ---
        expired_sidecar = ops_dir / ("resv-" + reservation_ttl + ".reserve")
        self.assertTrue(
            expired_sidecar.exists(),
            "precondition: the expired reservation sidecar survives its "
            "own ack (the ack's target is reap-exempt); expected %s"
            % expired_sidecar)
        r = self._cli("prefetch", "--query", AC6_GROUNDING,
                      "--namespace", NS, "--session-id", sid + "-sweep",
                      "--moment", "user_prompt", "--lane", "codex",
                      "--json", "--reserve")
        self.assertEqual(r.returncode, 0,
                         "sweep reserve failed: %s" % r.stderr[:300])
        self.assertFalse(
            expired_sidecar.exists(),
            "AC6: the expired reservation sidecar must be removed once "
            "terminal processing reaps it (%s)" % expired_sidecar)


def _run_case(class_name: str, method_name: str) -> None:
    suite = unittest.TestSuite()
    suite.addTest(class_name(method_name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise AssertionError(
            "%s.%s failed on this tree (see the runner output above)"
            % (class_name.__name__, method_name))


def test_midrow_cutoff_retry():
    """Published-argv wrapper: runs the AC5 TestCase assertions."""
    _run_case(MidrowCutoffRetryTests, "test_midrow_cutoff_retry")


def test_concurrent_ack_and_cleanup():
    """Published-argv wrapper: runs the AC6 TestCase assertions."""
    _run_case(ConcurrentAckAndCleanupTests, "test_concurrent_ack_and_cleanup")


if __name__ == "__main__":
    unittest.main()
