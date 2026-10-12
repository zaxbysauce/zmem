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
import time
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
NAMESPACE = "project:p02ledger"
SESSION = "sess-p02"
PARENT = "keep the flange calibrated before every launch"
NEIGHBOUR = "unrelated zebra migration patterns note"
NEW_NEIGHBOUR = "unrelated okapi migration patterns note"
FRESH_SESSION = "sess-p02-fresh"
SECRET_ROW = "flange calibrated launch uses api_key=AKIAIOSFODNN7EXAMPLE"
QUERY = "flange calibrated launch"
CALLS = 3


def _clean_env(tmp: Path) -> dict[str, str]:
    """A hermetic subprocess env: throwaway store, model absent, ambient
    zmem knobs stripped so an operator's environment cannot redirect the
    fixture (ZMEM_STORE outranks ZMEM_DATA and would silently retarget the
    operator's real ~/.zmem if it leaked through).

    The strip set is DERIVED from the knobs the lane under test actually
    reads, not hand-curated: `_TUNED_KNOBS` below is the complete list the
    review found live in inject.py/recall.py/links.py at this head. An
    ambient value for any of them changes the fixture's shape, so leaking one
    turns a product failure into a red test that looks like a code defect
    (e.g. ZMEM_INJECT_MARGIN=0.5 prunes the neighbour before it renders).
    Extending `_TUNED_KNOBS` is the supported way to keep this honest.
    """
    strip = {
        # store / data resolution and session identity
        "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_ROOT",
        "ZMEM_NAMESPACE", "ZMEM_HOST", "ZMEM_SESSION",
        "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
        "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
        # model / embeddings
        "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
        "ZMEM_EMBED_PROFILE", "ZMEM_LINK_LLM",
        # lane shape: budget, margins, floors, arms
        "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_INJECT_MARGIN",
        "ZMEM_CTX_BUDGET", "ZMEM_TIER_SLOTS", "ZMEM_MMR_LAMBDA",
        "ZMEM_GRAPH_SEED", "ZMEM_LINK_THRESHOLD",
        "ZMEM_VEC_NS_OVERFETCH_ENV", "ZMEM_VEC_NS_OVERFETCH_DEFAULT",
        "ZMEM_CROSS_ENCODER", "ZMEM_CROSS_ENCODER_SHADOW",
        "ZMEM_CAPTURE", "ZMEM_QUERY_CONTEXT",
        # delivery ledger + cross-project
        "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
        "ZMEM_CROSS_PROJECT", "ZMEM_CROSS_PROJECT_READ",
        "ZMEM_CROSS_PROJECT_WRITE", "ZMEM_CROSS_PROJECT_HAZARD_VERBS",
        # clock + fence markers
        "ZMEM_TEST_NOW", "ZMEM_FENCE_OPEN", "ZMEM_FENCE_CLOSE",
        "ZMEM_UNTRUSTED_FENCE",
        # every ZMEM_INJECT_FLOOR_* and ZMEM_ARM_CAP_* / ZMEM_UNFOLD_*
        # family, matched by prefix below
    } | {k for k in os.environ
         if k.startswith(("ZMEM_INJECT_FLOOR_", "ZMEM_ARM_CAP_", "ZMEM_UNFOLD_"))}
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

    def _run_cli(self, *args: str, extra_env: dict | None = None) -> str:
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, str(STORE_PY), *args],
            capture_output=True, text=True, env=env, timeout=300,
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

    def _call(self, session: str = SESSION) -> dict:
        """One passive injection call in the given session (the issue's argv)."""
        return json.loads(self._run_cli(
            "recall", "--query", QUERY, "--namespace", NAMESPACE,
            "--no-hybrid", "--no-bump", "--for-injection", "--json",
            "--session-id", session, "--moment", "user_prompt"))

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

    def test_delivered_parent_does_not_contribute_a_new_neighbour(self):
        """A delivered parent must contribute no neighbours at all — including
        a link edge added AFTER that parent was delivered.

        This is the only assertion in the suite that pins F2 (the
        ``exclude_ids`` seed skip in ``expand_recall_links``). Every other test
        passes even with that seed filter removed, because F1 already
        suppresses neighbours the session has actually seen. Here the new
        neighbour N2 has never been delivered, so only the seed skip can
        suppress it.
        """
        first = self._call()
        self._assert_arrival_precondition(first)
        self.assertIn(self.pid, self._ledger_ids(),
                      "precondition: the parent must be delivered on call 1")

        n2 = self._add_row(NEW_NEIGHBOUR)
        self._run_cli("links", "--add", "--id", self.pid, "--id", n2,
                      "--relation", "related")

        # Positive control (PRR-009): the delivered session renders NOTHING on
        # call 2 -- the parent and N are both legitimately suppressed -- so the
        # assertion below would also be satisfied by any unrelated
        # over-suppression. Prove the edge is live instead: the SAME query in a
        # FRESH session (empty ledger) must render N2. Only then is its absence
        # in this session attributable to the seed skip.
        control = json.loads(self._run_cli(
            "recall", "--query", QUERY, "--namespace", NAMESPACE,
            "--no-hybrid", "--no-bump", "--for-injection", "--json",
            "--session-id", FRESH_SESSION, "--moment", "user_prompt"))
        self.assertIn(
            f"[{n2}]", control.get("rendered") or "",
            "positive control: a fresh session must render the new neighbour, "
            "so its absence in the delivery session is caused by the seed "
            "skip and not by an unreachable edge")

        second = self._call()
        rendered_second = second.get("rendered") or ""
        self.assertEqual(
            f"[{n2}]" in rendered_second, False,
            "a link edge added after the parent was delivered must not "
            "re-render its neighbour in the same session")


