"""Tests for the taxonomy RFC decision artifact (issue #172, Workstream K PR 1).

Standard library only. Covers: the RFC's sole machine-readable json fence and
its validation against docs/rfc-schema.json through a bounded inline validator
that fails loudly on any schema keyword it does not implement; the exact
twelve-candidate set, decisions, and handoff mapping; the digest contract
(sha256 over raw snapshot bytes + raw decision-log bytes, no separator,
equal to tests/fixtures/replay/expected.json); byte-for-byte reproduction of
every recorded replay command plus delta replay provenance; read-only replay
inputs; fail-closed generator errors (missing input, digest mismatch) that
never rewrite output; and the docs/eval/tests-only diff scope.

Run: python tests/test_taxonomy_rfc.py   (no pytest — repo convention)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RFC_PATH = REPO_ROOT / "docs" / "RFC-taxonomy.md"
SCHEMA_PATH = REPO_ROOT / "docs" / "rfc-schema.json"
SNAPSHOT_PATH = REPO_ROOT / "tests" / "fixtures" / "replay" / "store.sqlite"
DECISIONS_PATH = REPO_ROOT / "tests" / "fixtures" / "replay" / "decisions.log"
EXPECTED_PATH = REPO_ROOT / "tests" / "fixtures" / "replay" / "expected.json"
DELTA_DIR = REPO_ROOT / "tests" / "fixtures" / "taxonomy" / "delta"
GENERATOR = REPO_ROOT / "tests" / "fixtures" / "taxonomy" / "replay_candidate.py"

CANDIDATES = (
    "state",
    "event",
    "observation",
    "runbook",
    "entity_card",
    "host",
    "service",
    "model",
    "repo",
    "file",
    "command",
    "error_class",
)
DECISIONS = ("accept", "reject", "defer")
HANDOFF = {
    "state": "#173 and #174",
    "event": "#178",
    "observation": "#137",
    "runbook": "#138",
    "entity_card": "#175 and #179",
    "host": "#175",
    "service": "#175",
    "model": "#175",
    "repo": "#175",
    "file": "#175",
    "command": "#175",
    "error_class": "#175",
}
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MACHINE_TOKENS = ("json", "jsonc", "json5")
PLACEHOLDER_MARKERS = ("placeholder", "todo", "tbd", "fixme", "dummy")

# Bounded schema vocabulary. docs/rfc-schema.json must stay inside this set;
# anything else fails loudly so validation can never be silently vacuous.
VALIDATION_KEYWORDS = {
    "type",
    "const",
    "enum",
    "pattern",
    "minLength",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
}
ANNOTATION_KEYWORDS = {
    "$schema",
    "$id",
    "$comment",
    "title",
    "description",
    "default",
    "examples",
    "$defs",
    "definitions",
}


def sole_json_block(text: str) -> object:
    """Extract the RFC's single machine-readable json fence."""
    fences: list[tuple[str, str]] = []
    in_fence = False
    info = ""
    body: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not in_fence:
            if stripped.startswith("```"):
                in_fence = True
                info = stripped[3:].strip()
                body = []
        elif stripped.startswith("```"):
            fences.append((info, "\n".join(body)))
            in_fence = False
        else:
            body.append(line)
    if in_fence:
        fences.append((info, "\n".join(body)))
    machine = [
        block
        for info_string, block in fences
        if info_string.split() and info_string.split()[0].lower() in MACHINE_TOKENS
    ]
    if len(machine) != 1:
        raise AssertionError(
            f"expected exactly one machine-readable json fence, found {len(machine)}"
        )
    return json.loads(machine[0])


def collect_keywords(schema: object, into: set[str]) -> None:
    """Record used keywords; fail loudly on any unimplemented keyword."""
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise AssertionError("schema is not an object")
    for key, value in schema.items():
        if key in VALIDATION_KEYWORDS:
            into.add(key)
        elif key not in ANNOTATION_KEYWORDS:
            raise AssertionError(f"schema uses unimplemented keyword: {key}")
        if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
            for subschema in value.values():
                collect_keywords(subschema, into)
        elif key in ("items", "additionalProperties") and isinstance(value, dict):
            collect_keywords(value, into)


