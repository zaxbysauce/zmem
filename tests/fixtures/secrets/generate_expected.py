#!/usr/bin/env python3
"""Deterministically generate the issue #180 secrets-fixture EXPECTED files.

Run from the repository root AFTER generate_inputs.py:

    python tests/fixtures/secrets/generate_expected.py

Writes, under tests/fixtures/secrets/:
  - patterns.expected.json        actual auto-mode policy output per input
  - sshpass.expected.jsonl        the normalized two-row ingest outcome
  - quarantine_row.expected.jsonl the canonical quarantine record at the
                                  fixed time 2026-09-10T12:00:00Z

Every file is written LF-pinned; one sha256 digest per file is printed. The
expected outputs are DERIVED from the shared capture policy (the issue's own
design): the PR description records the printed digests, and the tests compare
generated-vs-actual byte-for-byte after CRLF normalization.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parent
SCRIPTS = REPO / "skills" / "memory" / "scripts"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "storelib"))

FIXED_NOW = "2026-09-10T12:00:00Z"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def write_text_lf(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _import_policy():
    # Pin the scratch env BEFORE importing storelib: storelib freezes
    # STORE_PATH at import time and must never see the operator's real store.
    tmp = tempfile.mkdtemp(prefix="zmem180-fixexp-")
    os.environ["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    os.environ["ZMEM_DATA"] = tmp
    os.environ["ZMEM_MODELS_DIR"] = os.path.join(tmp, "missing-models")
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    from storelib.write import (CapturePolicyRefusal, apply_capture_policy,
                                quarantine_import_row)
    return tmp, apply_capture_policy, quarantine_import_row, CapturePolicyRefusal


def generate_patterns_expected(apply_capture_policy, CapturePolicyRefusal) -> None:
    matrix = json.loads((FIXTURE_DIR / "patterns.json").read_text(
        encoding="utf-8"))
    out = {"positive": [], "negative": []}
    for case in matrix["positive"]:
        entry = dict(case)
        if case["outcome"] == "quarantine":
            try:
                apply_capture_policy(content=case["input"], source_ref="",
                                     tags="", capture_mode="auto")
            except CapturePolicyRefusal as exc:
                entry["refusal_reason"] = exc.reason
            out["positive"].append(entry)
            continue
        content, _ref, _tags, _warns = apply_capture_policy(
            content=case["input"], source_ref="", tags="", capture_mode="auto")
        entry["expected"] = content
        out["positive"].append(entry)
    for case in matrix["negative"]:
        entry = dict(case)
        content, _ref, _tags, _warns = apply_capture_policy(
            content=case["input"], source_ref="", tags="", capture_mode="auto")
        entry["expected"] = content
        out["negative"].append(entry)
    write_text_lf(FIXTURE_DIR / "patterns.expected.json",
                  json.dumps(out, ensure_ascii=False, indent=2) + "\n")


def generate_sshpass_expected(apply_capture_policy, CapturePolicyRefusal) -> None:
    rows = [json.loads(line) for line in
            (FIXTURE_DIR / "sshpass.jsonl").read_text(
                encoding="utf-8").splitlines() if line.strip()]
    lines = []
    for row in rows:
        try:
            content, _ref, tags, _warns = apply_capture_policy(
                content=row["content"], source_ref=row["source_ref"],
                tags=row["tags"], capture_mode="auto")
        except CapturePolicyRefusal as exc:
            lines.append(json.dumps({
                "id": None,
                "result": "quarantined",
                "warnings": [{"type": "quarantined", "reason": exc.reason}],
            }, ensure_ascii=False, separators=(",", ":")))
            continue
        lines.append(json.dumps({
            "id": row["id"],
            "result": "stored",
            "content": content,
            "tags": tags,
        }, ensure_ascii=False, separators=(",", ":")))
    write_text_lf(FIXTURE_DIR / "sshpass.expected.jsonl", "\n".join(lines) + "\n")


def generate_quarantine_expected(quarantine_import_row) -> Path:
    row = json.loads((FIXTURE_DIR / "quarantine_row.jsonl").read_text(
        encoding="utf-8").splitlines()[0])
    tmp = tempfile.mkdtemp(prefix="zmem180-fixq-")
    try:
        target = quarantine_import_row(
            tmp, row, reason="source_ref_secret_like", now=FIXED_NOW)
        shutil.copyfile(target, FIXTURE_DIR / "quarantine_row.expected.jsonl")
        return Path(FIXTURE_DIR / "quarantine_row.expected.jsonl")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if REPO.resolve() != Path.cwd().resolve():
        print("run me from the repository root: "
              "python tests/fixtures/secrets/generate_expected.py")
        return 2
    tmp, apply_capture_policy, quarantine_import_row, CapturePolicyRefusal = (
        _import_policy())
    try:
        generate_patterns_expected(apply_capture_policy, CapturePolicyRefusal)
        generate_sshpass_expected(apply_capture_policy, CapturePolicyRefusal)
        generate_quarantine_expected(quarantine_import_row)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for name in ("patterns.expected.json", "sshpass.expected.jsonl",
                 "quarantine_row.expected.jsonl"):
        digest = sha256_of(FIXTURE_DIR / name)
        print(f"{digest}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
