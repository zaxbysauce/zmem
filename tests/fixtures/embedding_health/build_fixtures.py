"""Deterministic generator for the issue #131 embedding-health fixtures.

Writes all five fixture files byte-exactly (idempotent: re-running produces
identical bytes, so the committed digests never drift across platforms):

  - tampered-minilm.onnx  — a model-shaped file whose bytes can NEVER match
    the pinned ``_MODEL_SHA256``; placing it in a models dir alongside
    tokenizer.json drives the ``model_checksum_mismatch`` degradation path
    (the load-path checksum gate in ``embeddings._ensure_loaded``).
  - tokenizer.json        — the minimal valid-tokenizers-JSON shape the
    existing load-path tests use (tests/test_model_fallback.py writes
    ``b"{}"``); presence is all ``_check_available`` probes for.
  - verified-model.json   — operator-facing provenance stub for the fixture
    pair (the real pin lives in ``embeddings._MODEL_SHA256``).
  - expected-degraded.json — the EXACT compact sorted-key JSON line
    ``store.py embedding-health --json`` prints for the degraded checksum
    case against the fixture store. The ``models_dir`` field is pinned as
    the literal ``fixture-models``: the runtime command prints the RESOLVED
    models dir, so the consuming test rewrites that ONE field before the
    byte comparison — every other field is byte-pinned by this file.
  - expected-active.json   — the same payload shape for the ACTIVE vector
    lane (canary lane_health with a present model pair and a seeded
    watermark); ``last_successful_index`` is seeded via the meta table.

Prints the SHA-256 of both expected-*.json files so the pinned digests in
tests/test_model_fallback.py / tests/test_host_canary.py can be refreshed
deliberately (never by hand-editing the JSON).

Run:  python tests/fixtures/embedding_health/build_fixtures.py [--out DIR]
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

TAMPERED_MINILM_ONNX = b"not-a-model\n"

# Minimal valid tokenizers JSON — the exact shape the existing load-path
# fixtures in tests/test_model_fallback.py write for tokenizer.json.
TOKENIZER_JSON = b"{}"

VERIFIED_MODEL_JSON = (
    b'{"sha256":"fixture-sha256","dim":384,"profile":"minilm"}\n')

# `store.py embedding-health --json` output for the tampered-model case.
# Compact (no spaces), sorted keys, exactly one LF-terminated line. The
# `models_dir` value is the literal sentinel `fixture-models` — see the
# module docstring for how the consuming test substitutes the resolved dir.
# `notice_created` is pinned FALSE because it matches the real runtime: for
# a tampered model the command's FORCED `_ensure_loaded()` runs first and
# its own `_maybe_notice_degraded()` seam is the process's first notice
# creator — the explicit `degraded_notice()` call in cmd_embedding_health
# then loses the O_EXCL race and reports created:false while
# notice_present stays true (PR #306 review finding).
EXPECTED_DEGRADED_JSON = (
    b'{"last_successful_embed":null,"last_successful_index":null,'
    b'"models_dir":"fixture-models","notice_created":false,'
    b'"notice_present":true,"profile":"minilm",'
    b'"reason":"model_checksum_mismatch","unembedded_backlog":1,'
    b'"vector_status":"degraded"}\n')

# The active-lane payload (a canary lane_health vector object rendered in
# the same key set the store command prints).
EXPECTED_ACTIVE_JSON = (
    b'{"last_successful_embed":"2026-09-10T00:00:00Z",'
    b'"last_successful_index":"2026-09-10T00:00:00Z",'
    b'"models_dir":"fixture-models","notice_created":false,'
    b'"notice_present":false,"profile":"minilm","reason":null,'
    b'"unembedded_backlog":0,"vector_status":"active"}\n')

FILES = (
    ("tampered-minilm.onnx", TAMPERED_MINILM_ONNX),
    ("tokenizer.json", TOKENIZER_JSON),
    ("verified-model.json", VERIFIED_MODEL_JSON),
    ("expected-degraded.json", EXPECTED_DEGRADED_JSON),
    ("expected-active.json", EXPECTED_ACTIVE_JSON),
)

# Digests of the two byte-contract JSON fixtures (recorded here so a
# regeneration that changes them is visible in the diff of this file too).
PINNED_SHA256 = {
    "expected-degraded.json":
        "2ffa904ee8e68bbac551ccd45c96e618e23253b4ddb48c73b029765e922f334f",
    "expected-active.json":
        "04f0465bd0c7a01e0f1807785dec0348216bb23bc497e3579e7eb0592c9bcc61",
}


def build(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in FILES:
        (out_dir / name).write_bytes(payload)
    digest_degraded = hashlib.sha256(EXPECTED_DEGRADED_JSON).hexdigest()
    digest_active = hashlib.sha256(EXPECTED_ACTIVE_JSON).hexdigest()
    print("expected-degraded.json sha256=%s" % digest_degraded)
    print("expected-active.json   sha256=%s" % digest_active)
    assert digest_degraded == PINNED_SHA256["expected-degraded.json"]
    assert digest_active == PINNED_SHA256["expected-active.json"]
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--out", default=str(Path(__file__).resolve().parent),
        help="output directory (default: this fixture directory)")
    raise SystemExit(build(Path(ap.parse_args().out)))
