"""Supplementary DDL derivation test (issue #356, plan-critic round 1 R1).

The frozen C4 ratchet proves no hand-written entity-kind literal remains and
that _entity_ddl_sql renders ENTITY_KINDS — but a helper returning a
hard-coded twelve-kind string would pass both while still drifting on the
thirteenth kind. This test proves the derivation BEHAVIORALLY: in a
subprocess, patch schema_meta.ENTITY_KINDS to carry a thirteenth kind
BEFORE importing storelib.schema, then (a) init_db a fresh store and (b)
run a v15 -> v16 migration; both must accept the thirteenth kind with zero
SQL edits anywhere.

Run: python tests/test_schema_ddl_supp.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _store_guard  # noqa: F401  (issue #244 isolation guard)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"

_CHILD = r"""
import json, os, sqlite3, sys
sys.path.insert(0, os.environ["ZMEM_SCRIPTS"])
import schema_meta
# Patch the single authority BEFORE storelib.schema imports it.
schema_meta.ENTITY_KINDS = tuple(schema_meta.ENTITY_KINDS) + ("thirteenth",)
import storelib.schema as schema

kind_sql = schema._entity_ddl_sql()
assert "'thirteenth'" in kind_sql, "helper did not render the patched kind"

# (a) fresh store accepts the thirteenth kind.
fresh = sqlite3.connect(os.environ["ZMEM_FRESH"])
fresh.row_factory = sqlite3.Row
schema.init_db(fresh)
fresh.execute(
    "INSERT INTO entity (id, kind, canonical_name, created_at, updated_at) "
    "VALUES ('e1', 'thirteenth', 'Thirteenth Kind', '2026-11-01T00:00:00Z',"
    " '2026-11-01T00:00:00Z')")
fresh.commit()

# (b) the migration chain accepts it: downgrade to a v9-shaped store (the
# canonical MigrationTest pattern — drop the entity tables, stamp v9) and
# let migrate() rebuild them. The v10 block creates `entity` (one derived
# DDL site) and the v16 rebuild recreates it (two more derived sites);
# the thirteenth kind must be accepted at the END of the chain with zero
# SQL edits.
mig = sqlite3.connect(os.environ["ZMEM_MIGRATE"])
mig.row_factory = sqlite3.Row
schema.init_db(mig)
mig.execute("DROP TABLE memory_entity")
mig.execute("DROP TABLE entity_alias")
mig.execute("DROP TABLE entity")
mig.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
mig.commit()
schema.migrate(mig)
mig.execute(
    "INSERT INTO entity (id, kind, canonical_name, created_at, updated_at) "
    "VALUES ('e2', 'thirteenth', 'Thirteenth Kind', '2026-11-01T00:00:00Z',"
    " '2026-11-01T00:00:00Z')")
mig.commit()
print("SUPP_OK " + json.dumps(True))
"""


class ThirteenthKindTest(unittest.TestCase):
    def test_patched_kind_accepted_fresh_and_migrated(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-ddl-supp-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ)
        env["ZMEM_STORE"] = str(tmp / "store.sqlite")
        env["ZMEM_DATA"] = str(tmp)
        env["ZMEM_MODELS_DIR"] = str(tmp / "no-models")
        env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        env.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)
        env["ZMEM_SCRIPTS"] = str(SCRIPTS)
        env["ZMEM_FRESH"] = str(tmp / "fresh.sqlite")
        env["ZMEM_MIGRATE"] = str(tmp / "migrate.sqlite")
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD], cwd=str(REPO_ROOT), env=env,
            capture_output=True, text=True, timeout=120)
        self.assertEqual(
            proc.returncode, 0,
            f"child failed\nstdout: {proc.stdout[-1500:]!r}\n"
            f"stderr: {proc.stderr[-1500:]!r}")
        self.assertIn("SUPP_OK", proc.stdout)


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print("CHECK-PASS" if _result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if _result.wasSuccessful() else 1)
