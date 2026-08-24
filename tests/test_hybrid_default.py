"""Issue #58, 3.3: default hybrid on when embeddings exist.

Three contracts:
  1. ``hybrid=None`` (the default) auto-picks hybrid when embeddings
     are available, else lexical.
  2. ``hybrid=False`` (--no-hybrid) forces lexical-only.
  3. ``hybrid=True`` (--hybrid) is the explicit alias of the default.
  4. ``search`` is keyword-only by contract and stays lexical-only
     regardless of the new default sentinel (I1 critic-fix).
"""

from __future__ import annotations

import inspect
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "storelib"))


class HybridDefaultSignatureTests(unittest.TestCase):

    def test_recall_memory_hybrid_default_is_none_sentinel(self):
        """The default must be a sentinel (None), not False — the
        sentinel triggers the auto-pick on the first call."""
        import storelib.recall as recall_mod
        sig = inspect.signature(recall_mod.recall_memory)
        self.assertIsNone(
            sig.parameters["hybrid"].default,
            "recall_memory's hybrid default must be None (auto-pick sentinel); "
            "issue #58, 3.3",
        )

    def test_recall_memory_hybrid_annotation_accepts_none(self):
        import storelib.recall as recall_mod
        sig = inspect.signature(recall_mod.recall_memory)
        self.assertIn(
            "None", str(sig.parameters["hybrid"].annotation),
            "hybrid annotation must include None to mark it as the sentinel",
        )


class CliDispatchTests(unittest.TestCase):
    """The CLI must dispatch --hybrid / --no-hybrid / absent into the
    right hybrid_arg value."""

    def test_cli_has_no_hybrid_flag(self):
        """cli.py must register --no-hybrid on the recall subcommand."""
        import inspect
        import storelib.cli as cli_mod
        # Re-build the parser via main() internals — but main() runs
        # the full dispatch. Instead, scan the source for the
        # registration line so the ratchet does not depend on the
        # full main() invocation.
        src = inspect.getsource(cli_mod)
        self.assertIn(
            '"--no-hybrid"', src,
            "cli.py must register --no-hybrid on the recall subcommand "
            "(issue #58, 3.3)",
        )

    def test_search_dispatch_forces_hybrid_false(self):
        """search must pass hybrid=False explicitly so the new default
        sentinel does not silently flip search to vector-hybrid (I1
        critic-fix)."""
        import inspect
        import storelib.cli as cli_mod
        src = inspect.getsource(cli_mod)
        # Locate the search branch and confirm hybrid=False is passed.
        idx = src.find('"search":')
        self.assertGreater(idx, 0, "search branch not found in cli.py")
        block = src[idx:idx + 600]
        self.assertIn(
            "hybrid=False", block,
            "search dispatch must pass hybrid=False to preserve "
            "byte-identical keyword-only behavior (I1 critic-fix)",
        )


class HybridAutoPickTests(unittest.TestCase):
    """When embeddings are unavailable, hybrid=None must auto-fall-back
    to lexical. When embeddings are available, it must auto-pick hybrid.
    """

    def test_auto_falls_back_when_embeddings_absent(self):
        """``hybrid=None`` with no embeddings runtime → lexical-only."""
        import storelib.recall as recall_mod
        # Force the embeddings module to be None (model-absent path).
        original = recall_mod._embeddings
        recall_mod._embeddings = None
        try:
            # We can't actually call recall_memory without a DB, but
            # we can inspect the dispatch logic. The auto-pick is the
            # first 3 lines of recall_memory's body. Read the source
            # and assert the boolean expression matches the spec.
            src = inspect.getsource(recall_mod.recall_memory)
            self.assertIn(
                "if hybrid is None:",
                src,
                "recall_memory must resolve the None sentinel before "
                "passing it to the per-tier helper",
            )
            self.assertIn(
                "_embeddings and _embeddings.is_available()",
                src,
                "auto-pick must probe _embeddings.is_available()",
            )
        finally:
            recall_mod._embeddings = original


if __name__ == "__main__":
    unittest.main(verbosity=2)