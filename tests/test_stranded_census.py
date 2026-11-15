"""Issue #323 acceptance check: no stranded storelib function (AC1).

DISCRIMINATING — RED at base on the retirement family's leftovers; GREEN
only when the stranded set is empty. The guard is GENERAL — it asserts
nothing about specific function names in the passing state; on failure it
enumerates every stranded ``module:function`` so the disposition ledger
(issue AC2) can be built from the failure output.

Census contract (frozen; PR #452 review round tightened the reference
model after collision-masked strands were proven):

- DEF SET — every ``FunctionDef``/``AsyncFunctionDef`` parsed with ast
  from ``skills/memory/scripts/storelib/*.py``, EXCLUDING dunder protocol
  names (``__*__``): Python invokes those implicitly, so a reference
  census can never see their callers and flagging them would be noise.

- CALLER SCOPE — every ``*.py`` under ``skills/``, ``hooks/``, and
  ``scripts/`` (repo-relative POSIX path), excluding paths containing
  ``/test``, ``__pycache__``, and ``.agents/``. Every file in scope is
  parsed with ast; a function is WIRED when any of these hold:

      1. CODE REFERENCE — an ``ast.Name`` in Load context, or an
         ``ast.Attribute`` attribute, equal to the function name, in any
         in-scope module (the defining module included: internal helpers
         called by their own module are wired, recursion counts). A name
         that is ASSIGNED anywhere in the same module (``ast.Store``) is
         EXCLUDED from that module's reference set: a bound local or
         re-bound global of the same name is not a reference to the
         storelib function (the #452-review collision lesson:
         ``delivered_ids`` was masked by a local variable,
         ``cap`` by ``cap = _cap()``).
      2. IMPORT BINDING — a ``from X import <name> [as alias]`` /
         ``import <name>`` binding in any in-scope module. POLICY
         (documented, disclosed in the PR body): an import binding IS
         wiring — ``storelib/__init__.py`` and the training re-export
         surface are public API for out-of-scope consumers, and a name
         carried by a re-export is not stranded even if no in-repo code
         invokes it. Text occurrence is NEVER wiring: comments,
         docstrings, string/dict-key literals and word prefixes
         ("capture" vs ``cap``) contribute nothing (AST identifiers
         only, exact equality).
      3. STRANDED — a def whose name appears in NO in-scope module's
         reference set and NO import-binding set.

Known-good wiring anchors this census must respect (if the census flags
these, the reference matching is wrong, not the code):
      clear_retry_state                  -> skills/memory/scripts/storelib/cli.py
      evaluate_injection_items           -> scripts/eval_inject_runner.py
      injection_per_moment               -> scripts/eval_inject_runner.py

Known LOUD false-strands (PR #452 review round): functions whose only
references are excluded by the deliberately coarse binding exclusions
above, each hand-verified wired at the cited site. They are allowlisted
here — loudly, with evidence — instead of silently wiring the whole
collision class; a name on this list whose call site disappears must be
re-reviewed by hand:
      organize.rows_by_id  -> organize.py:544 calls rows_by_id(rows)
        (its organize.py Load is excluded by a same-module binding)
      recall.key           -> nested sort-key closure, used via the
        ``key=`` keyword binding at its enclosing sort call (keywords are
        neither Name nor Attribute nodes)

Static analysis only — no store is opened, no ZMEM_* environment is
pinned, and _store_guard is intentionally not imported (nothing here can
resolve a store).

CI-003-R1 counter-mutant: ``Ci003R1CounterMutantTest`` plants synthetic
defs in a scratch scan tree (the census's scan root is swapped for the
test and restored in finally — the census logic itself is untouched) to
prove parameter-name subtraction only masks real references and never
creates false negatives.

Run: python tests/test_stranded_census.py
"""
from __future__ import annotations

import ast
import sys
import tempfile
import unittest
import warnings
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORELIB_DIR = REPO_ROOT / "skills" / "memory" / "scripts" / "storelib"

# Wiring anchors from the issue: functions the census must NEVER flag.
_WIRED_ANCHORS = (
    ("clear_retry_state",
     "skills/memory/scripts/storelib/ops_tokens.py"),
    ("evaluate_injection_items",
     "skills/memory/scripts/storelib/eval_gold.py"),
    ("injection_per_moment",
     "skills/memory/scripts/storelib/eval_gold.py"),
)

# (module_rel_path, function_name) pairs the coarse binding exclusions
# strand LOUDLY although hand verification proves them wired. See the
# module docstring's "Known LOUD false-strands" block for the evidence.
_LOUD_FALSE_STRANDS = frozenset({
    ("skills/memory/scripts/storelib/organize.py", "rows_by_id"),
    ("skills/memory/scripts/storelib/recall.py", "key"),
})


