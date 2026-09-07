"""Query hygiene — stop words must not flood the lexical lane (issue #112, C1).

The FTS query builder (storelib/recall.py _recall_one_tier) turns EVERY
whitespace token into a `"<token>"*` prefix-wildcard OR term with no stoplist.
A query of just "the" or "and" therefore prefix-matches huge row populations:
any row carrying a token that STARTS with the stop word ("theory" for "the")
or the stop word itself ("and") is returned as a confident hit.

This check seeds exactly that trap row (standalone "and" + the word
"theory") plus one distinct legit row, then demands the closed-class English
stop words "the" and "and" match NOTHING, while a meaningful content token
still retrieves its row (contrast guard — the store is live and the lane
works, so the zero-result assertions can never pass trivially).

Lane isolation: --no-hybrid forces the lexical FTS lane, the exact surface
issue #112 fixes (root cause rules the vec/entity lanes out of scope).

Stores are throwaway temp stores driven through the real CLI subprocesses;
ambient ZMEM_* env is stripped from every child. No storelib import happens
in-process, so the per-subprocess env pin is the whole isolation contract.
Runs standalone: py -3 tests/test_query_hygiene_stopwords.py
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

NS = "project:hygiene-stopwords"
# The trap row: standalone "and", the word "theory" (the-prefix), and a
# standalone "the" — every stop-word shape the current builder matches on.
ROW_STOP = ("music theory says chords resolve to the tonic and cadences "
            "close phrases")
# Distinct legit row for the contrast guard (no the*/and* prefix tokens).
ROW_LEGIT = "quokka habitats span rottnest island scrubland"

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


class StopwordQueryTest(unittest.TestCase):
    """`recall --query "the"` / "and" must return zero rows (issue #112 AC3)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="zmem-hygiene-stop-")
        cls._env = _clean_env(cls._tmp)
        cls._run_cli("init")
        cls._seed(NS, ROW_STOP)
        cls._seed(NS, ROW_LEGIT)

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
        cls._run_cli("add", "--namespace", ns, "--type", "lesson",
                     "--content", content, "--signal", "test",
                     "--confidence", "0.9")

    def _recall_count(self, query: str) -> tuple[int, list[dict]]:
        r = self._run_cli("recall", "--query", query, "--namespace", NS,
                          "--json", "--no-hybrid")
        doc = json.loads(r.stdout)
        return doc["count"], doc["results"]

    def test_query_the_returns_zero_results(self):
        # "the"* prefix-matches "theory" (and the standalone "the") today,
        # returning the trap row as a confident hit.
        count, results = self._recall_count("the")
        self.assertEqual(
            count, 0,
            "closed-class stop word 'the' must match nothing; it matched: "
            + "; ".join(x["content"][:50] for x in results))

    def test_query_and_returns_zero_results(self):
        # The trap row carries a standalone "and"; today that is enough to
        # return it for a query of just "and".
        count, results = self._recall_count("and")
        self.assertEqual(
            count, 0,
            "closed-class stop word 'and' must match nothing; it matched: "
            + "; ".join(x["content"][:50] for x in results))

    def test_content_query_still_returns_its_row(self):
        # Contrast guard: the lane is not broken, a meaningful content token
        # still retrieves its row — so the zero-result assertions above can
        # never pass because of a dead store or a dead lane.
        count, results = self._recall_count("quokka")
        self.assertGreaterEqual(count, 1,
                                "legit content query must still hit its row")
        self.assertIn("quokka", results[0]["content"])
        self.assertNotIn("theory", results[0]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
