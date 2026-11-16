"""Arm's-length acceptance check: the namespace-maintenance
stranded-pointer law (issue trace 480-481-482, Fix 2, PRESERVING).

Published selector: class StrandedPointerTest, method
test_no_auto_remap_or_tombstone.

REAL stranded-pointer scenario: a knowledge pointer is TERMINAL in
namespace A (so the FB-031 guard permits the move); the namespace is
rekeyed A -> B through the REAL ``store.py rekey-namespace --confirm``
CLI.  Frozen contract:

- the LOCAL pointer row MOVES with the namespace (the namespace column
  A -> B; same row id, same content bytes, same digest/version, still
  live — never tombstoned);
- a REMOTE-side view of the same pointer (a second scratch store's copy
  of the row, representing the hub / remote-client state) is UNTOUCHED
  byte-for-byte: no auto-remap, no tombstone marker, no supersede
  written anywhere in the remote store, and no new rows appear there;
- no remediation artifacts appear locally beyond what existed before the
  move (the journal/delivery sidecars stay byte-identical; no tombstone
  marker files).

Green at base (the rekey lane moves only the source namespace's own live
rows and never writes any other store or mints remediation artifacts)
and must STAY green.  REFERENCE evidence for issue #482's maintenance
disclosure — the deployed-fleet side stays open on the issue.

Run: python -X utf8 tests/test_knowledge_maintenance_contract.py
"""

from __future__ import annotations
import os
import sys
import tempfile

# Scratch env pinned BEFORE the isolation guard and any storelib import.
_TMP = tempfile.mkdtemp(prefix="zmem-knmaint-mod-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ["ZMEM_DATA"] = _TMP
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
os.environ.setdefault("ZMEM_MODELS_DIR", os.path.join(_TMP, "no-models"))
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

import json
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable

_TARGET = "notes"
_KEY = "guides/deploy"
_REF = "knowledge:%s:%s" % (_TARGET, _KEY)
_NS_A = "project:stranded"
_NS_B = "project:moved"

_POINTER_COLUMNS = ("id, namespace, type, content, tags, source_ref, "
                    "superseded_at, supersede_reason, signal, taint, "
                    "source_hash, confidence")


def _open(store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(store))
    conn.row_factory = sqlite3.Row
    return conn


def _pointer_rows(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT %s FROM memory WHERE source_ref=? ORDER BY id"
        % _POINTER_COLUMNS, (_REF,))]


