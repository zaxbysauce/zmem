"""Frozen acceptance check: the BASE leg, replayed in an isolated worktree.

The trace's own replay runs each frozen check twice -- once at the recorded
base in a disposable worktree (the check must FAIL with its expected
signature) and once at the head (the check must PASS). The head side is
covered by the suite runs. This module reproduces the BASE side as a
standalone, runner-executable script, so the base failures carry the same
runner-originated receipt (revision-bound, provenance-checked, separate
output streams) as everything else.

It lives under `tests/support/` because the swarm probe runner executes its
target inside the tree it extracts from the revision.

Usage (this is the shape the probe request uses):

    python -X utf8 tests/support/frozen_base_leg.py \
        --check C1 \
        --base 5a5560ba44dae468abf36b37caa0c6deb40f8bf5 \
        --expect "AC1-506: a first open killed inside the v3 migration did not resume" \
        --frozen tests/test_audit_schema_recovery.py \
        --frozen tests/_store_env_pin.py \
        -- env ZMEM_STORE=... python -X utf8 -m pytest -q tests/test_audit_schema_recovery.py::test_interrupted_schema_and_fts

The frozen files are taken from the CURRENT tree (the extracted revision --
the frozen bytes) and copied into the disposable base worktree, which is
how the check is meant to be replayed: the NEW check against the OLD code.

Exit codes: 0 = the base leg FAILED with the expected signature (the
expected RED), 3 = it exited 0 (a VACUOUS base leg -- the check does not
discriminate), 4 = it failed with a DIFFERENT signature, 2 = a setup error.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASH = "C:/Program Files/Git/bin/bash.exe"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--expect", required=True)
    parser.add_argument("--frozen", action="append", default=[])
    parser.add_argument("--repo", default=os.environ.get("ZMEM_FROZEN_REPO", ""))
    parser.add_argument("--command", default="",
                        help="the shell command to run in the base worktree")
    args = parser.parse_args()

    command = args.command
    if not command:
        # the acceptance table's argv is a SHELL COMMAND ("env A=B python ..."),
        # so it is run through a shell, exactly as repro-check.sh runs it
        argv = list(args.argv)
        if argv and argv[0] == "--":
            argv = argv[1:]
        command = " ".join(argv)
    if not command:
        print("frozen_base_leg: no command given", file=sys.stderr)
        return 2

    repo = args.repo or subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    ).stdout.strip()
    if not repo or not Path(repo).is_dir():
        print("frozen_base_leg: no repository to take the base from", file=sys.stderr)
        return 2

    source = Path.cwd()
    worktree = Path(tempfile.mkdtemp(prefix="zmem-frozen-base-"))
    target = worktree / "tree"
    env = dict(os.environ)
    # pytest lives in the USER site-packages on this host, and the child's
    # HOME/USERPROFILE/APPDATA are redirected below -- which would hide it.
    # The redirect is only for the child's scratch dirs, so the user site's
    # location is preserved explicitly (the same interpreter that runs this
    # driver is the one that runs the check).
    user_site = subprocess.run(
        [sys.executable, "-c", "import site; print(site.getusersitepackages())"],
        capture_output=True, text=True).stdout.strip()
    if user_site:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (user_site + os.pathsep + existing) if existing else user_site
    env.update({
        "HOME": str(worktree / "home"),
        "USERPROFILE": str(worktree / "home"),
        "APPDATA": str(worktree / "appdata"),
        "LOCALAPPDATA": str(worktree / "appdata"),
        "TEMP": str(worktree / "tmp"),
        "TMP": str(worktree / "tmp"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    for sub in ("home", "appdata", "tmp"):
        (worktree / sub).mkdir(parents=True, exist_ok=True)

    try:
        proc = subprocess.run(
            ["git", "-C", repo, "worktree", "add", "--detach", str(target), args.base],
            capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            print("frozen_base_leg: git worktree failed: %s" % proc.stderr[-300:],
                  file=sys.stderr)
            return 2

        # the frozen bytes come from the CURRENT tree (the revision under
        # test); the base worktree supplies the OLD code
        for rel in args.frozen:
            src = source / rel
            dst = target / rel
            if not src.is_file():
                print("frozen_base_leg: frozen file missing: %s" % rel, file=sys.stderr)
                return 2
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        run = subprocess.run(
            [BASH, "-c", command], cwd=str(target), capture_output=True, text=True,
            env=env, timeout=1800)
        output = (run.stdout or "") + (run.stderr or "")
        print("=== the base leg of %s at %s ===" % (args.check, args.base[:12]))
        print("exit_code: %d" % run.returncode)
        print(output[-4000:])

        if run.returncode == 0:
            print("VERDICT: VACUOUS -- the check PASSED at the base, so it "
                  "does not discriminate", file=sys.stderr)
            return 3
        if not re.search(args.expect, output):
            print("VERDICT: WRONG FAILURE -- the base leg failed, but not "
                  "with the expected signature: %r" % args.expect, file=sys.stderr)
            return 4
        print("VERDICT: RED -- the base leg failed with the expected signature")
        return 0
    finally:
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force",
                        str(target)], capture_output=True, text=True, env=env)
        shutil.rmtree(worktree, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