class LedgerAccountingBoundTest(_LedgerBase):
    """Feedback-round additions: the accounting boundaries the first PR left
    unpinned (PRR-002, PRR-001R, PRR-010, PRR-030b)."""

    def _apply_class_fixture(self) -> None:
        self._add_row(PARENT, "--tags", "flange")
        self._add_row(NEIGHBOUR, "--tags", "zebra")

    def _assert_neighbour_arrival(self, row: dict) -> None:
        self.assertIs(row.get("_graph_arrival_only"), True)

    def _ledger_path(self):
        store = Path(self.env["ZMEM_STORE"]).expanduser().resolve()
        digest = hashlib.sha256(SESSION.encode("utf-8")).hexdigest()
        return store.parent / "ops" / (digest[:32] + ".ledger")

    def _ledger_entries(self) -> list:
        path = self._ledger_path()
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8")).get("entries", [])

    def _surfaced_count(self, row_id: str) -> int:
        """surfaced_count for a row, read back from the store."""
        import sqlite3
        conn = sqlite3.connect(str(Path(self.env["ZMEM_STORE"])))
        try:
            row = conn.execute(
                "SELECT quote(surfaced_count) FROM memory WHERE id=?",
                (row_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "row missing from the store")
        return int(row[0])

    def test_ledger_marks_which_rows_are_bump_eligible(self):
        """PRR-002 / PRR-015: the ledger must distinguish a query-matched row
        from a supplementary neighbour.

        The recorded set is every RENDERED row, which is wider than the set
        #136 allows to be rewarded. Without this marker the operation-feedback
        matcher (feedback._delivered_matcher_rows) reads a neighbour's text as
        matcher fuel and can move a durable trust counter for a row that was
        never query-matched.
        """
        self._call()
        entries = {e["id"]: e for e in self._ledger_entries()}
        self.assertIn(self.pid, entries, "parent must be recorded")
        self.assertIn(self.nid, entries, "neighbour must be recorded")
        self.assertIs(entries[self.pid].get("bump_eligible"), True,
                      "the query-matched parent is bump-eligible")
        self.assertIs(entries[self.nid].get("bump_eligible"), False,
                      "a neighbour that arrived only through the graph arm is "
                      "recorded but never bump-eligible")

    def test_withheld_row_is_admitted_but_never_recorded(self):
        """PRR-001R / #256: the rendered fence is the ceiling.

        A row carrying a credential shape is ADMITTED into the candidate pool
        (so it appears in candidate_ids) but must never render and must never be
        recorded. This is the only shape in which recording `rows` instead of
        `rows_present_in(rows, rendered)` is observable: the token-budget cut
        removes a row from `rows` before candidate_ids is captured, so the
        budget shape cannot distinguish the two, whereas a withheld row is in the
        pool and absent from the fence.
        """
        secret = self._add_row(SECRET_ROW)
        payload = self._call()
        self.assertIn(
            secret, set(payload.get("candidate_ids") or []),
            "precondition: the withheld row must be admitted to the candidate "
            "pool for this fixture to be discriminating")
        # A withheld row IS announced, but only as a content-free stub: the
        # bullet carries [WITHHELD: SECRET] and no text. What matters is that
        # its CONTENT never reaches the fence or the ledger.
        fence = payload.get("rendered") or ""
        self.assertIn("[WITHHELD: SECRET]", fence,
                      "precondition: a credential-shaped row must be marked")
        self.assertNotIn(
            "AKIAIOSFODNN7EXAMPLE", fence,
            "precondition: withheld content must not appear in the fence")
        recorded = {e["id"] for e in self._ledger_entries()}
        self.assertNotIn(
            secret, recorded,
            "a withheld row is not rendered content and must never be "
            "recorded as delivered (#114 / #256)")

    def test_feedback_matcher_ignores_a_non_bump_eligible_neighbour(self):
        """PRR-002: the marker must actually GATE the feedback matcher.

        The ledger test above only proves the marker is written. Without this,
        `feedback._delivered_matcher_rows` could keep reading every delivered
        row's text as matcher fuel and move a durable trust counter for a
        neighbour that was never query-matched -- defeating #136 on a path the
        bump filter never covered.
        """
        sys.path.insert(0, str(STORE_PY.parent))
        self._call()
        from storelib import feedback as feedback_mod

        store = Path(self.env["ZMEM_STORE"]).expanduser().resolve()
        data_dir = str(store.parent)
        # the CURRENT clock: an epoch of inf makes every entry look expired
        rows = feedback_mod._delivered_matcher_rows(
            data_dir, SESSION, time.time())
        matched = {r["id"] for r in rows}
        self.assertIn(
            self.pid, matched,
            "precondition: the query-matched parent must reach the matcher")
        self.assertNotIn(
            self.nid, matched,
            "a neighbour delivered only as a supplement must never be matched "
            "for an operation-feedback verdict (#136 reward law)")

    def test_graph_arm_neighbour_is_never_surfaced_bumped(self):
        """PRR-030b: the graph-arm clause of the #136 filter must be pinned.

        `bump_rows` drops rows carrying `link_relation` OR `_graph_arrival_only`.
        Every pre-existing telemetry fixture builds a neighbour that carries
        `link_relation`, so the surviving clause shielded them and nothing
        failed if the graph-arm clause were deleted. This fixture's neighbour
        arrives through the graph arm only, so only the clause under test can
        keep its surfaced_count at 0.
        """
        self._call()
        self.assertEqual(self._surfaced_count(self.nid), 0,
                         "a graph-arm neighbour renders but must never be "
                         "surfaced-bumped (#136)")
        self.assertEqual(self._surfaced_count(self.pid), 1,
                         "the query-matched parent is bumped exactly once")


if __name__ == "__main__":
    unittest.main(verbosity=2)
