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

import ops_tokens  # noqa: E402 (standalone module — no storelib import)

# storelib.recall is imported at TEST-RUN time (setUp), not at module level:
# importing storelib freezes STORE_PATH from the ambient env, and a
# module-level import here would freeze the DEFAULT store before
# test_eval_runner (which pins ZMEM_STORE for its in-process tests, relying
# on its own module import order) gets to pin — breaking co-runs like
# `python -m unittest tests.test_query_hygiene_terms tests.test_eval_runner`
# (repo-test hazard 276ee98b). These tests never touch a store, so deferring
# the import into setUp is free.


def _load_recall_api():
    from storelib.recall import (
        MAX_QUERY_TERMS,
        MIN_PREFIX_TERM_LEN,
        QUERY_STOPWORDS,
        _fts_expression,
        _normalize_query_terms,
    )
    return {
        "MAX_QUERY_TERMS": MAX_QUERY_TERMS,
        "MIN_PREFIX_TERM_LEN": MIN_PREFIX_TERM_LEN,
        "QUERY_STOPWORDS": QUERY_STOPWORDS,
        "_fts_expression": _fts_expression,
        "_normalize_query_terms": _normalize_query_terms,
    }


class _RecallApiMixin(unittest.TestCase):
    """Binds the lazily-imported recall API onto self at test-run time."""

    def setUp(self):
        super().setUp()
        for _k, _v in _load_recall_api().items():
            setattr(self, _k, _v)


class NormalizeTermsTest(_RecallApiMixin):

    def test_casefold_and_dedupe(self):
        self.assertEqual(
            self._normalize_query_terms("Deploy the THE pipeline deploy"),
            ["deploy", "pipeline"])

    def test_stopwords_dropped(self):
        self.assertEqual(self._normalize_query_terms("the and or of to"), [])

    def test_all_stopword_query_yields_empty_expression(self):
        self.assertEqual(
            self._fts_expression(self._normalize_query_terms("the")), "")

    def test_short_tokens_exact_no_wildcard(self):
        expr = self._fts_expression(["gh", "pr", "merge"])
        self.assertIn('"gh"', expr)
        self.assertNotIn('"gh"*', expr)
        self.assertIn('"merge"*', expr)

    def test_long_tokens_prefix_wildcard(self):
        expr = self._fts_expression(["deploy", "pipeline"])
        self.assertIn('"deploy"*', expr)
        self.assertIn('"pipeline"*', expr)

    def test_expression_column_filtered(self):
        expr = self._fts_expression(["deploy"])
        self.assertTrue(expr.startswith("{content tags} : ("))
        self.assertNotIn("namespace", expr)

    def test_internal_punctuation_preserved(self):
        # Dotted/hyphenated tokens stay whole (phrase semantics), only the
        # EDGES are stripped.
        self.assertEqual(
            self._normalize_query_terms("atomic-write-ratchet.test.ts."),
            ["atomic-write-ratchet.test.ts"])
        expr = self._fts_expression(["atomic-write-ratchet.test.ts"])
        self.assertIn('"atomic-write-ratchet.test.ts"*', expr)

    def test_edge_fts_operators_stripped(self):
        self.assertEqual(
            self._normalize_query_terms('"deploy"* (pipeline):'),
            ["deploy", "pipeline"])

    def test_cap_bounds_term_count(self):
        prose = " ".join("tok%02d" % i for i in range(60))
        terms = self._normalize_query_terms(prose)
        self.assertEqual(len(terms), self.MAX_QUERY_TERMS)
        self.assertLessEqual(
            len(self._fts_expression(terms).split(" OR ")),
            self.MAX_QUERY_TERMS)

    def test_cap_keeps_head_and_ops_tail(self):
        prose = " ".join("tok%02d" % i for i in range(30))
        composed = prose + " git stash pop atomic-write-ratchet.test.ts"
        terms = self._normalize_query_terms(composed)
        self.assertEqual(len(terms), self.MAX_QUERY_TERMS)
        self.assertEqual(terms[0], "tok00")
        for ops in ("git", "stash", "pop", "atomic-write-ratchet.test.ts"):
            self.assertIn(ops, terms,
                          f"ops token {ops!r} must survive the term cap")

    def test_empty_and_none_queries(self):
        self.assertEqual(self._normalize_query_terms(""), [])
        self.assertEqual(self._normalize_query_terms("   "), [])
        self.assertEqual(self._normalize_query_terms(None), [])


