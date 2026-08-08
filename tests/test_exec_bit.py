"""Regression guardrail for issue #28 (memory scripts non-executable in git).

Issue #28: `skills/memory/scripts/*.py` were tracked in git with mode `100644`
(non-executable) yet carry a `#!/usr/bin/env python` shebang. The reflect /
capture-failure / convention-capture / subagent-reflect hooks render a bare
direct-exec suggested command (`store.py add ...`) built via
`shlex.quote(store_py)`. On a fresh POSIX checkout (Linux/macOS) that command
hits `Permission denied`, silently breaking capture. Windows/Git-Bash ignore the
POSIX exec bit, so the defect only manifests on real POSIX.

This guard asserts the **committed** git mode of every shebang'd, direct-executed
memory script is `100755` (executable), so a fresh POSIX checkout materializes
them as runnable and a future commit that drops the exec bit fails CI.

Why we check the git COMMITTED tree (`git ls-tree HEAD`), not the filesystem:
Windows CI checks out with core.filemode=false, so the filesystem exec bit is
NOT set on disk there even when the tracked mode is correct — but the committed
mode is the authoritative value that a fresh POSIX checkout materializes as
filesystem mode. `git ls-tree HEAD` (with `git ls-files -s` fallback) is
therefore the platform-neutral, committed-state signal.

Run: python tests/test_exec_bit.py   (no pytest required — repo convention)
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Shebang'd, DIRECT-executed memory scripts that must be executable in git.
# (host.py is intentionally excluded — it has no shebang and is import-only,
# never direct-executed, so an exec bit there would be inert.)
REQUIRED_EXEC_SCRIPTS = [
    "skills/memory/scripts/store.py",
    "skills/memory/scripts/doctor.py",
    "skills/memory/scripts/embeddings.py",
    "skills/memory/scripts/import-store.py",
]


def _git_mode(rel_path: str) -> str:
    """Return the committed mode token (e.g. '100755') for rel_path.

    Prefers `git ls-tree HEAD <path>` (committed-tree-authoritative). Falls
    back to `git ls-files -s` (index) if `git ls-tree HEAD` is unavailable.
    Raises RuntimeError with a clear message if git is missing or the path is
    absent, rather than crashing the whole suite with FileNotFoundError.
    """
    tree_result = subprocess.run(
        ["git", "ls-tree", "HEAD", rel_path],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if tree_result.returncode == 0 and tree_result.stdout.strip():
        # Format: "<mode> <type> <sha>\t<path>"
        return tree_result.stdout.split()[0]

    # Fallback: index mode (occasionally HEAD refs may be behind or absent).
    index_result = subprocess.run(
        ["git", "ls-files", "-s", rel_path],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if index_result.returncode == 0 and index_result.stdout.strip():
        return index_result.stdout.split()[0]

    if tree_result.returncode != 0:
        # Almost always FileNotFoundError if git is missing; report clearly.
        raise RuntimeError(
            f"git binary not found or failed (exit {tree_result.returncode}); "
            "cannot verify exec-bit guard for " + rel_path
        )
    raise RuntimeError(f"path not tracked in git: {rel_path}")


class ExecBitGuardTest(unittest.TestCase):
    def test_required_scripts_are_committed_executable(self):
        """Every direct-exec memory script must be committed as 100755."""
        bad = []
        for rel in REQUIRED_EXEC_SCRIPTS:
            mode = _git_mode(rel)
            if mode != "100755":
                bad.append((rel, mode))
        if bad:
            detail = "; ".join(f"{p} -> {m}" for p, m in bad)
            self.fail(
                "Direct-exec memory scripts must be committed executable "
                "(mode 100755) so a fresh POSIX checkout can run the hook-rendered "
                f"`store.py ...` command. Found non-executable: {detail}. "
                "Fix with `git update-index --chmod=+x <path>` and commit."
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
