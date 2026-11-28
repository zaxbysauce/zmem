"""The conflict-marker guard's cases, as a SEPARATELY AUTHORED oracle.

This script exists so the guardrail's behaviour can be proven by a probe
that does not merely re-run the repo's own test module. It imports
`scripts/release_gate.py` from the revision it is run against and
asserts the exact expected diagnostic count for each input:

  1. the repo's real CHANGELOG          -> 0  (it ships no marker)
  2. `<<<<<<< HEAD`                      -> 1  (a HEAD-labelled marker)
  3. `>>>>>>> feature-branch`            -> 1  (a branch-labelled marker)
  4. `||||||| origin/main`               -> 1  (the diff3 base marker)
  5. `Title` then `=======`             -> 0  (a markdown setext underline
                                            is legitimate content, and the
                                            separator is only a marker
                                            INSIDE a conflict block)
  6. a bullet that mentions a marker -> 0  (a marker word in prose is
                                            not a marker)
  7. the same prose without the bullet marker -> 0

Cases 2-4 are the ones that a fixed LIST of marker labels would miss: git
writes seven marker characters and then an ARBITRARY label (the branch
name, HEAD, a path), so a check that only recognised `ours`/`theirs`
would let `<<<<<<< HEAD` through. Case 5 is the false-positive case: the
same seven `=` characters are a legitimate markdown setext underline
unless they sit inside a conflict block.

Run: python tests/support/conflict_marker_cases.py
(the runner also runs it as a `script` leg, with `scripts` on the
sys_path and `release_gate` named as a provenance module, so the receipt
records that the gate under test came from INSIDE the extracted
revision.)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATE_PATH = REPO_ROOT / "scripts" / "release_gate.py"

# (label, text, expected number of diagnostics)
CASES = [
    ("the repo's real CHANGELOG", None, 0),
    ("a HEAD-labelled marker", "<<<<<<< HEAD\n", 1),
    ("a branch-labelled marker", ">>>>>>> feature-branch\n", 1),
    ("a diff3 base marker", "||||||| origin/main\n", 1),
    ("a markdown setext underline", "Title\n=======\n", 0),
    ("a bullet that mentions a marker", "- prose mentions <<<<<<< HEAD\n", 0),
    ("prose that mentions a marker", "prose mentions <<<<<<< HEAD\n", 0),
]


def load_gate():
    spec = importlib.util.spec_from_file_location("release_gate", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    gate = load_gate()
    print("gate under test : %s" % GATE_PATH)
    print("repo root       : %s" % REPO_ROOT)
    failures = []
    for label, text, expected in CASES:
        if text is None:
            text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        found = len(gate._changelog_conflict_markers(text))
        print("%-32s expected=%d found=%d" % (label, expected, found))
        if found != expected:
            failures.append("%s: expected %d diagnostics, found %d"
                            % (label, expected, found))
    if failures:
        for failure in failures:
            print("FAIL: %s" % failure)
        return 1
    print("ALL %d CASES HOLD" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
