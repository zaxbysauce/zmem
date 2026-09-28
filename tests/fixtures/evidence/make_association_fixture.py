"""Generate the deterministic issue #171 association transport fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parent
ASSOCIATION_INPUT_NAME = "association-input.jsonl"
STORE = ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable

TS = "2026-09-10T00:00:00Z"
NAMESPACE = "fixture:evidence-171"
SESSION_ID = "00000000-0000-4000-8000-000000000191"
MEMORY_1 = "00000000-0000-4000-8000-000000000171"
MEMORY_2 = "00000000-0000-4000-8000-000000000172"
EVIDENCE_1 = "00000000-0000-4000-8000-000000000181"
EVIDENCE_2 = "00000000-0000-4000-8000-000000000182"
EVIDENCE_3 = "00000000-0000-4000-8000-000000000183"


def _line(value: dict[str, object]) -> bytes:
    rendered = json.dumps(value, ensure_ascii=False)
    return (rendered.replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
            .replace("\u0085", "\\u0085") + "\n").encode("utf-8")


def _memory(memory_id: str, content: str) -> dict[str, object]:
    return {
        "kind": "memory", "id": memory_id, "namespace": NAMESPACE,
        "type": "fact", "content": content, "tags": "", "source_ref": "",
        "confidence": 0.9, "signal": "test", "valid_from": TS,
        "valid_until": "", "update_of": "", "taint": "trusted_internal",
        "ingestion_ts": TS, "superseded_at": None, "supersede_reason": "",
        "merged_from": None, "trust_score": 1.0, "applied_count": 0,
        "violated_count": 0, "links": [],
    }


def _evidence(evidence_id: str, *, lane: str, moment: str, kind: str,
              excerpt: str, ref_path: str, ref_offset: int) -> dict[str, object]:
    digest = hashlib.sha256(f"{kind}|{TS}|{excerpt}".encode("utf-8")).hexdigest()
    return {
        "table": "evidence", "id": evidence_id, "session_id": SESSION_ID,
        "lane": lane, "moment": moment, "kind": kind, "ts": TS,
        "hash": digest, "excerpt": excerpt, "ref_path": ref_path,
        "ref_offset": ref_offset,
    }


def _input_bytes() -> bytes:
    rows: list[dict[str, object]] = [
        _memory(MEMORY_1, "fixture memory one"),
        _memory(MEMORY_2, "fixture memory two"),
        _evidence(
            EVIDENCE_1, lane="claude", moment="pretool", kind="tool_call",
            excerpt="command=git status", ref_path="hooks/zmem-session-start.sh",
            ref_offset=1,
        ),
        _evidence(
            EVIDENCE_2, lane="codex", moment="pretool", kind="tool_failure",
            excerpt="exit=1", ref_path="hooks/zmem-launch.js", ref_offset=2,
        ),
        _evidence(
            EVIDENCE_3, lane="zcode", moment="user_prompt", kind="turn",
            excerpt="turn complete", ref_path="tests/fixtures/evidence/association-input.jsonl",
            ref_offset=3,
        ),
        {"table": "memory_evidence", "memory_id": MEMORY_1, "evidence_id": EVIDENCE_1},
        {"table": "memory_evidence", "memory_id": MEMORY_1, "evidence_id": EVIDENCE_2},
        {"table": "memory_evidence", "memory_id": MEMORY_2, "evidence_id": EVIDENCE_3},
    ]
    return b"".join(_line(row) for row in rows)


def _env(scratch: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "ZMEM_STORE": str(scratch / "store.sqlite"),
        "ZMEM_DATA": str(scratch),
        "ZMEM_MODELS_DIR": str(scratch / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_HOME": str(ROOT),
    })
    for key in ("ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE"):
        env.pop(key, None)
    return env


def build(out: Path) -> tuple[str, str]:
    out = out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    input_path = out / ASSOCIATION_INPUT_NAME
    expected_path = out / "expected-association.jsonl"
    input_path.write_bytes(_input_bytes())
    with tempfile.TemporaryDirectory(prefix="zmem-171-fixture-store-") as td:
        scratch = Path(td)
        env = _env(scratch)
        initialized = subprocess.run(
            [PYTHON, str(STORE), "init"], cwd=ROOT, env=env,
            text=True, capture_output=True, timeout=60,
        )
        if initialized.returncode != 0:
            raise RuntimeError(initialized.stderr)
        imported = subprocess.run(
            [PYTHON, str(STORE), "ingest-jsonl", "--in", str(input_path)],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=60,
        )
        if imported.returncode != 0:
            raise RuntimeError(imported.stderr)
        exported = subprocess.run(
            [PYTHON, str(STORE), "export-jsonl", "--out", str(expected_path)],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=60,
        )
        if exported.returncode != 0:
            raise RuntimeError(exported.stderr)
    return (
        hashlib.sha256(input_path.read_bytes()).hexdigest(),
        hashlib.sha256(expected_path.read_bytes()).hexdigest(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=FIXTURE_DIR,
                        help="fixture directory (defaults to the checked-in path)")
    args = parser.parse_args()
    input_digest, expected_digest = build(args.out)
    print(f"{ASSOCIATION_INPUT_NAME} sha256={input_digest}")
    print(f"expected-association.jsonl sha256={expected_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
