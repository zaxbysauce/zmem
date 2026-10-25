"""compose_inject_query file-shaped token preservation — acceptance spec.

Defect under test: ``storelib.ops_tokens.compose_inject_query`` drops a
FILE-SHAPED token when it precedes runner words in the ops string. Passing
"atomic-write-ratchet.test.ts git commit" derives nothing useful today
(the head token is not a runner head, so the non-runner branch keeps only
the LAST word — "commit", a bare word that fails the file-shape gate) and
the exact file the session is working on never reaches the recall query.

POST-FIX CONTRACT pinned here (pure, in-process; no store touches):

- compose_inject_query("why is my tree dirty?",
  "atomic-write-ratchet.test.ts git commit", "") must CONTAIN
  "atomic-write-ratchet.test.ts" — the file-shaped token survives
  composition. (No claim about "git"/"commit": only the file-shaped
  preservation is the contract.)
- The empty-ops identity contract still holds:
  compose_inject_query("  prompt  ", "", "") == "prompt" (first 500
  of the stripped prompt).

ROUND-2A AMENDMENT — compose-level regression legs for the REAL inject.py
composition shapes (a fix for the file-shaped case must not chip the
subcommand/ref tokens the runner window already preserves):

- 'git status' joined alone keeps 'status';
- 'bun test foo.test.ts' keeps BOTH 'test' and 'foo.test.ts';
- 'gh pr merge' keeps 'pr' AND 'merge';
- 'git push origin/main' (the derived join of 'git push origin main')
  keeps the ref token 'origin/main' — the ``origin/`` basename-rule
  exemption in ``_clean_token`` must survive composition;
- the existing C8 shape 'atomic-write-ratchet.test.ts git commit' keeps
  the file-shaped head token.

At the pre-fix tree the bare subcommand/ref words (status, test, pr,
merge, origin/main) survive by the runner-window rules — those legs are
regression guards; the file-shaped C8 leg is RED. Round-2b finding 9:
every BARE-WORD token assertion (status, test, pr, merge) uses
whitespace-token membership (``in composed.split()``), never a substring
check — e.g. 'test' is a substring of 'foo.test.ts' and 'pr' of
'prompt', so bare assertIn would be vacuous. File-shaped/ref tokens
('foo.test.ts', 'origin/main') stay substring asserts (they are the
whole token); 'python'/'git' are deliberately not asserted anywhere (the
C8 contract disclaims claims about git/commit).

SAFETY: storelib freezes the store path at its first import, so the
environment is pinned to a throwaway temp store BEFORE any storelib
import (an unpinned import could auto-migrate the shared operator store
~/.zmem/store.sqlite). Ambient ZMEM_* knobs are stripped first.

Runs standalone: python -X utf8 tests/test_compose_inject_tokens_preserved.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"

# Mirror the _STRIP_ENV pattern of tests/test_subagent_tasktext.py:45-67;
# strip every ZMEM_* knob this test does not set, including a dev box's
# ZMEM_REMOTE_URL.
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_INJECT_FLOOR_LEX", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
    "ZMEM_SESSION", "ZMEM_REMOTE_URL",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
)

_PROMPT = "why is my tree dirty?"


class ComposeInjectTokensPreservedTest(unittest.TestCase):
    """compose_inject_query token-preservation legs (pure function)."""

    _tmp = ""
    _saved = {}
    _ops = None

    @classmethod
    def setUpClass(cls):
        cls._saved = {k: os.environ.get(k) for k in _STRIP_ENV}
        for key in _STRIP_ENV:
            os.environ.pop(key, None)
        cls._tmp = tempfile.mkdtemp(prefix="zmem-compose-spec-")
        # PIN before the first storelib import (store path freezes at
        # import).
        os.environ["ZMEM_STORE"] = os.path.join(cls._tmp, "store.sqlite")
        os.environ["ZMEM_DATA"] = cls._tmp
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_SESSION"] = "compose-tokens-spec-session"
        sys.path.insert(0, str(SCRIPTS))
        sys.path.insert(0, str(SCRIPTS / "storelib"))
        import ops_tokens  # noqa: E402  (import happens AFTER env pin)
        cls._ops = ops_tokens

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _compose(self, ops: str) -> str:
        composed = self._ops.compose_inject_query(_PROMPT, ops, "")
        self.assertIsInstance(composed, str,
                              "compose_inject_query must return a string, "
                              "got %r" % type(composed))
        return composed

    def test_file_shaped_token_survives_composition(self):
        # The original C8 leg: only the file-shaped preservation is the
        # contract — no assertion on git/commit.
        composed = self._compose("atomic-write-ratchet.test.ts git commit")
        self.assertIn(
            "atomic-write-ratchet.test.ts", composed,
            "the file-shaped token must survive composition; got %r"
            % composed)

    def test_empty_ops_identity(self):
        # Empty-ops identity (legacy-neutrality pin).
        identity = self._ops.compose_inject_query("  prompt  ", "", "")
        self.assertEqual(
            identity, "prompt",
            "empty-ops identity broken: expected 'prompt', got %r"
            % identity)

    def test_status_subcommand_token_preserved(self):
        # 'git status' joined alone: the primary subcommand word survives
        # composition (regression guard for the file-shaped fix).
        # Whitespace-token membership, not substring: 'status' must be a
        # whole token (round-2b finding 9 — a substring check could be
        # satisfied by an unrelated token that merely contains it).
        composed = self._compose("git status")
        self.assertIn(
            "status", composed.split(),
            "the 'status' token from 'git status' must survive "
            "composition; got %r" % composed)

    def test_bun_test_tokens_preserved(self):
        # 'bun test foo.test.ts': BOTH the subcommand word 'test' and the
        # file-shaped 'foo.test.ts' survive. 'test' is asserted as a
        # whitespace token (it is also a substring of 'foo.test.ts', so a
        # bare substring assert would be vacuous — round-2b finding 9);
        # the file-shaped token stays a substring assert (it IS the
        # whole token).
        composed = self._compose("bun test foo.test.ts")
        self.assertIn(
            "test", composed.split(),
            "the 'test' token from 'bun test foo.test.ts' must survive "
            "composition; got %r" % composed)
        self.assertIn(
            "foo.test.ts", composed,
            "the file-shaped 'foo.test.ts' token must survive "
            "composition; got %r" % composed)

    def test_gh_pr_merge_tokens_preserved(self):
        # 'gh pr merge': the subcommand word 'pr' AND the hazardous
        # subcommand 'merge' both survive. Both are asserted as
        # whitespace tokens (bare-word tokens — substring asserts could
        # be satisfied by unrelated containing tokens, e.g. 'pr' inside
        # 'prompt').
        composed = self._compose("gh pr merge")
        self.assertIn(
            "pr", composed.split(),
            "the 'pr' token from 'gh pr merge' must survive composition; "
            "got %r" % composed)
        self.assertIn(
            "merge", composed.split(),
            "the 'merge' token from 'gh pr merge' must survive "
            "composition; got %r" % composed)

    def test_ref_token_origin_slash_preserved(self):
        # 'git push origin main' derives the join 'git push origin/main';
        # the ``origin/`` basename-rule exemption must keep the REF token
        # intact through composition.
        composed = self._compose("git push origin/main")
        self.assertIn(
            "origin/main", composed,
            "the ref token 'origin/main' must survive composition (the "
            "origin/ basename-rule exemption); got %r" % composed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
