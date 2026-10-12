"""Guardrail for issue #250's defect class.

The behavioural contract is pinned by ``test_p02_ledger_records_expansion.py``
(a neighbour must be recorded, and must render once). This module pins the
*shape* that made the defect possible: the passive selector built ONE list for
TWO accounting sinks, filtered it for popularity (#136), and handed the same
filtered list to the delivery ledger (#114). Rows exempt from one semantics were
silently exempt from the other.

A future refactor can collapse the two lists back together without breaking any
existing fixture — the fixtures pin the outcome, not the structure. This guard
fails on the structure, so the regression cannot be reintroduced quietly.

Reads the source of ``select_and_budget_for_injection`` (inject.py)
and of ``recall_memory`` (recall.py); it imports
no storelib module, so it cannot freeze a store path or touch the operator's
``~/.zmem``.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

STORELIB = Path(__file__).resolve().parent.parent / "skills" / "memory" / "scripts" / "storelib"
INJECT_PY = STORELIB / "inject.py"
RECALL_PY = STORELIB / "recall.py"


def _selector_source() -> str:
    return INJECT_PY.read_text(encoding="utf-8")


def _selector_body() -> ast.FunctionDef:
    """The AST node for select_and_budget_for_injection."""
    tree = ast.parse(_selector_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) \
                and node.name == "select_and_budget_for_injection":
            return node
    raise AssertionError("select_and_budget_for_injection not found in inject.py")


def _assigned_name(node: ast.AST) -> str | None:
    """Name a plain or ANNOTATED assignment binds.

    A type annotation (``ledger_rows: list = ...``) is semantically the same
    binding; treating only ``ast.Assign`` as an assignment made the guardrail
    fail on a purely syntactic edit while missing nothing behavioural.
    """
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return (node.targets[0].id
                if isinstance(node.targets[0], ast.Name) else None)
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _assigned_value(node: ast.AST) -> ast.AST | None:
    """Right-hand side of a plain or annotated assignment."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.value
    return None


def _call_arg_names(func: ast.FunctionDef, dotted: str) -> list[ast.Call]:
    """Every Call node in ``func`` whose callee name is ``dotted``."""
    out: list[ast.Call] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
        elif isinstance(node.func, ast.Name):
            name = node.func.id
        else:
            name = None
        if name == dotted:
            out.append(node)
    return out


def _rowset_names(func: ast.FunctionDef) -> set[str]:
    """Locals in ``func`` that hold a list of row dicts.

    A rowset is assigned from ``rows_present_in(...)`` or from a list
    comprehension over another rowset. Naming them lets the sink arguments be
    matched by meaning rather than by position, so a scalar argument like
    ``resolved_data`` or ``moment`` is never mistaken for the row set.
    """
    names: set[str] = set()
    assigns = [node for node in ast.walk(func)
               if _assigned_name(node) is not None]
    for node in assigns:
        target = _assigned_name(node)
        value = _assigned_value(node)
        if isinstance(value, ast.Call):
            if isinstance(value.func, ast.Attribute) \
                    and value.func.attr == "rows_present_in":
                names.add(target)
        elif isinstance(value, ast.ListComp) and len(value.generators) == 1:
            it = value.generators[0].iter
            if isinstance(it, ast.Name) and it.id in names:
                names.add(target)
            elif isinstance(it, ast.Call):
                names.add(target)
    return names


def _consumed_rowset(func: ast.FunctionDef, call: ast.Call) -> str | None:
    """Name of the rowset ``call`` consumes, or None."""
    rowsets = _rowset_names(func)
    for arg in call.args:
        if isinstance(arg, ast.ListComp) and len(arg.generators) == 1:
            it = arg.generators[0].iter
            if isinstance(it, ast.Name):
                return it.id
            if isinstance(it, ast.Subscript) and isinstance(it.value, ast.Name):
                return it.value.id
        elif isinstance(arg, ast.Name) and arg.id in rowsets:
            return arg.id
        elif isinstance(arg, ast.Subscript) and isinstance(arg.value, ast.Name) \
                and arg.value.id in rowsets:
            return arg.value.id
    return None


