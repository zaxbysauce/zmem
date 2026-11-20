"""Insert closeout-request (index 3) and closeout-gate (index 8) fixture
cases into tests/fixtures/launcher/codex-cases.json at the exact flat
manifest positions, and matching {} expected envelopes into
codex-expected.json (both new verbs are launcher passthroughs — not in
TRANSLATED_HOOKS — so the fixture child prints a bare {} object and the
committed expected envelope is {})."""

import json

CASES = "tests/fixtures/launcher/codex-cases.json"
EXPECTED = "tests/fixtures/launcher/codex-expected.json"

STDIN = {
    "session_id": "00000000-0000-4000-8000-000000000188",
    "cwd": "C:\\zmem-188\\project",
    "namespace": "project:zmem-188",
    "timestamp": "2026-09-10T00:00:00Z",
}


def case(verb: str) -> dict:
    return {
        "verb": verb,
        "stdin": dict(STDIN),
        # Launcher passthrough: the stub child prints a bare {} object and
        # the launcher emits it verbatim (no sentinel translation).
        "child_stdout": "{}",
    }


with open(CASES, "r", encoding="utf-8") as fh:
    cases_doc = json.load(fh)
with open(EXPECTED, "r", encoding="utf-8") as fh:
    expected = json.load(fh)

verbs = [c["verb"] for c in cases_doc["cases"]]
assert verbs == [
    "session-start", "recall", "capture-correction", "pretool-recall",
    "convention-capture", "capture-failure", "reflect", "subagent-recall",
    "subagent-reflect", "precompact", "session-end",
], verbs

cases_doc["cases"].insert(3, case("closeout-request"))
cases_doc["cases"].insert(8, case("closeout-gate"))

# closeout-gate sits in the Stop event AFTER reflect; the expected map is
# verb-keyed, so order does not matter there — just add both keys.
expected["closeout-request"] = {}
expected["closeout-gate"] = {}

with open(CASES, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(cases_doc, fh, indent=2)
    fh.write("\n")
with open(EXPECTED, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(expected, fh, indent=2)
    fh.write("\n")

print("cases:", [c["verb"] for c in cases_doc["cases"]])
print("expected keys:", sorted(expected))
