"""Issue #250 acceptance checks: expansion-arrival rows render but are never
ledgered as delivered.

In ``storelib/inject.py`` the list of rendered rows (``present``) is filtered
to drop rows carrying ``link_relation`` or ``_graph_arrival_only`` before it
is passed to BOTH ``_bump_telemetry`` and ``ledger.record``. That filter is
the correct popularity law (expansion rows must never be bumped — popularity
rewards query-matched rows only), but it is the wrong delivery law: a row
that arrives only through budgeted 1-hop link expansion, or only through the
graph-seed arm, still renders every event in a session and therefore IS
delivered — so it must be recorded in the session's delivery ledger, or
nothing ever suppresses its re-delivery on the next call.

Fixture (the two rows the issue names, built through the real CLI):
  parent P     "keep the flange calibrated before every launch" (query-matched)
  neighbour N  "unrelated zebra migration patterns note"        (expansion-only)
  links --add P N --relation related

``GraphArmNeighbourTest`` additionally tags P ``flange`` and N ``zebra``: the
query seeds the entity lane through P's tag and the graph arm walks one hop
to N, so N's result row carries ``_graph_arrival_only=True`` and NO
``link_relation`` (N is already in the fused set when link expansion runs, so
expansion cannot claim it). ``LinkExpansionNeighbourTest`` adds NO tags: the
entity/graph arms stay empty and N arrives only through link expansion, so
N's row carries ``link_relation="related"``.

Every test first pins the ARRIVAL-SHAPE PRECONDITION — N renders on the
first call and its result row carries the shape its class names — so a
fixture that stops rendering N, or renders it through a different arm, fails
loudly instead of passing for the wrong reason.

Ledger truth: the selector resolves its sidecar directory from the RESOLVED
store's parent (inject.py's ``_injection_data_dir``), so with
``ZMEM_STORE=<tmp>/store.sqlite`` the ledger for a session lives at
``<tmp>/ops/<sha256(session_id)[:32]>.ledger`` — NOT ``ZMEM_DATA/ops/...``.
The file is read directly from disk; storelib is never imported (its
STORE_PATH freezes at first import).

Drives the real ``store.py`` CLI via subprocess against a throwaway temp
store; the operator's ``~/.zmem`` is never a test target. No network, no
model download (``ZMEM_MODELS_DIR`` pinned at a missing directory).

Run: python tests/test_p02_ledger_records_expansion.py [Class[.test]]
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
NAMESPACE = "project:p02ledger"
SESSION = "sess-p02"
PARENT = "keep the flange calibrated before every launch"
NEIGHBOUR = "unrelated zebra migration patterns note"
QUERY = "flange calibrated launch"
CALLS = 3


def _clean_env(tmp: Path) -> dict[str, str]:
    """A hermetic subprocess env: throwaway store, model absent, ambient
    zmem knobs stripped so an operator's environment cannot redirect the
    fixture (ZMEM_STORE outranks ZMEM_DATA and would silently retarget the
    operator's real ~/.zmem if it leaked through)."""
    strip = {
        "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_ROOT",
        "ZMEM_NAMESPACE", "ZMEM_HOST", "ZMEM_INJECT",
        "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_INJECT_FLOOR_RECENT",
        "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_TEST_NOW",
        "ZMEM_EMBED_PROFILE", "ZMEM_CROSS_ENCODER", "ZMEM_QUERY_CONTEXT",
        "ZMEM_SESSION", "ZMEM_GRAPH_SEED",
        "ZMEM_CROSS_PROJECT", "ZMEM_CROSS_PROJECT_READ",
        "ZMEM_CROSS_PROJECT_WRITE", "ZMEM_CROSS_PROJECT_HAZARD_VERBS",
        "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID", "CLAUDE_PLUGIN_DATA",
        "ZCODE_PLUGIN_DATA", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    }
    env = {k: v for k, v in os.environ.items() if k not in strip}
    env.update({
        "ZMEM_STORE": str(tmp / "store.sqlite"),
        "ZMEM_DATA": str(tmp / "data"),
        "ZMEM_MODELS_DIR": str(tmp / "no-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_AUTO_REKEY": "0",
        "ZMEM_INJECT": "1",
        "PYTHONUTF8": "1",
    })
    return env


class _LedgerBase(unittest.TestCase):
    """Two linked rows in a throwaway store; N queried through one session."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-p02-ledger-"))
        self.env = _clean_env(self.tmp)
        self.pid = self._add_row(PARENT)
        self.nid = self._add_row(NEIGHBOUR)
        self._apply_class_fixture()
        self._run_cli("links", "--add", "--id", self.pid, "--id", self.nid,
                      "--relation", "related")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _apply_class_fixture(self) -> None:
        """Class hook between seeding and linking; default adds no tags, so
        the neighbour can only arrive through link expansion."""

    def _run_cli(self, *args: str) -> str:
        proc = subprocess.run(
            [sys.executable, str(STORE_PY), *args],
            capture_output=True, text=True, env=self.env, timeout=300,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"store.py {' '.join(args)} -> rc={proc.returncode}\n{proc.stderr}")
        return proc.stdout

    def _add_row(self, content: str, *extra: str) -> str:
        stdout = self._run_cli(
            "add", "--namespace", NAMESPACE, "--type", "fact",
            "--content", content, "--signal", "test",
            "--confidence", "0.9", "--json", *extra)
        payload = json.loads(stdout)
        row = payload if isinstance(payload, dict) else payload[0]
        rid = row.get("id") or (row.get("memory") or {}).get("id")
        self.assertTrue(rid, f"no id in add --json output: {stdout!r}")
        return rid

    def _call(self) -> dict:
        """One passive injection call in the shared session (the issue's argv)."""
        return json.loads(self._run_cli(
            "recall", "--query", QUERY, "--namespace", NAMESPACE,
            "--no-hybrid", "--no-bump", "--for-injection", "--json",
            "--session-id", SESSION, "--moment", "user_prompt"))

    def _ledger_ids(self) -> list[str]:
        """Delivered ids for the session, read from the resolved sidecar.

        The ledger dir is the RESOLVED store's parent (not ZMEM_DATA), and
        storelib must not be imported to read it (STORE_PATH freezes at
        first import), so the file is parsed directly from disk.
        """
        digest = hashlib.sha256(SESSION.encode("utf-8")).hexdigest()
        store = Path(self.env["ZMEM_STORE"]).expanduser().resolve()
        path = store.parent / "ops" / (digest[:32] + ".ledger")
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [entry["id"] for entry in payload.get("entries", [])]

    def _assert_arrival_precondition(self, payload: dict) -> None:
        """Loud fixture gate: N must render on this call and its result row
        must carry the arrival shape this class names — anything else means
        the fixture is broken and the test must not pass silently."""
        rendered = payload.get("rendered") or ""
        self.assertIn(
            f"[{self.nid}]", rendered,
            "precondition: the neighbour must render on the first call, or "
            "this test proves nothing")
        rows = [row for row in payload.get("results", [])
                if row.get("id") == self.nid]
        self.assertEqual(
            len(rows), 1,
            f"precondition: the neighbour must appear exactly once in "
            f"results: {payload.get('results')!r}")
        self._assert_neighbour_arrival(rows[0])

    def _assert_neighbour_arrival(self, row: dict) -> None:
        """Assert N arrived through the shape this class names.

        Overridden by each concrete class. The base implementation fails
        loudly rather than silently passing, so a new subclass that forgets
        to declare its arrival shape cannot pass for the wrong reason.
        """
        raise AssertionError(
            f"{type(self).__name__} must override _assert_neighbour_arrival "
            "to pin the arrival shape its fixture produces"
        )


class GraphArmNeighbourTest(_LedgerBase):
    """N is tagged (``zebra``) and P carries the query-matched tag
    (``flange``), so N is rescued by the graph-seed arm: its row carries
    ``_graph_arrival_only=True`` and no ``link_relation``."""

    def _apply_class_fixture(self) -> None:
        # Re-adding identical content updates the SAME row in place (same
        # id), so this tags the seeded rows rather than duplicating them.
        self._add_row(PARENT, "--tags", "flange")
        self._add_row(NEIGHBOUR, "--tags", "zebra")

    def _assert_neighbour_arrival(self, row: dict) -> None:
        self.assertIs(
            row.get("_graph_arrival_only"), True,
            f"precondition: the tagged fixture must deliver the neighbour "
            f"through the graph-seed arm (_graph_arrival_only): {row!r}")
        self.assertFalse(
            row.get("link_relation"),
            f"precondition: a graph-arm neighbour must carry no "
            f"link_relation: {row!r}")

    def test_graph_neighbour_renders_once_across_three_calls(self):
        """Once a rendered graph-arm neighbour is ledgered, later calls in
        the same session must not re-deliver it: N renders exactly once
        across three calls (at base it renders on every call)."""
        payloads = [self._call() for _ in range(CALLS)]
        self._assert_arrival_precondition(payloads[0])
        rendered_calls = sum(
            1 for payload in payloads
            if f"[{self.nid}]" in (payload.get("rendered") or ""))
        self.assertEqual(rendered_calls, 1)

    def test_rendered_graph_neighbour_recorded_in_ledger(self):
        """A row that renders on the injection lane is delivered, so the
        graph-arm neighbour must appear in the session's delivery ledger
        alongside its query-matched parent."""
        payload = self._call()
        self._assert_arrival_precondition(payload)
        ids = self._ledger_ids()
        self.assertIn(
            self.pid, ids,
            "precondition: the query-matched parent must be recorded in "
            "the delivery ledger, or this test proves nothing")
        self.assertEqual(self.nid in ids, True)


class LinkExpansionNeighbourTest(_LedgerBase):
    """No tags anywhere, so the entity/graph arms stay empty and N arrives
    only through budgeted 1-hop link expansion: its row carries
    ``link_relation="related"``."""

    def _assert_neighbour_arrival(self, row: dict) -> None:
        self.assertEqual(
            row.get("link_relation"), "related",
            f"precondition: the untagged fixture must deliver the neighbour "
            f"through link expansion (link_relation): {row!r}")
        self.assertFalse(
            row.get("_graph_arrival_only"),
            f"precondition: an expansion neighbour must not be flagged "
            f"_graph_arrival_only: {row!r}")

    def test_expansion_neighbour_renders_once_across_three_calls(self):
        """Once a rendered link-expansion neighbour is ledgered, later calls
        in the same session must not re-deliver it: N renders exactly once
        across three calls (at base it renders on every call)."""
        payloads = [self._call() for _ in range(CALLS)]
        self._assert_arrival_precondition(payloads[0])
        rendered_calls = sum(
            1 for payload in payloads
            if f"[{self.nid}]" in (payload.get("rendered") or ""))
        self.assertEqual(rendered_calls, 1)

    def test_rendered_expansion_neighbour_recorded_in_ledger(self):
        """A row that renders on the injection lane is delivered, so the
        link-expansion neighbour must appear in the session's delivery
        ledger alongside its query-matched parent."""
        payload = self._call()
        self._assert_arrival_precondition(payload)
        ids = self._ledger_ids()
        self.assertIn(
            self.pid, ids,
            "precondition: the query-matched parent must be recorded in "
            "the delivery ledger, or this test proves nothing")
        self.assertEqual(self.nid in ids, True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