class LedgerBumpSetsAreDistinctTest(unittest.TestCase):
    """Issue #250 guardrail: two sinks, two sets."""

    def setUp(self):
        self.func = _selector_body()

    def test_ledger_record_and_bump_telemetry_receive_distinct_lists(self):
        """The two sinks must not receive the same row set."""
        record_calls = _call_arg_names(self.func, "record")
        bump_calls = _call_arg_names(self.func, "_bump_telemetry")

        self.assertTrue(record_calls, "ledger.record call not found in the selector")
        self.assertTrue(bump_calls, "_bump_telemetry call not found in the selector")

        recorded = {n for n in (_consumed_rowset(self.func, c) for c in record_calls) if n}
        bumped = {n for n in (_consumed_rowset(self.func, c) for c in bump_calls) if n}

        self.assertIn(
            "ledger_rows", recorded,
            "ledger.record must consume a dedicated ledger_rows set, not the "
            "popularity-filtered list (issue #250)",
        )
        self.assertIn(
            "bump_rows", bumped,
            "_bump_telemetry must consume a dedicated bump_rows set",
        )
        self.assertNotIn(
            "present", recorded | bumped,
            "one 'present' list feeding both sinks is the issue #250 defect shape "
            "and must not come back",
        )
        self.assertEqual(
            recorded & bumped, set(),
            "the two accounting sinks must not share a row set",
        )

    def test_bump_set_is_derived_from_the_ledger_set(self):
        """bump_rows must be a subset of ledger_rows, never the reverse.

        The subset direction is what lets one withheld-secret guard cover both
        consumers: withheld rows are removed from ledger_rows first, so
        anything derived from it is already clean.
        """
        assigns = _rowset_comprehensions(self.func)
        bump_targets = {name for name, src in assigns.items() if src == "ledger_rows"}
        self.assertIn(
            "bump_rows", bump_targets,
            "bump_rows must be derived from ledger_rows so the withheld-secret "
            "guard covers both consumers (issue #250 / #256)",
        )
        ledger_targets = {name for name, src in assigns.items() if src == "bump_rows"}
        self.assertNotIn(
            "ledger_rows", ledger_targets,
            "ledger_rows must not be derived from bump_rows; that would re-exempt "
            "expansion and graph-only rows from the delivery ledger (issue #250)",
        )

    def test_ledger_set_is_still_bounded_by_the_rendered_fence(self):
        """#114: the rendered fence is the ceiling. Never record a row the model
        did not see, e.g. a budget-dropped candidate."""
        record_calls = _call_arg_names(self.func, "record")
        self.assertTrue(record_calls)
        self.assertEqual(
            _consumed_rowset(self.func, record_calls[0]), "ledger_rows",
            "ledger.record must consume ledger_rows",
        )
        chain = _assignment_chain(self.func, "ledger_rows")
        self.assertTrue(chain, "no assignment to ledger_rows found")
        self.assertIn(
            "ledger.rows_present_in(rows, rendered)", chain[0],
            "ledger_rows must be seeded from rows_present_in(rows, rendered) so a "
            "budget-dropped or gate-rejected row is never recorded (#114)",
        )

    def test_withheld_guard_precedes_the_popularity_filter(self):
        """#256: a withheld row is not rendered content — never bumped, never
        ledgered.

        The guard lives on ledger_rows, BEFORE bump_rows is derived from it, so
        one filter covers both consumers. A second copy of the filter on
        bump_rows could drift and is rejected.
        """
        ledger_chain = _assignment_chain(self.func, "ledger_rows")
        bump_chain = _assignment_chain(self.func, "bump_rows")
        self.assertTrue(ledger_chain, "no assignment to ledger_rows found")
        self.assertTrue(bump_chain, "no assignment to bump_rows found")

        withheld_at = [i for i, src in enumerate(ledger_chain)
                       if "withheld_for_secret" in src]
        self.assertTrue(
            withheld_at,
            "ledger_rows must drop withheld-for-secret rows (#256)",
        )
        self.assertEqual(
            withheld_at[0], len(ledger_chain) - 1,
            "the withheld-secret guard must be the LAST filter applied to "
            "ledger_rows, so nothing downstream can reintroduce a withheld row",
        )
        self.assertEqual(
            len(bump_chain), 1,
            "bump_rows must be derived in one step from the already-filtered "
            "ledger_rows; a re-filter here could drift from the withheld guard",
        )
        self.assertNotIn(
            "withheld_for_secret", bump_chain[0],
            "bump_rows inherits the withheld guard from ledger_rows; repeating "
            "the filter here is the drift risk (#256)",
        )
        self.assertIn("link_relation", bump_chain[0],
                      "bump_rows is the popularity set and must apply the #136 filter")

    def test_plain_recall_path_does_not_own_the_ledger(self):
        """``recall_memory`` does not record deliveries.

        A plain recall must never write the delivery ledger: if a future edit
        hands ``recall_memory`` either accounting set, the two sinks would fuse
        again on a second path. ``recall_memory`` lives in ``recall.py``, not
        ``inject.py``, so this parses that module — an earlier draft scanned
        ``inject.py``, where the function does not exist, and the assertion
        never executed.
        """
        tree = ast.parse(RECALL_PY.read_text(encoding="utf-8"))
        funcs = [node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef)
                 and node.name == "recall_memory"]
        self.assertEqual(
            len(funcs), 1,
            "recall_memory must be found exactly once in recall.py for this "
            "guard to be meaningful")
        for name in ("ledger_rows", "bump_rows"):
            self.assertFalse(
                _assignment_chain(funcs[0], name),
                f"recall_memory now assigns {name}; the delivery ledger stays "
                "owned by the passive selector only",
            )
        self.assertFalse(
            any(isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "record"
                and isinstance(node.func.value, ast.Name)
                and "ledger" in node.func.value.id.lower()
                for node in ast.walk(funcs[0])),
            "recall_memory must not call a ledger record function; the plain "
            "lane is not a delivery surface",
        )


def _assignment_chain(func: ast.FunctionDef, name: str) -> list[str]:
    """Source of every ``name = <expr>`` in ``func``, in source order.

    A name may be assigned more than once (ledger_rows is filtered, then
    filtered again), so this returns the chain rather than the final value.
    """
    out: list[tuple[int, str]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == name:
            out.append((node.lineno, ast.unparse(node.value)))
    return [src for _, src in sorted(out)]


def _rowset_comprehensions(func: ast.FunctionDef) -> dict[str, str]:
    """Map ``name = [row for row in <iterable>]`` -> iterable name."""
    out: dict[str, str] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.ListComp) \
                and len(node.value.generators) == 1:
            it = node.value.generators[0].iter
            if isinstance(it, ast.Name):
                out[node.targets[0].id] = it.id
    return out


if __name__ == "__main__":
    unittest.main()