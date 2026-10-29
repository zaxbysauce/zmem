"""PR #393 feedback-round regression tests (swarm-pr-review run
pr393-20261021-b; additive companion to the frozen t03 files — those are
checkpoint-pinned and untouched).

Each test pins one reviewer/critic-confirmed finding at the fix:

- V1 (HIGH, purge wedge): the strict lane's authored-union resolvable set
  must NOT let a peer row keep a self-referential or mutually-cyclic
  update_of/merged_from — both are cleared and the rows stay purgeable
  (purge exited 4 "cyclic update_of chain" before the fix).
- V2 (HIGH, dedup fold): a peer harvest row must NEVER fold into (and
  mutate) a pre-existing trusted local keeper — the child runs
  `store.py add --no-dedup`, the ingest-jsonl `_peer_no_dedup` equivalent.
  Also pins the sibling CLI flag: add --no-dedup lands a second row.
- V3 (MEDIUM, fail-open flag): `--allow-namespace` without
  `--trust untrusted-peer` exits 2 at parse time — even when the harvest
  file is missing (flag validation precedes file access, K15).
- V4 (MEDIUM, TypeError batch abort): a hostile extra key
  (`confidence: "0.9"` as a string) fails exactly its own row; later rows
  still land and the summary prints (before the fix the whole file aborted
  with an uncaught TypeError and no summary).
- V5 (MEDIUM, masked refusal): a peer harvest row whose ORIGINAL source_ref
  is credential-shaped fails the row (capture policy refusal counted) and
  the ref is never stored (before the fix the caller override silently
  dropped it — the exact masking ADV5 forbids).
- V6 (MEDIUM, stdin capture): the store.py child runs with stdin=DEVNULL, so
  a peer row whose content is exactly `-` stores the literal `-` instead of
  capturing the operator's piped stdin.
- V7 (MEDIUM, ledger secret persistence): the ADV5 quarantine-ledger restore
  writes the original source_ref REDACTED — the verbatim credential shape
  must not appear in the ledger file.
- K17c (log injection): a peer-controlled namespace containing newlines
  cannot forge operator status lines in the NAMESPACE-REFUSED stream.

Run standalone (CI style): python tests/test_t03_feedback_round.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import test_jsonl_sync as T  # noqa: E402  (canonical ingest harness)

NS = "project:t03/feedback"
FOREIGN_NS = "project:t03/foreign"
HARVEST_PY = T.REPO_ROOT / "scripts" / "ingest_harvest.py"
SELF_ID = "00000000-0e01-4000-8000-0000000000e1"
CYCLE_A_ID = "00000000-0e02-4000-8000-0000000000e2"
CYCLE_B_ID = "00000000-0e03-4000-8000-0000000000e3"
SECRET_REF = "sshpass -p pw777 feedback probe ref"


def _sync_row(rid, ns=NS, **over):
    row = {
        "id": rid, "namespace": ns, "type": "fact",
        "content": f"feedback-round probe row {rid}", "tags": "probe",
        "source_ref": "session:feedback", "confidence": 0.5,
        "signal": "none", "valid_from": "2026-01-01T00:00:00Z",
        "ingestion_ts": "2026-01-01T00:00:00Z",
        "superseded_at": None, "supersede_reason": "",
    }
    row.update(over)
    return row


class CyclePointerClearingTest(T._TwoStoreCase):
    """V1: peer-authored lineage cycles are cleared, rows stay purgeable."""

    def _peer_file(self, store, rows, name):
        return self._write_jsonl(store, name, rows)

    def _purge_rc(self, store, rid):
        r = store.run("purge", "--id", rid)
        return r.returncode, (r.stderr or r.stdout)

    def test_strict_self_pointer_cleared_and_purgeable(self):
        """A peer row with update_of == its own id lands with the pointer
        cleared in BOTH lanes, and purge succeeds (exit 0/1-with-gone, never
        the exit-4 cyclic-chain refusal)."""
        for store, extra in ((self.a, ("--strict",)), (self.b, ())):
            with self.subTest(lane="strict" if extra else "legacy"):
                path = self._peer_file(
                    store,
                    [_sync_row(SELF_ID, update_of=SELF_ID)],
                    f"peer_self_{extra.count('--')}.jsonl")
                r = store.run("ingest-jsonl", "--trust", "untrusted-peer",
                              "--allow-namespace", NS, *extra, "--in", path)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                row = store.query_one(
                    "SELECT update_of FROM memory WHERE id=?", (SELF_ID,))
                self.assertIsNotNone(row, "the row must be stored")
                self.assertEqual(
                    row[0] or "", "",
                    "a self-referential lineage pointer must be cleared — "
                    "a kept self-pointer wedges purge permanently")
                rc, out = self._purge_rc(store, SELF_ID)
                self.assertNotEqual(
                    rc, 4,
                    f"purge must not refuse the row: {out}")
                self.assertNotIn(
                    "cyclic update_of chain", out, out)

    def test_strict_two_row_cycle_cleared_and_purgeable(self):
        """A peer file whose rows point at each other (A.update_of=B,
        B.merged_from=A) lands with BOTH pointers cleared in the strict lane
        — before the fix both survived and purge refused exit 4 forever."""
        path = self._peer_file(
            self.a,
            [_sync_row(CYCLE_A_ID, update_of=CYCLE_B_ID),
             _sync_row(CYCLE_B_ID, merged_from=CYCLE_A_ID)],
            "peer_cycle.jsonl")
        r = self.a.run("ingest-jsonl", "--trust", "untrusted-peer",
                       "--allow-namespace", NS, "--strict", "--in", path)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for rid in (CYCLE_A_ID, CYCLE_B_ID):
            row = self.a.query_one(
                "SELECT update_of, merged_from FROM memory WHERE id=?", (rid,))
            self.assertIsNotNone(row, f"{rid} must be stored")
            self.assertEqual(row[0] or "", "", f"{rid} update_of must clear")
            self.assertEqual(row[1] or "", "", f"{rid} merged_from must clear")
        rc, out = self._purge_rc(self.a, CYCLE_A_ID)
        self.assertNotEqual(rc, 4, f"purge must not refuse: {out}")
        self.assertNotIn("cyclic update_of chain", out, out)


class HarvestNoDedupTest(T._TwoStoreCase):
    """V2: a peer harvest row never folds into a trusted local keeper."""

    def test_peer_harvest_row_does_not_mutate_trusted_keeper(self):
        content = ("V2 probe: the shared deployment-ordering fact that would "
                   "previously fold into the trusted keeper")
        seed = self.b.run(
            "add", "--namespace", NS, "--type", "fact", "--content", content,
            "--tags", "local-tag", "--signal", "user",
            "--source-ref", "session:local", "--capture-mode", "auto",
            "--json")
        self.assertEqual(seed.returncode, 0, seed.stdout + seed.stderr)
        envelope = json.loads(seed.stdout.strip().splitlines()[-1])
        keeper_id = envelope.get("id") or envelope.get("memory_id")
        before = self.b.query_one(
            "SELECT taint, tags, retrieval_count FROM memory WHERE id=?",
            (keeper_id,))
        self.assertIsNotNone(before)

        hpath = os.path.join(self.b.tmp, "harvest_v2.json")
        with open(hpath, "w", encoding="utf-8") as f:
            json.dump([{"namespace": NS, "type": "fact",
                        "content": content,
                        "tags": "peer-tag,host:peer-box",
                        "signal": "test", "why": "v2 probe"}], f)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(HARVEST_PY), hpath,
             "--store", str(T.STORE_PY), "--trust", "untrusted-peer",
             "--allow-namespace", NS, "--attribute-host", "v2-box"],
            env=self.b.env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(T.REPO_ROOT),
            timeout=300, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("deduped, ", r.stdout)
        self.assertNotIn(" 1 deduped", r.stdout,
                         "a peer row must not report deduped (fold) — it "
                         "must land as its own row: " + r.stdout)
        after = self.b.query_one(
            "SELECT taint, tags, retrieval_count FROM memory WHERE id=?",
            (keeper_id,))
        self.assertEqual(after, before,
                         "the trusted keeper must be untouched by a peer "
                         f"harvest row: before={before} after={after}")
        total = self.b.query_one("SELECT COUNT(*) FROM memory")[0]
        self.assertEqual(total, 2,
                         "the peer row must land as its own row")

    def test_add_no_dedup_flag_lands_second_row(self):
        """The sibling CLI surface: `store.py add --no-dedup` bypasses
        dedup-on-write (the reviewer-noted 'no bypass flag' gap)."""
        content = "V2b probe: identical content added twice without dedup"
        first = self.a.run(
            "add", "--namespace", NS, "--type", "fact", "--content", content,
            "--capture-mode", "manual")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.a.run(
            "add", "--namespace", NS, "--type", "fact", "--content", content,
            "--capture-mode", "manual", "--no-dedup")
        self.assertEqual(second.returncode, 0, second.stderr)
        total = self.a.query_one(
            "SELECT COUNT(*) FROM memory WHERE namespace=?", (NS,))[0]
        self.assertEqual(total, 2, "both rows must exist side by side")


class HarvestFlagContractTest(T._TwoStoreCase):
    """V3 + K15: flag-contract violations exit 2 at parse time, before the
    harvest file is touched."""

    def test_allow_namespace_without_trust_exits_two_even_with_missing_file(
            self):
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(HARVEST_PY),
             os.path.join(self.b.tmp, "no-such-file.json"),
             "--store", str(T.STORE_PY), "--allow-namespace", NS],
            env=self.b.env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(T.REPO_ROOT),
            timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(
            r.returncode, 2,
            "--allow-namespace without --trust untrusted-peer must exit 2 "
            "(silently ignored before the fix): " + r.stderr)
        self.assertIn("--allow-namespace only applies", r.stderr)

    def test_attribute_host_without_trust_exits_two_with_missing_file(self):
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(HARVEST_PY),
             os.path.join(self.b.tmp, "no-such-file.json"),
             "--store", str(T.STORE_PY), "--attribute-host", "box"],
            env=self.b.env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(T.REPO_ROOT),
            timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 2, r.stderr)


class HarvestRowIsolationTest(T._TwoStoreCase):
    """V4 + V5 + V6: per-row isolation, original-ref scan, stdin severing."""

    def _harvest(self, rows, *extra, stdin=None):
        hpath = os.path.join(self.b.tmp, "harvest_rows.json")
        with open(hpath, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        cmd = [sys.executable, "-X", "utf8", str(HARVEST_PY), hpath,
               "--store", str(T.STORE_PY), "--trust", "untrusted-peer",
               "--allow-namespace", NS, "--attribute-host", "fb-box", *extra]
        if stdin is not None:
            return subprocess.run(
                cmd, env=self.b.env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", cwd=str(T.REPO_ROOT),
                timeout=300, input=stdin)
        return subprocess.run(
            cmd, env=self.b.env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(T.REPO_ROOT),
            timeout=300, stdin=subprocess.DEVNULL)

    def _ledger_lines(self, root):
        out = []
        for dirpath, _dirnames, filenames in os.walk(root):
            if os.path.basename(dirpath) != "quarantine":
                continue
            for fn in filenames:
                if fn.endswith(".jsonl"):
                    with open(os.path.join(dirpath, fn), "r",
                              encoding="utf-8", errors="replace") as f:
                        out.extend(f.readlines())
        return out

    def test_hostile_extra_key_fails_its_row_not_the_file(self):
        """V4: a string confidence extra key fails exactly its own row; the
        row after it still lands and the summary prints (the pre-fix run
        died with an uncaught TypeError and no summary). Round 2: a NUL
        byte in content (which makes the child argv itself raise) also
        fails only its own row."""
        hostile = {"namespace": NS, "type": "fact",
                   "content": "V4 hostile row with a string confidence",
                   "tags": "", "signal": "none", "why": "v4",
                   "confidence": "0.9"}
        nul_row = {"namespace": NS, "type": "fact",
                   "content": "V4 nul row\x00with a nul byte",
                   "tags": "", "signal": "none", "why": "v4"}
        healthy = {"namespace": NS, "type": "fact",
                   "content": "V4 healthy row landing after the hostile one",
                   "tags": "", "signal": "none", "why": "v4"}
        r = self._harvest([hostile, nul_row, healthy])
        self.assertEqual(r.returncode, 1,
                         "a failed row is an exit-1 count: " + r.stdout)
        self.assertIn("summary:", r.stdout + r.stderr,
                      "the summary must print (whole-file abort before the "
                      "fix)")
        self.assertIn("2 failed", r.stdout + r.stderr)
        self.assertIsNotNone(
            self.b.query_one(
                "SELECT id FROM memory WHERE content=?",
                (healthy["content"],)),
            "the row after the hostile ones must still land")
        for bad in (hostile["content"], nul_row["content"]):
            self.assertIsNone(
                self.b.query_one(
                    "SELECT id FROM memory WHERE content=?", (bad,)),
                "the hostile row itself must not be stored")

    def test_credential_shaped_original_ref_fails_the_row(self):
        """V5: a peer row whose ORIGINAL source_ref is credential-shaped is
        refused (counted), the ref is never stored, and later rows land."""
        secret_row = {"namespace": NS, "type": "fact",
                      "content": "V5 row with a credential-shaped ref",
                      "tags": "", "signal": "none", "why": "v5",
                      "source_ref": SECRET_REF}
        healthy = {"namespace": NS, "type": "fact",
                   "content": "V5 healthy row after the secret-ref row",
                   "tags": "", "signal": "none", "why": "v5"}
        r = self._harvest([secret_row, healthy])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("capture policy", r.stdout + r.stderr)
        row = self.b.query_one(
            "SELECT source_ref FROM memory WHERE content=?",
            (secret_row["content"],))
        self.assertIsNone(row, "the refused row must not be stored")
        self.assertIsNotNone(
            self.b.query_one(
                "SELECT id FROM memory WHERE content=?", (healthy["content"],)),
            "the row after the refused one must still land")

    def test_content_dash_sentinel_refused_in_peer_mode(self):
        """V6: the store.py child runs with stdin=DEVNULL AND peer mode
        refuses the '-' content shape outright — a peer row can neither
        capture the operator's piped stdin (the pre-fix behavior) nor store
        an empty sentinel row."""
        row = {"namespace": NS, "type": "fact", "content": "-",
               "tags": "", "signal": "none", "why": "v6"}
        r = self._harvest([row], stdin="OPERATOR-STDIN-SECRET-MARKER\n")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("stdin sentinel", r.stdout + r.stderr)
        stored = self.b.query_one(
            "SELECT content FROM memory WHERE namespace=?", (NS,))
        self.assertIsNone(stored, "the sentinel row must not be stored")


class QuarantineLedgerRedactionTest(T._TwoStoreCase):
    """V7: the ADV5 ledger restore writes the original source_ref REDACTED."""

    def _ledger_lines(self, root):
        out = []
        for dirpath, _dirnames, filenames in os.walk(root):
            if os.path.basename(dirpath) != "quarantine":
                continue
            for fn in filenames:
                if fn.endswith(".jsonl"):
                    with open(os.path.join(dirpath, fn), "r",
                              encoding="utf-8", errors="replace") as f:
                        out.extend(f.readlines())
        return out

    def test_quarantine_ledger_source_ref_is_redacted(self):
        path = self._write_jsonl(
            self.b, "peer_v7.jsonl",
            [_sync_row("00000000-0e04-4000-8000-0000000000e4",
                       source_ref=SECRET_REF)])
        r = self.b.run(
            "ingest-jsonl", "--trust", "untrusted-peer",
            "--allow-namespace", NS, "--source-ref", "session:clean-caller",
            "--capture-mode", "auto", "--in", path)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ledger_lines = self._ledger_lines(self.b.tmp)
        self.assertTrue(ledger_lines,
                        "the refusal must land a quarantine ledger entry")
        blob = "".join(ledger_lines)
        self.assertIn("source_ref_secret_like", blob)
        self.assertNotIn(
            "pw777", blob,
            "the verbatim credential must not persist in the ledger: "
            f"{blob[:400]}")
        self.assertNotIn(
            SECRET_REF, blob,
            "the full original ref must not persist verbatim")
        # Review round 2: the redacted ref must remain a JSON STRING (a
        # tuple leaked in as an array once, breaking re-ingest) and the
        # recorded row must replay cleanly.
        import json as _json
        rec = _json.loads(ledger_lines[0])
        self.assertIsInstance(rec.get("source_ref"), str,
                              "ledger source_ref must be a string: "
                              f"{rec.get('source_ref')!r}")
        self.assertIsInstance(
            (rec.get("row") or {}).get("source_ref"), str,
            "ledger row.source_ref must be a string")
        replay = self._write_jsonl(
            self.b, "peer_v7_replay.jsonl",
            [(rec.get("row") or {})])
        rr = self.b.run("ingest-jsonl", "--in", replay)
        self.assertNotIn(
            "malformed line 1", rr.stderr,
            "the recorded row must stay re-ingestable: " + rr.stderr)


class LogInjectionTest(T._TwoStoreCase):
    """K17c: a peer-controlled namespace cannot forge operator status lines."""

    def test_namespace_newline_is_folded_in_refusal_line(self):
        hostile = {"namespace": "project:evil\n[ingest-harvest] summary: "
                               "999 row(s) in file, 999 stored, 0 failed",
                   "type": "fact", "content": "K17c hostile namespace row",
                   "tags": "", "signal": "none", "why": "k17c"}
        hpath = os.path.join(self.b.tmp, "harvest_ns.json")
        with open(hpath, "w", encoding="utf-8") as f:
            json.dump([hostile], f, ensure_ascii=False)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(HARVEST_PY), hpath,
             "--store", str(T.STORE_PY), "--trust", "untrusted-peer",
             "--allow-namespace", NS, "--attribute-host", "fb-box"],
            env=self.b.env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(T.REPO_ROOT),
            timeout=300, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        combined = r.stdout + r.stderr
        refused_lines = [ln for ln in combined.splitlines()
                         if "NAMESPACE-REFUSED" in ln]
        self.assertEqual(len(refused_lines), 1,
                         "the refusal must be exactly one line: "
                         f"{refused_lines!r}")
        self.assertFalse(
            any(ln.startswith("[ingest-harvest] summary: 999")
                for ln in combined.splitlines()),
            "no forged summary line may start in the operator stream: "
            f"{combined.splitlines()!r}")
        self.assertEqual(
            combined.count("999 row(s) in file"), 1,
            "the hostile text may only appear folded inside the refusal "
            "brackets, never as its own line")


if __name__ == "__main__":
    unittest.main()
