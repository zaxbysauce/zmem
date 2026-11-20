#!/usr/bin/env python3
"""Issue #498 prompt-assertion guardrails (plan R7): PAA unit legs the frozen
suite does not carry (ownership, duration, multi-focus, negation,
hypothetical, clause binding, constraint protection, qualified-fact survival),
the eval-verifier repair's drop/rescue/inflate legs, and exact delivered-set
assertions on the real injection lane.

Hermetic: env pinned at module import (before any storelib import), lexical
embeddings only, subprocess drives the real store.py CLI. CI runs this file
as `python -X utf8 tests/test_498_prompt_assertions.py`.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-pa498-"))
os.environ["ZMEM_STORE"] = str(SCRATCH / "store.sqlite")
os.environ["ZMEM_DATA"] = str(SCRATCH)
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
_EMPTY = SCRATCH / "models-empty"
_EMPTY.mkdir(exist_ok=True)
os.environ["ZMEM_MODELS_DIR"] = str(_EMPTY)
_HOME = SCRATCH / "home"
_HOME.mkdir(exist_ok=True)
os.environ["HOME"] = str(_HOME)
os.environ["USERPROFILE"] = str(_HOME)
for _k in ("ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR", "CLAUDE_SESSION_ID",
           "ZCODE_SESSION_ID", "ZMEM_SESSION", "ZMEM_NAMESPACE", "ZMEM_HOST"):
    os.environ.pop(_k, None)

SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from storelib import inject  # noqa: E402  (env pinned above)

STORE_PY = SCRIPTS / "store.py"
NS = "project:pa498"


def _cli(*args, timeout=180):
    return subprocess.run(
        [sys.executable, str(STORE_PY)] + list(args),
        capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT))


def _add(content, tags, type_="fact"):
    r = _cli("add", "--namespace", NS, "--type", type_, "--content", content,
             "--tags", tags, "--signal", "test", "--confidence", "0.9",
             "--json")
    if r.returncode != 0:
        raise RuntimeError("seed failed: " + r.stderr[-200:])
    return json.loads(r.stdout).get("id")


def _recall(query, namespace=NS):
    sid = "pa498-" + uuid.uuid4().hex[:10]
    r = _cli("recall", "--query", query, "--namespace", namespace,
             "--limit", "5", "--include-global", "--global-limit", "3",
             "--no-bump", "--for-injection", "--json",
             "--session-id=" + sid, "--moment", "user_prompt",
             "--lane", "codex")
    if r.returncode != 0:
        raise RuntimeError("recall failed: " + r.stderr[-200:])
    return json.loads(r.stdout)


def _contents(env):
    return [row.get("content", "") for row in (env.get("results") or [])]


class FrameParseTests(unittest.TestCase):
    """PAA frame parsing: the critic-mandated grammar legs."""

    def test_ownership_and_duration_parse(self):
        self.assertEqual(
            inject.parse_row_assertion(
                "The east gantry is owned by Marrow Lines.")["relation"],
            "ownership")
        self.assertEqual(
            inject.parse_row_assertion(
                "The east gantry is owned by Marrow Lines.")["value"],
            "Marrow Lines")
        self.assertEqual(
            inject.parse_row_assertion(
                "Coastal surveys take four hours.")["relation"], "duration")

    def test_multi_question_prompt_produces_multiple_foci(self):
        frame = inject.analyze_prompt(
            "The Kennet parcel goes to Rowan Dock. "
            "Who approves it? How often is it audited?")
        rels = {f["relation"] for f in frame["foci"]}
        self.assertIn("approver", rels)
        self.assertIn("frequency", rels)

    def test_negated_and_hypothetical_clauses_yield_no_assertions(self):
        self.assertEqual(
            inject.analyze_prompt(
                "The Kennet parcel is not sent to Pine Crossing. "
                "Where does it go?")["assertions"], [])
        self.assertEqual(
            inject.analyze_prompt(
                "If the parcel were sent to Pine Crossing, who would "
                "approve it?")["assertions"], [])

    def test_clause_bound_subject_affinity(self):
        frame = inject.analyze_prompt(
            "The current Morrowfield weekend scan goes to Rose Yard. "
            "What destination does Kennet policy use for unsealed parcels?")
        focus = [f for f in frame["foci"] if f["relation"] == "destination"]
        self.assertTrue(focus)
        self.assertFalse(focus[0]["answered"])
        drops, survivors = inject.partition_prompt_assertions(frame, [
            {"id": "m", "type": "fact",
             "content": "The current Morrowfield weekend scan goes to Rose Yard."},
            {"id": "k", "type": "fact",
             "content": "Kennet policy sends unsealed parcels to Rowan Dock."},
        ])
        self.assertEqual(drops.get("m"), "redundant")
        self.assertNotIn("k", drops)


class PartitionGuardTests(unittest.TestCase):

    def test_constraint_and_decision_rows_are_never_dropped(self):
        frame = inject.analyze_prompt(
            "This Kennet unsealed parcel is being sent to Pine Crossing. "
            "What destination should be used?")
        drops, survivors = inject.partition_prompt_assertions(frame, [
            {"id": "c1", "type": "constraint",
             "content": "Kennet policy sends unsealed parcels to Rowan Dock."},
            {"id": "d1", "type": "decision",
             "content": "Kennet policy sends unsealed parcels to Rowan Dock."},
        ])
        self.assertEqual(drops, {})
        self.assertEqual(len(survivors), 2)

    def test_qualified_fact_survives_unsupported_focus(self):
        frame = inject.analyze_prompt(
            "What does the Kennet unsealed parcel route to Pine Crossing "
            "cost?")
        drops, _ = inject.partition_prompt_assertions(frame, [
            {"id": "q", "type": "fact",
             "content": "Kennet unsealed parcels routed via Pine Crossing "
                        "cost 40 marks when the harbor wave log is clear."},
        ])
        self.assertNotIn("q", drops)

    def test_destination_only_fact_drops_under_cost_focus(self):
        frame = inject.analyze_prompt(
            "What does the Kennet unsealed parcel route to Pine Crossing "
            "cost?")
        drops, _ = inject.partition_prompt_assertions(frame, [
            {"id": "d", "type": "fact",
             "content": "Kennet policy sends unsealed parcels to Rowan Dock."},
        ])
        # The question clause yields no assertion, so the destination-only
        # fact falls to ORTHOGONAL (unsupported cost focus), not conflict.
        self.assertEqual(drops.get("d"), "orthogonal")


class VerifierRepairTests(unittest.TestCase):
    """The eval-verifier independent derivation (plan R6/R12 option a)."""

    ROWS = [
        {"id": "rowan", "type": "fact", "confidence": 0.9, "signal": "test",
         "content": "Kennet policy sends unsealed parcels to Rowan Dock.",
         "_rel_lex": 0.6},
        {"id": "lyle", "type": "fact", "confidence": 0.9, "signal": "test",
         "content": "Harbor master Lyle approves every Kennet shipment.",
         "_rel_cos": 0.47},
        {"id": "badge", "type": "fact", "confidence": 0.9, "signal": "test",
         "content": "Security badge replacements are issued at the Vantor "
                    "desk.", "_rel_cos": 0.28},
    ]

    def _expected(self, query):
        sys.path.insert(0, str(REPO_ROOT / "skills/memory/scripts"))
        from storelib import eval_gold
        import storelib.inject as inj
        _floor, gate_none, grounded = inj._gate_constants()
        return {r["id"] for r in eval_gold._expected_selection(
            query, [dict(r) for r in self.ROWS],
            floor=_floor, gate_none_floor=gate_none, grounded=grounded)}

    def test_conflict_drop_and_rescue_derivation(self):
        got = self._expected(
            "This Kennet unsealed parcel is being sent to Pine Crossing. "
            "Who approves the job?")
        self.assertEqual(got, {"lyle"})  # conflict drops rowan; badge
        # is relation-incompatible; lyle rescues at 0.47

    def test_rescue_inflate_outcome_detected_by_independent_derivation(self):
        # The verifier's expectation for the mixed prompt is exactly {lyle};
        # a production rescue-inflate (admitting badge too) would render
        # {lyle, badge} != expected and raise BypassError in
        # _verify_real_lane. Pin the expected set here so a helper-level
        # inflation changes the verifier's answer and is caught.
        expected = self._expected(
            "This Kennet unsealed parcel is being sent to Pine Crossing. "
            "Who approves the job?")
        self.assertIn("lyle", expected)
        self.assertNotIn("badge", expected)
        self.assertNotIn("rowan", expected)


class ExactSetLaneTests(unittest.TestCase):
    """Exact delivered-set assertions through the real injection lane."""

    @classmethod
    def setUpClass(cls):
        cls.rowan = _add(
            "Kennet policy sends unsealed parcels to Rowan Dock.", "kennet")
        cls.lyle = _add(
            "Harbor master Lyle approves every Kennet shipment.", "kennet")
        cls.morrow = _add(
            "The current Morrowfield weekend scan goes to Rose Yard.",
            "morrowfield")
        cls.badge = _add(
            "Security badge replacements are issued at the Vantor desk.",
            "badges")

    def test_mixed_prompt_delivers_exactly_the_approval_fact(self):
        env = _recall(
            "This Kennet unsealed parcel is being sent to Pine Crossing. "
            "Who approves the job?")
        contents = _contents(env)
        self.assertTrue(any("Lyle" in c for c in contents), env)
        self.assertFalse(any("Rowan Dock" in c for c in contents), env)

    def test_self_answered_prompt_delivers_nothing(self):
        env = _recall(
            "The current Morrowfield weekend scan goes to Rose Yard. "
            "Where does this scan go?")
        self.assertEqual(_contents(env), [])

    def test_different_item_prompt_delivers_exactly_kennet_default(self):
        env = _recall(
            "The current Morrowfield weekend scan goes to Rose Yard. "
            "What destination does Kennet policy use for unsealed parcels?")
        contents = _contents(env)
        self.assertTrue(any("Rowan Dock" in c for c in contents), env)
        self.assertFalse(any("Rose Yard" in c for c in contents), env)


if __name__ == "__main__":
    unittest.main(verbosity=2)
