"""Run each issue #135 acceptance replay in its own subprocess.

The validators and portable fixtures live under ``tests/fixtures/training/repro``
so CI never needs the issue-trace working files.  Each test intentionally
executes one validator independently and checks its stable ``ACn_OK`` marker.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPRO = ROOT / "tests" / "fixtures" / "training" / "repro"


class TrainingAcceptanceReproTests(unittest.TestCase):
    def _run_validator(self, name: str, marker: str) -> None:
        result = subprocess.run(
            [sys.executable, str(REPRO / name)],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=240,
        )
        output = (result.stdout or "") + (result.stderr or "")
        self.assertEqual(result.returncode, 0, output)
        self.assertIn(marker, output, output)

    def test_ac1_cli_surfaces(self) -> None:
        self._run_validator(
            "C1_cli_surfaces.py",
            "AC1_OK_suite_read_only_snapshot_binding_and_ledger_preserved",
        )

    def test_ac2_complete_capture(self) -> None:
        self._run_validator(
            "C2_complete_capture.py", "AC2_OK_complete_sft_row_contract_and_redaction"
        )

    def test_ac3_preferences(self) -> None:
        self._run_validator(
            "C3_preferences.py", "AC3_OK_explicit_correction_preferences_only"
        )

    def test_ac4_label_thresholds(self) -> None:
        self._run_validator(
            "C4_label_thresholds.py", "AC4_OK_label_thresholds_and_trust_floor"
        )

    def test_ac5_split_dedup(self) -> None:
        self._run_validator(
            "C5_split_dedup.py", "AC5_OK_lineage_split_exact_dedup_and_deletion_map"
        )

    def test_ac6_refusal_atomicity(self) -> None:
        self._run_validator(
            "C6_refusal_atomicity.py",
            "AC6_OK_missing_governance_refusal_without_exportable_partial",
        )

    def test_ac7_quarantine(self) -> None:
        self._run_validator(
            "C7_quarantine.py", "AC7_OK_quarantine_opt_in_redacted_bounded"
        )

    def test_ac8_snapshot_binding(self) -> None:
        self._run_validator(
            "C8_snapshot_binding.py",
            "AC8_OK_emitted_ack_completion_binding_and_replay",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
