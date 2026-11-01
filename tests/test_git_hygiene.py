"""Frozen acceptance check C9 (issue #360, AC9): every NETWORK-CAPABLE git
invocation in the test suite runs non-interactively with the credential
prompt disabled. GREEN-at-base standing guard (ratchet).

Scope (coordinator decision, pre-freeze): #360's defect class is the git
call that can prompt for credentials — fetch, pull, push, clone,
ls-remote, `remote update`, `submodule update --remote`. Local
read/config subcommands (rev-parse, merge-base, diff, ls-files, ls-tree,
status, init, commit-family, config, remote add/remove, ...) can never
prompt and are out of the flagged class; the base-tree census of 40
unguarded LOCAL sites is disclosed verbatim in
.agents/issue-traces/353-356-357-360-store-integrity/repro/c9-local-git-census.txt.

The AST walker finds git invocation sites (subprocess.run/call/check_call/
check_output/Popen as module attribute or `import subprocess as X` alias,
argv heads that are constants or resolvable names like
REAL_GIT = shutil.which("git"), shell strings, local bindings, plus
os.system). A site is NETWORK-class when the subcommand token after `git`
(global -c/-C/... options skipped) is flagged; it must then carry
GIT_TERMINAL_PROMPT set to the literal '0' as the EFFECTIVE value in the
passed env (dict literal / {**os.environ,...} / dict(...) / resolvable
binding / | merge, evaluated in override order — later dict entries,
right-hand merge operands, and the latest preceding ambient assignment
win; plan-critic round 2) or ambiently when no env= is passed.
stdin=DEVNULL is NOT a guard (git credential prompts read the terminal,
not stdin). A subcommand fed through unresolvable *args forwarding into a
helper is statically invisible (documented residual; no such call site
exists today).

At base the only network sites are test_taxonomy_rfc.py's guarded
fetch/deepen loop, so the ratchet is GREEN at base and fails on any NEW
unguarded network call with:
    AssertionError: unguarded network git invocations: tests/<f>.py:<line>, ...
A discrimination self-test (unguarded fetch MUST flag; guarded and local
shapes MUST NOT) and a positive control on tests/test_taxonomy_rfc.py
(fetch-family sites MUST be recognized AND guarded — killing a mutant
where network detection breaks and everything looks green) keep it honest.
No storelib import happens in this file; the hermetic env pin is kept for
uniform discipline (restored in tearDownClass).

Run: python tests/test_git_hygiene.py
"""

from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

_HERMETIC_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                  "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_NEWER_SCHEMA")
_SUBPROC = {"run", "call", "check_call", "check_output", "Popen"}
_PROMPT = "GIT_TERMINAL_PROMPT"
_UNKNOWN = "?"  # key present, winning value not a provable '0' literal
_NETWORK_SUBCMDS = {"fetch", "pull", "push", "clone", "ls-remote"}
# git OPTIONS that consume the NEXT argv token (their value).
_TAKES_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace",
                "--super-prefix", "--exec-path"}


def _subprocess_receivers(tree) -> set[str]:
    """Names the file binds to the subprocess module ('subprocess' plus
    every `import subprocess as X` alias, wherever the import sits)."""
    names = {"subprocess"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess" and alias.asname:
                    names.add(alias.asname)
    return names


def _method(fn: ast.AST) -> str | None:
    return fn.attr if isinstance(fn, ast.Attribute) else None


def _resolve_argv(expr, binds) -> list | None:
    """The argv ELEMENT list when expr is a git invocation, else None."""
    if isinstance(expr, (ast.List, ast.Tuple)):
        elts = list(expr.elts)
        if not elts or isinstance(elts[0], ast.Starred):
            return None
        first = elts[0]
        if isinstance(first, ast.Constant) and first.value == "git":
            return elts
        if isinstance(first, ast.Name):  # [REAL_GIT, "init", ...]
            for _ln, v in binds.get(first.id, []):
                if isinstance(v, ast.Call) and _method(v.func) == "which" \
                        and v.args and isinstance(v.args[0], ast.Constant) \
                        and v.args[0].value == "git":
                    return elts
        return None
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str) \
            and expr.value.split()[:1] == ["git"]:
        return [ast.Constant(value=t) for t in expr.value.split()]
    if isinstance(expr, ast.Call):
        return None  # shutil.which("git") as the WHOLE argv: no subcommand
    if isinstance(expr, ast.Name):
        for _ln, v in binds.get(expr.id, []):
            resolved = _resolve_argv(v, binds)
            if resolved is not None:
                return resolved
        return None
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return _resolve_argv(expr.left, binds)
    return None


