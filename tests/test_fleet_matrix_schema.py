"""Issue #363 AC3 — the fleet capability matrix grows a committed JSON
Schema and a dependency-free guardrail that validates the artifact against
it.

``docs/fleet-capability-matrix.schema.json`` (committed beside the matrix)
describes the matrix: required top-level keys, the rows array with its
required fields, input objects closed to {name, locality} with the locality
enum taken from the values actually present in the committed matrix
(client_local | hub_authoritative), the hub_ops enum, and
``additionalProperties: false`` on both the row and the input objects — so
a renamed or invented key is a validation error, not a silent extra.

This file does NOT import jsonschema (the check is stdlib-only, per the
frozen test-command contract): it ships a mini-validator for the draft
2020-12 subset the schema uses — ``type`` (string or list), ``enum``,
``required``, ``properties``, ``additionalProperties: false`` and ``items``
— and asserts:

1. the committed matrix validates against the committed schema;
2. a row carrying an unknown field (or an off-enum locality/hub_op) is
   rejected by name;
3. a row with a REQUIRED key renamed away (harness -> harness_name) is
   rejected for the missing key, not silently accepted;
4. the matrix carries a top-level "$schema" key pointing at the committed
   schema file.  This is the RED at base: the committed matrix predates
   the #363 update and has no "$schema" key yet; once the matrix update
   lands, the pointer resolves to the file beside it.

Checks 1-3 hold at base AND must keep holding after the change (the schema
is written against the matrix's existing shape, with "$schema"/"schema"
allowed so the #363 addition validates too).

Runs under pytest and standalone (CI convention):
``python -X utf8 tests/test_fleet_matrix_schema.py``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX_PATH = REPO_ROOT / "docs" / "fleet-capability-matrix.json"
SCHEMA_PATH = REPO_ROOT / "docs" / "fleet-capability-matrix.schema.json"

# --- the stdlib mini-validator (draft 2020-12 subset) -----------------------
# Keywords exercised by the committed schema: type (string or list of
# strings), enum (type-strict membership), required, properties,
# additionalProperties: false, items.  Unknown keywords (title,
# description, $id, $schema) are annotations and ignored.


def _type_ok(value, name):
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    raise AssertionError(f"mini-validator: unsupported type name {name!r}")


def _type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _enum_ok(value, choices):
    # Type-strict: True must not satisfy an enum containing 1.
    return any(type(value) is type(cand) and value == cand
               for cand in choices)


def validate(document, schema, path="$"):
    """Return a list of human-readable validation errors (empty = valid)."""
    errors: list[str] = []
    declared = schema.get("type")
    if declared is not None:
        names = [declared] if isinstance(declared, str) else list(declared)
        if not any(_type_ok(document, name) for name in names):
            errors.append(
                f"{path}: {_type_name(document)} does not match type "
                f"{names}")
            return errors  # wrong shape: deeper checks are meaningless
    if "enum" in schema and not _enum_ok(document, schema["enum"]):
        errors.append(
            f"{path}: {document!r} is not one of {schema['enum']!r}")
    if isinstance(document, dict):
        for key in schema.get("required", ()):
            if key not in document:
                errors.append(f"{path}: missing required key {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(document) - set(properties))
            if unknown:
                errors.append(
                    f"{path}: unknown key(s) {unknown} "
                    f"(additionalProperties is false)")
        for key, value in document.items():
            if key in properties:
                errors.extend(
                    validate(value, properties[key], f"{path}.{key}"))
    if isinstance(document, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(document):
            errors.extend(
                validate(item, schema["items"], f"{path}[{index}]"))
    return errors


def _load_matrix():
    # Unguarded on purpose: the matrix is a committed artifact of the #280
    # contract; if it is missing the FileNotFoundError IS the failure.
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _load_schema():
    assert SCHEMA_PATH.is_file(), (
        f"{SCHEMA_PATH} is missing — the committed schema is part of the "
        f"#363 check surface")
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _clone(value):
    return json.loads(json.dumps(value))


def test_committed_matrix_validates_against_schema():
    """Check 1: the committed docs/fleet-capability-matrix.json validates
    against the committed schema — every row carries the required fields
    with the right types, every input is closed to {name, locality}, and no
    row or input carries an undeclared key.  Holds at base and must keep
    holding after the #363 matrix update (whose "$schema"/"schema" keys are
    declared, allowed properties)."""
    errors = validate(_load_matrix(), _load_schema())
    assert not errors, (
        f"the committed matrix violates docs/fleet-capability-matrix"
        f".schema.json: {errors}")


def test_row_with_unknown_field_is_rejected():
    """Check 2: additionalProperties:false bites — a row carrying an
    undeclared field is rejected naming the unknown key, and an off-enum
    locality or hub_op is rejected by enum membership.  The negative legs
    prove the validator is not a rubber stamp for check 1."""
    schema = _load_schema()
    row_schema = schema["properties"]["rows"]["items"]
    matrix = _load_matrix()
    base = next(row for row in matrix["rows"]
                if "ingest" in row.get("hub_ops", []))

    unknown_field = _clone(base)
    unknown_field["confidence_tier"] = "gold"
    errors = validate(unknown_field, row_schema)
    assert any("unknown key(s)" in e and "confidence_tier" in e
               for e in errors), errors

    bad_locality = _clone(base)
    bad_locality["inputs"] = [dict(bad_locality["inputs"][0],
                                   locality="server_side")]
    errors = validate(bad_locality, row_schema)
    assert any("locality" in e and "server_side" in e for e in errors), (
        errors)

    bad_op = _clone(base)
    bad_op["hub_ops"] = ["delete"]
    errors = validate(bad_op, row_schema)
    assert any("hub_ops" in e and "delete" in e for e in errors), errors


def test_row_with_renamed_key_is_rejected():
    """Check 3: a REQUIRED key renamed away (harness -> harness_name) is a
    validation failure for the MISSING key — a matrix regenerated with a
    renamed column cannot slip through as merely carrying an extra
    field."""
    schema = _load_schema()
    row_schema = schema["properties"]["rows"]["items"]
    matrix = _load_matrix()
    base = _clone(matrix["rows"][0])
    assert "harness" in base, base
    base["harness_name"] = base.pop("harness")
    errors = validate(base, row_schema)
    assert any("missing required key 'harness'" in e for e in errors), (
        errors)
    assert any("harness_name" in e and "unknown key" in e
               for e in errors), errors


def test_matrix_carries_schema_pointer():
    """Check 4 (RED at base): the matrix must carry a top-level "$schema"
    key pointing at the committed schema file beside it, so a consumer can
    locate the contract mechanically.  At base the committed matrix has no
    "$schema" key (it predates the #363 update) and this fails; once the
    matrix update lands, the value resolves to
    docs/fleet-capability-matrix.schema.json."""
    matrix = _load_matrix()
    assert "$schema" in matrix, (
        "docs/fleet-capability-matrix.json carries no \"$schema\" key — the "
        "matrix must point at docs/fleet-capability-matrix.schema.json "
        "(issue #363)")
    pointer = matrix["$schema"]
    assert isinstance(pointer, str) and pointer.strip(), pointer
    assert pointer.endswith("fleet-capability-matrix.schema.json"), pointer
    resolved = MATRIX_PATH.parent / pointer
    assert resolved.is_file(), (
        f"the matrix \"$schema\" pointer {pointer!r} does not resolve to the "
        f"committed schema file beside it ({resolved})")


_CHECKS = (
    test_committed_matrix_validates_against_schema,
    test_row_with_unknown_field_is_rejected,
    test_row_with_renamed_key_is_rejected,
    test_matrix_carries_schema_pointer,
)


def main() -> int:
    """CI runs ``python tests/test_fleet_matrix_schema.py`` directly (no
    pytest dependency); pytest collects the same functions."""
    failures = []
    for check in _CHECKS:
        try:
            check()
            print(f"PASS {check.__name__}")
        except Exception as exc:  # noqa: BLE001 - report every failure
            failures.append((check.__name__, str(exc)))
            print(f"FAIL {check.__name__}: {exc}")
    if failures:
        print(f"{len(failures)} matrix-schema check(s) failed")
        return 1
    print("all matrix-schema checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
