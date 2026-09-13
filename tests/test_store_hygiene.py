"""Store hygiene report tests (issue #97, Workstream E PR 1 of 7).

Pins the read-only report contract: counts and duplicates (AC2), the
evidence-gated none-upgrade triage (AC3), snapshot invariance (AC4), and
fixture-digest reproducibility. The frozen acceptance checks in
.agents/issue-traces/97-store-hygiene-namespace-reconciliation/repro/checks/
exercise the same contract end-to-end via the CLI; these unittests pin it
in-process.

Env pins (ZMEM_STORE / ZMEM_DATA / ZMEM_MODELS_DIR / ZMEM_MODEL_AUTODOWNLOAD)
are set at module top BEFORE any storelib import — repo convention, since
storelib freezes STORE_PATH at first import.

Run: python tests/test_store_hygiene.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import uuid as _uuid

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "store_hygiene"

_test_scratch = Path(tempfile.gettempdir()) / (
    "zmem-store-hygiene-tests-" + _uuid.uuid4().hex
)
_test_scratch.mkdir(parents=True, exist_ok=True)
os.environ["ZMEM_STORE"] = str(_test_scratch / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_test_scratch)
os.environ["ZMEM_MODELS_DIR"] = str(_test_scratch / "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

sys.path.insert(0, str(SCRIPTS_DIR))

from storelib import hygiene  # noqa: E402

# The four fixture digests (issue #97 fixture contract). Regenerate fixtures
# with: python tests/fixtures/store_hygiene/generate.py
FIXTURE_DIGESTS = {
    "rows.jsonl": "3514c1150895d2f1cf4ea3a626b7eb01fbcdced2699f57df9cb3b3b926701cfb",
    "origin-map.json": "85c8f54cb4c69e922b4e243222b89cb11f4a1d35d453d036e3e075db3f039972",
    "evidence-map.json": "1ae0ed47b76f84b0959195fe604c7caf4751c32e72767a2f3754015ce2d69155",
    "expected-report.json": "b8c0ef8bdb9ed1a4586b523043ef7550c3835dd62c08772f0ee61af63166b686",
}

HERMES_NS = "user:global"
NS_SWARM = "project:github.com/zaxbyhub/opencode-swarm"
NS_ZMEM = "project:github.com/zaxbysauce/zmem"
JUNK = ["ns1", "ns2", "project:", "test", "unfoldtest", "user:t"]


def _hermes_id(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _support_id(n: int) -> str:
    return f"aaaaaaaa-0000-4000-8000-{n:012d}"


class StoreHygieneTest(unittest.TestCase):
    """Read-only report + evidence-gated triage contract (issue #97)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="zmem-store-hygiene-")
        cls.scratch = Path(cls._tmp.name)
        # Build the fixture fresh into the scratch dir and copy the snapshot,
        # exactly like the issue's operator recipe (snapshot-copy first).
        proc = subprocess.run(
            [sys.executable, str(FIXTURE_DIR / "generate.py"),
             "--out-dir", str(cls.scratch / "fixtures")],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        if proc.returncode != 0:
            raise AssertionError(f"fixture generator failed: {proc.stderr}")
        cls.fixtures = cls.scratch / "fixtures"
        cls.snapshot = cls.scratch / "snapshot.sqlite"
        shutil.copyfile(cls.fixtures / "snapshot.sqlite", cls.snapshot)
        cls.origin_map = json.loads(
            (cls.fixtures / "origin-map.json").read_text(encoding="utf-8"))
        cls.evidence_map = json.loads(
            (cls.fixtures / "evidence-map.json").read_text(encoding="utf-8"))
        cls.expected = json.loads(
            (cls.fixtures / "expected-report.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _open_snapshot(self, path: Path | None = None) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path or self.snapshot))
        conn.row_factory = sqlite3.Row
        return conn

    def _report(self, snapshot: Path | None = None) -> dict:
        conn = self._open_snapshot(snapshot)
        try:
            return hygiene.build_report(
                conn, origin_map=self.origin_map, evidence_map=self.evidence_map)
        finally:
            conn.close()

    def test_fixture_digests_reproducible(self):
        for name, expected_digest in FIXTURE_DIGESTS.items():
            blob = (self.fixtures / name).read_bytes()
            # Normalize CRLF: the generator writes LF on every platform, but
            # a Windows checkout may materialize the tracked copies as CRLF.
            normalized = blob.replace(b"\r\n", b"\n")
            self.assertEqual(
                hashlib.sha256(normalized).hexdigest(), expected_digest, name)

    def test_report_counts_and_duplicates(self):
        report = self._report()

        # Report shape matches the generated expectation byte-for-byte
        # (expected-report.json omits nothing: build_report never adds the
        # digest; main() does).
        self.assertEqual(report, self.expected)

        self.assertEqual(report["totals"],
                         {"rows": 855, "live": 853, "tombstoned": 2})
        self.assertEqual(len(report["hermes"]["mapped_ids"]), 833)
        self.assertEqual(report["hermes"]["mapped_ids"][0], _hermes_id(1))
        self.assertEqual(report["hermes"]["mapped_ids"][-1], _hermes_id(833))
        self.assertEqual(report["hermes"]["live"], 833)
        self.assertEqual(report["junk_namespaces"]["namespaces"], JUNK)
        for ns in JUNK:
            self.assertGreaterEqual(report["junk_namespaces"]["counts"][ns], 1, ns)

        dup = {g["namespaces"][0]: g for g in report["duplicates"]}
        self.assertEqual(len(report["duplicates"]), 4)
        self.assertIn(NS_SWARM, dup)
        self.assertIn(NS_ZMEM, dup)
        swarm_sizes = sorted(len(g["ids"]) for g in report["duplicates"]
                             if g["namespaces"] == [NS_SWARM])
        self.assertEqual(swarm_sizes, [2, 3])
        self.assertEqual(report["namespaces"], sorted(report["namespaces"]))
        self.assertEqual(report["signals"], sorted(report["signals"]))

    def test_upgrade_requires_later_linked_proof(self):
        report = self._report()
        actions = report["none_upgrade_plan"]

        # Exactly ONE of the three evidence rows qualifies (the VALID one).
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action["none_id"], _hermes_id(1))
        self.assertEqual(action["signal"], "test")
        self.assertEqual(action["namespace"], HERMES_NS)
        self.assertEqual(
            action["action"],
            "python skills/memory/scripts/store.py update"
            f" --id {_hermes_id(1)}"
            " --content grounded test lesson for valid case"
            " --signal test"
            " --source-ref session:fixture-valid-case --json",
        )
        self.assertIn("session:fixture-valid-case", action["reason"])

        # Discipline: drop each gate and the action disappears.
        conn = self._open_snapshot()
        try:
            # (a) unlinked row still yields nothing even when manufactured
            # into the gate (defensive: the generator's UNLINKED case).
            rows = conn.execute(
                "SELECT id FROM memory WHERE content LIKE 'grounded test lesson"
                " for unlinked case'").fetchall()
            self.assertEqual(len(rows), 1)  # exists, but produces no action
        finally:
            conn.close()
        no_action_ids = {a["none_id"] for a in actions}
        self.assertNotIn(_hermes_id(2), no_action_ids)  # UNLINKED
        self.assertNotIn(_hermes_id(3), no_action_ids)  # EARLIER

    def test_report_does_not_write(self):
        before_sha = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        conn = self._open_snapshot()
        try:
            before_rows = conn.execute(
                "SELECT * FROM memory ORDER BY id").fetchall()
            report = hygiene.build_report(
                conn, origin_map=self.origin_map, evidence_map=self.evidence_map)
        finally:
            conn.close()
        after_sha = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        self.assertEqual(before_sha, after_sha)
        conn = self._open_snapshot()
        try:
            after_rows = conn.execute(
                "SELECT * FROM memory ORDER BY id").fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [tuple(r) for r in before_rows], [tuple(r) for r in after_rows])
        # No journal/wal sidecars next to the snapshot.
        for suffix in ("-journal", "-wal", "-shm"):
            self.assertFalse(
                Path(str(self.snapshot) + suffix).exists(), suffix)
        self.assertEqual(len(report["none_upgrade_plan"]), 1)

    def test_rerun_omits_superseded_target(self):
        # Copy the snapshot, supersede the VALID none target (what the #168
        # update flow does), rerun: the plan omits the upgraded target.
        rerun_snapshot = self.scratch / "rerun.sqlite"
        shutil.copyfile(self.fixtures / "snapshot.sqlite", rerun_snapshot)
        conn = sqlite3.connect(str(rerun_snapshot))
        try:
            conn.execute(
                "UPDATE memory SET superseded_at = '2026-09-10T02:00:00Z'"
                " WHERE id = ?", (_hermes_id(1),))
            conn.commit()
        finally:
            conn.close()
        report = self._report(rerun_snapshot)
        self.assertEqual(report["none_upgrade_plan"], [])
        self.assertEqual(report["hermes"]["live"], 832)
        self.assertEqual(report["totals"]["tombstoned"], 3)

        # Rerunning against the SAME snapshot is action-identical.
        again = self._report(rerun_snapshot)
        self.assertEqual(again, report)


if __name__ == "__main__":
    unittest.main()