def _network_subcmd(elts) -> str | None:
    """The flagged subcommand when the argv is network-capable, else None.
    Global options before the subcommand (-c PAIR, -C PATH, --flag) are
    skipped; an unresolvable token (Name/Starred) fails open to local."""
    tokens: list[str | None] = []
    for e in elts[1:]:
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            tokens.append(e.value)
        else:
            tokens.append(None)
    rest = tokens
    while rest:
        tok = rest[0]
        if tok is not None and tok in _TAKES_VALUE and len(rest) >= 2:
            rest = rest[2:]
        elif tok is not None and tok.startswith("-"):
            rest = rest[1:]
        else:
            break
    if not rest or rest[0] is None:
        return None
    sub = rest[0]
    if sub in _NETWORK_SUBCMDS:
        return sub
    if sub == "remote" and len(rest) > 1 and rest[1] == "update":
        return "remote update"
    # Any `submodule update` is network-capable: --init fetches when the
    # submodule is not cached locally, so flags do not narrow the class
    # (plan-critic round 1).
    if sub == "submodule" and len(rest) > 1 and rest[1] == "update":
        return "submodule update"
    # `git archive --remote=<url>` (either spelling) transfers from a remote.
    if sub == "archive" and any(
            (t == "--remote" or (t is not None and t.startswith("--remote=")))
            for t in rest):
        return "archive --remote"
    return None


def _prompt_effective_value(expr, binds):
    """Effective GIT_TERMINAL_PROMPT value: '0', another literal, or None.

    Evaluation is flow-sensitive in override order (plan-critic round 2):
    later dict entries, right-hand `|` merge operands, later dict()/update()
    arguments, and the latest name binding win. A key whose winning value is
    not a string literal yields the sentinel '?' (present but not provably
    disabled) so unknown shapes read as UNGUARDED — the fail-closed
    direction for a standing ratchet.
    """
    if expr is None:
        return None
    if isinstance(expr, ast.Dict):
        val = None
        for i, v in enumerate(expr.values):
            k = expr.keys[i] if i < len(expr.keys) else None
            if isinstance(k, ast.Constant) and k.value == _PROMPT:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    val = v.value
                else:
                    val = _UNKNOWN  # present, not provably '0'
            elif k is None:  # **merge at this position overrides earlier
                got = _prompt_effective_value(v, binds)
                if got is not None:
                    val = got
        return val
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        right = _prompt_effective_value(expr.right, binds)
        if right is not None:
            return right
        return _prompt_effective_value(expr.left, binds)
    if isinstance(expr, ast.Call):
        name = _method(expr.func) or (
            expr.func.id if isinstance(expr.func, ast.Name) else "")
        if name in ("dict", "update", "setdefault", "copy"):
            val = None
            for a in expr.args:
                got = _prompt_effective_value(a, binds)
                if got is not None:
                    val = got
            for kw in expr.keywords:
                got = _prompt_effective_value(kw.value, binds)
                if got is not None:
                    val = got
            return val
        return None
    if isinstance(expr, ast.Name):
        # Round 3: an env dict mutated after binding (.update()/.setdefault()/
        # item-assign) can flip the prompt value after the '0' was observed.
        # Tracking exact mutation order is out of scope for a ratchet — any
        # mutation on the name anywhere in the scope reads as UNKNOWN
        # (fail-closed: the site is flagged until the final value is
        # inlined into the dict literal passed to subprocess).
        if expr.id in binds.get("__mutated_names__", ()):
            return _UNKNOWN
        entries = binds.get(expr.id, [])
        if not entries:
            return None
        # binds maps name -> [(lineno, value_expr)] sorted by lineno; the
        # LAST assignment before use is the effective env.
        return _prompt_effective_value(entries[-1][1], binds)
    return None


