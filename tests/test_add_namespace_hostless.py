"""Hostless project namespace warning at write time — acceptance spec.

Defect under test: ``store.py add`` accepts a HOSTLESS project namespace
(``project:zaxbysauce/zmem`` — org/repo with no git host) SILENTLY,
stranding rows outside the canonical ``project:github.com/<org>/<repo>``
form that the automatic hooks resolve. The near-miss splits one project's
memory across two unreachable namespaces.

CONTRACT (amended — WARN, do not refuse): REJECTION would break legitimate
hierarchical project namespaces that the repo's own frozen fixtures use
(``project:t03/harvest`` in tests/test_t03_harvest_trust.py is exactly
this shape), so a hostless ``org/repo`` project namespace is not a
validation error. The post-fix contract pinned here (public
``store.py add`` subprocess):

- ``add --namespace "project:zaxbysauce/zmem"`` SUCCEEDS (rc 0, row
  stored) and reports the near miss STRUCTURALLY: the ``--json``
  envelope's ``warnings`` list carries an object with
  ``"type": "namespace_project_near_miss"`` whose ``"canonical"`` value
  contains ``project:github.com/zaxbysauce/zmem``; stderr names the
  canonical form.
- ``add --namespace "project:github.com/zaxbysauce/zmem"`` (canonical)
  still stores with NO ``namespace_project_near_miss`` warning.

All stores are throwaway temp stores; ambient zmem env is stripped from
every child process (storelib freezes the store path at first import — an
unpinned child could auto-migrate the shared operator store). Runs
standalone: python -X utf8 tests/test_add_namespace_hostless.py
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

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"

HOSTLESS_NS = "project:zaxbysauce/zmem"
CANONICAL_NS = "project:github.com/zaxbysauce/zmem"
CONTENT = "hostless near-miss probe row"

# Mirror the _STRIP_ENV pattern of tests/test_subagent_tasktext.py:45-67.
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_INJECT_FLOOR_LEX", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
    "ZMEM_SESSION", "ZMEM_REMOTE_URL", "ZMEM_AUTO_REKEY",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
)


def _clean_env(tmp: str, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_SESSION": "hostless-ns-spec-session",
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _add(env: dict, namespace: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE_PY), "add",
         "--namespace", namespace, "--type", "fact",
         "--content", CONTENT, "--signal", "test",
         "--confidence", "0.9", "--json"],
        capture_output=True, text=True, env=env, timeout=180,
    )


def _rows_under(env: dict, namespace: str) -> list:
    """All live rows the store reports under ``namespace``."""
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE_PY), "search",
         "--text", "hostless", "--namespace", namespace,
         "--limit", "20", "--json"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    if r.returncode != 0:
        return []  # an unusable/absent namespace reports no rows
    parsed = None
    try:
        # search --json emits a pretty-printed multi-line object.
        parsed = json.loads((r.stdout or "").strip())
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
        return parsed["results"]
    return []


def _json_payload(proc: subprocess.CompletedProcess) -> dict:
    """The ``--json`` write envelope (``{"ok", "id", "result",
    "warnings"}``); {} when stdout carries no parseable object."""
    try:
        parsed = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _near_miss_warnings(proc: subprocess.CompletedProcess) -> list:
    warnings = _json_payload(proc).get("warnings")
    if not isinstance(warnings, list):
        return []
    return [w for w in warnings
            if isinstance(w, dict)
            and w.get("type") == "namespace_project_near_miss"]


class AddNamespaceHostlessTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-hostless-ns-")
        self._env = _clean_env(self._tmp)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_hostless_warns_canonical_hint_and_stores(self):
        r = _add(self._env, HOSTLESS_NS)
        self.assertEqual(
            r.returncode, 0,
            "the hostless near-miss namespace must still STORE (a refusal "
            f"would break hierarchical project namespaces like project:t03/harvest); "
            f"stderr={r.stderr!r}")
        rows = _rows_under(self._env, HOSTLESS_NS)
        self.assertTrue(rows, "the hostless write must land a live row")
        near_miss = _near_miss_warnings(r)
        self.assertTrue(
            near_miss,
            "the --json envelope must carry a warning object with "
            f"type=namespace_project_near_miss; got {r.stdout!r}")
        canonical = near_miss[0].get("canonical") or ""
        self.assertIn(
            CANONICAL_NS, str(canonical),
            f"the near-miss warning's canonical value must name "
            f"{CANONICAL_NS}; got {canonical!r}")
        self.assertIn(
            "project:github.com/", r.stderr,
            "stderr must name the canonical form project:github.com/ "
            f"alongside the stored row; stderr={r.stderr!r}")

    def test_canonical_name_accepted_near_miss_sibling(self):
        r = _add(self._env, CANONICAL_NS)
        self.assertEqual(
            r.returncode, 0,
            f"the canonical namespace must be accepted; stderr={r.stderr!r}")
        rows = _rows_under(self._env, CANONICAL_NS)
        self.assertTrue(
            rows, "the canonical write must land a live row")
        self.assertEqual(
            _near_miss_warnings(r), [],
            "the canonical key is not a near miss — no "
            "namespace_project_near_miss warning may attach to it "
            f"(warnings={_json_payload(r).get('warnings')!r})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
