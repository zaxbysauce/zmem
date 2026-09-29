"""Issue #256 acceptance checks: the passive lane must withhold credentials.

Seven executable acceptance criteria, every one driven through the EXACT argv
hooks/lib/zmem-recall-body.py builds for a UserPromptSubmit recall:

  recall --query "flange calibration deploy" --namespace project:q02withhold
      --limit 5 --include-global --global-limit 3 --no-bump --for-injection
      --json --session-id sess-q02 --moment user_prompt --lane claude

  AC1 PassiveSecretWithholdTest.test_envelope_carries_no_credential
      — no credential bytes anywhere in the hook-argv stdout envelope
        (results + rendered).
  AC2 PassiveSecretWithholdTest.test_withheld_row_id_is_emitted
      — the withheld row still rides `results` with its id, minus the
        credential bytes (a fix that drops the row entirely stays RED).
  AC3 PassiveSecretWithholdTest.test_envelope_counts_secret_withhold
      — the envelope counts the withhold in integer counters whose key names
        contain "secret", summing to 1.
  AC4 LedgerWithholdTest.test_ledger_holds_no_credential
      — the per-session delivery ledger under <ZMEM_DATA>/ops/*.ledger*
        (the escalation-matcher fuel) carries no credential bytes.
  AC5 PassiveSecretWithholdTest.test_withhold_makes_no_surfaced_write
      — a withheld row records no surfaced_count write.
  AC6 SshpassShapeWithholdTest.test_sshpass_value_not_rendered
      — an sshpass -p '<value>' password is withheld even though the value
        is too short for the generic secret patterns. KNOWN DEPENDENCY:
        stays RED until issue #180's sshpass detector lands — correct and
        expected; do not weaken.
  AC7 GitShaNotWithheldTest.test_credential_withheld_but_git_sha_rendered
      — the credential is withheld while a 40-hex git SHA in the same
        delivery still renders (withholding must be shape-precise).

The incident row lives in namespace user:global (it rides the
--include-global tier) and is seeded via the real CLI with
--signal test --confidence 0.9 so the injection gate passes. Capture stays
manual, so the stored bytes keep the credential verbatim — withholding is a
read-lane concern; each test pins that precondition plus "the recall selects
the row" before its main assertion.

At this base the passive lane renders credentials verbatim (a seeded ghp_
token appears twice in the hook-argv stdout — once in results, once in
rendered — and no secret-named envelope counter exists), so all seven checks
are RED. This file is the frozen acceptance contract for the fix: drive the
real store.py CLI via subprocess against throwaway temp stores, never the
box store.

Run: python tests/test_q02_passive_secret_withhold.py   (no pytest required)
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
PYTHON = sys.executable

NS = "project:q02withhold"
NS_GLOBAL = "user:global"
SESSION_ID = "sess-q02"
DEFAULT_QUERY = "flange calibration deploy"

# The tests/test_redaction.py:31 fake — matches a SECRET_CREDENTIAL_PATTERNS
# gh-token shape, so a manual-capture add only warns (advisory) and stores
# the token verbatim.
FAKE = "ghp_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
# Deliberately too short for the generic secret patterns; only the
# sshpass-shape detector (issue #180) owns this value.
FAKE_SSH = "Xy7" + "fake9Q"
# A 40-hex git commit SHA must NOT be caught by credential withholding.
SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"

INCIDENT_CONTENT = f"flange calibration deploy uses token {FAKE} on the runner"
CLEAN_CONTENT = "flange calibration deploy checklist for the runner"
SSHPASS_CONTENT = (
    f"flange calibration deploy runs sshpass -p '{FAKE_SSH}' ssh ops@node")
SHA_CONTENT = f"flange calibration deploy pinned at commit {SHA}"

# Ambient knobs that must not leak into a fixture subprocess (mirrors the
# pops in tests/test_zero_write_passive.py: clock pin, budget override, embed
# profile, ambient session id, and the cross-project policy pair).
_POP_ENV = ("ZMEM_TEST_NOW", "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_EMBED_PROFILE",
            "ZMEM_SESSION", "ZMEM_CROSS_PROJECT",
            "ZMEM_CROSS_PROJECT_HAZARD_VERBS")

# Child-process reader for the session delivery ledger. storelib is imported
# only in the child (this test process never imports it), and the data dir
# rides argv — never env — exactly as delivery_ledger.delivered_ids expects.
_LEDGER_IDS_CHILD = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from storelib.delivery_ledger import delivered_ids\n"
    "print(json.dumps(delivered_ids(sys.argv[2], sys.argv[3])))\n"
)


class _WithholdBase(unittest.TestCase):
    """Throwaway store + data dir; every check drives the real CLI."""

    def setUp(self):
        # mkdtemp + explicit teardown cleanup: NOT a context-managed
        # TemporaryDirectory — the Windows PermissionError hazard when a
        # handle outlives the context exit.
        self.tmp = tempfile.mkdtemp(prefix="zmem-q02-withhold-")
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.data_dir = self.tmp
        self.env = dict(os.environ)
        self.env["ZMEM_STORE"] = self.store
        self.env["ZMEM_DATA"] = self.data_dir
        self.env["ZMEM_INJECT"] = "1"
        self.env["ZMEM_CAPTURE_MODE"] = "manual"
        self.env["ZMEM_AUTO_REKEY"] = "0"
        # Force lexical recall: a models dir that never exists + no
        # autodownload means pool membership cannot depend on embeddings.
        self.env["ZMEM_MODELS_DIR"] = os.path.join(self.tmp, "models-missing")
        self.env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        for key in _POP_ENV:
            self.env.pop(key, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixture helpers ---------------------------------------------------

    def _run_cli(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            cwd=str(REPO_ROOT), env=dict(self.env),
            capture_output=True, text=True, timeout=90,
        )

    def _add_row(self, namespace, content):
        """Seed one gate-passing row via the real CLI; return its id."""
        r = self._run_cli("add", "--namespace", namespace, "--type", "fact",
                          "--content", content, "--signal", "test",
                          "--confidence", "0.9", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["id"]

    def _read_store(self, sql, params):
        conn = sqlite3.connect(
            "file:" + self.store.replace(os.sep, "/") + "?mode=ro", uri=True)
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def _stored_content(self, row_id):
        row = self._read_store("SELECT content FROM memory WHERE id = ?",
                               (row_id,))
        self.assertIsNotNone(row, "seeded row missing from the store")
        return row[0]

    def _surfaced_count(self, row_id):
        row = self._read_store(
            "SELECT surfaced_count FROM memory WHERE id = ?", (row_id,))
        self.assertIsNotNone(row, "seeded row missing from the store")
        return int(row[0] or 0)

    def seed_incident(self):
        """Seed the user:global credential row and pin the verbatim-storage
        precondition — manual capture keeps the stored bytes intact;
        withholding is a read-lane concern."""
        row_id = self._add_row(NS_GLOBAL, INCIDENT_CONTENT)
        self.assertIn(FAKE, self._stored_content(row_id),
                      "precondition: manual capture mode must store the "
                      "credential verbatim")
        return row_id

    def hook_recall(self, query=DEFAULT_QUERY):
        """Run the EXACT argv hooks/lib/zmem-recall-body.py builds."""
        return self._run_cli(
            "recall", "--query", query,
            "--namespace", NS,
            "--limit", "5",
            "--include-global",
            "--global-limit", "3",
            "--no-bump", "--for-injection", "--json",
            "--session-id", SESSION_ID,
            "--moment", "user_prompt",
            "--lane", "claude",
        )

    def env_json(self, r):
        return json.loads(r.stdout)

    def assert_selected(self, doc, row_id, note="precondition"):
        ids = [row.get("id") for row in doc.get("results") or []]
        self.assertIn(row_id, ids,
                      f"{note}: the recall must select the row, or this "
                      "check proves nothing")

    def _delivered_ids(self):
        """delivered_ids(<data_dir>, <session_id>) read in a CHILD process."""
        r = subprocess.run(
            [PYTHON, "-c", _LEDGER_IDS_CHILD, str(SCRIPTS_DIR),
             self.data_dir, SESSION_ID],
            cwd=str(REPO_ROOT), env=dict(self.env),
            capture_output=True, text=True, timeout=90,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)


class PassiveSecretWithholdTest(_WithholdBase):
    """AC1/AC2/AC3/AC5: the hook-argv envelope and its telemetry."""

    def test_envelope_carries_no_credential(self):
        # AC1: no credential bytes anywhere in the stdout envelope — at base
        # the token appears twice (once in results, once in rendered).
        row_id = self.seed_incident()
        r = self.hook_recall()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = self.env_json(r)
        self.assert_selected(doc, row_id)
        self.assertEqual(r.stdout.count(FAKE), 0)

    def test_withheld_row_id_is_emitted(self):
        # AC2: withholding suppresses the credential BYTES, not the row —
        # the entry with the seeded id must still ride results, minus FAKE.
        # A fix that drops the row entirely stays RED here.
        row_id = self.seed_incident()
        r = self.hook_recall()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = self.env_json(r)
        self.assert_selected(doc, row_id)
        emitted = sum(
            1 for entry in doc["results"]
            if entry.get("id") == row_id and FAKE not in json.dumps(entry))
        self.assertEqual(emitted, 1)

    def test_envelope_counts_secret_withhold(self):
        # AC3: the envelope reports the withhold in an integer counter whose
        # key name contains "secret" (bools excluded), summing to 1.
        row_id = self.seed_incident()
        r = self.hook_recall()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = self.env_json(r)
        self.assert_selected(doc, row_id)
        total = sum(
            value for key, value in doc.items()
            if "secret" in str(key).lower()
            and isinstance(value, int) and not isinstance(value, bool))
        self.assertEqual(total, 1)

    def test_withhold_makes_no_surfaced_write(self):
        # AC5: a withheld row is never surfaced-counted — no popularity
        # telemetry for content the model did not receive.
        row_id = self.seed_incident()
        before = self._surfaced_count(row_id)
        r = self.hook_recall()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = self.env_json(r)
        self.assert_selected(doc, row_id)
        after = self._surfaced_count(row_id)
        self.assertEqual(after - before, 0)


class LedgerWithholdTest(_WithholdBase):
    """AC4: the delivery ledger (escalation-matcher fuel) stays clean."""

    def test_ledger_holds_no_credential(self):
        # The clean project row guarantees a ledger is written even once the
        # credential row is withheld from delivery.
        incident_id = self.seed_incident()
        clean_id = self._add_row(NS, CLEAN_CONTENT)
        r = self.hook_recall()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = self.env_json(r)
        self.assert_selected(doc, incident_id,
                             "precondition 1 (credential row selected)")
        delivered = self._delivered_ids()
        self.assertIn(clean_id, delivered,
                      "precondition 2: the clean row must be recorded in the "
                      "session ledger, or this check proves nothing")
        ops_dir = os.path.join(self.data_dir, "ops")
        hits = 0
        for path in glob.glob(os.path.join(ops_dir, "*.ledger*")):
            with open(path, "rb") as handle:
                hits += handle.read().lower().count(
                    FAKE.lower().encode("utf-8"))
        self.assertEqual(hits, 0)


class SshpassShapeWithholdTest(_WithholdBase):
    """AC6: the sshpass -p '<value>' shape is withheld even though the value
    is too short for the generic secret patterns.

    KNOWN DEPENDENCY: expected to stay RED until issue #180's sshpass
    detector lands — that is correct and expected; do not weaken this."""

    def test_sshpass_value_not_rendered(self):
        row_id = self._add_row(NS_GLOBAL, SSHPASS_CONTENT)
        r = self.hook_recall()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = self.env_json(r)
        self.assert_selected(doc, row_id)
        self.assertEqual(r.stdout.count(FAKE_SSH), 0)


class GitShaNotWithheldTest(_WithholdBase):
    """AC7: withholding is shape-precise — the credential goes, a 40-hex git
    commit SHA in the same delivery stays rendered. A fix keying off the
    generic all-hex patterns would give (0, 0) and stay RED."""

    def test_credential_withheld_but_git_sha_rendered(self):
        incident_id = self.seed_incident()
        sha_id = self._add_row(NS, SHA_CONTENT)
        r = self.hook_recall()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = self.env_json(r)
        self.assert_selected(doc, incident_id)
        self.assert_selected(doc, sha_id)
        rendered = doc["rendered"]
        self.assertIsInstance(rendered, str)
        self.assertEqual((rendered.count(FAKE), rendered.count(SHA)), (0, 1))


class WithholdBudgetAndNoSessionTest(_WithholdBase):
    """Review round 2 (F-267-1/F-267-2): the re-scan must classify BEFORE
    the token budget (a credential straddling the cut is withheld whole,
    never leaked as a clipped fragment) and must cover the no-session
    `--for-injection` lane, not only the session selector."""

    def test_credential_straddling_budget_cut_is_withheld_whole(self):
        # Seed padded incident rows so some row's token straddles the
        # budget cut at ZMEM_INJECT_TOKEN_BUDGET=250: under the old
        # post-budget ordering the cut clipped the ghp_ token to a prefix
        # the patterns no longer matched and the fragment shipped in the
        # envelope and the ledger (probe-verified pre-fix). Under the
        # pre-budget classification every admitted row is withheld whole.
        pads = (30, 34, 38, 42, 46)
        ids = []
        for reps in pads:
            ids.append(self._add_row(
                NS_GLOBAL,
                ("flange calibration deploy " * reps) + INCIDENT_CONTENT))
        self.env["ZMEM_INJECT_TOKEN_BUDGET"] = "250"
        r = self.hook_recall()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout.count(FAKE), 0,
            "no clipped credential fragment may reach the stdout envelope")
        doc = self.env_json(r)
        for entry in doc.get("results", []):
            if entry.get("id") in ids:
                self.assertNotIn(FAKE, json.dumps(entry))
        withheld = [e for e in doc.get("results", [])
                    if e.get("withheld_for_secret")]
        self.assertGreaterEqual(
            len(withheld), 1,
            "precondition: at least one padded incident row must have been "
            "selected and withheld")
        ledger_files = glob.glob(
            os.path.join(self.data_dir, "ops", "*.ledger*"))
        for path in ledger_files:
            self.assertNotIn(
                FAKE, Path(path).read_text(encoding="utf-8"),
                "no clipped credential fragment may reach the ledger")

    def test_no_session_for_injection_withholds(self):
        # F-267-2: recall --for-injection --json WITHOUT --session-id is the
        # same passive gate+budget lane (MCP/eval callers omit session ids);
        # it must withhold like the hook argv does.
        row_id = self.seed_incident()
        r = self._run_cli(
            "recall", "--query", DEFAULT_QUERY,
            "--namespace", NS, "--limit", "5", "--include-global",
            "--global-limit", "3", "--no-bump", "--for-injection", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(FAKE, r.stdout)
        doc = json.loads(r.stdout)
        entries = [e for e in doc.get("results", [])
                   if e.get("id") == row_id]
        self.assertTrue(entries, "the seeded row must still ride results")
        for entry in entries:
            self.assertTrue(entry.get("withheld_for_secret"))
            self.assertEqual(entry.get("content"), "")
        self.assertGreaterEqual(
            sum(v for k, v in doc.items()
                if "secret" in str(k).lower() and isinstance(v, int)
                and not isinstance(v, bool)),
            1,
            "the no-session envelope must count the withhold")


if __name__ == "__main__":
    unittest.main(verbosity=2)
