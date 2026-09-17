"""Build deterministic schema-14 evidence transport fixtures.

The generator writes only to the required scratch ``--out`` directory and
uses temporary source/destination stores.  It never reads or mutates the
operator store or the committed fixture oracle.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parent
SCRIPTS = ROOT / "skills" / "memory" / "scripts"

EVIDENCE_ID_1 = "00000000-0000-4000-8000-000000000001"
EVIDENCE_ID_2 = "00000000-0000-4000-8000-000000000002"
EPISODE_ID = "00000000-0000-4000-8000-000000000101"
MEMORY_ID = "00000000-0000-4000-8000-000000000201"
SESSION_ID = "00000000-0000-4000-8000-000000000301"
TS = "2026-09-10T00:00:00Z"
HOSTS = ("claude", "codex", "zcode")
HERMES_SESSION = "fixture-session-hermes"
_ROUTE_ENV_KEYS = (
    "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODEL_URL",
    "ZMEM_EMBED_PROFILE", "ZMEM_CROSS_ENCODER_MODEL", "HOME", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA",
)


@contextlib.contextmanager
def _isolated_env(scratch: Path):
    saved = {key: os.environ.get(key) for key in _ROUTE_ENV_KEYS}
    for key in _ROUTE_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.update({
        "ZMEM_STORE": str(scratch / "source.sqlite"),
        "ZMEM_DATA": str(scratch),
        "ZMEM_MODELS_DIR": str(scratch / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "HOME": str(scratch / "home"),
        "USERPROFILE": str(scratch / "home"),
        "APPDATA": str(scratch / "appdata"),
        "LOCALAPPDATA": str(scratch / "localappdata"),
    })
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _seed_parents(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO memory "
        "(id, namespace, type, content, tags, source_ref, source_hash, confidence, "
        "signal, valid_from, valid_until, update_of, taint, superseded_at, ingestion_ts, "
        "retrieval_count, surfaced_count, merged_from, content_norm, trust_score, "
        "applied_count, violated_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "NULL, ?, 0, 0, NULL, ?, 1.0, 0, 0)",
        (MEMORY_ID, "project:fixture", "fact", "Deterministic evidence parent",
         "", "", "", 0.9, "test", TS, "", "", "trusted_internal", TS,
         "deterministic evidence parent"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO episode "
        "(id, namespace, started_at, ended_at, summary_memory_id, token_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (EPISODE_ID, "project:fixture", TS, TS, MEMORY_ID, 3),
    )
    conn.commit()


def _input_lines() -> bytes:
    return (
        '{"table":"evidence","id":"00000000-0000-4000-8000-000000000001",'
        '"session_id":"00000000-0000-4000-8000-000000000301","lane":"codex",'
        '"moment":"user_prompt","kind":"turn","ts":"2026-09-10T00:00:00Z",'
        '"hash":"bb83bc8a5a493b723ee6929f001baa627bab834bd73ac8e50bd14fb7c84e54ad",'
        '"excerpt":"First turn for deterministic fixture.",'
        '"ref_path":"fixtures/169/session.txt","ref_offset":0}\n'
        '{"table":"evidence","id":"00000000-0000-4000-8000-000000000002",'
        '"session_id":"00000000-0000-4000-8000-000000000301","lane":"zcode",'
        '"moment":"pretool","kind":"tool_call","ts":"2026-09-10T00:00:00Z",'
        '"hash":"73e778a2d3f86e77c9794d889600194e36cd8cdf610c1266fb804ed77a964551",'
        '"excerpt":"Tool call completed for deterministic fixture.",'
        '"ref_path":"fixtures/169/tools.jsonl","ref_offset":1}\n'
        '{"table":"episode_evidence","episode_id":"00000000-0000-4000-8000-000000000101",'
        '"evidence_id":"00000000-0000-4000-8000-000000000001"}\n'
        '{"table":"memory_evidence","memory_id":"00000000-0000-4000-8000-000000000201",'
        '"evidence_id":"00000000-0000-4000-8000-000000000002"}\n'
    ).encode("utf-8")


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _evidence_expected(
    *, evidence_id: str, session_id: str, lane: str, moment: str,
    kind: str, excerpt: str, ref_path: str,
) -> dict[str, object]:
    digest = hashlib.sha256(f"{kind}|{TS}|{excerpt}".encode("utf-8")).hexdigest()
    return {
        "id": evidence_id,
        "session_id": session_id,
        "lane": lane,
        "moment": moment,
        "kind": kind,
        "ts": TS,
        "hash": digest,
        "excerpt": excerpt,
        "ref_path": ref_path,
        "ref_offset": None,
    }


def _host_fixture_candidates(out: Path) -> list[Path]:
    """Write host/Hermes candidates under scratch ``out`` only.

    Committed expected arrays are reviewed literals.  This function deliberately
    uses ``candidate-`` names so running the generator cannot overwrite them.
    """
    written: list[Path] = []
    launcher_inputs: dict[str, list[dict[str, object]]] = {}
    for host in HOSTS:
        session = f"fixture-session-{host}"
        ref_path = f"fixture/{host}.json"
        base = 1 if host == "claude" else 2 if host == "codex" else 3
        events = (
            ("convention-capture", "git status", "tool_call"),
            ("convention-capture", "python -m unittest", "tool_call"),
            ("capture-failure", "exit 1", "tool_failure"),
            ("reflect", "complete", "turn"),
        )
        inputs: list[dict[str, object]] = []
        expected: list[dict[str, object]] = []
        for offset, (hook, command, kind) in enumerate(events, 1):
            evidence_id = f"00000000-0000-4000-8000-000000000{base}{offset:02d}"
            if hook == "reflect":
                payload: dict[str, object] = {
                    "session_id": session, "stop_hook_active": False,
                    "result": command, "evidence_id": evidence_id,
                    "ref_path": ref_path,
                }
                moment = "session_start"
                excerpt = f"turn={command}"
            else:
                payload = {
                    "session_id": session, "tool_name": "Bash",
                    "tool_input": {"command": command},
                    "evidence_id": evidence_id, "ref_path": ref_path,
                }
                moment = "pretool"
                excerpt = ("error=permission denied" if kind == "tool_failure" else command)
            inputs.append({"hook": hook, "payload": payload})
            expected.append(_evidence_expected(
                evidence_id=evidence_id, session_id=session, lane=host,
                moment=moment, kind=kind, excerpt=excerpt, ref_path=ref_path,
            ))
        launcher_inputs[host] = inputs
        input_path = out / f"launcher-inputs-{host}.jsonl"
        input_path.write_bytes(b"".join(_canonical_json(item) for item in inputs))
        written.append(input_path)
        expected_path = out / f"candidate-expected-{host}.json"
        expected_path.write_bytes(_canonical_json(expected))
        written.append(expected_path)

    hermes = [
        {"tool_name": "shell", "args": {"command": "git status"},
         "session_id": HERMES_SESSION, "task_id": "task-170",
         "tool_call_id": "call-170-1", "result": {"status": "success"}, "duration_ms": 10},
        {"tool_name": "shell", "args": {"command": "python -m unittest"},
         "session_id": HERMES_SESSION, "task_id": "task-170",
         "tool_call_id": "call-170-2", "result": {"status": "success"}, "duration_ms": 11},
        {"tool_name": "shell", "args": {"command": "exit 1"},
         "session_id": HERMES_SESSION, "task_id": "task-170",
         "tool_call_id": "call-170-3", "result": {"status": "error"}, "duration_ms": 12},
    ]
    hermes_path = out / "hermes-post-tool.json"
    hermes_path.write_bytes(_canonical_json(hermes))
    written.append(hermes_path)
    for lane, prefix, width in (("hermes-provider", "4", 2), ("hermes-compat", "41", 1)):
        expected: list[dict[str, object]] = []
        for offset, event in enumerate(hermes, 1):
            result = event["result"]
            assert isinstance(result, dict)
            kind = "tool_failure" if result.get("status") == "error" else "tool_call"
            excerpt = json.dumps({
                "tool_name": event["tool_name"], "args": event["args"],
                "result": result, "duration_ms": event["duration_ms"],
            }, ensure_ascii=False, separators=(",", ":"))
            expected.append(_evidence_expected(
                evidence_id=f"00000000-0000-4000-8000-000000000{prefix}{offset:0{width}d}",
                session_id=HERMES_SESSION, lane=lane, moment="pretool", kind=kind,
                excerpt=excerpt, ref_path=f"hermes://task-170/call-170-{offset}",
            ))
        expected_path = out / f"candidate-expected-{lane}.json"
        expected_path.write_bytes(_canonical_json(expected))
        written.append(expected_path)
    launcher_path = out / "launcher-inputs.json"
    launcher_path.write_bytes(_canonical_json(launcher_inputs))
    written.append(launcher_path)
    return written


def build(out: Path) -> tuple[str, ...]:
    out = out.expanduser().resolve()
    if out == FIXTURE_DIR.resolve():
        raise ValueError("--out must be a scratch directory, not the committed fixture directory")
    out.mkdir(parents=True, exist_ok=True)
    input_path = out / "input.jsonl"
    expected_path = out / "export.expected.jsonl"
    input_path.write_bytes(_input_lines())

    with tempfile.TemporaryDirectory(prefix="zmem-evidence-fixture-") as td, \
            _isolated_env(Path(td)):
        scratch = Path(td)
        sys.path.insert(0, str(SCRIPTS))
        from storelib.evidence import write_evidence
        from storelib.schema import init_db, migrate
        from storelib.sync import cmd_export_jsonl, cmd_ingest_jsonl_strict

        source = sqlite3.connect(os.environ["ZMEM_STORE"])
        source.row_factory = sqlite3.Row
        init_db(source)
        migrate(source)
        _seed_parents(source)
        write_evidence(
            source, session_id=SESSION_ID, lane="codex", moment="user_prompt",
            kind="turn", ts=TS, excerpt="First turn for deterministic fixture.",
            ref_path="fixtures/169/session.txt", ref_offset=0, id=EVIDENCE_ID_1,
        )
        write_evidence(
            source, session_id=SESSION_ID, lane="zcode", moment="pretool",
            kind="tool_call", ts=TS,
            excerpt="Tool call completed for deterministic fixture.",
            ref_path="fixtures/169/tools.jsonl", ref_offset=1, id=EVIDENCE_ID_2,
        )
        source.execute(
            "INSERT INTO episode_evidence (episode_id, evidence_id) VALUES (?, ?)",
            (EPISODE_ID, EVIDENCE_ID_1),
        )
        source.execute(
            "INSERT INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
            (MEMORY_ID, EVIDENCE_ID_2),
        )
        source.commit()
        source_export = scratch / "source.jsonl"
        cmd_export_jsonl(source, out=str(source_export))
        source_bytes = source_export.read_bytes()
        source.close()

        destination_path = scratch / "destination.sqlite"
        os.environ["ZMEM_STORE"] = str(destination_path)
        destination = sqlite3.connect(destination_path)
        destination.row_factory = sqlite3.Row
        init_db(destination)
        migrate(destination)
        _seed_parents(destination)
        if cmd_ingest_jsonl_strict(
            destination, in_path=str(input_path), source_ref=None,
        ) != 0:
            raise RuntimeError("strict evidence fixture import failed")
        destination_export = scratch / "destination.jsonl"
        cmd_export_jsonl(destination, out=str(destination_export))
        destination_bytes = destination_export.read_bytes()
        destination.close()
        if destination_bytes != source_bytes:
            raise RuntimeError("evidence fixture export is not byte-stable")
        expected_path.write_bytes(destination_bytes)
        candidates = _host_fixture_candidates(out)
    return (
        f"input.jsonl sha256={hashlib.sha256(input_path.read_bytes()).hexdigest()}",
        f"export.expected.jsonl sha256={hashlib.sha256(expected_path.read_bytes()).hexdigest()}",
        *[f"{path.name} sha256={hashlib.sha256(path.read_bytes()).hexdigest()}" for path in candidates],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out", type=Path, required=True,
        help="scratch output directory; the committed fixture directory is rejected",
    )
    args = parser.parse_args()
    try:
        digests = build(args.out)
    except ValueError as exc:
        parser.error(str(exc))
    for digest in digests:
        print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