@contextmanager
def _quiet_parse():
    """ast.parse on the analyzed sources re-emits their own Deprecation
    warnings (invalid escape sequences inside the repo's regex strings);
    scoped suppression keeps the failure evidence clean."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield


def _module_references(source: str) -> tuple[set, set]:
    """(refs, imports) for one module.

    refs — identifiers referenced by CODE: ``ast.Attribute`` attribute
    names (any context: ``x.manifest(...)`` uses ``manifest`` even where
    a local of the same name also exists), plus ``ast.Name`` nodes in
    Load context EXCLUDING every id that is ASSIGNED (Store/Del context)
    anywhere in the module (a bound local is its own binding, not a
    reference to a storelib function of the same name — the #452-review
    collision lesson: ``delivered_ids`` was masked by a local variable,
    ``cap`` by ``cap = _cap()``). Aliased imports are followed:
    ``from x import y as z`` makes references to ``z`` references to
    ``y``. A def statement's own name is never an ``ast.Name`` node, so
    a def cannot wire itself; comments, docstrings and string literals
    are not AST nodes and contribute nothing.

    imports — every imported name (the documented binding-is-wiring
    policy: ``storelib/__init__.py`` and the training re-export surface
    are public API for out-of-scope consumers), with aliased imports
    translated back to the imported name.

    SyntaxError -> ({}, {}) (an unparseable file contributes nothing).
    """
    try:
        with _quiet_parse():
            tree = ast.parse(source)
    except SyntaxError:
        return set(), set()
    loads: set = set()
    stores: set = set()
    params: set = set()
    attrs: set = set()
    aliases: dict = {}
    imports: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                loads.add(node.id)
            else:
                stores.add(node.id)
        elif isinstance(node, ast.arg):
            params.add(node.arg)
        elif isinstance(node, ast.Attribute):
            attrs.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imports.add(alias.name)
                if alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
    refs = attrs | (loads - stores - params)
    refs |= {aliases[a] for a in list(refs) if a in aliases}
    imports |= {aliases[a] for a in list(imports) if a in aliases}
    return refs, imports


def _module_defs(tree: ast.Module) -> set:
    """Every function/method name defined in a module, dunder protocols
    excluded."""
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not (node.name.startswith("__") and node.name.endswith("__"))
    }


def run_census():
    """Return (stranded, wired_callers) where stranded is a sorted list of
    ``module.py:function`` with zero callers and wired_callers maps a
    function name to its caller files when non-empty (anchors only)."""
    module_defs = {}
    refs = {}
    imports = {}
    files = {}
    for rel, path in _caller_scope_files():
        source = path.read_text(encoding="utf-8", errors="replace")
        files[rel] = source
        if rel.startswith("skills/memory/scripts/storelib/"):
            try:
                with _quiet_parse():
                    module_defs[rel] = _module_defs(ast.parse(source))
            except SyntaxError as exc:
                raise AssertionError(
                    "storelib module failed to parse — census cannot run: "
                    + rel + ": " + str(exc)) from exc
        refs[rel], imports[rel] = _module_references(source)

    stranded = []
    wired_callers = {}
    anchor_names = {name for name, _ in _WIRED_ANCHORS}
    for rel in sorted(module_defs):
        for name in sorted(module_defs[rel]):
            callers = [crel for crel in sorted(files)
                       if name in refs[crel] or name in imports[crel]]
            if not callers:
                entry = rel + ":" + name
                if (rel, name) in _LOUD_FALSE_STRANDS:
                    continue
                stranded.append(entry)
            elif name in anchor_names:
                wired_callers[name] = callers
    return sorted(stranded), wired_callers


def _caller_scope_files():
    """Every non-test production .py under skills/, hooks/, scripts/ as
    (posix_rel_path, Path) — repo-relative POSIX paths only (no
    backslashes in anything this module builds)."""
    for scope in ("skills", "hooks", "scripts"):
        for path in (REPO_ROOT / scope).rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if "/test" in rel or "__pycache__" in rel or "/.agents/" in rel:
                continue
            yield rel, path


class StrandedCensusTest(unittest.TestCase):
    maxDiff = None  # the full stranded enumeration IS the evidence

    @classmethod
    def setUpClass(cls):
        cls.stranded, cls.wired_callers = run_census()

    def test_no_stranded_storelib_functions(self):
        listing = "\n".join("  " + entry for entry in self.stranded)
        self.assertEqual(
            self.stranded, [],
            "stranded storelib functions (defined with zero non-test "
            "callers under skills/, hooks/, scripts/ — wire each into a "
            "real production caller or remove it with its tests):\n"
            + listing)

    def test_known_wired_anchors_are_not_stranded(self):
        # Caller-shape sanity anchors from the issue: clear_retry_state is
        # CLI-wired (storelib/cli.py) and the eval pair is wired via
        # scripts/eval_inject_runner.py. If THIS census flags them, the
        # census's reference matching is wrong (the #253 lesson: the
        # caller model must cover real dispatch).
        flagged = {
            name: defining
            for name, defining in _WIRED_ANCHORS
            if any(entry == defining + ":" + name
                   for entry in self.stranded)
        }
        self.assertEqual(
            flagged, {},
            "census reference matching is wrong: known-wired functions "
            "flagged as stranded " + repr(flagged))
        for name, _defining in _WIRED_ANCHORS:
            self.assertTrue(
                self.wired_callers.get(name),
                "anchor " + name + " must have at least one caller in the "
                "census's caller scope")


class Ci003R1CounterMutantTest(unittest.TestCase):
    """CI-003-R1 counter-mutant (round-3 cluster 5): parameter-name
    subtraction in the reference model may only ever MASK references that
    are not real, never mask a real one — and, symmetrically, a
    same-named PARAMETER elsewhere must never count as a reference that
    rescues a stranded def (the exact collision class behind the rekey
    false positive).

    The mutation this pins against: a census variant that stopped
    subtracting parameter names (``loads - stores - params``) would count
    the parameter's own Load as a reference, and a genuinely stranded def
    would slip through unflagged. The fixture plants, in a scratch scan
    tree (REPO_ROOT is swapped for the test and restored in finally —
    the census logic itself is untouched):

      * a synthetic storelib def whose name is ALSO a parameter name in
        another in-scope module — its only appearance there is the
        parameter and that parameter's own Load, and it has ZERO real
        call-site references: the census MUST flag it stranded;
      * the inverse control: a second synthetic def WITH a real
        call-site reference in another in-scope module: the census must
        NOT flag it (subtraction never masks a real reference).
    """

    _DEFS_REL = "skills/memory/scripts/storelib/ci003r1_defs.py"
    _STRANDED_ENTRY = _DEFS_REL + ":ci003r1_collision_probe"
    _CONTROL_ENTRY = _DEFS_REL + ":ci003r1_referenced_probe"

    def test_param_collision_still_strands_and_real_reference_does_not(self):
        with tempfile.TemporaryDirectory(prefix="zmem-ci003r1-") as raw:
            root = Path(raw)
            defs_dir = root / "skills" / "memory" / "scripts" / "storelib"
            defs_dir.mkdir(parents=True)
            (defs_dir / "ci003r1_defs.py").write_text(
                "def ci003r1_collision_probe(record):\n"
                "    # Synthetic CI-003-R1 def: its name collides with a\n"
                "    # PARAMETER in ci003r1_param_user.py and it has ZERO\n"
                "    # real call-site references - it must be flagged.\n"
                "    return record\n"
                "\n"
                "\n"
                "def ci003r1_referenced_probe(record):\n"
                "    # Synthetic CI-003-R1 control def: wired by a REAL\n"
                "    # call site in ci003r1_real_caller.py - never flagged.\n"
                "    return record\n",
                encoding="utf-8")
            scripts_dir = root / "scripts"
            scripts_dir.mkdir(parents=True)
            (scripts_dir / "ci003r1_param_user.py").write_text(
                "def enqueue(ci003r1_collision_probe=None):\n"
                "    # The name appears ONLY as a parameter (and that\n"
                "    # parameter's own Load): a bound parameter is not a\n"
                "    # reference to the storelib function - the exact\n"
                "    # collision class that caused the rekey false\n"
                "    # positive.\n"
                "    return ci003r1_collision_probe\n",
                encoding="utf-8")
            (scripts_dir / "ci003r1_real_caller.py").write_text(
                "def run(record):\n"
                "    return ci003r1_referenced_probe(record)\n",
                encoding="utf-8")
            # Swap the census's scan root for the scratch tree; restore
            # the real repo root in finally (the census reads REPO_ROOT
            # at scan time, so the swap scopes exactly this run).
            global REPO_ROOT
            saved_root = REPO_ROOT
            REPO_ROOT = root
            try:
                stranded, _wired = run_census()
            finally:
                REPO_ROOT = saved_root
        self.assertIn(self._STRANDED_ENTRY, stranded,
                      "the census MUST flag the zero-reference def whose "
                      "name collides with a parameter elsewhere - "
                      "parameter-name subtraction may never create a "
                      "false negative (the rekey collision class)")
        self.assertNotIn(self._CONTROL_ENTRY, stranded,
                         "the census must NOT flag the same-shaped def "
                         "that HAS a real call-site reference - "
                         "subtraction only masks non-references")
        self.assertEqual(
            stranded, [self._STRANDED_ENTRY],
            "the synthetic tree's full stranded set is exactly the "
            "collision probe: nothing else may strand, and the collision "
            "may not be masked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
