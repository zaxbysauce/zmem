"""Frozen acceptance check C4 (issue #356): the entity-kind CHECK constraint
is DERIVED from schema_meta.ENTITY_KINDS, never hand-copied.

Two ratchets in SchemaDdlSingleSourceTest:

1. test_entity_kind_check_literals_are_derived — source ratchet plus the
   derivation helper. At base (a988469) schema.py hand-copies the twelve-kind
   list into FOUR independent SQL literals (fresh init_db DDL, the
   _ENTITY_V16_DDL_RECREATED module constant, _migrate_v16's staging
   entity_v16 CREATE, and the v10/v9 legacy migration block) and no
   derivation helper exists, so the check fails with:
       AssertionError: entity kind CHECK literals are not derived from
       schema_meta.ENTITY_KINDS at base
   After the fix the planned helper schema._entity_ddl_sql() (referenced
   INSIDE this method only, so the base leg fails with the intended
   AssertionError rather than an import error) must render the kind list
   from ENTITY_KINDS, and NO source window may still inline the entity
   vocabulary inside a CHECK (kind IN ...) literal. The ratchet is scoped
   to the ENTITY-kind vocabulary on purpose: the `evidence` table's own
   `CHECK (kind IN ('turn','tool_call',...))` (schema.py ~line 1352) is a
   DIFFERENT vocabulary and must not be flagged.
2. test_entity_kind_vocabulary_has_twelve_kinds — pins the count of
   ENTITY_KINDS at 12 so a silent vocabulary append trips this file.

Hermetic discipline: the store env is pinned in setUpClass BEFORE the
first storelib import anywhere in the process (storelib freezes
STORE_PATH at import) and restored in tearDownClass. schema_meta itself is
stdlib-only, but storelib.schema (imported inside method 1 for the helper)
is not.

Run: python tests/test_schema_ddl_single_source.py
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import _store_guard  # noqa: F401  (issue #244 isolation guard)
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
SCHEMA_PY = SCRIPTS_DIR / "storelib" / "schema.py"

_HERMETIC_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                  "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_NEWER_SCHEMA")

# A CHECK literal's kind list spans at most a few source lines (the widest
# hand copy, _ENTITY_V16_DDL_RECREATED, wraps the 12 kinds over 3 lines).
_WINDOW_LINES = 5
# Two-or-more distinct quoted ENTITY_KINDS inside one CHECK window is a
# hand-copied vocabulary list. A single quoted kind can appear in comments
# or the evidence vocabulary without being a copy of the entity list.
_HAND_COPY_THRESHOLD = 2

_FROZEN_MESSAGE = ("entity kind CHECK literals are not derived from "
                   "schema_meta.ENTITY_KINDS at base")


class SchemaDdlSingleSourceTest(unittest.TestCase):
    _tmpdir: str | None = None
    _saved_env: dict | None = None

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="zmem-c4-ddl-")
        cls._saved_env = {k: os.environ.get(k) for k in _HERMETIC_KEYS}
        os.environ["ZMEM_STORE"] = str(Path(cls._tmpdir) / "store.sqlite")
        os.environ["ZMEM_DATA"] = str(Path(cls._tmpdir) / "data")
        os.environ["ZMEM_MODELS_DIR"] = str(Path(cls._tmpdir) / "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))

    @classmethod
    def tearDownClass(cls):
        if cls._saved_env is not None:
            for k, v in cls._saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            cls._saved_env = None
        if cls._tmpdir is not None:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)
            cls._tmpdir = None

    def test_entity_kind_check_literals_are_derived(self):
        from schema_meta import ENTITY_KINDS  # stdlib-only, base symbol

        source = SCHEMA_PY.read_text(encoding="utf-8")
        lines = source.splitlines()

        # (a) Source ratchet: every `CHECK (kind IN` window that inlines the
        # ENTITY-kind vocabulary (>= 2 distinct quoted kinds) is a hand copy.
        # A derivation helper renders the kinds from ENTITY_KINDS, so its own
        # source window carries ZERO quoted kind names — the evidence
        # table's CHECK carries a disjoint vocabulary and is never flagged.
        hand_copied = []
        for i, line in enumerate(lines):
            if "CHECK (kind IN" not in line:
                continue
            window = "\n".join(lines[i:i + _WINDOW_LINES])
            quoted = set(re.findall(r"['\"]([A-Za-z_]+)['\"]", window))
            overlap = quoted & set(ENTITY_KINDS)
            if len(overlap) >= _HAND_COPY_THRESHOLD:
                hand_copied.append(f"schema.py:{i + 1} ({len(overlap)} "
                                   f"entity kinds inlined)")

        # (b) The derivation helper must exist and render EXACTLY the
        # ENTITY_KINDS vocabulary, quoted. Referenced inside this method so
        # the base leg (helper absent) fails with the frozen AssertionError
        # instead of an ImportError/AttributeError escaping the check.
        helper = None
        try:
            from storelib import schema as schema_mod
            helper = getattr(schema_mod, "_entity_ddl_sql", None)
        except AttributeError:
            helper = None
        rendered_ok = False
        if callable(helper):
            rendered = helper()
            if isinstance(rendered, str):
                tokens = [t for t
                          in re.findall(r"['\"]([A-Za-z_]+)['\"]", rendered)
                          if t in set(ENTITY_KINDS)]
                rendered_ok = (
                    set(tokens) == set(ENTITY_KINDS)
                    and len(tokens) == len(set(ENTITY_KINDS)))

        print(f"[C4] hand_copied={hand_copied} "
              f"helper={'present' if callable(helper) else 'ABSENT'} "
              f"rendered_ok={rendered_ok}")
        if hand_copied or not rendered_ok:
            raise AssertionError(_FROZEN_MESSAGE)

    def test_entity_kind_vocabulary_has_twelve_kinds(self):
        from schema_meta import ENTITY_KINDS

        self.assertEqual(
            len(ENTITY_KINDS), 12,
            f"schema_meta.ENTITY_KINDS must carry exactly 12 kinds "
            f"(got {len(ENTITY_KINDS)}: {list(ENTITY_KINDS)}); a vocabulary "
            "change must update the derived DDL deliberately, not silently")


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print("CHECK-PASS" if _result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if _result.wasSuccessful() else 1)
