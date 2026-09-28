from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "training"
sys.path.insert(0, str(FIXTURE_DIR))
sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))

import build_fixtures  # noqa: E402
from storelib import schema  # noqa: E402
from storelib.training import write_training_views  # noqa: E402


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class TrainingExporterGoldenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_profile = os.environ.get("ZMEM_EMBED_PROFILE")
        os.environ["ZMEM_EMBED_PROFILE"] = "fake"
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.init_db(self.conn)
        schema.migrate(self.conn)
        build_fixtures._insert_fixture(self.conn, build_fixtures.load_cases())

    def tearDown(self) -> None:
        self.conn.close()
        if self.old_profile is None:
            os.environ.pop("ZMEM_EMBED_PROFILE", None)
        else:
            os.environ["ZMEM_EMBED_PROFILE"] = self.old_profile

    def test_real_export_matches_checked_in_parquet_and_json_goldens(self) -> None:
        cases = build_fixtures.load_cases()
        with tempfile.TemporaryDirectory(prefix="training-golden-test-") as tmp:
            output = Path(tmp)
            result = write_training_views(
                self.conn,
                out_dir=str(output),
                namespace=cases["namespace"],
                snapshot_id=cases["snapshot_id"],
                reviewer_confirmed=True,
            )

            self.assertEqual(result["sft_count"], 3)
            self.assertEqual(result["preference_count"], 1)
            self.assertEqual(result["deletion_count"], 2)
            for generated, expected in (
                ("sft-000.parquet", "expected-sft.parquet"),
                ("preferences-000.parquet", "expected-preferences.parquet"),
                ("deletion-map.json", "expected-deletion-map.json"),
            ):
                self.assertEqual(
                    (output / generated).read_bytes(),
                    (FIXTURE_DIR / expected).read_bytes(),
                    generated,
                )

            import pyarrow.parquet as pq
            self.assertEqual(
                _json_bytes(pq.read_table(output / "sft-000.parquet").to_pylist()),
                (FIXTURE_DIR / "expected-sft.json").read_bytes(),
            )
            self.assertEqual(
                _json_bytes(pq.read_table(output / "preferences-000.parquet").to_pylist()),
                (FIXTURE_DIR / "expected-preferences.json").read_bytes(),
            )

            deletion_map = json.loads((output / "deletion-map.json").read_text())
            self.assertEqual(
                {row["reason"] for row in deletion_map},
                {"exact_duplicate", "semantic_duplicate"},
            )
            preferences = pq.read_table(output / "preferences-000.parquet").to_pylist()
            self.assertEqual(preferences[0]["chosen"]["memory_id"], "mem-new")
            self.assertEqual(preferences[0]["rejected"]["memory_id"], "mem-old")

    def test_golden_detects_a_mutated_export_projection(self) -> None:
        cases = build_fixtures.load_cases()
        with tempfile.TemporaryDirectory(prefix="training-golden-mutation-") as tmp:
            output = Path(tmp)
            write_training_views(
                self.conn,
                out_dir=str(output),
                namespace=cases["namespace"],
                snapshot_id=cases["snapshot_id"],
                reviewer_confirmed=True,
            )
            import pyarrow.parquet as pq
            rows = pq.read_table(output / "sft-000.parquet").to_pylist()
            rows[0]["assistant_response"] = "mutated exporter output"
            self.assertNotEqual(
                _json_bytes(rows),
                (FIXTURE_DIR / "expected-sft.json").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
