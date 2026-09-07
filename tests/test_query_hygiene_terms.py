"""Unit pins for the lexical query hygiene (issue #112, Workstream C-1).

Pins `_normalize_query_terms` / `_fts_expression` — the single choke point
every recall surface flows through — plus the contract that matters most
for the ops lane: QUERY_STOPWORDS is disjoint from the operation-token
vocabulary, so the reserved ops slice can never be stoplisted away.

Run standalone: py -3 tests/test_query_hygiene_terms.py
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "storelib"))

from storelib.recall import (  # noqa: E402
    MAX_QUERY_TERMS,
    MIN_PREFIX_TERM_LEN,
    QUERY_STOPWORDS,
    _fts_expression,
    _normalize_query_terms,
)


class NormalizeTermsTest(unittest.TestCase):
    def test_casefold_and_dedupe(self):
        self.assertEqual(
            _normalize_query_terms("Deploy the THE pipeline deploy"),
            ["deploy", "pipeline"])

    def test_stopwords_dropped(self):
        self.assertEqual(_normalize_query_terms("the and or of to"), [])

    def test_all_stopword_query_yields_empty_expression(self):
        self.assertEqual(_fts_expression(_normalize_query_terms("the")), "")

    def test_short_tokens_exact_no_wildcard(self):
        expr = _fts_expression(["gh", "pr", "merge"])
        self.assertIn('"gh"', expr)
        self.assertNotIn('"gh"*', expr)
        self.assertIn('"merge"*', expr)

    def test_long_tokens_prefix_wildcard(self):
        expr = _fts_expression(["deploy", "pipeline"])
        self.assertIn('"deploy"*', expr)
        self.assertIn('"pipeline"*', expr)

    def test_expression_column_filtered(self):
        expr = _fts_expression(["deploy"])
        self.assertTrue(expr.startswith("{content tags} : ("))
        self.assertNotIn("namespace", expr)

    def test_internal_punctuation_preserved(self):
        # Dotted/hyphenated tokens stay whole (phrase semantics), only the
        # EDGES are stripped.
        self.assertEqual(
            _normalize_query_terms("atomic-write-ratchet.test.ts."),
            ["atomic-write-ratchet.test.ts"])
        expr = _fts_expression(["atomic-write-ratchet.test.ts"])
        self.assertIn('"atomic-write-ratchet.test.ts"*', expr)

    def test_edge_fts_operators_stripped(self):
        self.assertEqual(
            _normalize_query_terms('"deploy"* (pipeline):'),
            ["deploy", "pipeline"])

    def test_cap_bounds_term_count(self):
        prose = " ".join("tok%02d" % i for i in range(60))
        terms = _normalize_query_terms(prose)
        self.assertEqual(len(terms), MAX_QUERY_TERMS)
        self.assertLessEqual(len(_fts_expression(terms).split(" OR ")),
                             MAX_QUERY_TERMS)

    def test_cap_keeps_head_and_ops_tail(self):
        prose = " ".join("tok%02d" % i for i in range(30))
        composed = prose + " git stash pop atomic-write-ratchet.test.ts"
        terms = _normalize_query_terms(composed)
        self.assertEqual(len(terms), MAX_QUERY_TERMS)
        self.assertEqual(terms[0], "tok00")
        for ops in ("git", "stash", "pop", "atomic-write-ratchet.test.ts"):
            self.assertIn(ops, terms,
                          f"ops token {ops!r} must survive the term cap")

    def test_empty_and_none_queries(self):
        self.assertEqual(_normalize_query_terms(""), [])
        self.assertEqual(_normalize_query_terms("   "), [])
        self.assertEqual(_normalize_query_terms(None), [])


class StoplistOpsVocabularyDisjointTest(unittest.TestCase):
    """The #112 guardrail rung: the stoplist is closed-class English ONLY.
    Any overlap with the ops-token vocabulary would silently stoplist the
    high-signal lane the whole #85/#88 design reserves."""

    # The ops vocabulary the ring can emit: runner heads + hazardous
    # subcommands + file-shaped tokens (ops_tokens._RUNNER_HEADS /
    # _HAZARDOUS_SUBS plus the words DECISION_ROWS rely on).
    OPS_VOCABULARY = frozenset("""
        git stash pop bun test gh pr merge reset soft push fetch pull
        origin main head worktree rebase force-with-lease
        atomic-write-ratchet.test.ts bunfig.toml
    """.split())

    def test_stopword_set_is_frozenset(self):
        self.assertIsInstance(QUERY_STOPWORDS, frozenset)

    def test_stoplist_disjoint_from_ops_vocabulary(self):
        overlap = QUERY_STOPWORDS & self.OPS_VOCABULARY
        self.assertEqual(
            overlap, set(),
            f"stoplist must not contain ops vocabulary, found: {sorted(overlap)}")

    def test_min_prefix_len_is_three(self):
        self.assertEqual(MIN_PREFIX_TERM_LEN, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
