"""Pre-tool query sanitation (sanitize_pretool_command / pretool_query_text)
and its wiring into select_and_budget_for_injection.

The pretool lane exists to surface hazard lessons before an OPERATION; cd/ls
chains, read-only inspection and inline scripts name none, and their raw text
used to feed the lexical/vector/entity lanes with unrelated tokens.

Runs standalone: python tests/test_pretool_query_sanitize.py
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

# storelib freezes the store path at FIRST import: pin the sandbox before it.
_TMP = tempfile.mkdtemp(prefix="zmem-pretool-sanitize-")
atexit.register(shutil.rmtree, _TMP, True)
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ["ZMEM_DATA"] = _TMP
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
for _k in list(os.environ):
    if _k.startswith("ZMEM_SESSION") or _k in (
            "CLAUDE_PROJECT_DIR", "ZCODE_PROJECT_DIR", "ZMEM_PRETOOL_SANITIZE",
            "ZMEM_QUERY_CONTEXT", "ZMEM_TEST_NOW", "ZMEM_HOME"):
        os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _store_guard  # noqa: E402,F401  (issue #244; after the sandbox pin)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from storelib import inject as inject_mod  # noqa: E402
from storelib import ops_tokens as ops  # noqa: E402
from storelib import schema as schema_mod  # noqa: E402

assert Path(schema_mod._resolve_store_path()).parent == Path(_TMP).resolve() \
    or str(schema_mod._resolve_store_path()).startswith(_TMP), \
    "store path escaped the sandbox"

CD_LS = ('cd "E:/repo/.claude/worktrees/x" && ls && wc -c README.md && '
         'ls skills hooks 2>/dev/null | head -150')

COMMAND_TABLE = [
    (CD_LS, ""),
    ("sed -n 1,40p hooks/lib/x.py; echo ==", ""),
    ("sed -i s/a/b/ src/x.py", "sed -i s/a/b/ src/x.py"),
    ("sed -ni s/a/b/p src/x.py", "sed -ni s/a/b/p src/x.py"),
    # Search tools are operations: kept, relative path unchanged.
    ("grep -n foo skills/memory/scripts/store.py",
     "grep -n foo skills/memory/scripts/store.py"),
    # Final-critic round: inline interpreter code and a shell fed on stdin
    # can do anything, so the sanitizer ABSTAINS (whole command returned
    # whitespace-collapsed, exactly the pre-change query text).
    ("python - <<'EOF'\nimport os\nEOF", "python - <<'EOF' import os EOF"),
    ('python -c "print(1)"', 'python -c "print(1)"'),
    ('py -c "print(1)"', 'py -c "print(1)"'),
    ('node -e "1+1"', 'node -e "1+1"'),
    ('cd /a/b && python -c "import shutil; shutil.rmtree(\'build\')"',
     'cd /a/b && python -c "import shutil; shutil.rmtree(\'build\')"'),
    ("echo 'rm -rf /srv/data' | bash", "echo 'rm -rf /srv/data' | bash"),
    ("cat <<EOF | sh\nrm -rf x\nEOF", "cat <<EOF | sh rm -rf x EOF"),
    ("bash < deploy.sh", "bash < deploy.sh"),
    # Utilities with a write mode are not on the read-only list.
    ("uniq in.txt important.cfg", "uniq in.txt important.cfg"),
    ("sed -n 'w /etc/passwd' f", "sed -n w /etc/passwd f"),
    ("find . -name x -fprint out", "find -name x -fprint out"),
    ('wc -l <<< "x"', ""),
    ("bash run.sh", "bash run.sh"),
    # A shell -c string is a command: sanitized recursively, never dropped.
    ('bash -c "git push --force origin main"', "git push --force origin main"),
    ('bash -c "cd /tmp/x && ls"', ""),
    ("sh -c 'cat a.txt; git stash pop'", "git stash pop"),
    ("bash <<'EOF'\ngit stash pop\nEOF", "git stash pop"),
    ('bash -lc "git stash pop"', "git stash pop"),
    ('pwsh -c "git push --force origin main"', "git push --force origin main"),
    ("powershell -Command git push --force", "git push --force"),
    ("cmd /c del /q build", "del q build"),
    # -e is not inline code for a shell; a script run is an operation.
    ("bash -e deploy.sh", "bash -e deploy.sh"),
    ("sh -e ./release.sh --prod", "sh -e ./release.sh --prod"),
    # FAIL TOWARD KEEPING: a command after a heredoc survives ...
    ("cd repo && cat > notes.md <<EOF\nit's text\nEOF\ngit push -f origin main",
     "cd repo && cat > notes.md <<EOF it's text EOF "
     "git push -f origin main"),
    ("python - <<'PY'\nprint(1)\nPY\ngit reset --hard origin/main",
     "python - <<'PY' print(1) PY git reset --hard origin/main"),
    ("cat <<EOF\nx\nEOF", ""),
    # ... a newline separates commands like ';' ...
    ("ls\ngit push", "git push"),
    # ... a `<<` with no terminator line is not a heredoc ...
    ('python -c "x = 1 << y"\ngit push', 'python -c "x = 1 << y" git push'),
    ('echo "x << y"\ngit push', "git push"),
    # ... command substitution is never dropped with its inspection head ...
    ('echo "$(git reset --hard origin/main)"',
     "echo $(git reset --hard origin/main)"),
    ("echo `git clean -fdx`", "echo `git clean -fdx`"),
    # ... redirection to a real file is a write ...
    ("echo x > important.cfg", "echo x important.cfg"),
    ("head -c 0 > /etc/hosts", "head -c hosts"),
    ("ls > /dev/null", ""),
    ("ls 2>&1", ""),
    # ... in-place interpreters and write-capable utilities are kept.
    ("perl -pi -e 's/a/b/' f.txt", "perl -pi -e 's/a/b/' f.txt"),
    ("perl -e 'print 1'", "perl -e 'print 1'"),
    ("sort -o f f", "sort -o f f"),
    # A dropped segment piping INTO a kept command is that command's input:
    # the sanitizer abstains and the whole text is kept.
    ("echo y | git clean -fdx", "echo y | git clean -fdx"),
    ("cat p.diff | git apply", "cat p.diff | git apply"),
    ('find . -name "*.py" | xargs rm', 'find . -name "*.py" | xargs rm'),
    ('echo "import shutil; shutil.rmtree(\'build\')" | python',
     'echo "import shutil; shutil.rmtree(\'build\')" | python'),
    ("echo 'rm -rf /srv/data' | sudo bash",
     "echo 'rm -rf /srv/data' | sudo bash"),
    ("echo 'rm -rf x' | ssh host", "echo 'rm -rf x' | ssh host"),
    ("cat <<EOF | bash -s prod\nrm\nEOF", "cat <<EOF | bash -s prod rm EOF"),
    # ... while a kept command piping into a dropped one loses nothing.
    ("git log --oneline | head -5", "git log --oneline"),
    ("ls | head", ""),
    # A kept command reading a heredoc / file on stdin is not judged.
    ("python3.12 - <<EOF\nx\nEOF", "python3.12 - <<EOF x EOF"),
    ("uv run python - <<EOF\nx\nEOF", "uv run python - <<EOF x EOF"),
    ("make -f - <<EOF\nall:\nEOF", "make -f - <<EOF all: EOF"),
    ("python < wipe.py", "python < wipe.py"),
    ("mysql prod < wipe.sql", "mysql prod < wipe.sql"),
    ("wc -l < f", ""),
    ("sed -n '1w out.txt' f", "sed -n 1w out.txt f"),
    ("sed -n '1,40p' f", ""),
    ("sudo ls", "sudo ls"),
    # Second review round: process substitution is a command, not a
    # redirection target ...
    ("cat <(rm -rf build)", "cat <(rm -rf build)"),
    ("diff <(git -C r reset --hard) b",
     "diff <(git -C r reset --hard) b"),
    ('python <(echo "import shutil; shutil.rmtree(1)")',
     'python <(echo "import shutil; shutil.rmtree(1)")'),
    ("sudo bash <(echo 'rm -rf /srv/data')",
     "sudo bash <(echo 'rm -rf /srv/data')"),
    ("ls <(cat a)", "ls <(cat a)"),
    ("(cd x && ls)", ""),
    # ... a quoted `<<` must not swallow real commands as a heredoc body
    # (tokenizer and pre-pass disagree -> raw text kept) ...
    ('echo "use <<EOF"\nrm -rf build\ncat <<EOF\nx\nEOF',
     'echo "use <<EOF" rm -rf build cat <<EOF x EOF'),
    ("cat <<\\A\nx\nA\nbash <<B\nrm -rf /srv/data\nB", "rm -rf data"),
    # ... a heredoc fed to something that executes it is kept ...
    ("ssh host <<'EOF'\nrm -rf /srv\nEOF", "ssh host rm -rf /srv"),
    ("sudo bash <<EOF\nrm -rf /x\nEOF", "sudo bash rm -rf /x"),
    ("psql prod <<SQL\nDROP TABLE t;\nSQL", "psql prod DROP TABLE t;"),
    # ... and an assignment whose value runs a command is not plumbing.
    ("X=`kubectl delete ns prod`", "X=`kubectl delete ns prod`"),
    ("python tests/test_inject.py", "python tests/test_inject.py"),
    ("python -m pkg.mod", "python -m pkg.mod"),
    ('cd "E:/a b/repo" && git stash pop', "git stash pop"),
    ("git push --force origin main", "git push --force origin main"),
    ("npm test 2>/dev/null | tail -5", "npm test"),
    ("npm test 2>&1", "npm test"),
    # Leading env assignments are dropped; the operation stays.
    ("FOO=1 pytest tests/test_a.py", "FOO=1 pytest tests/test_a.py"),
    ("RAILS_ENV=production rake db:drop",
     "RAILS_ENV=production rake db:drop"),
    ("rm -rf /", "rm -rf /"),
    ("rm -rf ~/", "rm -rf ~"),
    ("chmod -R 777 C:\\", "chmod -R 777 C:"),
    # A pipe into a subshell is input to whatever runs inside it.
    ("echo 'rm -rf /srv/data' | (cd / && sudo bash)",
     "echo 'rm -rf /srv/data' | (cd / && sudo bash)"),
    # sed -n is read-only only for a plain address+print script.
    ("sed -n '1e rm -rf /srv/data' f", "sed -n 1e rm -rf /srv/data f"),
    ("sed -n 's/a/b/e' f", "sed -n s/a/b/e f"),
    ("sed -n '$p' f", ""),
    ("sed -n -e '$p' f", "sed -n -e $p f"),
    ("sed -n -e '1p' -e '1e rm -rf /srv/data' f",
     "sed -n -e 1p -e 1e rm -rf /srv/data f"),
    ("sed -n 1p -e '1e rm -rf /srv/data' f",
     "sed -n 1p -e 1e rm -rf /srv/data f"),
    ("sed -n '/re/p' f", "sed -n /re/p f"),
    ("FOO=1 ls", ""),
    ("find . -name x -delete", "find -name x -delete"),
    ("find . -name x", ""),
    ("git commit -F - <<EOF\nmsg\nEOF", "git commit -F - <<EOF msg EOF"),
    ("cat > notes.md <<EOF\nhi\nEOF", "cat > notes.md <<EOF hi EOF"),
    ("cat > wipe.sh <<'EOF'\nrm -rf /srv/data\nEOF\nbash wipe.sh",
     "cat > wipe.sh <<'EOF' rm -rf /srv/data EOF bash wipe.sh"),
    # Absolute paths collapse to their basename; relative ones stay.
    ("git add /srv/proj/x.py", "git add x.py"),
    ("git add C:\\work\\a\\f.txt", "git add f.txt"),
    ("git worktree add ../check main", "git worktree add ../check main"),
    ("git reset --soft origin/main", "git reset --soft origin/main"),
    ("cat /etc/hosts && git push", "git push"),
    ("ls", ""),
    ("", ""),
]


class SanitizeCommandTest(unittest.TestCase):
    def test_table(self):
        for command, expected in COMMAND_TABLE:
            with self.subTest(command=command):
                self.assertEqual(ops.sanitize_pretool_command(command),
                                 expected)

    def test_unbalanced_quote_never_raises(self):
        # Text that cannot be tokenized is kept whole (whitespace-collapsed):
        # never guess at a command the tokenizer could not read.
        self.assertEqual(
            ops.sanitize_pretool_command('echo  "oops && git push origin'),
            'echo "oops && git push origin')
        self.assertIsInstance(
            ops.sanitize_pretool_command("git commit -m 'unterminated"), str)

    def test_non_string_and_cap(self):
        self.assertEqual(ops.sanitize_pretool_command(None), "")
        self.assertLessEqual(
            len(ops.sanitize_pretool_command("git " + "a" * 2000)), 500)

    def test_gold_pretool_contract(self):
        gold = REPO_ROOT / "eval" / "injection_gold.jsonl"
        seen = {}
        for line in gold.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("moment") != "pretool":
                continue
            q = item["query"]
            got = ops.sanitize_pretool_command(q)
            ident = " ".join(q.split())
            seen[item["id"]] = got
            if item["expect"] == "inject":
                self.assertEqual(got, ident, item["id"])
            else:
                self.assertIn(got, (ident, ""), item["id"])
        self.assertTrue(seen)
        self.assertEqual(seen["neg-pt-ls"], "")
        self.assertEqual(seen["ig-pt-rg"], "rg --json pattern src")


class PretoolQueryTextTest(unittest.TestCase):
    def test_none_cases(self):
        for value in (None, "git push", ["x"], {}, {"command": "  "},
                      {"url": "http://x"}, {"command": 5}):
            with self.subTest(value=value):
                self.assertIsNone(ops.pretool_query_text(value))

    def test_edit_style_absolute_path(self):
        self.assertEqual(ops.pretool_query_text(
            {"file_path": "E:/r/skills/memory/scripts/storelib/inject.py"}),
            "inject.py")
        self.assertEqual(ops.pretool_query_text(
            {"file_path": "skills/memory/scripts/storelib/inject.py"}),
            "skills/memory/scripts/storelib/inject.py")

    def test_description_follows_command(self):
        self.assertEqual(ops.pretool_query_text(
            {"command": "ls -la", "description": "List files"}), "")
        self.assertEqual(ops.pretool_query_text(
            {"command": "git stash pop", "description": "Pop stash"}),
            "git stash pop Pop stash")

    def test_description_kept_without_command(self):
        self.assertEqual(ops.pretool_query_text(
            {"description": "Review the migration plan",
             "prompt": "long prompt"}), "Review the migration plan")

    def test_kill_switch(self):
        with mock.patch.dict(os.environ, {"ZMEM_PRETOOL_SANITIZE": "0"}):
            self.assertIsNone(ops.pretool_query_text({"command": "ls"}))
        with mock.patch.dict(os.environ, {"ZMEM_PRETOOL_SANITIZE": "1"}):
            self.assertEqual(ops.pretool_query_text({"command": "ls"}), "")


_STASH = ("pretoolcanary hazard: a later blind git stash pop can apply a "
          "foreign pre-existing stash; verify git stash list first")
_LS_ROWS = [
    "ls output of readme.md skills hooks is stale after a worktree checkout; "
    "cd and ls the directory before trusting wc counts",
    "readme.md skills hooks listing hazard: head of ls skills hooks hides "
    "files, use wc and cd carefully",
    _STASH,
    "basenameratchetcanary: basename ratchet citation re-pin local battery "
    "after renaming test files",
    "wccanary: wc tally hazard, wc and tally disagree about trailing lines",
]
NS = "project:sanitize"


def _env():
    env = dict(os.environ)
    env.update({"ZMEM_STORE": os.environ["ZMEM_STORE"], "ZMEM_DATA": _TMP,
                "ZMEM_MODEL_AUTODOWNLOAD": "0", "PYTHONUTF8": "1"})
    return env


class SelectorIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for content in _LS_ROWS:
            r = subprocess.run(
                [sys.executable, str(SCRIPTS / "store.py"), "add",
                 "--namespace", NS, "--type", "lesson", "--content", content,
                 "--signal", "test", "--confidence", "0.9"],
                capture_output=True, text=True, env=_env(), timeout=120)
            assert r.returncode == 0, r.stdout + r.stderr

    def _select(self, tool_input, **kw):
        conn = schema_mod.connect()
        try:
            flat = " ".join(str(v) for v in tool_input.values())
            env = inject_mod.select_and_budget_for_injection(
                conn, query=flat, namespace=NS, moment="pretool",
                session_id="sanitize-" + uuid.uuid4().hex[:10],
                budget_tokens=2000, data_dir=_TMP, pretool_input=tool_input,
                **kw)
        finally:
            conn.close()
        self.assertIn("rendered", env, sorted(env.keys()))
        return env

    def test_inspection_command_is_silent_and_legacy_is_not(self):
        call = {"command": CD_LS, "description": "List skills and hooks"}
        self.assertEqual(ops.compose_pretool_query(call), ([], ""))
        env = self._select(call)
        self.assertEqual(env["results"], [], sorted(env.keys()))
        self.assertEqual(env["reason"], "empty-pool")
        # Discrimination: with the kill switch the same call retrieves.
        with mock.patch.dict(os.environ, {"ZMEM_PRETOOL_SANITIZE": "0"}):
            legacy = self._select(call)
        self.assertTrue(legacy["results"], sorted(legacy.keys()))

    def test_silent_return_records_nothing(self):
        sid = "sanitize-ledger-" + uuid.uuid4().hex[:8]
        conn = schema_mod.connect()
        try:
            inject_mod.select_and_budget_for_injection(
                conn, query=CD_LS, namespace=NS, moment="pretool",
                session_id=sid, budget_tokens=2000, data_dir=_TMP,
                pretool_input={"command": CD_LS})
        finally:
            conn.close()
        from storelib import delivery_ledger as dl
        self.assertEqual(list(dl.delivered(_TMP, sid)), [])

    def test_stash_pop_still_injects(self):
        env = self._select({"command": "git stash pop"})
        self.assertIn("pretoolcanary", env["rendered"],
                      sorted(env.keys()))

    def test_inspection_naming_a_file_queries_the_file_only(self):
        # `wc tally README.md` is sanitized to "" but derive_ops_tokens still
        # yields "readme.md": the selector looks the FILE up and nothing
        # else.  The wccanary row matches the raw words (wc, tally) and not
        # the file name, so it separates "tokens only" from "raw text".
        call = {"command": "wc tally README.md"}
        self.assertEqual(ops.pretool_query_text(call), "")
        self.assertEqual(ops.compose_pretool_query(call)[0], ["readme.md"])
        env = self._select(call)
        self.assertIn("readme.md", env["rendered"], sorted(env.keys()))
        self.assertNotIn("wccanary", env["rendered"])
        # Discrimination: the raw query (kill switch) does reach wccanary.
        with mock.patch.dict(os.environ, {"ZMEM_PRETOOL_SANITIZE": "0"}):
            legacy = self._select(call)
        self.assertIn("wccanary", legacy["rendered"], sorted(legacy.keys()))

    def test_hazard_after_heredoc_still_injects(self):
        # Review finding (HIGH): a destructive command the sanitizer could
        # mis-parse must never be silenced.  Each shape below was silent in
        # the first cut of this change.
        for command in (
                "cd repo && cat > notes.md <<EOF\ntext\nEOF\ngit stash pop",
                'pwsh -c "git stash pop"',
                "python - <<'PY'\nprint(1)\nPY\ngit stash pop",
                'echo "$(git stash pop)"',
                "ls\ngit stash pop"):
            with self.subTest(command=command):
                env = self._select({"command": command,
                                    "description": "tidy up"})
                self.assertIn("pretoolcanary", env["rendered"],
                              sorted(env.keys()))

    def test_command_checkpoint_overrides_an_empty_sanitizer_verdict(self):
        # Even if the sanitizer wrongly returns "" for a command, a
        # checkpoint phrase the store derives from the COMMAND keeps the
        # legacy raw query and the phrase.  `git stash pop` derives no
        # file-shaped token, so without the checkpoint branch this call
        # would fall through to the silent return: the second half proves
        # that by removing the checkpoint and watching it go silent.
        call = {"command": "git stash pop", "description": "restore work"}
        with mock.patch.object(ops, "pretool_query_text", return_value=""):
            env = self._select(call)
            self.assertIn("pretoolcanary", env["rendered"],
                          sorted(env.keys()))
            with mock.patch.object(ops, "checkpoint_query_expansion",
                                   return_value=""):
                silenced = self._select(call)
        self.assertEqual(silenced["results"], [], sorted(silenced.keys()))

    def test_inline_code_is_never_silenced(self):
        # Final-critic finding: inline interpreter code can do anything
        # (here: a hazard named only inside a Python heredoc, and one named
        # only in the description).  The sanitizer abstains, so the query is
        # the pre-change text and the hazard lesson is still delivered.
        for call in (
                {"command": "python - <<'EOF'\nimport os\n"
                            "os.system('git stash pop')\nEOF",
                 "description": "run helper"},
                {"command": "python -c \"import os\"",
                 "description": "git stash pop the saved work"},
                {"command": "echo 'git stash pop' | bash"},
                # No literal the checkpoint matcher recognises in these
                # two: only the abstain path can keep the hazard words.
                {"command": "echo \"pretoolcanary foreign stash hazard\""
                            " | python"},
                {"command": "echo 'pretoolcanary foreign stash hazard'"
                            " | sudo bash"}):
            with self.subTest(call=call):
                text = ops.pretool_query_text(call)
                self.assertTrue(text)
                self.assertIn("stash", text)
                env = self._select(call)
                self.assertIn("pretoolcanary", env["rendered"],
                              sorted(env.keys()))
                # Abstaining means the SAME rows as the pre-change query.
                with mock.patch.dict(os.environ,
                                     {"ZMEM_PRETOOL_SANITIZE": "0"}):
                    legacy = self._select(call)
                self.assertEqual(
                    sorted(r["id"] for r in env["results"]),
                    sorted(r["id"] for r in legacy["results"]))

    def test_bare_runner_token_does_not_become_the_query(self):
        # If the sanitizer returns "" while the only derived token is a bare
        # runner word (no file shape), the call is silent rather than
        # querying that one generic word.
        call = {"command": "python helper", "description": "probe"}
        with mock.patch.object(ops, "pretool_query_text", return_value=""), \
                mock.patch.object(ops, "compose_pretool_query",
                                  return_value=(["python"], "")):
            env = self._select(call)
        self.assertEqual(env["results"], [], sorted(env.keys()))
        self.assertEqual(env["reason"], "empty-pool")

    def test_query_context_off_skips_sanitation(self):
        # ZMEM_QUERY_CONTEXT=0 removes the matcher that cross-checks the
        # sanitizer, so the sanitizer is not applied at all.
        call = {"command": CD_LS, "description": "List skills and hooks"}
        with mock.patch.dict(os.environ, {"ZMEM_QUERY_CONTEXT": "0"}):
            env = self._select(call)
        self.assertTrue(env["results"], sorted(env.keys()))

    def test_description_checkpoint_does_not_revive_inspection(self):
        # A pure-inspection command whose DESCRIPTION trips a checkpoint
        # phrase stays silent: the description went with the command.
        call = {"command": "ls", "description": "list the test files"}
        self.assertEqual(ops.pretool_query_text(call), "")
        self.assertEqual(ops.compose_pretool_query(call)[0], [])
        self.assertTrue(ops.compose_pretool_query(call)[1])
        env = self._select(call)
        self.assertEqual(env["results"], [], sorted(env.keys()))
        self.assertNotIn("basenameratchetcanary", env["rendered"])
        with mock.patch.dict(os.environ, {"ZMEM_PRETOOL_SANITIZE": "0"}):
            legacy = self._select(call)
        self.assertIn("basenameratchetcanary", legacy["rendered"],
                      sorted(legacy.keys()))


if __name__ == "__main__":
    unittest.main()