def _all_rows(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT %s FROM memory ORDER BY id" % _POINTER_COLUMNS)]


class StrandedPointerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-knmaint-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.env = {**os.environ}
        self.env["ZMEM_STORE"] = str(self.tmp / "store.sqlite").replace("\\", "/")
        self.env["ZMEM_DATA"] = str(self.tmp).replace("\\", "/")
        self.env["ZMEM_MODELS_DIR"] = str(self.tmp / "no-models")
        self.env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        self.env.pop("ZMEM_REMOTE_URL", None)
        r = self._cli("stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        (self.tmp / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                _TARGET: {"kind": "markdown", "root": str(self.vault)}}}
        }), encoding="utf-8")

    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO_ROOT), timeout=180)

    def _init_second_store(self, name: str) -> Path:
        """A second scratch store (the REMOTE-side view holder)."""
        data_dir = self.tmp / name
        data_dir.mkdir()
        env = {**self.env,
               "ZMEM_STORE": str(data_dir / "store.sqlite").replace("\\", "/"),
               "ZMEM_DATA": str(data_dir).replace("\\", "/")}
        r = subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), "stats"],
            capture_output=True, text=True, encoding="utf-8",
            env=env, cwd=str(REPO_ROOT), timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        return data_dir / "store.sqlite"

    def test_no_auto_remap_or_tombstone(self):
        # A REAL capture mints the TERMINAL pointer row in namespace A
        # through the full pipeline (journal, stake, markdown create,
        # SQLite CAS link) — applied history does not block the guard.
        cap = self._cli("knowledge", "capture", "--target", _TARGET,
                        "--key", _KEY, "--namespace", _NS_A,
                        "--content", "stranded pointer body", "--json")
        self.assertEqual(cap.returncode, 0, cap.stdout + cap.stderr)
        receipt = json.loads(cap.stdout)
        self.assertTrue(receipt.get("ok"), receipt)
        self.assertEqual(receipt["pointer"]["status"], "created")

        local_store = self.tmp / "store.sqlite"
        conn = _open(local_store)
        self.addCleanup(conn.close)
        before_rows = _pointer_rows(conn)
        self.assertEqual(len(before_rows), 1)
        before = before_rows[0]
        self.assertEqual(before["namespace"], _NS_A)
        self.assertIsNone(before["superseded_at"])

        # The REMOTE-side view: a second scratch store holding a verbatim
        # copy of the pointer row (the hub / remote-client state).
        remote_store = self._init_second_store("remote-view")
        rconn = _open(remote_store)
        self.addCleanup(rconn.close)
        full = conn.execute(
            "SELECT * FROM memory WHERE id=?", (before["id"],)).fetchone()
        columns = full.keys()
        rconn.execute(
            "INSERT INTO memory (%s) VALUES (%s)"
            % (",".join(columns), ",".join("?" * len(columns))),
            tuple(full[c] for c in columns))
        rconn.commit()
        remote_before_rows = _all_rows(rconn)
        remote_dir_before = sorted(
            str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*")
            if p.is_file() and "remote-view" in str(p.relative_to(self.tmp)))

        # Local recovery state snapshot (sidecars stay byte-identical).
        journal_path = self.tmp / "knowledge" / "journal.json"
        delivery_path = self.tmp / "knowledge" / "delivery.json"
        journal_before = journal_path.read_bytes()
        delivery_before = delivery_path.read_bytes()
        files_before = sorted(str(p.relative_to(self.tmp))
                              for p in self.tmp.rglob("*")
                              if p.is_file() and "remote-view"
                              not in str(p.relative_to(self.tmp)))

        # THE MOVE, through the REAL CLI.
        r = self._cli("rekey-namespace", "--from", _NS_A,
                      "--to", _NS_B, "--confirm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("rekeyed", r.stdout + r.stderr)

        # Local pointer row MOVED with the namespace — same row, same
        # bytes, still live (no tombstone, no rewrite, no new row).
        after_rows = _pointer_rows(conn)
        self.assertEqual(len(after_rows), 1,
                         "no duplicate pointer rows may appear: %r"
                         % (after_rows,))
        after = after_rows[0]
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["namespace"], _NS_B,
                         "the LOCAL pointer row moves with the namespace")
        self.assertEqual(after["content"], before["content"],
                         "the pointer row content stays byte-identical")
        self.assertEqual(after["type"], before["type"])
        self.assertEqual(after["tags"], before["tags"])
        self.assertEqual(after["source_hash"], before["source_hash"])
        self.assertIsNone(after["superseded_at"],
                          "the move must never tombstone the pointer")
        self.assertFalse(bool(after.get("supersede_reason")),
                          "no tombstone reason may be recorded")

        # The pointer still resolves through the public surface under the
        # NEW namespace with the same digest/version.
        status = self._cli("knowledge", "status", "--target", _TARGET,
                           "--key", _KEY, "--namespace", _NS_B, "--json")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        pointers = [p.get("pointer") for p in
                    json.loads(status.stdout).get("pointers") or []
                    if p.get("target") == _TARGET and p.get("key") == _KEY]
        self.assertEqual(len(pointers), 1)
        self.assertEqual(pointers[0]["row_id"], before["id"])
        self.assertEqual(pointers[0]["version"], receipt["version"])
        self.assertEqual(pointers[0]["full_sha256"], receipt["digest"])

        # The REMOTE-side view is UNTOUCHED byte-for-byte: no auto-remap,
        # no tombstone marker, no supersede, no new rows anywhere there.
        self.assertEqual(_all_rows(rconn), remote_before_rows,
                         "the remote copy must stay byte-identical (no "
                         "auto-remap, no tombstone, no supersede)")
        remote_dir_after = sorted(
            str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*")
            if p.is_file() and "remote-view" in str(p.relative_to(self.tmp)))
        self.assertEqual(remote_dir_after, remote_dir_before,
                         "no remediation artifacts may appear in the "
                         "remote view")

        # No local remediation artifacts beyond what existed: the sidecars
        # stay byte-identical (terminal history passes the guard without
        # writes); the only new files are the sanctioned fence lock files.
        self.assertEqual(journal_path.read_bytes(), journal_before)
        self.assertEqual(delivery_path.read_bytes(), delivery_before)
        files_after = sorted(str(p.relative_to(self.tmp))
                             for p in self.tmp.rglob("*")
                             if p.is_file() and "remote-view"
                             not in str(p.relative_to(self.tmp)))
        added = set(files_after) - set(files_before)
        self.assertTrue(
            all(name.endswith(".lock") for name in added),
            "only the coordination lock files may appear; got %r"
            % (sorted(added),))


if __name__ == "__main__":
    unittest.main()
