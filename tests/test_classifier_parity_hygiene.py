"""Leak-proof gate for tests/test_classifier_parity.py's temp-database hygiene.

The parity suite can stay green while leaking temp files: its assertions
never inspect post-teardown temp state. This driver closes that hole (PR
#441 closeout item NEW-3a): it runs the parity file in a SUBPROCESS with an
isolated TMP directory, then asserts the directory holds no sqlite artifacts
after the child exits — the main db and the sqlite3 ``-wal``/``-shm``
siblings included. A second, in-process leg pins the registered cleanup
itself: after ``doCleanups`` the main db and both siblings are gone.

Run: python tests/test_classifier_parity_hygiene.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PARITY = REPO_ROOT / "tests" / "test_classifier_parity.py"


class ParityTempHygieneTest(unittest.TestCase):
    def test_parity_suite_leaves_no_temp_artifacts(self):
        tmp_root = tempfile.mkdtemp(prefix="zmem-parity-hygiene-")
        self.addCleanup(shutil.rmtree, tmp_root, ignore_errors=True)
        data_dir = os.path.join(tmp_root, "data")
        os.makedirs(data_dir, exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("ZMEM_", "CLAUDE_", "ZCODE_"))}
        env.update({
            "TMP": tmp_root, "TEMP": tmp_root, "TMPDIR": tmp_root,
            "ZMEM_DATA": data_dir,
            "ZMEM_STORE": os.path.join(data_dir, "store.sqlite"),
            "PYTHONUTF8": "1",
        })
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(PARITY)],
            capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(
            r.returncode, 0,
            "parity child failed before the leak check: %s"
            % ((r.stderr or "")[-400:]))
        leftovers = sorted(
            p.name for p in Path(tmp_root).iterdir() if p.name != "data")
        self.assertEqual(
            leftovers, [],
            "the parity suite leaked temp artifacts into its TMP dir")

    def test_cleanup_removes_wal_siblings(self):
        spec = importlib.util.spec_from_file_location("parity_mod", PARITY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        case = mod.DbWiringIntegrationTest(
            "test_db_details_recover_inputs_and_verdicts")
        case.setUp()
        try:
            path = case._make_db(with_part_table=True)
            self.assertTrue(os.path.exists(path))
            # Plant the sqlite3 -wal/-shm sibling names directly: a clean
            # connection close checkpoints and deletes real siblings, so
            # planting pins the helper's sibling-removal branch itself.
            for s in ("-wal", "-shm"):
                open(path + s, "wb").close()
                self.assertTrue(os.path.exists(path + s))
            case.doCleanups()  # fires _make_db's registered cleanup
            for s in ("", "-wal", "-shm"):
                self.assertFalse(
                    os.path.exists(path + s),
                    "cleanup left %s behind" % (path + s))
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
