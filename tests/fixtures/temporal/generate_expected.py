"""Deterministic expected-range generator for the issue #178 phrase fixture.

Usage:
    python tests/fixtures/temporal/generate_expected.py \
        tests/fixtures/temporal/phrases.json \
        tests/fixtures/temporal/phrases.expected.json

No clock reads: every record in ``phrases.json`` carries its own fixed
``now`` (the issue pins all twenty to 2026-09-10T15:30:00Z), and that value
is the ONLY timestamp passed to ``storelib.temporal.parse_relative_range``.
The output is a compact UTF-8 JSON array with exactly one trailing LF,
holding ``phrase`` / ``expected_start`` / ``expected_end`` (the DERIVED
values, in input order); the output SHA-256 is printed to stdout.

House isolation contract (the tests/test_auto_retain.py pattern from the
#177 work): the store environment is pinned at the VERY TOP, before any
storelib import — the generator never opens a store, but importing
``storelib.temporal`` loads the ``storelib`` package, whose ``schema``
module freezes STORE_PATH at import time. No ``~/.zmem`` access, ever.
"""

from __future__ import annotations

import os
import sys
import tempfile

# --- env pin BEFORE any storelib import (house pattern) --------------------
_ISOLATION = tempfile.mkdtemp(prefix="zmem-178-gen-")
os.environ["ZMEM_STORE"] = os.path.join(_ISOLATION, "store.sqlite")
os.environ["ZMEM_DATA"] = _ISOLATION
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_ISOLATION, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_SCRIPTS = os.path.join(_REPO_ROOT, "skills", "memory", "scripts")
for _path in (_SCRIPTS, os.path.join(_SCRIPTS, "storelib")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from storelib.temporal import parse_relative_range  # noqa: E402


def derive_records(phrases_path):
    """Run the parser over each fixture record; no clock, no store."""
    import json

    with open(phrases_path, "r", encoding="utf-8") as handle:
        records = json.load(handle)
    if not isinstance(records, list) or not records:
        raise SystemExit("phrases fixture must be a non-empty JSON array")
    derived = []
    for index, record in enumerate(records):
        start, end = parse_relative_range(
            record["phrase"], prompt_timestamp=record["now"])
        if not start or not end:
            raise SystemExit(
                "phrase %d (%r) derived an empty range against now=%r"
                % (index, record["phrase"], record["now"]))
        derived.append({
            "phrase": record["phrase"],
            "expected_start": start,
            "expected_end": end,
        })
    return derived


def main(argv):
    if len(argv) != 3:
        raise SystemExit(
            "usage: python tests/fixtures/temporal/generate_expected.py "
            "<phrases.json> <phrases.expected.json>")
    phrases_path, output_path = argv[1], argv[2]
    records = derive_records(phrases_path)
    import hashlib
    import json

    payload = json.dumps(
        records, ensure_ascii=False, separators=(",", ":")) + "\n"
    data = payload.encode("utf-8")
    with open(output_path, "wb") as handle:
        handle.write(data)
    print(hashlib.sha256(data).hexdigest())


if __name__ == "__main__":
    main(sys.argv)
