"""Independent acceptance checks for issue #139 provenance commands.

Each test is one executable check (C1 through C5).  The checks call the
process boundary or the public renderer seam and build all inputs below a
throwaway directory.  No production module is imported before the test
environment is isolated.

The base checkout intentionally reports native errors because ``source`` is a
new command.  The external check-author logs record those raw errors; the
same tests are expected to become green after the implementation lands.

C6 has a separately provisioned, mandatory CI entry point because it imports
the pinned Hermes provider. See ``hermes-provider-requirements.md`` for the
exact command. It is deliberately outside this generic Python test module:
the ordinary CI test loop runs every ``tests/test_*.py`` file with Python 3.11
and must never treat a missing optional provider as a skip or a pass.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
CHECKS = ROOT / "tests" / "fixtures" / "source" / "repro" / "checks.py"

_SANITIZED_ENV_KEYS = frozenset(
    (
        "ZMEM_STORE",
        "ZMEM_DATA",
        "ZMEM_MODELS_DIR",
        "ZMEM_MODEL_AUTODOWNLOAD",
        "ZMEM_HOME",
        "ZMEM_NAMESPACE",
        "ZMEM_HOST",
        "ZMEM_SESSION",
        "ZMEM_TRANSCRIPT",
        "ZMEM_AGENT_TRANSCRIPT",
        "ZMEM_CODEX_MEMORY",
        "ZMEM_HERMES_SESSIONS",
        "HERMES_HOME",
        "PYTHONPATH",
    )
)


class SourceAcceptanceTest(unittest.TestCase):
    """Typed generic-runner checks for numbered criteria C1 through C5."""

    def _run_check(self, check_id: str) -> subprocess.CompletedProcess[str]:
        # Sanitize only the child environment.  Mutating os.environ here would
        # pollute unrelated tests in the same interpreter.
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in _SANITIZED_ENV_KEYS
        }
        env.update(
            {
                "ZMEM_MODEL_AUTODOWNLOAD": "0",
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return subprocess.run(
            [PYTHON, str(CHECKS), check_id, "--repo-root", str(ROOT)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def _assert_check_green(self, check_id: str) -> None:
        result = self._run_check(check_id)
        self.assertEqual(
            result.returncode,
            0,
            f"{check_id} failed (base runs are recorded as native errors):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_C1_read_only_source_has_zero_writes_and_network_calls(self):
        """AC1: isolated source resolution preserves all input/state hashes."""
        self._assert_check_green("C1")

    def test_C2_evidence_first_show_returns_offsets_and_truncation_fields(self):
        """AC2: evidence wins over source_ref and display is redacted/bounded."""
        self._assert_check_green("C2")

    def test_C3_refusals_are_exit_one_and_cli_errors_are_exit_two(self):
        """AC3: unsafe, malformed, and redaction/decode inputs fail closed."""
        self._assert_check_green("C3")

    def test_C4_scan_is_literal_and_capped_at_fifty_matches(self):
        """AC4: scan stays in the resolved session and treats ``.*`` literally."""
        self._assert_check_green("C4")

    def test_C5_explicit_hint_is_opt_in_and_passive_bytes_are_preserved(self):
        """AC5: explicit recall gets one hint while passive rendering is unchanged."""
        self._assert_check_green("C5")

if __name__ == "__main__":
    unittest.main(verbosity=2)
