"""Windows runner-head derivation — acceptance spec.

Defect under test: ``storelib.ops_tokens.derive_ops_tokens`` does not
recognize Windows runner heads, so the operation-token lane goes blind on
the primary Windows boxes:

- "python.exe -m pytest tests/"  derives NOTHING today ("python.exe" is
  not a runner head; the non-runner branch keeps only the last word
  "tests/", which collapses to an empty basename) — the pytest run leaves
  no recall signal at all.
- "py script.py" derives only ["script.py"] — the Windows launcher "py"
  is not a runner head, and post-fix it must alias "python" so the
  descriptor carries the interpreter.
- "C:\\tools\\git.exe status" derives NOTHING today (the backslash path
  head is not a runner head; the last word "status" is a bare word).

POST-FIX CONTRACT pinned here (pure, in-process; no store touches):

- derive_ops_tokens("python.exe -m pytest tests/") == ["python"]
  (an extension-suffixed runner head resolves to the canonical runner).
- derive_ops_tokens("py script.py") == ["python", "script.py"]
  (the Windows launcher aliases the canonical "python" runner).
- derive_ops_tokens("python script.py") == ["python", "script.py"]
  (POSIX alias parity — the control that must keep holding).
- derive_ops_tokens("C:\\tools\\git.exe status") == ["git", "status"]
  (a backslash head with an extension resolves to runner "git" and keeps
  the primary subcommand).

SAFETY: storelib freezes the store path at its first import, so the
environment is pinned to a throwaway temp store BEFORE any storelib
import (an unpinned import could auto-migrate the shared operator store
~/.zmem/store.sqlite). Ambient ZMEM_* knobs are stripped first.

Plain runnable module (no pytest/unittest dependency):
python -X utf8 tests/test_windows_runner_heads.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"

# Mirror the _STRIP_ENV pattern of tests/test_subagent_tasktext.py:45-67;
# strip every ZMEM_* knob this test does not set, including a dev box's
# ZMEM_REMOTE_URL.
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_INJECT_FLOOR_LEX", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
    "ZMEM_SESSION", "ZMEM_REMOTE_URL",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
)


def main() -> int:
    saved = {k: os.environ.get(k) for k in _STRIP_ENV}
    tmp = tempfile.mkdtemp(prefix="zmem-winheads-spec-")
    for key in _STRIP_ENV:
        os.environ.pop(key, None)
    # PIN before the first storelib import (store path freezes at import).
    os.environ["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    os.environ["ZMEM_DATA"] = tmp
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    os.environ["ZMEM_SESSION"] = "winheads-spec-session"
    try:
        sys.path.insert(0, str(SCRIPTS))
        sys.path.insert(0, str(SCRIPTS / "storelib"))
        import ops_tokens  # noqa: E402  (import happens AFTER env pin)

        cases = [
            # (command, expected tokens, note)
            ("python.exe -m pytest tests/", ["python"],
             "extension-suffixed runner head resolves to the canonical "
             "runner"),
            ("py script.py", ["python", "script.py"],
             "the Windows launcher py aliases the canonical python runner"),
            ("python script.py", ["python", "script.py"],
             "POSIX alias parity (control)"),
            ("C:\\tools\\git.exe status", ["git", "status"],
             "a backslash head with an extension resolves to runner git "
             "and keeps the primary subcommand"),
        ]
        for command, expected, note in cases:
            got = ops_tokens.derive_ops_tokens(command)
            assert got == expected, (
                "derive_ops_tokens(%r) must be %r (%s); got %r"
                % (command, expected, note, got))

        print("PASS test_windows_runner_heads")
        return 0
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