def _ambient_effective_value_before(scope, line: int):
    """Effective ambient GIT_TERMINAL_PROMPT at `line`: the latest
    `os.environ[...] = <literal>` assignment before it (plan-critic round
    2 — an earlier '0' overridden by a later '1' must NOT read guarded)."""
    best_line = -1
    val = None
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and getattr(node, "lineno", 0) < line:
            for t in node.targets:
                if (isinstance(t, ast.Subscript)
                        and isinstance(t.value, ast.Attribute)
                        and t.value.attr == "environ"
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value == _PROMPT):
                    if node.lineno > best_line:
                        best_line = node.lineno
                        if isinstance(node.value, ast.Constant) \
                                and isinstance(node.value.value, str):
                            val = node.value.value
                        else:
                            val = _UNKNOWN
    return val


def _scope_bindings(scope) -> dict[str, list]:
    """name -> [(lineno, value_expr)] with entries sorted by lineno, so a
    name's LAST assignment is its effective value (plan-critic round 2).
    Also records '__mutated_names__': names mutated via .update()/.setdefault()/
    item-assign/|= after binding — env lookups through such a name read as
    UNKNOWN (plan-critic round 3, fail-closed)."""
    binds: dict[str, list] = {}
    mutated: set[str] = set()
    for node in ast.walk(scope):
        targets: list = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                binds.setdefault(t.id, []).append(
                    (getattr(node, "lineno", 0), node.value))
            elif (isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Name)):
                mutated.add(t.value.id)
        if (isinstance(node, ast.AugAssign)
                and isinstance(node.target, ast.Name)
                and isinstance(node.op, ast.BitOr)):
            mutated.add(node.target.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("update", "setdefault") \
                and isinstance(node.func.value, ast.Name):
            mutated.add(node.func.value.id)
    for entries in binds.values():
        entries.sort(key=lambda e: e[0])
    if mutated:
        binds["__mutated_names__"] = sorted(mutated)
    return binds


def scan_sites(src: str, filename: str = "<snippet>") -> list[dict]:
    """Every NETWORK-class git invocation site: {file, line, subcmd,
    guarded}. Local-read/config subcommands are out of the flagged class."""
    tree = ast.parse(src, filename=filename)
    receivers = _subprocess_receivers(tree)
    sites: list[dict] = []

    def visit(node, scope, binds):
        for child in ast.iter_child_nodes(node):
            child_binds, child_scope = binds, scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Module)):
                # Inner bindings ADD to the outer scope's (a module-level
                # argv binding like REAL_GIT = shutil.which("git") stays
                # resolvable inside methods).
                child_binds = dict(binds)
                for k, v in _scope_bindings(child).items():
                    child_binds.setdefault(k, []).extend(v)
                child_scope = child
            if isinstance(child, ast.Call):
                fn = child.func
                fname = _method(fn)
                is_site = False
                if isinstance(fn, ast.Attribute) and isinstance(fn.value,
                                                                ast.Name):
                    if fname in _SUBPROC and fn.value.id in receivers:
                        is_site = True
                    elif fname == "system" and fn.value.id == "os":
                        is_site = True
                if is_site and child.args:
                    elts = _resolve_argv(child.args[0], binds)
                    if elts is not None:
                        subcmd = _network_subcmd(elts)
                        if subcmd is not None:
                            # Guard = EFFECTIVE GIT_TERMINAL_PROMPT value
                            # '0' in the child env (override order
                            # evaluated) or ambiently before the call.
                            # stdin=DEVNULL alone is NOT a guard (round 1);
                            # a later '1' override defeats an earlier '0'
                            # (round 2).
                            env_expr = next(
                                (kw.value for kw in child.keywords
                                 if kw.arg == "env"), None)
                            if env_expr is not None:
                                ok = _prompt_effective_value(
                                    env_expr, binds) == "0"
                            else:
                                ok = _ambient_effective_value_before(
                                    scope, child.lineno) == "0"
                            sites.append({"file": filename,
                                          "line": child.lineno,
                                          "subcmd": subcmd,
                                          "guarded": bool(ok)})
            visit(child, child_scope, child_binds)

    visit(tree, tree, _scope_bindings(tree))
    return sites


