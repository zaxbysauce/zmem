"""Query hygiene — normalized shape visible in recall --explain (issue #112, C6).

Issue #112 scope item: "Record the new query shape in recall --explain
output so a reader can see what was actually matched." For a query with
mixed-case, duplicated and stop-word tokens the recorded shape must show
the NORMALIZED form: casefolded, deduplicated, stop words dropped — and
names only content/tags columns (the namespace column must not be part of
the match scope).

Check shape (NEW-SURFACE: no shape is recorded at base, so this check
FAILS on the current tree by design):
- seed a row matching the meaningful tokens, run
  `recall --explain --json --query "Deploy the THE pipeline"`;
- find the recorded query shape FIELD-NAME AGNOSTICALLY (same discovery as
  tests/test_query_hygiene_termcap.py: any string / list-of-strings inside
  the explain envelope excluding the raw `query` echo, `target`, and
  per-row `verdicts`);
- assert SOME shape candidate carries the normalized form:
  * contains lowercase "deploy" and "pipeline" as words (casefolded),
  * contains no "the" as a word in ANY case (stop words dropped; the
    duplicated "the"/"THE" collapses away entirely),
  * never names the "namespace" column (match scope is content/tags only).

Presence needles are on the normalized tokens, not on a field name, so a
correct implementation with any field name passes — but at base no shape is
recorded at all, so the check fails.

Lane isolation: --no-hybrid forces the lexical FTS lane under fix. Store is
a throwaway temp store driven through the real CLI subprocesses; ambient
ZMEM_* env is stripped from every child. No storelib import happens
in-process. Runs standalone: py -3 tests/test_query_hygiene_explain.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"

NS = "project:hygiene-explain"
ROW = "deploy pipeline canary rollout gates on the acceptance suite"
# Mixed-case originals, a duplicated stop word in two cases, one stop word.
QUERY = "Deploy the THE pipeline"

_RE_DEPLOY = re.compile(r"\bdeploy\b")      # case-SENSITIVE: proves casefold
_RE_PIPELINE = re.compile(r"\bpipeline\b")  # case-SENSITIVE: proves casefold
_RE_THE = re.compile(r"\bthe\b", re.IGNORECASE)     # stop word, any case
_RE_NAMESPACE = re.compile(r"\bnamespace\b", re.IGNORECASE)  # match scope

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


class ExplainShapeTest(unittest.TestCase):
    """The recorded query shape shows the normalized match (issue #112 C6)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="zmem-hygiene-expl-")
        cls._env = _clean_env(cls._tmp)
        cls._run_cli("init")
        cls._run_cli("add", "--namespace", NS, "--type", "fact",
                     "--content", ROW, "--signal", "test",
                     "--confidence", "0.9")

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

    @staticmethod
    def _shape_texts(explain: dict, raw_query: str) -> list[tuple[str, str]]:
        """All (path, text) candidate strings inside the explain envelope.

        Walks the explain object, skipping the three surfaces that are NOT
        the recorded query shape: the raw `query` echo (by key at the top
        level and by exact-string match anywhere), `target`, and `verdicts`
        (per-row detail/neighbor content is row text, not query shape).
        Lists of strings are joined into one candidate (a term list).
        """
        out: list[tuple[str, str]] = []

        def walk(node, path):
            if isinstance(node, dict):
                for k, v in node.items():
                    if path == () and k in ("verdicts", "target", "query"):
                        continue
                    walk(v, path + (k,))
            elif isinstance(node, list):
                if node and all(isinstance(x, str) for x in node):
                    out.append(("/".join(path), " ".join(node)))
                for i, v in enumerate(node):
                    walk(v, path + (str(i),))
            elif isinstance(node, str):
                if node == raw_query:
                    return  # the verbatim echo is not the normalized shape
                out.append(("/".join(path), node))

        walk(explain, ())
        return out

    def test_explain_records_normalized_query_shape(self):
        r = self._run_cli("recall", "--explain", "--json", "--no-hybrid",
                          "--query", QUERY, "--namespace", NS)
        doc = json.loads(r.stdout)

        # Sanity (passes at base): the row really is retrieved for the
        # mixed-case query — proves the pipeline ran over a live row, so
        # the shape assertion below cannot fail on a dead store.
        self.assertGreaterEqual(doc["count"], 1,
                                "seeded row must be retrieved for the "
                                "mixed-case query")
        self.assertIn("deploy", doc["results"][0]["content"])

        # The NEW SURFACE: the explain envelope must record the normalized
        # query shape. At base no field carries it, so no candidate shows
        # the meaningful tokens and this fails.
        explain = doc.get("explain")
        self.assertIsInstance(explain, dict,
                              "recall --explain --json must carry the "
                              "explain envelope")
        normalized = [
            path for path, text in self._shape_texts(explain, QUERY)
            if (_RE_DEPLOY.search(text) and _RE_PIPELINE.search(text)
                and not _RE_THE.search(text)
                and not _RE_NAMESPACE.search(text))
        ]
        self.assertTrue(
            normalized,
            "recall --explain must record the normalized query shape "
            "(casefolded tokens, stop words dropped, namespace column out "
            "of the match scope); no explain field shows the normalized "
            "form of the query")


if __name__ == "__main__":
    unittest.main(verbosity=2)