class Review111FixesTest(_RecallApiMixin):
    """PR #146 review round: pins for the tokenizer-aware normalization
    fixes (curly-quote stopword bypass, contraction adjacency flood) and
    the boundary gaps the review identified."""

    def test_curly_quoted_stopword_is_stopped(self):
        # “the” must normalize to the stoplisted form, not survive as a
        # curly-quoted term that unicode61 parses back to bare "the".
        self.assertEqual(
            self._normalize_query_terms("“the” collies"),
            ["collies"])

    def test_curly_quoted_phrase_head_does_not_leak(self):
        # '“the plan” for tomorrow' pre-fix leaked '“the' as a term; the
        # edge-strip now eats the curly quote so the stopword applies.
        self.assertEqual(
            self._normalize_query_terms("“the plan” for tomorrow"),
            ["plan", "tomorrow"])

    def test_contraction_segments_split_and_stopped(self):
        # don't -> FTS phrase [don, t*]; both halves are stoplisted
        # ("don" and "t" are deliberate QUERY_STOPWORDS entries), so the
        # token drops entirely instead of flooding via the [don, t*]
        # adjacency phrase.
        self.assertEqual(
            self._normalize_query_terms("don't stop the deploy"),
            ["stop", "deploy"])
        # all segments stopword/short -> token dropped entirely
        self.assertEqual(self._normalize_query_terms("it's fine"), ["fine"])
        # a meaningful stem survives: dog's -> dog (s dropped)
        self.assertEqual(
            self._normalize_query_terms("the dog's breakfast"),
            ["dog", "breakfast"])

    def test_cap_boundary_at_25_terms(self):
        # exactly one over the cap: head 0-11 + tail 13-24 survive, the
        # middle term (index 12) is the dropped one — pin the drop choice.
        prose = " ".join("tok%02d" % i for i in range(25))
        terms = self._normalize_query_terms(prose)
        self.assertEqual(len(terms), self.MAX_QUERY_TERMS)
        self.assertEqual(terms[:12], ["tok%02d" % i for i in range(12)])
        self.assertEqual(terms[12:], ["tok%02d" % i for i in range(13, 25)])

    def test_three_char_boundary_wildcards(self):
        # MIN_PREFIX_TERM_LEN = 3: exactly 3 chars gets the wildcard.
        expr = self._fts_expression(["abc"])
        self.assertIn('"abc"*', expr)
        expr2 = self._fts_expression(["ab"])
        self.assertIn('"ab"', expr2)
        self.assertNotIn('"ab"*', expr2)

    def test_internal_double_quote_is_escaped(self):
        expr = self._fts_expression(['fo"o'])
        self.assertIn('"fo""o"', expr)

    def test_all_stopword_expression_is_empty(self):
        terms = self._normalize_query_terms("the and of to")
        self.assertEqual(terms, [])
        self.assertEqual(self._fts_expression(terms), "")


class StoplistOpsVocabularyDisjointTest(_RecallApiMixin):
    """The #112 guardrail rung: the stoplist is function words plus common
    degree/time adverbs. Any overlap with the ops-token vocabulary would
    silently stoplist the high-signal lane the whole #85/#88 design
    reserves."""

    # The ops vocabulary the ring can emit, built from the REAL sources:
    # runner heads + hazardous subcommands come straight from ops_tokens
    # (so a new entry there is automatically covered); file-shaped tokens
    # are corpus-dependent, so the DECISION_ROWS literals stay as a pinned
    # supplement (issue #112 review: a hand-written subset alone could not
    # catch a future stopword overlap in the real sets).
    OPS_VOCABULARY = (
        frozenset(ops_tokens._RUNNER_HEADS)
        | frozenset(ops_tokens._HAZARDOUS_SUBS)
        | frozenset([
            "pr", "force-with-lease", "origin", "main", "head", "test",
            "atomic-write-ratchet.test.ts", "bunfig.toml",
        ])
    )

    def test_stopword_set_is_frozenset(self):
        self.assertIsInstance(self.QUERY_STOPWORDS, frozenset)

    def test_stoplist_disjoint_from_ops_vocabulary(self):
        overlap = self.QUERY_STOPWORDS & self.OPS_VOCABULARY
        self.assertEqual(
            overlap, set(),
            f"stoplist must not contain ops vocabulary, found: {sorted(overlap)}")

    def test_min_prefix_len_is_three(self):
        self.assertEqual(self.MIN_PREFIX_TERM_LEN, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
