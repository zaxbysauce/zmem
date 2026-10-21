"""Release-gate integrity: duplicate [Unreleased] headings (RED pins).

The drift mode parses `## [Unreleased]` with a first-match regex, so a
changelog carrying TWO [Unreleased] headings is misread: the (empty) first
heading masks content under the second, and `--check-unreleased-drift`
exits 0 on a changelog that is actually drifting. Post-fix contract:

  - gate.unreleased_heading_lines(text) returns every [Unreleased] heading
    line number (missing today);
  - check_unreleased_drift FAILS (exit 1) on a changelog with duplicate
    [Unreleased] headings, naming BOTH line numbers;
  - unreleased_has_content detects content under ANY [Unreleased] heading,
    not just the first;
  - a single EMPTY [Unreleased] anywhere still exits 0 with "no unreleased
    drift" (the floor — green at base and after the fix).

House convention, pytest-FLAT functions plus a __main__ runner that invokes
every test by name (CI runs `python tests/test_*.py` — no pytest
collection): a silent exit 0 would be test theater under this runner, so
every failure is collected, printed, and exits 1.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_PY = REPO_ROOT / "scripts" / "release_gate.py"

# Spec-load the gate exactly like tests/test_release_parity.py (the module
# lives at scripts/release_gate.py, not in a package).
_spec = importlib.util.spec_from_file_location(
    "zmem_release_gate_unreleased", GATE_PY)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

_UNRELEASED_HEADING_RE = re.compile(r"(?m)^## \[Unreleased\][^\n]*$")

# One [Unreleased] at the top, a released section, then a SECOND
# [Unreleased] carrying the only content.
TEXT_WITH_TWO_UNRELEASED = (
    "# Changelog\n"
    "\n"
    "## [Unreleased]\n"
    "\n"
    "## [0.2.0] — 2026-01-02\n"
    "\n"
    "Released body.\n"
    "\n"
    "## [Unreleased]\n"
    "\n"
    "- merged fix parked under the second heading only (#91)\n"
    "\n"
    "## [0.1.0] — 2026-01-01\n"
    "\n"
    "Old body.\n"
)

# A single EMPTY [Unreleased] in a NON-first position (floor pin: any
# position, no content, no duplicate → clean).
TEXT_WITH_ONE_EMPTY_UNRELEASED = (
    "# Changelog\n"
    "\n"
    "## [0.2.0] — 2026-01-02\n"
    "\n"
    "Released body.\n"
    "\n"
    "## [Unreleased]\n"
    "\n"
    "## [0.1.0] — 2026-01-01\n"
    "\n"
    "Old body.\n"
)


def _heading_lines(text: str) -> list:
    """The [Unreleased] heading line numbers of `text` (1-based, in order)."""
    return [
        text.count("\n", 0, m.start()) + 1
        for m in _UNRELEASED_HEADING_RE.finditer(text)
    ]


def _run_drift_gate(changelog_text: str):
    """check_unreleased_drift in-process on a scratch root; (rc, output)."""
    root = Path(tempfile.mkdtemp(prefix="zmem-unreleased-"))
    try:
        (root / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gate.check_unreleased_drift(root)
        return rc, buf.getvalue()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_duplicate_unreleased_headings_fail():
    text = TEXT_WITH_TWO_UNRELEASED
    dup_lines = _heading_lines(text)
    # Fixture sanity (green at base): two headings at known line numbers.
    assert len(dup_lines) == 2, f"fixture rot: expected 2 headings, got {dup_lines}"

    # The diagnostic helper must exist and report BOTH line numbers
    # (RED at base: the function is missing entirely).
    lines_fn = getattr(gate, "unreleased_heading_lines", None)
    assert lines_fn is not None, (
        "gate.unreleased_heading_lines is missing — duplicate "
        "[Unreleased] headings are not diagnosed"
    )
    reported = list(lines_fn(text))
    assert len(reported) == 2, (
        f"expected both [Unreleased] heading line numbers {dup_lines}, "
        f"got {reported}"
    )
    assert sorted(reported) == sorted(dup_lines), (
        f"expected line numbers {dup_lines}, got {reported}"
    )

    # The drift gate must FAIL on the duplicate-heading changelog, naming
    # both line numbers (RED at base: exits 0 via "no unreleased drift").
    rc, out = _run_drift_gate(text)
    assert rc == 1, (
        f"duplicate [Unreleased] headings (lines {dup_lines}) must fail "
        f"the drift gate, got rc={rc}:\n{out}"
    )
    for n in dup_lines:
        assert str(n) in out, (
            f"line {n} (a duplicate [Unreleased] heading) must appear in "
            f"the gate output:\n{out}"
        )


def test_unreleased_content_detected_at_any_position():
    text = TEXT_WITH_TWO_UNRELEASED
    # Content sits ONLY under the second heading; the first heading's body
    # is empty, which today masks the content entirely.
    assert gate.unreleased_has_content(text) is True, (
        "[Unreleased] content under a LATER heading must be detected — "
        "only the first heading is read today"
    )

    rc, out = _run_drift_gate(text)
    assert "no unreleased drift" not in out, (
        "the gate took the early-exit clean path although the changelog "
        f"carries [Unreleased] content:\n{out}"
    )
    # No git and no bump marker in the fixture: the post-fix drift exit
    # for unserved content is the failure exit.
    assert rc == 1, (
        f"content under a later [Unreleased] heading must fail the drift "
        f"gate, got rc={rc}:\n{out}"
    )


def test_single_empty_unreleased_stays_green():
    rc, out = _run_drift_gate(TEXT_WITH_ONE_EMPTY_UNRELEASED)
    assert rc == 0, (
        f"a single empty [Unreleased] at any position is the clean state, "
        f"got rc={rc}:\n{out}"
    )
    assert "no unreleased drift" in out, (
        f"the clean state must say so:\n{out}"
    )


if __name__ == "__main__":
    test_names = sorted(
        n for n in dir() if n.startswith("test_") and callable(globals()[n]))
    failures = []
    for name in test_names:
        try:
            globals()[name]()
            print(f"PASS {name}")
        except Exception:
            failures.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
    if failures:
        print(f"\n{len(failures)} test(s) failed: {', '.join(failures)}")
        sys.exit(1)
    print(f"\nall {len(test_names)} tests passed")
    sys.exit(0)
