"""Issue #157 contract tests: counterfactual replay evaluator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVALUATOR = ROOT / "scripts" / "eval_counterfactual.py"
FIXTURES = ROOT / "tests" / "fixtures" / "counterfactual"
SCHEMA = ROOT / "eval" / "counterfactual-schema.json"
PYTHON = sys.executable

# Pin the store env at import time, before any storelib import can freeze
# a wrong STORE_PATH (the house pattern).
_SCRATCH_MODULE = Path(tempfile.mkdtemp(prefix="zmem-cf-tests-"))
for _key in list(os.environ):
    if _key.startswith("ZMEM_") or _key in {"CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"}:
        os.environ.pop(_key, None)
os.environ.update({
    "ZMEM_STORE": str(_SCRATCH_MODULE / "ambient.sqlite"),
    "ZMEM_DATA": str(_SCRATCH_MODULE),
    "ZMEM_HOME": str(_SCRATCH_MODULE / "home"),
    "ZMEM_MODELS_DIR": str(_SCRATCH_MODULE / "missing-models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONUTF8": "1",
})


def _env(scratch: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("ZMEM_") or key in {"CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"}:
            env.pop(key, None)
    env.update({
        "ZMEM_STORE": str(scratch / "ambient.sqlite"),
        "ZMEM_DATA": str(scratch),
        "ZMEM_HOME": str(scratch / "home"),
        "ZMEM_MODELS_DIR": str(scratch / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_EMBED_PROFILE": "fake",
        "ZMEM_TEST_NOW": "2026-06-01T00:00:00Z",
        "PYTHONUTF8": "1",
    })
    return env


def _run(scratch: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(EVALUATOR), *argv],
        cwd=str(ROOT), env=_env(scratch),
        capture_output=True, text=True, timeout=300,
    )


def _load_module():
    spec = importlib.util.spec_from_file_location("eval_counterfact", EVALUATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CounterfactualFixtureTest(unittest.TestCase):
    """The committed five-session oracle: bytes, rates, agreement."""

    def test_stub_conditions_match_expected(self):
        with tempfile.TemporaryDirectory(prefix="zmem-cf-fixture-") as raw:
            scratch = Path(raw)
            run = _run(
                scratch,
                "--tasks", str(FIXTURES / "tasks.json"),
                "--store", str(FIXTURES / "store.sqlite"),
                "--model-id", "recorded-stub-v1",
                "--json-out", str(scratch / "cf.json"),
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(
                (scratch / "cf.json").read_bytes(),
                (FIXTURES / "expected.json").read_bytes(),
            )

    def test_memory_changes_repeated_failure_rate(self):
        report = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        rates = {c["inject"]: c["repeated_failure_rate"] for c in report["conditions"]}
        self.assertEqual(rates[1], 0.0)
        self.assertEqual(rates[0], 1.0)

    def test_first_action_agreement_uses_recorded_success(self):
        report = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        for condition in report["conditions"]:
            self.assertIn("first_action_agreement", condition)
        agreement = {c["inject"]: c["first_action_agreement"] for c in report["conditions"]}
        self.assertEqual(agreement[1], 1.0)  # 5/5 stub actions equal the recorded success
        self.assertEqual(agreement[0], 0.0)  # 0/5 without memory
        for condition in report["conditions"]:
            self.assertEqual(condition["task_count"], 5)
            self.assertEqual(condition["tool_call_count"], 5)
            self.assertEqual(
                [s["session_id"] for s in condition["sessions"]],
                [f"session-{index:02d}" for index in range(1, 6)],
            )


class CounterfactualSchemaTest(unittest.TestCase):
    """eval/counterfactual-schema.json validates the committed report."""

    def test_report_validates_against_schema(self):
        import jsonschema

        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        report = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        jsonschema.validate(report, schema)
        broken = json.loads(json.dumps(report))
        del broken["conditions"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(broken, schema)
        for bad_rate in (-0.1, 1.1):
            mutated = json.loads(json.dumps(report))
            mutated["conditions"][0]["repeated_failure_rate"] = bad_rate
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(mutated, schema)


class CounterfactualSafetyTest(unittest.TestCase):
    """Refusals, env restoration, and the model-call gate."""

    def test_home_store_is_refused(self):
        home_store = Path.home() / ".zmem" / "store.sqlite"
        with tempfile.TemporaryDirectory(prefix="zmem-cf-home-") as raw:
            scratch = Path(raw)
            run = _run(
                scratch,
                "--tasks", str(FIXTURES / "tasks.json"),
                "--store", str(home_store),
                "--model-id", "recorded-stub-v1",
            )
            self.assertEqual(run.returncode, 2)
            self.assertIn(
                "[eval] refusing operator home store; pass an isolated --store path",
                run.stderr or "",
            )

    def test_environment_is_restored(self):
        module = _load_module()
        tasks = json.loads((FIXTURES / "tasks.json").read_text(encoding="utf-8"))["tasks"]
        os.environ["ZMEM_TEST_NOW"] = "2026-06-01T00:00:00Z"
        os.environ["ZMEM_EMBED_PROFILE"] = "fake"
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_MODELS_DIR"] = str(_SCRATCH_MODULE / "missing-models")
        store_copy = _SCRATCH_MODULE / "cf-copy.sqlite"
        store_copy.write_bytes((FIXTURES / "store.sqlite").read_bytes())
        os.environ["ZMEM_STORE"] = str(store_copy)
        os.environ["ZMEM_INJECT"] = "7"
        try:
            module.run_condition(tasks, inject=True, model_id="recorded-stub-v1", allow_model_calls=False)
            self.assertEqual(os.environ.get("ZMEM_INJECT"), "7")
            module.run_condition(tasks, inject=False, model_id="recorded-stub-v1", allow_model_calls=False)
            self.assertEqual(os.environ.get("ZMEM_INJECT"), "7")
            # Restoration also fires on a mid-run exception (try/finally wraps
            # the whole body): a selector that raises must not leak the toggle.
            # run_condition resolves the selector via a lazy
            # `from storelib.inject import ...`, so patch the SOURCE module.
            scripts_dir = str(ROOT / "skills" / "memory" / "scripts")
            saved_path = sys.path[:]
            sys.path.insert(0, scripts_dir)
            try:
                import storelib.inject as inject_mod
            finally:
                sys.path[:] = saved_path

            class _Boom(Exception):
                pass

            def _raise(*args, **kwargs):
                raise _Boom("selector exploded")

            original_fn = inject_mod.select_and_budget_for_injection
            inject_mod.select_and_budget_for_injection = _raise
            try:
                with self.assertRaises(_Boom):
                    module.run_condition(tasks, inject=True, model_id="recorded-stub-v1", allow_model_calls=False)
            finally:
                inject_mod.select_and_budget_for_injection = original_fn
            self.assertEqual(os.environ.get("ZMEM_INJECT"), "7")
        finally:
            os.environ.pop("ZMEM_INJECT", None)

    def test_real_model_path_is_skipped_without_opt_in(self):
        with tempfile.TemporaryDirectory(prefix="zmem-cf-skip-") as raw:
            scratch = Path(raw)
            run = _run(
                scratch,
                "--tasks", str(FIXTURES / "tasks.json"),
                "--store", str(FIXTURES / "store.sqlite"),
                "--model-id", "real-model-v1",
                "--json-out", str(scratch / "skip.json"),
            )
            combined = (run.stdout or "") + (run.stderr or "")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("SKIPPED: model calls disabled", combined)
            report = json.loads((scratch / "skip.json").read_text(encoding="utf-8"))
            self.assertTrue(report["skipped"])
            for condition in report["conditions"]:
                self.assertTrue(condition["skipped"])
            before = hashlib.sha256((FIXTURES / "store.sqlite").read_bytes()).hexdigest()
            stub = _run(
                scratch,
                "--tasks", str(FIXTURES / "tasks.json"),
                "--store", str(FIXTURES / "store.sqlite"),
                "--model-id", "recorded-stub-v1",
                "--json-out", str(scratch / "stub.json"),
            )
            self.assertEqual(stub.returncode, 0, stub.stderr)
            after = hashlib.sha256((FIXTURES / "store.sqlite").read_bytes()).hexdigest()
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
