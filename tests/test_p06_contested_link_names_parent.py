"""Acceptance checks for issue #254 (Workstream P PR 6 of 6): the
[CONTESTED LINK] marker names the opposing row.

Every check in this module is RED at base and GREEN after the fix: at base
the fence renderer appends the bare ``[CONTESTED LINK]`` marker and never
reads the row's ``link_of``, so the reader cannot tell WHICH row refutes the
expanded neighbour; after the fix the marker carries the opposing row's id
and the admission/delivery mirrors charge and recognize the longer marker.

Run single methods exactly as the frozen acceptance table records them:

    python tests/test_p06_contested_link_names_parent.py ContestedMarkerCliTest.test_contested_marker_names_parent_id
    python tests/test_p06_contested_link_names_parent.py ContestedMarkerAccountingTest.test_named_marker_keeps_delivery_detection_exact
    python tests/test_p06_contested_link_names_parent.py ContestedMarkerAccountingTest.test_named_marker_is_covered_by_fence_row_cost

House pattern (test_memory_links.py): CLI legs drive the real store.py
subprocess against a throwaway ZMEM_STORE; accounting legs are in-process
against pure renderer/ledger functions. The store env is pinned BEFORE the
first storelib import so a module-level import can never freeze the ambient
operator store.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"

# Pin the store env before the first storelib import (storelib freezes
# STORE_PATH at first import from ambient env — an unpinned import would
# point at the operator's real ~/.zmem store).
_TMP = tempfile.mkdtemp(prefix="zmem-p06-pin-")
os.environ["ZMEM_STORE"] = str(Path(_TMP) / "store.sqlite")
os.environ["ZMEM_DATA"] = _TMP
os.environ["ZMEM_MODELS_DIR"] = str(Path(_TMP) / "no-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
for _k in ("ZMEM_BACKUP_DIR", "ZMEM_MMR_LAMBDA", "ZMEM_LINK_THRESHOLD"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(SCRIPTS_DIR))

# In-process rows for the accounting checks, in the exact shape the issue
# specifies: the contested child carries link_of = the parent's id.
PARENT = "11111111-aaaa-4bbb-8ccc-000000000001"
CHILD = "22222222-aaaa-4bbb-8ccc-000000000002"
_BASE_FIELDS = {
    "type": "fact",
    "signal": "test",
    "confidence": 0.9,
    "_score": 0.9,
    "namespace": "project:links",
}
PARENT_ROW = dict(
    _BASE_FIELDS,
    id=PARENT,
    content="the cache ttl setting should always be ten minutes "
    "for the rate limiter service",
)
CHILD_ROW = dict(
    _BASE_FIELDS,
    id=CHILD,
    content="the cache ttl setting should never be ten minutes "
    "for the rate limiter service",
    link_relation="contradicts",
    link_of=PARENT,
    link_score=0.9,
    contested_link=True,
)


def _clean_env(store_path: str) -> dict:
    env = dict(os.environ)
    env["ZMEM_STORE"] = store_path
    env["ZMEM_MODELS_DIR"] = str(Path(store_path).parent / "no-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env.pop("ZMEM_DATA", None)
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_MMR_LAMBDA", None)
    env.pop("ZMEM_LINK_THRESHOLD", None)
    return env


class ContestedMarkerCliTest(unittest.TestCase):
    """AC1: the CLI fenced recall names the opposing row's id."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-p06-cli-")
        self.store = str(Path(self.tmp) / "store.sqlite")
        self.env = _clean_env(self.store)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_store(self, *args):
        return subprocess.run(
            [sys.executable, str(STORE_PY), *args],
            capture_output=True, text=True, env=self.env, timeout=120)

    def add(self, content):
        r = self.run_store("add", "--namespace", "project:links", "--type",
                           "fact", "--content", content, "--signal", "test",
                           "--tags", "cache")
        self.assertEqual(r.returncode, 0, f"add failed:\n{r.stdout}\n{r.stderr}")
        for line in r.stdout.splitlines():
            if line.startswith("[zmem] added memory "):
                return line.split()[3]
        self.fail(f"no added-id line in stdout:\n{r.stdout}")

    def test_contested_marker_names_parent_id(self):
        """RED at base (bare marker, parent id absent from the bullet);
        GREEN after the fix (marker names the child's link_of)."""
        self.add("the cache ttl setting should always be ten minutes "
                 "for the rate limiter service")
        self.add("the cache ttl setting should never be ten minutes "
                 "for the rate limiter service")
        # Resolve the contested pair from the JSON envelope: exactly one
        # main row and its contradicts expansion, whichever way ranking
        # breaks (rank-agnostic per the #61 test's PRR-002 lesson).
        rj = self.run_store("recall", "--query", "cache ttl rate limiter",
                            "--namespace", "project:links", "--limit", "1",
                            "--json")
        self.assertEqual(rj.returncode, 0, rj.stderr)
        rows = json.loads(rj.stdout)["results"]
        mains = [r for r in rows if "link_relation" not in r]
        extras = [r for r in rows if r.get("link_relation") == "contradicts"]
        self.assertEqual(len(mains), 1, rows)
        self.assertEqual(len(extras), 1, rows)
        parent_id = extras[0]["link_of"]
        self.assertEqual(parent_id, mains[0]["id"],
                         "the expansion child must name the main row")
        # The fenced render: the single contested bullet line must carry the
        # opposing row's id.
        rf = self.run_store("recall", "--query", "cache ttl rate limiter",
                            "--namespace", "project:links", "--limit", "1")
        self.assertEqual(rf.returncode, 0, rf.stderr)
        lines = [ln for ln in rf.stdout.splitlines() if "CONTESTED LINK" in ln]
        self.assertEqual(len(lines), 1, rf.stdout)
        self.assertEqual(parent_id in lines[0], True)


class ContestedMarkerAccountingTest(unittest.TestCase):
    """AC2/AC3: the named marker keeps delivery detection exact and stays
    covered by fence_row_cost on both wires."""

    def test_named_marker_keeps_delivery_detection_exact(self):
        """RED at base (parent id absent from the child's bullet); GREEN
        after the fix, with rows_present_in still detecting the rendered
        child (and only the rendered child) as delivered."""
        from storelib import delivery_ledger as dl
        from storelib.recall import _format_fenced_recall
        text = _format_fenced_recall([CHILD_ROW], "header",
                                     legacy_injection_wire=True)
        line = [ln for ln in text.splitlines() if CHILD in ln][0]
        self.assertEqual(
            (PARENT in line,
             [r["id"] for r in dl.rows_present_in([CHILD_ROW], text)]
             == [CHILD],
             [r["id"] for r in dl.rows_present_in([PARENT_ROW], text)]
             == []),
            (True, True, True))

    def test_named_marker_is_covered_by_fence_row_cost(self):
        """RED at base (marker carries no id to cover); GREEN after the fix
        (admission charges the named marker on both wires)."""
        from storelib import inject as inject_mod
        from storelib.recall import _format_fenced_recall
        observed = []
        for wire in (False, True):
            shell = inject_mod.estimate_tokens(_format_fenced_recall(
                [], "header", legacy_injection_wire=wire))
            rendered = _format_fenced_recall([CHILD_ROW], "header",
                                             legacy_injection_wire=wire)
            contribution = inject_mod.estimate_tokens(rendered) - shell
            cost = inject_mod.fence_row_cost(CHILD_ROW,
                                             legacy_injection_wire=wire)
            observed.append((PARENT in rendered, cost >= contribution))
        self.assertEqual(observed, [(True, True), (True, True)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
