"""Query hygiene — namespace text must not make rows candidates (issue #112, C2).

The FTS5 table indexes the `namespace` column (storelib/schema.py memory_fts)
and the single MATCH site is unqualified, so the namespace string
"project:github.com/zaxbysauce/zmem" tokenizes to
project|github|com|zaxbysauce|zmem and participates in matching. A query
token that happens to equal any of those ("zmem", "github") therefore
returns EVERY row in the namespace — even rows whose content never mentions
them. A user prompt mentioning "zmem" turns the whole project namespace
into a candidate pool (the mechanism behind the #111 false injections).

This check seeds a row in namespace project:github.com/zaxbysauce/zmem
whose content never mentions zmem/github/project tokens, plus a contrast
row in a different namespace, then demands "zmem" and "github" scoped to
that namespace return ZERO rows, while a real content token still retrieves
the row (contrast guard — the row is findable, just not via namespace text).

Lane isolation: --no-hybrid forces the lexical FTS lane, the exact surface
issue #112 fixes (the fix column-filters the MATCH to {content tags}).

Stores are throwaway temp stores driven through the real CLI subprocesses;
ambient ZMEM_* env is stripped from every child. No storelib import happens
in-process, so the per-subprocess env pin is the whole isolation contract.
Runs standalone: py -3 tests/test_query_hygiene_namespace.py
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

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"

# The namespace string tokenizes to project|github|com|zaxbysauce|zmem —
# exactly the flood surface of the issue.
NS_TARGET = "project:github.com/zaxbysauce/zmem"
NS_OTHER = "user:global"
# Content with NO zmem/github/project/com/zaxbysauce token anywhere: any hit
# for "zmem"/"github" can only come from the namespace column.
ROW_TARGET = "border collies need daily exercise every single day"
# Contrast row in a different namespace (distinct legit content).
ROW_OTHER = "quokka habitats span rottnest island scrubland"

# Ambient zmem env that must never reach a child process (mirrors
# tests/test_ops_tokens._STRIP_ENV).
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_CONVENTION_INTERVAL",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW", "ZMEM_AUTO_REKEY",
)


def _clean_env(tmp: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_EMBED_PROFILE": "fake",
        "ZMEM_MODELS_DIR": "/nonexistent-zmem-models-dir",
        "PYTHONUTF8": "1",
    })
    return env


class NamespaceColumnTest(unittest.TestCase):
    """Namespace-only token matches must yield an empty pool (issue #112 AC3)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="zmem-hygiene-ns-")
        cls._env = _clean_env(cls._tmp)
        cls._run_cli("init")
        cls._seed(NS_TARGET, ROW_TARGET)
        cls._seed(NS_OTHER, ROW_OTHER)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    @classmethod
    def _run_cli(cls, *args: str) -> subprocess.CompletedProcess:
        r = subprocess.run(
            [sys.executable, str(STORE_PY), *args],
            capture_output=True, text=True, env=cls._env, timeout=120,
            cwd=REPO_ROOT)
        assert r.returncode == 0, f"CLI {args[0]} failed: {r.stderr[-800:]}"
        return r

    @classmethod
    def _seed(cls, ns: str, content: str) -> None:
        cls._run_cli("add", "--namespace", ns, "--type", "fact",
                     "--content", content, "--signal", "test",
                     "--confidence", "0.9")

    def _recall_count(self, query: str) -> tuple[int, list[dict]]:
        r = self._run_cli("recall", "--query", query,
                          "--namespace", NS_TARGET,
                          "--json", "--no-hybrid")
        doc = json.loads(r.stdout)
        return doc["count"], doc["results"]

    def test_query_zmem_scoped_returns_zero_results(self):
        # Today "zmem" matches the row purely via the namespace string
        # tokenizing to project|github|com|zaxbysauce|zmem.
        count, results = self._recall_count("zmem")
        self.assertEqual(
            count, 0,
            "'zmem' must not match rows whose content never mentions it "
            "(namespace column must be out of the MATCH scope); matched: "
            + "; ".join(x["content"][:50] for x in results))

    def test_query_github_scoped_returns_zero_results(self):
        # Same mechanism via the "github" namespace token.
        count, results = self._recall_count("github")
        self.assertEqual(
            count, 0,
            "'github' must not match rows whose content never mentions it "
            "(namespace column must be out of the MATCH scope); matched: "
            + "; ".join(x["content"][:50] for x in results))

    def test_real_content_token_still_returns_row(self):
        # Contrast guard: the target row IS returned for a real content
        # token — so the zero-result assertions above can never pass
        # because of a dead store or a dead lane.
        count, results = self._recall_count("collies")
        self.assertGreaterEqual(
            count, 1, "a real content token must still hit its row")
        self.assertIn("collies", results[0]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
