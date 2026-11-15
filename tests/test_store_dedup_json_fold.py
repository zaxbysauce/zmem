"""PR #470 review (F-004/OFB-03): pin the `add --json` fold lane.

Obligation under test (issue #469 context / PR #470 O-005): when a
near-duplicate add FOLDS under `--json`, stdout stays strictly
machine-readable (exactly one JSON object, result "deduped") and the
fold disclosure + refresh notices land on stderr only. Before this
test no suite paired --json with a folding add — the property rested
solely on the structural stdout swap in storelib/cli.py.

Discriminating signature: RED on any change that lets fold-branch
output reach stdout under --json (e.g. a print moved outside the swap
window); GREEN at the PR #470 feedback head.

Runs under the repo's unittest-only convention:
``python -X utf8 tests/test_store_dedup_json_fold.py``
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable

BOILER = (
    "zmem ship record for workstream followup pr review session with plan critic rounds "
    "implementation reviewer and final critic gates all approve at head tree identity "
    "release version bump with eight manifests changelog newest first section dated "
    "canary report regenerated support report stamped after canary five lanes fixture "
    "regenerated release gate emit manifest after git add verification replayed frozen "
    "checks red to green mutation probes killed scan deferred clean full test loop green "
    "anchor receipt published on the target issue with semantics digest stable"
)
CONTENT_A = BOILER + " pr 467"
CONTENT_B = BOILER + " pr 468 zetafold"
NS = "project:dedupjson469"


def _env(tmp: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env.pop("ZMEM_DATA", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["ZMEM_EMBED_PROFILE"] = "fake"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("ZMEM_DEDUP_THRESHOLD", None)
    env.pop("ZMEM_REMOTE_URL", None)
    return env


class DedupJsonFoldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-dedupjson469-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = _env(self.tmp)

    def _run(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=180,
        )

    def test_fold_under_json_keeps_stdout_machine_readable(self):
        """A near-duplicate fold under --json keeps stdout exactly one
        JSON object (result "deduped") with the human notices on stderr.
        RED if fold-branch output ever reaches stdout under --json."""
        rinit = self._run("init")
        self.assertEqual(rinit.returncode, 0, rinit.stderr)
        ra = self._run("add", "--json", "--namespace", NS, "--type", "fact",
                       "--content", CONTENT_A, "--signal", "test")
        self.assertEqual(ra.returncode, 0, ra.stderr)
        payload_a = json.loads(ra.stdout)  # strict: exactly one JSON value
        self.assertEqual(payload_a.get("result"), "stored", ra.stdout)

        rb = self._run("add", "--json", "--namespace", NS, "--type", "fact",
                       "--content", CONTENT_B, "--signal", "test")
        self.assertEqual(rb.returncode, 0, rb.stderr)
        payload_b = json.loads(rb.stdout)  # strict: exactly one JSON value
        self.assertEqual(
            payload_b.get("result"), "deduped",
            f"the near-duplicate add must fold under --json; stdout:\n{rb.stdout}")
        self.assertIn(
            "[zmem] dedup:", rb.stderr,
            f"the fold disclosure must stay on stderr under --json; stderr:\n{rb.stderr}")
        self.assertNotIn(
            "[zmem]", rb.stdout,
            f"no human [zmem] line may leak into --json stdout; stdout:\n{rb.stdout}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