def scan_unguarded_network(src: str, filename: str = "<snippet>") -> list[str]:
    return [f"{s['file']}:{s['line']}" for s in scan_sites(src, filename)
            if not s["guarded"]]


class GitHygieneTest(unittest.TestCase):
    _tmpdir: str | None = None
    _saved_env: dict | None = None

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="zmem-c9-git-")
        cls._saved_env = {k: os.environ.get(k) for k in _HERMETIC_KEYS}
        os.environ["ZMEM_STORE"] = str(Path(cls._tmpdir) / "store.sqlite")
        os.environ["ZMEM_DATA"] = str(Path(cls._tmpdir) / "data")
        os.environ["ZMEM_MODELS_DIR"] = str(Path(cls._tmpdir) / "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)

    @classmethod
    def tearDownClass(cls):
        if cls._saved_env is not None:
            for k, v in cls._saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            cls._saved_env = None
        if cls._tmpdir is not None:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)
            cls._tmpdir = None

    def test_network_git_invocations_are_noninteractive(self):
        """Standing ratchet (GREEN at base): every network-capable git call
        carries an EFFECTIVE GIT_TERMINAL_PROMPT='0' (override order
        evaluated; stdin=DEVNULL is NOT a guard)."""
        findings: list[str] = []
        for path in sorted(TESTS_DIR.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            try:
                found = scan_unguarded_network(
                    src, path.relative_to(REPO_ROOT).as_posix())
            except SyntaxError as exc:
                findings.append(f"{path.name}: unparseable ({exc})")
                continue
            findings.extend(found)
        if findings:
            raise AssertionError(
                "unguarded network git invocations: " + ", ".join(findings))

    def test_scanner_discriminates_known_shapes(self):
        """The scan must be able to fail: unguarded fetch-family shapes are
        flagged; guarded and local-read shapes are not."""
        guarded = (
            "import subprocess, os\n"
            "def f():\n"
            "    subprocess.run(['git', 'fetch', 'origin'],\n"
            "                   env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},\n"
            "                   stdin=subprocess.DEVNULL)\n")
        ambient = (
            "import subprocess, os\n"
            "def h():\n"
            "    os.environ['GIT_TERMINAL_PROMPT'] = '0'\n"
            "    subprocess.run(['git', 'pull'], capture_output=True)\n")
        bare = (
            "import subprocess\n"
            "def g():\n"
            "    subprocess.run(['git', 'fetch', 'origin'], check=True)\n")
        with_flags = (
            "import subprocess\n"
            "def w():\n"
            "    subprocess.run(['git', '-c', 'user.email=t@t',\n"
            "                    'fetch', 'origin'], check=True)\n")
        local_read = (
            "import subprocess\n"
            "def r():\n"
            "    subprocess.run(['git', 'rev-parse', 'HEAD'], check=True)\n")
        remote_update = (
            "import subprocess\n"
            "def u():\n"
            "    subprocess.run(['git', 'remote', 'update'], check=True)\n")
        ls_remote = (
            "import subprocess\n"
            "def l():\n"
            "    subprocess.run(['git', 'ls-remote', 'origin'])\n")
        prompt_enabled = (
            "import subprocess, os\n"
            "def p():\n"
            "    subprocess.run(['git', 'fetch', 'origin'],\n"
            "                   env={**os.environ, 'GIT_TERMINAL_PROMPT': '1'})\n")
        stdin_only = (
            "import subprocess\n"
            "def s():\n"
            "    subprocess.run(['git', 'fetch', 'origin'],\n"
            "                   stdin=subprocess.DEVNULL)\n")
        submodule_init = (
            "import subprocess\n"
            "def m():\n"
            "    subprocess.run(['git', 'submodule', 'update', '--init'])\n")
        archive_remote = (
            "import subprocess\n"
            "def a():\n"
            "    subprocess.run(['git', 'archive', '--remote=up', 'HEAD'])\n")
        guarded_submodule = (
            "import subprocess, os\n"
            "def n():\n"
            "    subprocess.run(['git', 'submodule', 'update', '--remote'],\n"
            "                   env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})\n")
        # Plan-critic round 2: a later '1' override must defeat an earlier
        # '0' — the EFFECTIVE value is what git sees.
        dict_override = (
            "import subprocess\n"
            "def d():\n"
            "    subprocess.run(['git', 'fetch'], env={\n"
            "        'GIT_TERMINAL_PROMPT': '0',\n"
            "        'GIT_TERMINAL_PROMPT': '1'})\n")
        merge_override = (
            "import subprocess, os\n"
            "def mm():\n"
            "    subprocess.run(['git', 'fetch'], env=\n"
            "        {'GIT_TERMINAL_PROMPT': '0'}\n"
            "        | {'GIT_TERMINAL_PROMPT': '1'})\n")
        ambient_override = (
            "import subprocess, os\n"
            "def am():\n"
            "    os.environ['GIT_TERMINAL_PROMPT'] = '0'\n"
            "    os.environ['GIT_TERMINAL_PROMPT'] = '1'\n"
            "    subprocess.run(['git', 'fetch'])\n")
        # Plan-critic round 3: a bound env dict mutated after the '0' was
        # set can flip the value; the scanner must read it as UNKNOWN
        # (fail-closed -> flagged).
        update_after_bind = (
            "import subprocess, os\n"
            "def ub():\n"
            "    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}\n"
            "    env.update({'GIT_TERMINAL_PROMPT': '1'})\n"
            "    subprocess.run(['git', 'fetch'], env=env)\n")
        self.assertEqual(scan_unguarded_network(guarded), [])
        self.assertEqual(scan_unguarded_network(ambient), [])
        self.assertEqual(scan_unguarded_network(local_read), [])
        self.assertEqual(scan_unguarded_network(guarded_submodule), [])
        self.assertEqual(scan_unguarded_network(bare), ["<snippet>:3"])
        self.assertEqual(scan_unguarded_network(with_flags), ["<snippet>:3"])
        self.assertEqual(scan_unguarded_network(remote_update), ["<snippet>:3"])
        self.assertEqual(scan_unguarded_network(ls_remote), ["<snippet>:3"])
        self.assertEqual(scan_unguarded_network(prompt_enabled),
                         ["<snippet>:3"])
        self.assertEqual(scan_unguarded_network(stdin_only), ["<snippet>:3"])
        self.assertEqual(scan_unguarded_network(submodule_init),
                         ["<snippet>:3"])
        self.assertEqual(scan_unguarded_network(archive_remote),
                         ["<snippet>:3"])
        self.assertEqual(scan_unguarded_network(dict_override),
                         ["<snippet>:3"])
        self.assertEqual(scan_unguarded_network(merge_override),
                         ["<snippet>:3"])
        self.assertEqual(scan_unguarded_network(ambient_override),
                         ["<snippet>:5"])
        self.assertEqual(scan_unguarded_network(update_after_bind),
                         ["<snippet>:5"])

    def test_known_guarded_fetch_site_is_recognized(self):
        """Positive control on the live suite: tests/test_taxonomy_rfc.py's
        fetch/deepen loop MUST be classified as network AND guarded — a
        scanner regression that stops detecting network calls (everything
        looks local, ratchet goes vacuously green) fails here."""
        src = (TESTS_DIR / "test_taxonomy_rfc.py").read_text(encoding="utf-8")
        sites = scan_sites(src, "tests/test_taxonomy_rfc.py")
        network = [s for s in sites if s["subcmd"]]
        self.assertTrue(
            network, "the taxonomy_rfc fetch/deepen loop must be detected "
            "as network-class git invocations")
        self.assertIn("fetch", {s["subcmd"] for s in network})
        self.assertGreaterEqual(len(network), 2)
        self.assertEqual(
            [f"{s['file']}:{s['line']}" for s in network
             if not s["guarded"]],
            [], "every taxonomy_rfc fetch-family call must carry the "
            "non-interactive guard")


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print("CHECK-PASS" if _result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if _result.wasSuccessful() else 1)
