"""Query hygiene — capped term count visible in recall --explain (issue #112, C3).

The FTS query builder has no term cap: a 60-token prose prompt becomes 60
`"<token>"*` OR terms today. Issue #112 scope requires (a) the total term
count bounded (24) and (b) the new query shape RECORDED in `recall --explain`
output so a reader can see what was actually matched.

Check shape (NEW-SURFACE: no shape is recorded at base, so this check
ERRORS/FAILS on the current tree by design):
- run `recall --explain --json` with a 60-DISTINCT-token query (tok00..tok59)
  against a seeded row that contains the head tokens (row really matches);
- find the recorded query shape FIELD-NAME AGNOSTICALLY: any string or
  list-of-strings inside the explain envelope (excluding the raw `query`
  echo, `target`, and per-row `verdicts`, whose detail/neighbor content is
  row text, not query shape) that carries query tokens;
- assert some shape field carries a bounded term list: at least 3 distinct
  query tokens (a real term list, not a stray mention) and at most 24
  distinct query tokens (the cap).

The >= 3 floor stops a stray single-token mention from passing vacuously;
the <= 24 ceiling catches an implementation that records the shape but does
not cap. A correct implementation recording the shape under ANY field name
(a terms list, an FTS query string, a shape object) passes.

Lane isolation: --no-hybrid forces the lexical FTS lane under fix. Store is
a throwaway temp store driven through the real CLI subprocesses; ambient
ZMEM_* env is stripped from every child. No storelib import happens
in-process. Runs standalone: py -3 tests/test_query_hygiene_termcap.py
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

NS = "project:hygiene-termcap"
# The seeded row contains the head tokens, so the 60-token query really
# retrieves it at base (sanity assertion) — the check measures the explain
# surface, not an empty pipeline run.
ROW_ANCHOR = ("termcap anchor row about tok00 tok01 tok02 tok03 tok04 "
              "bounded retrieval")
# 60 DISTINCT prose tokens: no stop words, all >= 3 chars, none a prefix of
# another, none colliding with the namespace tokens.
QUERY_60 = " ".join(f"tok{i:02d}" for i in range(60))
TERM_CAP = 24  # issue #112 scope: total term count capped at 24

_TOKEN_RE = re.compile(r"\btok\d{2}\b")

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


class TermCapExplainTest(unittest.TestCase):
    """A 60-token prompt must surface a bounded (<= 24) recorded term list."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="zmem-hygiene-cap-")
        cls._env = _clean_env(cls._tmp)
        cls._run_cli("init")
        cls._run_cli("add", "--namespace", NS, "--type", "fact",
                     "--content", ROW_ANCHOR, "--signal", "test",
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

    def test_sixty_token_query_records_bounded_term_shape(self):
        r = self._run_cli("recall", "--explain", "--json", "--no-hybrid",
                          "--query", QUERY_60, "--namespace", NS)
        doc = json.loads(r.stdout)

        # Sanity (passes at base): the anchor row really is retrieved for
        # the 60-token prompt — proves the pipeline ran over a live row, so
        # the shape assertion below cannot fail on a dead store.
        self.assertGreaterEqual(doc["count"], 1,
                                "anchor row must be retrieved for the "
                                "60-token query")
        self.assertIn("termcap", doc["results"][0]["content"])

        # The NEW SURFACE: the explain envelope must record the normalized
        # query shape. At base no field carries the terms matched, so the
        # candidate scan below comes back empty and this fails.
        explain = doc.get("explain")
        self.assertIsInstance(explain, dict,
                              "recall --explain --json must carry the "
                              "explain envelope")
        shapes = [(p, t, len(set(_TOKEN_RE.findall(t))))
                  for p, t in self._shape_texts(explain, QUERY_60)
                  if _TOKEN_RE.search(t)]
        bounded = [s for s in shapes if TERM_CAP >= s[2] >= 3]
        self.assertTrue(
            bounded,
            "recall --explain must record the bounded query shape for a "
            "60-token prompt: no field carries a term list of 3-24 query "
            f"tokens (cap {TERM_CAP}); candidates seen: "
            f"{[(p, n) for p, _t, n in shapes]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
