"""Read-only counterfactual replay: the same recorded tasks with and without memory.

The evaluator replays a recorded five-session task set twice - once with the
real passive injection lane active (``ZMEM_INJECT=1``) and once with delivery
disabled (``ZMEM_INJECT=0``) - through the deterministic ``recorded-stub-v1``
model path, and reports the repeated-failure rate plus first-action agreement
per condition.  It is strictly read-only: the store snapshot is opened through
a read-only URI, the injection selector's delivery ledger is redirected to a
scratch directory, and the store SHA-256 is verified before and after the stub
run.  A real model is only ever touched with ``--allow-model-calls``; without
it a non-stub model id prints one skip line and exits 0 without importing or
downloading anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "memory" / "scripts"
EVAL_PIN_TS = "2026-06-01T00:00:00Z"
STUB_MODEL_ID = "recorded-stub-v1"
SKIP_LINE = "SKIPPED: model calls disabled"
TASK_FIELDS = (
    "prompt", "tool_input", "tool_output", "memory_row_id",
    "recorded_successful_action", "recorded_no_memory_action",
    "namespace", "timestamp",
)


class EvalError(ValueError):
    """Operational failure for the stable exit-2 surface."""


def _operator_store_candidates() -> set[Path]:
    """Resolve operator-store aliases before the environment is isolated."""
    env = os.environ
    candidates: set[Path] = set()

    def add(raw: str | os.PathLike[str] | None) -> None:
        if not raw:
            return
        try:
            candidates.add(Path(raw).expanduser().resolve())
        except (OSError, RuntimeError, TypeError, ValueError):
            return

    if env.get("ZMEM_STORE"):
        add(env.get("ZMEM_STORE"))
    elif env.get("ZMEM_DATA"):
        add(Path(env["ZMEM_DATA"]).expanduser() / "store.sqlite")
    elif env.get("CLAUDE_PLUGIN_DATA"):
        add(Path(env["CLAUDE_PLUGIN_DATA"]).expanduser() / "store.sqlite")
    elif env.get("ZCODE_PLUGIN_DATA"):
        add(Path(env["ZCODE_PLUGIN_DATA"]).expanduser() / "store.sqlite")
    home = Path(os.path.expanduser("~"))
    add(home / ".zmem" / "store.sqlite")
    add(home / ".zcode" / "memory" / "store.sqlite")
    return candidates


def _is_operator_store(store: Path, candidates: set[Path]) -> bool:
    resolved = store.expanduser().resolve()
    for candidate in candidates:
        try:
            resolved.relative_to(candidate)
        except ValueError:
            continue
        return True
    return False


def _bootstrap_env(store: str) -> None:
    """Install every store/model/data path before importing ``storelib``."""
    store_path = Path(store).expanduser().resolve()
    for key in list(os.environ):
        if key.startswith("ZMEM_") or key in {"CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"}:
            os.environ.pop(key, None)
    os.environ["ZMEM_STORE"] = str(store_path)
    os.environ["ZMEM_DATA"] = str(store_path.parent / "data")
    os.environ["ZMEM_EMBED_PROFILE"] = "fake"
    os.environ["ZMEM_MODELS_DIR"] = str(store_path.parent / "missing-models")
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    os.environ["ZMEM_TEST_NOW"] = EVAL_PIN_TS
    os.environ["PYTHONUTF8"] = "1"


def _load_tasks(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvalError(f"[eval] invalid tasks set: {exc}\n") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        raise EvalError("[eval] invalid tasks set: top-level 'tasks' list required\n")
    tasks = payload["tasks"]
    for index, task in enumerate(tasks):
        if not isinstance(task, dict) or sorted(task.keys()) != sorted(TASK_FIELDS):
            raise EvalError(
                f"[eval] invalid tasks set: task {index} must carry exactly "
                f"the fields {', '.join(TASK_FIELDS)}\n"
            )
    return tasks


class RecordedToolExecutor:
    """Return each recorded tool output without a subprocess, socket, or network."""

    def __init__(self, tasks: list[dict]) -> None:
        self._outputs: dict[str, str] = {}
        self._memory_row_ids: dict[str, str] = {}
        for task in tasks:
            self._outputs[task["tool_input"]] = task["tool_output"]
            self._memory_row_ids[task["tool_input"]] = task["memory_row_id"]
        self.calls = 0

    def execute(self, tool_input: str) -> str:
        if tool_input not in self._outputs:
            raise EvalError(f"[eval] recorded tool input not found in tasks set\n")
        self.calls += 1
        return self._outputs[tool_input]


def _stub_action(task: dict, fence: str | None) -> str:
    if fence is not None and task["memory_row_id"] in fence:
        return task["recorded_successful_action"]
    return task["recorded_no_memory_action"]


def _session_id(index: int) -> str:
    return f"session-{index + 1:02d}"


def run_condition(
    tasks: list[dict], *, inject: bool, model_id: str, allow_model_calls: bool
) -> dict:
    """Replay every task once under one ZMEM_INJECT condition.

    The caller's ZMEM_INJECT value is saved before anything runs and restored
    in a finally block that wraps the whole body, so an exception mid-replay
    cannot leak the toggled value into the caller's process.
    """
    saved = os.environ.get("ZMEM_INJECT")
    scratch_dir: str | None = None
    conn = None
    try:
        os.environ["ZMEM_INJECT"] = "1" if inject else "0"
        executor = RecordedToolExecutor(tasks)
        actions: list[str] = []
        fences = 0
        sessions: list[dict] = []
        if inject:
            scratch = tempfile.mkdtemp(prefix="zmem-counterfactual-ledger-")
            scratch_dir = scratch
            saved_path = sys.path[:]
            try:
                sys.path.insert(0, str(SCRIPTS))
                from storelib.inject import select_and_budget_for_injection  # type: ignore[import-not-found]
            finally:
                sys.path[:] = saved_path
            conn = sqlite3.connect(
                Path(os.environ["ZMEM_STORE"]).resolve().as_uri() + "?mode=ro",
                uri=True,
            )
            # The storelib recall lane indexes columns by name; a default
            # tuple row factory breaks it and the selector degrades the
            # error into an empty pool.
            conn.row_factory = sqlite3.Row
        for index, task in enumerate(tasks):
            session_id = _session_id(index)
            fence: str | None = None
            if inject:
                envelope = select_and_budget_for_injection(
                    conn,
                    query=task["prompt"],
                    namespace=task["namespace"],
                    moment="user_prompt",
                    session_id=session_id,
                    lane="claude",
                    data_dir=scratch_dir,
                )
                rendered = envelope.get("rendered") or ""
                if rendered.strip():
                    fence = rendered
                    fences += 1
            # The stub consumes the fence text exactly as delivered; the
            # recorded tool exchange is answered from tasks.json bytes.
            actions.append(_stub_action(task, fence))
            executor.execute(task["tool_input"])
            sessions.append({
                "session_id": session_id,
                "task_count": 1,
                "tool_call_count": 1,
            })
        eligible = [t for t in tasks if t.get("recorded_successful_action")]
        matches = sum(
            1 for action, task in zip(actions, tasks)
            if task.get("recorded_successful_action")
            and action == task["recorded_successful_action"]
        )
        failures = sum(
            1 for action, task in zip(actions, tasks)
            if action != task["recorded_successful_action"]
        )
        agreement = matches / len(eligible) if eligible else 0
        return {
            "inject": 1 if inject else 0,
            "repeated_failure_rate": failures / len(tasks) if tasks else 0,
            "first_action_agreement": agreement,
            "task_count": len(tasks),
            "tool_call_count": executor.calls,
            "sessions": sessions,
            "skipped": False,
        }
    finally:
        if conn is not None:
            conn.close()
        if saved is None:
            os.environ.pop("ZMEM_INJECT", None)
        else:
            os.environ["ZMEM_INJECT"] = saved


def _validate_report(report: dict) -> None:
    top = (
        "schema_version", "model_id", "task_count", "tool_call_count",
        "conditions", "generated_at", "skipped",
    )
    if sorted(report.keys()) != sorted(top):
        raise EvalError("[eval] report keys do not match the counterfactual schema\n")
    if report["schema_version"] != 1:
        raise EvalError("[eval] report schema_version must be 1\n")
    if not isinstance(report["model_id"], str) or not report["model_id"]:
        raise EvalError("[eval] report model_id must be a non-empty string\n")
    for count in ("task_count", "tool_call_count"):
        value = report[count]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise EvalError(f"[eval] report {count} must be a non-negative integer\n")
    if not isinstance(report["skipped"], bool):
        raise EvalError("[eval] report skipped must be boolean\n")
    if report["generated_at"] != EVAL_PIN_TS:
        raise EvalError(f"[eval] report generated_at must be {EVAL_PIN_TS}\n")
    conditions = report["conditions"]
    if not isinstance(conditions, list) or len(conditions) != 2:
        raise EvalError("[eval] report must carry exactly two conditions\n")
    condition_keys = (
        "inject", "repeated_failure_rate", "first_action_agreement",
        "task_count", "tool_call_count", "sessions", "skipped",
    )
    for condition in conditions:
        if sorted(condition.keys()) != sorted(condition_keys):
            raise EvalError("[eval] condition keys do not match the counterfactual schema\n")
        if condition["inject"] not in (0, 1):
            raise EvalError("[eval] condition inject must be 0 or 1\n")
        for rate in ("repeated_failure_rate", "first_action_agreement"):
            value = condition[rate]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0.0 <= value <= 1.0:
                raise EvalError(f"[eval] condition {rate} must be a number in [0, 1]\n")
        if not isinstance(condition["skipped"], bool):
            raise EvalError("[eval] condition skipped must be boolean\n")
        if not isinstance(condition["sessions"], list):
            raise EvalError("[eval] condition sessions must be a list\n")
        for session in condition["sessions"]:
            if sorted(session.keys()) != ["session_id", "task_count", "tool_call_count"]:
                raise EvalError("[eval] session keys do not match the counterfactual schema\n")
            if not isinstance(session["session_id"], str) or not session["session_id"]:
                raise EvalError("[eval] session_id must be a non-empty string\n")


def _report_bytes(report: dict) -> bytes:
    return (json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval_counterfactual.py")
    parser.add_argument("--tasks", dest="tasks", type=str, required=True, help="recorded counterfactual task set")
    parser.add_argument("--store", dest="store", type=str, required=True, help="isolated read-only store snapshot")
    parser.add_argument("--model-id", dest="model_id", type=str, required=True, help="pinned model identifier")
    parser.add_argument("--allow-model-calls", dest="allow_model_calls", action="store_true", default=False, help="permit real model calls outside CI")
    parser.add_argument("--json-out", dest="json_out", type=str, default=None, help="write the counterfactual report")
    return parser


def main() -> int:
    args = _parser().parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    try:
        candidates = _operator_store_candidates()
        try:
            store_path = Path(args.store).expanduser().resolve()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise EvalError(f"[eval] invalid --store path: {exc}\n") from exc
        if _is_operator_store(store_path, candidates):
            sys.stderr.write("[eval] refusing operator home store; pass an isolated --store path\n")
            return 2
        if not store_path.is_file():
            raise EvalError("[eval] --store must be an existing store snapshot\n")
        tasks_path = Path(args.tasks).expanduser().resolve()
        if _is_operator_store(tasks_path, candidates) or not tasks_path.is_file():
            raise EvalError("[eval] --tasks must be an existing tasks file\n")
        tasks = _load_tasks(tasks_path)
        out = Path(args.json_out).expanduser().resolve() if args.json_out else None
        is_stub = args.model_id == STUB_MODEL_ID
        if not is_stub and not args.allow_model_calls:
            print(SKIP_LINE)
            if out is not None:
                report = {
                    "schema_version": 1,
                    "model_id": args.model_id,
                    "task_count": 0,
                    "tool_call_count": 0,
                    "conditions": [
                        {"inject": flag, "repeated_failure_rate": 0, "first_action_agreement": 0,
                         "task_count": 0, "tool_call_count": 0, "sessions": [], "skipped": True}
                        for flag in (1, 0)
                    ],
                    "generated_at": EVAL_PIN_TS,
                    "skipped": True,
                }
                _validate_report(report)
                _write_atomic(out, _report_bytes(report))
            return 0
        if not is_stub:
            sys.stderr.write(f"[eval] no adapter registered for model id: {args.model_id}\n")
            return 2
        _bootstrap_env(str(store_path))
        store_sha_before = hashlib.sha256(store_path.read_bytes()).hexdigest()
        conditions = [
            run_condition(tasks, inject=flag, model_id=args.model_id, allow_model_calls=args.allow_model_calls)
            for flag in (True, False)
        ]
        if hashlib.sha256(store_path.read_bytes()).hexdigest() != store_sha_before:
            raise EvalError("[eval] store bytes changed during the stub run\n")
        report = {
            "schema_version": 1,
            "model_id": args.model_id,
            "task_count": len(tasks),
            "tool_call_count": conditions[0]["tool_call_count"] + conditions[1]["tool_call_count"],
            "conditions": conditions,
            "generated_at": EVAL_PIN_TS,
            "skipped": False,
        }
        _validate_report(report)
        if out is not None:
            _write_atomic(out, _report_bytes(report))
        return 0
    except EvalError as exc:
        message = str(exc)
        if not message.endswith("\n"):
            message += "\n"
        sys.stderr.write(message)
        return 2
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        sys.stderr.write(f"[eval] evaluation failed: {type(exc).__name__}\n")
        return 2


def _write_atomic(path: Path, data: bytes) -> None:
    if path.parent and not path.parent.is_dir():
        raise EvalError("[eval] --json-out parent directory does not exist\n")
    import tempfile as _tempfile

    fd, temporary = _tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise EvalError(f"[eval] cannot write report: {exc}\n") from exc


if __name__ == "__main__":
    sys.exit(main())
