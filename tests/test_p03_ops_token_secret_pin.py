"""Issue #251 feedback-round pin — swarm-pr-review PRR-008.

The non-runner branch of the ops-token derivation guards TWO conjuncts:
``not t.startswith("-")`` (the issue #251 dash rule, pinned by
tests/test_p03_ops_token_flag_tail.py) and ``not _SECRET_SHAPE_RE.match(t)``
(review PRR-91-002: a secret-shaped basename must never reach the ring or
the recall query). Before this file the secret conjunct had NO pin on the
non-runner path — the PR #312 review executed a partial mutation that
removed only ``and not _SECRET_SHAPE_RE.match(t):`` from the guard and made
``derive_ops_tokens("echo ghp_...")`` emit the literal credential token
while the entire suite stayed green. This file pins that conjunct on both
copies (storelib and the hermes-provider mirror).

Deliberately a NEW, UNFROZEN file: the issue-tracer checkpoint anchor for
#251 was published before implementation, so post-anchor additions live in
their own files and never in the frozen bytes of
tests/test_p03_ops_token_flag_tail.py (blob 5bfa679...).

Env isolation: ZMEM_STORE / ZMEM_DATA / ZMEM_MODELS_DIR /
ZMEM_MODEL_AUTODOWNLOAD are pinned to a scratch dir at MODULE level before
any import. Runs standalone: ``python tests/test_p03_ops_token_secret_pin.py``
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Store-isolation env MUST be pinned before any storelib import anywhere in
# this process (hard assignment, not setdefault).
_MODULE_SCRATCH = tempfile.mkdtemp(prefix="zmem-p03-secret-")
os.environ["ZMEM_STORE"] = os.path.join(_MODULE_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = _MODULE_SCRATCH
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_MODULE_SCRATCH, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "storelib"))

import ops_tokens  # noqa: E402  (env + path pins above are load-bearing)

# Non-runner events (head is NOT in _RUNNER_HEADS) whose last word is
# secret-shaped. The dash conjunct would not catch these — only
# _SECRET_SHAPE_RE does; the file-shape variants additionally exercise the
# "secret beats file-shaped" precedence.
SECRET_SHAPES = (
    "echo ghp_AbCdEf123456",          # bare secret-shaped token
    "cp notes sk-testsecret123.txt",   # file-shaped secret (has a dot)
    "upload glpat-XY7890ABCD1234",     # gitlab PAT prefix
)


def _load_provider(name="zmem_p03_secret_provider"):
    """Load hermes-plugin/__init__.py with the agent-stub it imports."""
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", mp)
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "hermes-plugin" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class StorelibNonRunnerSecretTest(unittest.TestCase):
    """C-storelib: the non-runner secret conjunct must drop every shape."""

    def test_non_runner_secret_shapes_dropped(self):
        for event in SECRET_SHAPES:
            with self.subTest(event=event):
                self.assertEqual(ops_tokens.derive_ops_tokens(event), [],
                                 "secret-shaped non-runner tail leaked: %r"
                                 % (event,))

    def test_dash_and_secret_conjuncts_both_hold(self):
        # The dash rule still fires on its own shapes and the secret rule on
        # its own — the guard keeps both conjuncts (the partial-mutation
        # escape this file exists to block removed exactly one of them).
        self.assertEqual(ops_tokens.derive_ops_tokens("edit notes -x.txt"),
                         [])
        self.assertEqual(ops_tokens.derive_ops_tokens("echo ghp_AbCdEf123456"),
                         [])
        # A benign non-runner basename must still pass (the guards must not
        # over-filter into "drop everything").
        self.assertEqual(
            ops_tokens.derive_ops_tokens("open src/report-config.ts"),
            ["report-config.ts"])


class ProviderNonRunnerSecretTest(unittest.TestCase):
    """C-provider: the hermes-provider mirror carries the same conjunct."""

    def test_provider_non_runner_secret_shapes_dropped(self):
        mod = _load_provider()
        for event in SECRET_SHAPES:
            with self.subTest(event=event):
                self.assertEqual(
                    mod._derive_provider_ops_tokens(event), [],
                    "provider mirror leaked a secret-shaped tail: %r"
                    % (event,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
