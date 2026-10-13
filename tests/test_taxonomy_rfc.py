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
# The recorded decision split (4 defer / 8 reject / 0 accept) is the RFC's
# product; pin it so a flipped decision cannot ship green.
DECISION_EXPECTED = {
    "state": "defer",
    "event": "defer",
    "observation": "defer",
    "runbook": "defer",
    "entity_card": "reject",
    "host": "reject",
    "service": "reject",
    "model": "reject",
    "repo": "reject",
    "file": "reject",
    "command": "reject",
    "error_class": "reject",
}
COMPATIBILITY_LITERAL = "unchanged: no database schema or runtime behavior changes in #172"
ROLLBACK_LITERAL = "delete the RFC and delta artifacts; no store rollback required"
MIGRATION_NONE = "none in #172"
# Paths whose presence in a diff marks it as issue-#172 work; the diff-scope
# gate below enforces its rules only on diffs touching these.
SIGNATURE_PATHS = (
    "docs/RFC-taxonomy.md",
    "docs/rfc-schema.json",
    "tests/fixtures/taxonomy/",
    "tests/test_taxonomy_rfc.py",
)
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
    """Test-own canonicalization: a separate copy, never imported from the generator.

    The generator module is deliberately not imported, so this check does not
    depend on the generator's module wiring; the algorithm mirrors the
    contract's canonicalization spec (recursive key sort; arrays sorted by
    canonical JSON bytes), so it agrees with a spec-conformant generator by
    construction, not by import.
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
        self.assertEqual(
            list(names),
            list(CANDIDATES),
            "candidates must appear in the contract's fixed order",
        )
        self.assertEqual(len(set(names)), len(names), "duplicate candidate names")
        for entry in candidates:
            self.assertIn(entry["decision"], DECISIONS)
            self.assertEqual(
                entry["decision"],
                DECISION_EXPECTED[entry["name"]],
                f"recorded decision for {entry['name']} changed",
            )
            self.assertEqual(entry["handoff"], HANDOFF[entry["name"]])
            self.assertEqual(entry["compatibility"], COMPATIBILITY_LITERAL)
            self.assertEqual(entry["rollback"], ROLLBACK_LITERAL)
            if entry["decision"] == "accept":
                self.assertIn("no migration in #172", entry["migration"])
            else:
                self.assertEqual(entry["migration"], MIGRATION_NONE)

    def test_digest_matches_expected_snapshot(self) -> None:
        computed = hashlib.sha256(
            SNAPSHOT_PATH.read_bytes() + DECISIONS_PATH.read_bytes()
        ).hexdigest()
        expected_doc = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            expected_doc["store_sha256"],
            hashlib.sha256(SNAPSHOT_PATH.read_bytes()).hexdigest(),
            "expected.json store_sha256 must match the committed snapshot bytes",
        )
        expected = expected_doc["input_digest"]
        self.assertEqual(computed, expected)
        self.assertRegex(computed, DIGEST_RE)
        self.assertEqual(self.artifact["input_digest"], computed)
        for entry in self.artifact["candidates"]:
            self.assertEqual(entry["input_digest"], computed)


class TaxonomyRfcReplayTest(unittest.TestCase):
    """AC3/AC5: replay reproduction, provenance, and read-only inputs."""

    def _recorded_commands(self) -> list[tuple[dict[str, object], list[str]]]:
        artifact = sole_json_block(RFC_PATH.read_text(encoding="utf-8"))
        recorded = []
        for entry in artifact["candidates"]:
            recorded.append((entry, shlex.split(entry["replay_command"])))
        return recorded

    def test_candidate_replays_match_committed_deltas(self) -> None:
        expected_doc = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
        reference_replay = canonicalize(expected_doc)
        # The delta directory must hold exactly the twelve named .json files:
        # no missing delta, and no stray extra .json sibling can hide among
        # them (non-json siblings such as generator *.tmp files are outside
        # this assertion's view).
        self.assertEqual(
            sorted(p.name for p in DELTA_DIR.glob("*.json")),
            sorted(f"{name}.json" for name in CANDIDATES),
        )
        for entry, argv in self._recorded_commands():
            name = entry["name"]
            with self.subTest(candidate=name):
                # The recorded command must be the exact contract command:
                # one --candidate, one --out, the generator script, and the
                # three #155 fixture paths — before --out is redirected.
                self.assertEqual(argv[0], "python")
                self.assertEqual(argv.count("--candidate"), 1)
                self.assertEqual(argv.count("--out"), 1)
                self.assertEqual(
                    argv[1], "tests/fixtures/taxonomy/replay_candidate.py"
                )
                self.assertEqual(argv[argv.index("--candidate") + 1], name)
                self.assertEqual(
                    argv[argv.index("--snapshot") + 1], "tests/fixtures/replay/store.sqlite"
                )
                self.assertEqual(
                    argv[argv.index("--decisions") + 1], "tests/fixtures/replay/decisions.log"
                )
                self.assertEqual(
                    argv[argv.index("--expected") + 1], "tests/fixtures/replay/expected.json"
                )
                self.assertEqual(
                    argv[argv.index("--out") + 1],
                    f"tests/fixtures/taxonomy/delta/{name}.json",
                )
                # The RFC's own observed_delta pointer must name the artifact
                # this test verifies.
                self.assertEqual(
                    entry["observed_delta"],
                    f"tests/fixtures/taxonomy/delta/{name}.json",
                )
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
                    # The delta's own fields carry the no-change contract.
                    self.assertEqual(delta["candidate"], name)
                    self.assertEqual(
                        delta["input_digest"],
                        expected_doc["input_digest"],
                        f"delta root input_digest must equal the #155 digest ({name})",
                    )
                    self.assertEqual(delta["observed_delta"]["runtime_change"], "none")
                    self.assertEqual(delta["observed_delta"]["schema_change"], "none")
                finally:
                    shutil.rmtree(scratch, ignore_errors=True)

    def test_replay_is_read_only(self) -> None:
        inputs = (SNAPSHOT_PATH, DECISIONS_PATH, EXPECTED_PATH)
        before = {path: file_sha256(path) for path in inputs}
        scratch = Path(tempfile.mkdtemp(prefix="taxonomy-readonly-"))
        try:
            env = isolated_env(scratch)
            for entry, argv in self._recorded_commands():
                name = entry["name"]
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
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            stdin=subprocess.DEVNULL,
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

    def _merge_base(self) -> str:
        """Resolve merge-base(HEAD, origin/main) as the drift-proof comparison.

        `git diff origin/main` (two dots) compares the main TREE against this
        branch, so a sibling PR merging to main mid-flight makes unrelated
        files appear in the diff and fails AC6 for changes this branch never
        made. The merge-base is the point this branch actually diverged from;
        diffing from it reports exactly this PR's changes under any amount of
        base drift. The contract's own local recipe has the same fragility on
        a busy repo, which is why the comparison ref is resolved here.

        CI checkouts can be shallow (this repo's test job uses actions/checkout
        fetch-depth 2; a truncated history below the fork point still makes
        merge-base exit 1 with no stderr). Deepen the repository in bounded
        steps and retry until the ancestor exists (this repo's whole history
        is a few hundred commits, so one or two steps always suffice); fail
        closed with a diagnostic if it never resolves.
        """
        last = ""
        for _ in range(8):
            proc = subprocess.run(
                ["git", "merge-base", "HEAD", "origin/main"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            merge_base = proc.stdout.strip()
            if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", merge_base):
                return merge_base
            last = (proc.stderr or proc.stdout).strip()
            deepen = subprocess.run(
                ["git", "fetch", "--no-tags", "--deepen", "250", "origin"],
                cwd=str(REPO_ROOT),
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            if deepen.returncode != 0:
                last = f"merge-base failed ({last}); deepen failed ({deepen.stderr.strip()[:200]})"
                break
        self.fail(f"cannot resolve merge-base(HEAD, origin/main) even after deepening: {last[:300]}")

    def test_diff_scope_is_docs_eval_tests_only(self) -> None:
        self._ensure_origin_main()
        merge_base = self._merge_base()
        head_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(head_proc.returncode, 0, head_proc.stderr)
        if merge_base == head_proc.stdout.strip():
            # HEAD is main itself (post-merge push CI): the PR diff is empty
            # by definition and judging zero paths would be vacuous. This gate
            # governs issue-#172 PR authoring, not main.
            return
        proc = subprocess.run(
            ["git", "diff", "--numstat", merge_base, "HEAD"],
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
        # Enforce the docs/eval/tests scope only on diffs that touch the
        # issue-#172 artifacts: later PRs that never touch this RFC must not
        # be judged by this PR's scope rule. A diff touching any signature
        # path is non-empty by construction, so the gate cannot pass vacuously.
        if not any(path.startswith(SIGNATURE_PATHS) for path in changed):
            return
        self.assertTrue(changed, "signature-path diff produced no changed paths")
        for path in changed:
            self.assertTrue(
                path.startswith(allowed),
                f"changed path outside docs/ eval/ tests/: {path}",
            )
        self.assertNotIn("release-manifest.json", changed)


if __name__ == "__main__":
    unittest.main()