def validate(instance: object, schema: object, where: str) -> None:
    """Bounded Draft-2020-12 subset validator (stdlib only)."""
    if schema is True:
        return
    if schema is False:
        raise AssertionError(f"{where}: schema forbids any value")
    if not isinstance(schema, dict):
        raise AssertionError(f"schema at {where} is not an object")
    unknown = sorted(set(schema) - VALIDATION_KEYWORDS - ANNOTATION_KEYWORDS)
    if unknown:
        raise AssertionError(f"schema uses unimplemented keyword(s): {unknown}")
    if "type" in schema:
        declared = schema["type"]
        names = [declared] if isinstance(declared, str) else list(declared)
        if not any(_type_ok(instance, name) for name in names):
            raise AssertionError(f"{where}: expected type {declared!r}, got {type(instance).__name__}")
    if "const" in schema and instance != schema["const"]:
        raise AssertionError(f"{where}: {instance!r} != const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise AssertionError(f"{where}: {instance!r} not in enum")
    if (
        "pattern" in schema
        and isinstance(instance, str)
        and re.search(schema["pattern"], instance) is None
    ):
        raise AssertionError(f"{where}: {instance!r} does not match {schema['pattern']!r}")
    if "minLength" in schema and isinstance(instance, str) and len(instance) < schema["minLength"]:
        raise AssertionError(f"{where}: string shorter than minLength {schema['minLength']}")
    if isinstance(instance, dict):
        for key in schema.get("required", ()):
            if key not in instance:
                raise AssertionError(f"{where}: missing required key {key!r}")
        properties = schema.get("properties", {})
        for key, subschema in properties.items():
            if key in instance:
                validate(instance[key], subschema, f"{where}.{key}")
        if "additionalProperties" in schema:
            extra = sorted(key for key in instance if key not in properties)
            additional = schema["additionalProperties"]
            if additional is False and extra:
                raise AssertionError(f"{where}: additional properties present: {extra}")
            if isinstance(additional, dict):
                for key in extra:
                    validate(instance[key], additional, f"{where}.{key}")
    if isinstance(instance, list):
        if "items" in schema:
            for index, element in enumerate(instance):
                validate(element, schema["items"], f"{where}[{index}]")
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise AssertionError(f"{where}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise AssertionError(f"{where}: more than maxItems {schema['maxItems']}")


def _type_ok(instance: object, name: object) -> bool:
    if name == "object":
        return isinstance(instance, dict)
    if name == "array":
        return isinstance(instance, list)
    if name == "string":
        return isinstance(instance, str)
    if name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if name == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if name == "boolean":
        return isinstance(instance, bool)
    if name == "null":
        return instance is None
    raise AssertionError(f"schema uses unimplemented type name: {name!r}")


def canonicalize(value: object) -> object:
    """Test-own canonicalization, implemented independently of the generator.

    Deliberately not imported from replay_candidate: a from-the-start wrong
    generator canonicalizer must not be able to agree with itself here.
    """
    if isinstance(value, dict):
        return {key: canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        items = [canonicalize(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ),
        )
    return value


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def isolated_env(scratch: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["ZMEM_STORE"] = str(scratch / "store.sqlite")
    env["ZMEM_DATA"] = str(scratch)
    env["ZMEM_MODELS_DIR"] = str(scratch / "models-do-not-create")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    return env


def one_line(text: str) -> str | None:
    """Normalize a subprocess stderr to its single content line, or None."""
    if not text.endswith("\n"):
        return None
    body = text[:-1]
    if body.endswith("\r"):
        body = body[:-1]
    if "\n" in body or "\r" in body:
        return None
    return body


class TaxonomyRfcSchemaTest(unittest.TestCase):
    """AC1/AC2/AC4: the RFC artifact, its schema, set, and digests."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rfc_text = RFC_PATH.read_text(encoding="utf-8")
        cls.artifact = sole_json_block(cls.rfc_text)
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_rfc_has_one_valid_json_block(self) -> None:
        # sole_json_block already rejects any second machine-readable fence.
        self.assertIsInstance(self.artifact, dict)
        used: set[str] = set()
        collect_keywords(self.schema, used)
        validate(self.artifact, self.schema, "$")
        lowered = json.dumps(self.artifact, ensure_ascii=True).lower()
        hits = [marker for marker in PLACEHOLDER_MARKERS if marker in lowered]
        self.assertEqual(hits, [], f"placeholder marker(s) in RFC json block: {hits}")

    def test_candidate_set_and_handoffs_are_complete(self) -> None:
        candidates = self.artifact["candidates"]
        names = [entry["name"] for entry in candidates]
        self.assertEqual(len(names), len(CANDIDATES))
        self.assertEqual(len(set(names)), len(names), "duplicate candidate names")
        self.assertEqual(set(names), set(CANDIDATES))
        for entry in candidates:
            self.assertIn(entry["decision"], DECISIONS)
            self.assertEqual(entry["handoff"], HANDOFF[entry["name"]])

    def test_digest_matches_expected_snapshot(self) -> None:
        computed = hashlib.sha256(
            SNAPSHOT_PATH.read_bytes() + DECISIONS_PATH.read_bytes()
        ).hexdigest()
        expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))["input_digest"]
        self.assertEqual(computed, expected)
        self.assertRegex(computed, DIGEST_RE)
        self.assertEqual(self.artifact["input_digest"], computed)
        for entry in self.artifact["candidates"]:
            self.assertEqual(entry["input_digest"], computed)


class TaxonomyRfcReplayTest(unittest.TestCase):
    """AC3/AC5: replay reproduction, provenance, and read-only inputs."""

    def _recorded_commands(self) -> list[tuple[str, list[str]]]:
        artifact = sole_json_block(RFC_PATH.read_text(encoding="utf-8"))
        recorded = []
        for entry in artifact["candidates"]:
            recorded.append((entry["name"], shlex.split(entry["replay_command"])))
        return recorded

    def test_candidate_replays_match_committed_deltas(self) -> None:
        expected_doc = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
        reference_replay = canonicalize(expected_doc)
        for name, argv in self._recorded_commands():
            with self.subTest(candidate=name):
                self.assertEqual(argv[argv.index("--candidate") + 1], name)
                scratch = Path(tempfile.mkdtemp(prefix=f"taxonomy-replay-{name}-"))
                try:
                    out_path = scratch / f"{name}.json"
                    argv = list(argv)
                    argv[argv.index("--out") + 1] = str(out_path)
                    argv[0] = sys.executable
                    proc = subprocess.run(
                        argv,
                        cwd=str(REPO_ROOT),
                        env=isolated_env(scratch),
                        capture_output=True,
                        timeout=180,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
                    committed = (DELTA_DIR / f"{name}.json").read_bytes()
                    self.assertEqual(out_path.read_bytes(), committed)
                    # Provenance: the committed replay payload is the
                    # canonicalized parsed expected.json, not a doctored copy.
                    delta = json.loads(committed.decode("utf-8"))
                    self.assertEqual(delta["observed_delta"]["replay"], reference_replay)
                finally:
                    shutil.rmtree(scratch, ignore_errors=True)

    def test_replay_is_read_only(self) -> None:
        inputs = (SNAPSHOT_PATH, DECISIONS_PATH, EXPECTED_PATH)
        before = {path: file_sha256(path) for path in inputs}
        scratch = Path(tempfile.mkdtemp(prefix="taxonomy-readonly-"))
        try:
            env = isolated_env(scratch)
            for name, argv in self._recorded_commands():
                argv = list(argv)
                argv[0] = sys.executable
                argv[argv.index("--out") + 1] = str(scratch / f"{name}.json")
                proc = subprocess.run(
                    argv, cwd=str(REPO_ROOT), env=env, capture_output=True, timeout=180
                )
                self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        after = {path: file_sha256(path) for path in inputs}
        self.assertEqual(before, after, "replay inputs were modified by the replay commands")


class TaxonomyReplayErrorTest(unittest.TestCase):
    """AC9: generator failures are fail-closed and never rewrite output."""

    def _scratch_inputs(self, scratch: Path) -> dict[str, Path]:
        paths = {
            "snapshot": scratch / "store.sqlite",
            "decisions": scratch / "decisions.log",
            "expected": scratch / "expected.json",
        }
        shutil.copyfile(SNAPSHOT_PATH, paths["snapshot"])
        shutil.copyfile(DECISIONS_PATH, paths["decisions"])
        shutil.copyfile(EXPECTED_PATH, paths["expected"])
        return paths

    def _run(self, scratch: Path, paths: dict[str, Path], snapshot: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(GENERATOR),
                "--candidate",
                "state",
                "--snapshot",
                str(snapshot),
                "--decisions",
                str(paths["decisions"]),
                "--expected",
                str(paths["expected"]),
                "--out",
                str(scratch / "out.json"),
            ],
            cwd=str(REPO_ROOT),
            env=isolated_env(scratch),
            capture_output=True,
            timeout=180,
        )

    def test_missing_input_fails_closed(self) -> None:
        scratch = Path(tempfile.mkdtemp(prefix="taxonomy-missing-"))
        rfc_before = file_sha256(RFC_PATH)
        try:
            paths = self._scratch_inputs(scratch)
            missing = scratch / "missing.sqlite"
            paths["snapshot"].unlink()
            proc = self._run(scratch, paths, missing)
            self.assertEqual(proc.returncode, 1)
            line = one_line(proc.stderr.decode("utf-8"))
            self.assertEqual(line, f"[taxonomy] input error: missing file {missing}")
            self.assertFalse((scratch / "out.json").exists())
            self.assertEqual(file_sha256(RFC_PATH), rfc_before, "RFC was rewritten on failure")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_digest_mismatch_fails_closed(self) -> None:
        scratch = Path(tempfile.mkdtemp(prefix="taxonomy-mismatch-"))
        rfc_before = file_sha256(RFC_PATH)
        fixture_hashes = {
            path: file_sha256(path) for path in (SNAPSHOT_PATH, DECISIONS_PATH, EXPECTED_PATH)
        }
        try:
            paths = self._scratch_inputs(scratch)
            out_file = scratch / "out.json"
            out_file.write_bytes(b"sentinel\n")
            document = json.loads(paths["expected"].read_text(encoding="utf-8"))
            digest = document["input_digest"]
            mutated = ("1" if digest[0] == "0" else "0") + digest[1:]
            document["input_digest"] = mutated
            paths["expected"].write_text(json.dumps(document), encoding="utf-8")
            proc = self._run(scratch, paths, paths["snapshot"])
            self.assertEqual(proc.returncode, 1)
            computed = hashlib.sha256(
                SNAPSHOT_PATH.read_bytes() + DECISIONS_PATH.read_bytes()
            ).hexdigest()
            line = one_line(proc.stderr.decode("utf-8"))
            self.assertEqual(
                line,
                f"[taxonomy] input error: digest mismatch: expected {mutated} computed {computed}",
            )
            self.assertEqual(out_file.read_bytes(), b"sentinel\n")
            self.assertEqual(file_sha256(RFC_PATH), rfc_before, "RFC was rewritten on failure")
            for path, digest_before in fixture_hashes.items():
                self.assertEqual(file_sha256(path), digest_before, f"fixture rewritten: {path}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


class TaxonomyDiffScopeTest(unittest.TestCase):
    """AC6: the change touches only docs/, eval/, and tests/."""

    def _ensure_origin_main(self) -> None:
        """Materialize refs/remotes/origin/main when the checkout lacks it.

        A pull_request-event checkout fetches only the PR merge ref, so
        `git diff --numstat origin/main` would fail with `ambiguous argument
        'origin/main'` (exit 128) on CI. The contract's own diff-scope command
        still runs unchanged below; this only supplies its precondition, and
        workflow edits are outside this test's allowed diff prefixes.
        """
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "origin/main"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            timeout=60,
        )
        if probe.returncode == 0:
            return
        fetch = subprocess.run(
            [
                "git",
                "fetch",
                "--no-tags",
                "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        self.assertEqual(
            fetch.returncode,
            0,
            f"origin/main absent and fetch failed: {fetch.stderr}",
        )

    def test_diff_scope_is_docs_eval_tests_only(self) -> None:
        self._ensure_origin_main()
        proc = subprocess.run(
            ["git", "diff", "--numstat", "origin/main"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        allowed = ("docs/", "eval/", "tests/")
        changed: list[str] = []
        for line in proc.stdout.splitlines():
            fields = line.split("\t")
            self.assertEqual(len(fields), 3, f"unexpected numstat line: {line!r}")
            path = fields[2]
            if " => " in path:  # rename: judge by the new path
                path = path.split(" => ")[-1].strip("{}")
            changed.append(path)
            self.assertTrue(
                path.startswith(allowed),
                f"changed path outside docs/ eval/ tests/: {path}",
            )
        self.assertNotIn("release-manifest.json", changed)


if __name__ == "__main__":
    unittest.main()
