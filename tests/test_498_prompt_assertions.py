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

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-pa498-"))
atexit.register(shutil.rmtree, SCRATCH, True, None)
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

# PR #526 feedback (parallel-review MX-TI-003): the child env strip list
# now matches the sibling admission suite's _STRIP_ENV exactly.
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_MODEL_AUTODOWNLOAD",
    "ZMEM_MODELS_DIR", "ZMEM_SESSION", "CLAUDE_SESSION_ID",
    "ZCODE_SESSION_ID", "ZMEM_EMBED_PROFILE", "ZMEM_QUERY_CONTEXT",
    "ZMEM_TRANSCRIPT", "ZMEM_TIER0", "ZMEM_SKILLS_DIRS", "ZMEM_PYTHON",
    "ZMEM_CROSS_PROJECT", "ZMEM_REMOTE_URL", "ZMEM_CROSS_ENCODER",
)

SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

# Module-level scratch + env pin BEFORE any storelib/_store_guard import:
# the #244 guard judges the ambient env at ITS import, and storelib freezes
# STORE_PATH on first import (this file imports storelib in-process for the
# parser/partition tests, so the pin MUST precede both).
os.environ["ZMEM_STORE"] = os.environ.get("ZMEM_STORE") or str(
    SCRATCH / "guard-pin.sqlite")
os.environ["ZMEM_DATA"] = os.environ.get("ZMEM_DATA") or str(SCRATCH)
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)
from storelib import inject  # noqa: E402  (env pinned above)

STORE_PY = SCRIPTS / "store.py"
NS = "project:pa498"


def _cli(*args, timeout=180):
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.environ["ZMEM_STORE"],
        "ZMEM_DATA": os.environ["ZMEM_DATA"],
        "ZMEM_INJECT": "1",
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_MODELS_DIR": str(_EMPTY),
        "HOME": str(_HOME),
        "USERPROFILE": str(_HOME),
    })
    return subprocess.run(
        [sys.executable, str(STORE_PY)] + list(args),
        capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT),
        env=env)


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
            inject.parse_row_assertions(
                "The east gantry is owned by Marrow Lines.")[0]["relation"],
            "ownership")
        self.assertEqual(
            inject.parse_row_assertions(
                "The east gantry is owned by Marrow Lines.")[0]["value"],
            "Marrow Lines")
        self.assertEqual(
            inject.parse_row_assertions(
                "Coastal surveys take four hours.")[0]["relation"], "duration")

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
        # Implementation-review round 1: apostrophe contractions must be
        # negation too ("isn't/doesn't/won't ... sent to X"), or a denied
        # destination parses as an affirmative assertion and conflict-drops
        # a relevant stored fact.
        for neg in ("isn't", "doesn't", "won't", "wasn't", "can't",
                    "isn’t"):
            self.assertEqual(
                inject.analyze_prompt(
                    "The Kennet parcel %s sent to Pine Crossing. "
                    "Where does it go?" % neg)["assertions"], [],
                "contraction %r parsed as an affirmation" % neg)
        drops, _ = inject.partition_prompt_assertions(
            inject.analyze_prompt(
                "The Kennet parcel isn't sent to Pine Crossing. "
                "Where does it go?"),
            [{"id": "d", "type": "fact", "confidence": 0.9,
              "signal": "test",
              "content": "Kennet policy sends unsealed parcels to "
                         "Rowan Dock."}])
        self.assertNotIn("d", drops)

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


class DropLabelAndFloorTests(unittest.TestCase):
    """M5/M6 mutation kills: the drop LABEL (redundant vs conflict) and the
    rescue floor are load-bearing, not interchangeable."""

    ROW = {"id": "d", "type": "fact", "confidence": 0.9, "signal": "test",
           "content": "Kennet policy sends unsealed parcels to Rowan Dock."}

    def test_same_value_is_redundant_different_value_is_conflict(self):
        frame_same = inject.analyze_prompt(
            "Kennet policy sends unsealed parcels to Rowan Dock today.")
        drops, _ = inject.partition_prompt_assertions(frame_same, [dict(self.ROW)])
        self.assertEqual(drops.get("d"), "redundant")
        frame_diff = inject.analyze_prompt(
            "This Kennet unsealed parcel is being sent to Pine Crossing.")
        drops2, _ = inject.partition_prompt_assertions(frame_diff, [dict(self.ROW)])
        self.assertEqual(drops2.get("d"), "conflict")

    def test_rescue_refuses_weak_lane(self):
        self.assertEqual(inject.QUESTION_RESCUE_FLOOR, 0.25,
                         "the rescue floor is a documented fixed constant")
        weak = {"id": "w", "type": "fact", "confidence": 0.9,
                "signal": "test",
                "content": "Harbor master Lyle approves every Kennet shipment.",
                "_rel_cos": 0.15}
        frame = inject.analyze_prompt(
            "This Kennet unsealed parcel is being sent to Pine Crossing. "
            "Who approves the job?")
        nominees = inject.rescue_nominees(frame, [weak], [])
        self.assertNotIn("w", nominees,
                         "a 0.15 lane is below the 0.25 rescue floor")
        # PR #526 feedback (FB-104): the boundary is the literal 0.25 — a
        # 0.2499 lane is refused, a 0.25 lane is admitted (the old pin only
        # refused 0.15, so any floor in (0.15, 0.30] stayed green).
        at = dict(weak, id="w-at", _rel_cos=0.25)
        self.assertIn("w-at", inject.rescue_nominees(frame, [at], []),
                      "a lane exactly at the floor is admitted")
        under = dict(weak, id="w-under", _rel_cos=0.2499)
        self.assertNotIn(
            "w-under", inject.rescue_nominees(frame, [under], []),
            "a lane one tick below the floor is refused")

    def test_rescue_relation_gate_beats_higher_wrong_relation_lane(self):
        # M4b kill: the wrong-relation row (badge, 0.45) OUTRANKS the right
        # one (lyle, 0.30). The relation gate must keep badge out of the
        # nominees even when it is the best-scoring row on the page.
        rows = [
            {"id": "lyle", "type": "fact", "confidence": 0.9,
             "signal": "test",
             "content": "Harbor master Lyle approves every Kennet shipment.",
             "_rel_cos": 0.30},
            {"id": "badge", "type": "fact", "confidence": 0.9,
             "signal": "test",
             "content": "Security badge replacements are issued at the "
                        "Vantor desk.", "_rel_cos": 0.45},
        ]
        frame = inject.analyze_prompt(
            "This Kennet unsealed parcel is being sent to Pine Crossing. "
            "Who approves the job?")
        nominees = inject.rescue_nominees(frame, rows, [])
        self.assertEqual(set(nominees), {"lyle"},
                         "relation-incompatible badge must not outrank lyle")


class VerifierRepairTests(unittest.TestCase):
    """The eval-verifier independent derivation (plan R6/R12 option a)."""

    ROWS = [
        {"id": "rowan", "type": "fact", "confidence": 0.9, "signal": "test",
         "content": "Kennet policy sends unsealed parcels to Rowan Dock.",
         "_rel_lex": 0.6},
        {"id": "lyle", "type": "fact", "confidence": 0.9, "signal": "test",
         "content": "Harbor master Lyle approves every Kennet shipment.",
         "_rel_cos": 0.30},
        # M4b discriminator: the WRONG-relation row outranks the right one
        # (0.45 > 0.30) while staying BELOW the ordinary 0.50 cos floor —
        # with the relation gate intact the rescue set is {lyle}; with
        # relation+subject gates both removed, badge wins and this fixture
        # goes red.
        {"id": "badge", "type": "fact", "confidence": 0.9, "signal": "test",
         "content": "Security badge replacements are issued at the Vantor "
                    "desk.", "_rel_cos": 0.45},
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
        # PR #526 feedback (FB-104/TI-002): exact-set strength — the mixed
        # prompt delivers EXACTLY ONE fact, the approval fact.
        self.assertEqual(len(contents), 1, env)
        self.assertIn("Lyle", contents[0])
        self.assertFalse(any("Vantor" in c for c in contents), env)

    def test_self_answered_prompt_delivers_nothing(self):
        env = _recall(
            "The current Morrowfield weekend scan goes to Rose Yard. "
            "Where does this scan go?")
        self.assertEqual(_contents(env), [])
        # Positive control (FB-204/TEST-006): a neutral query against the
        # same pool delivers the fact, proving the empty delivery is the
        # partition's doing and not an empty pool or floor rejection.
        env3 = _recall("morrowfield weekend scan", namespace=NS)
        self.assertTrue(
            any("Rose Yard" in c for c in _contents(env3)), env3)

    def test_different_item_prompt_delivers_exactly_kennet_default(self):
        env = _recall(
            "The current Morrowfield weekend scan goes to Rose Yard. "
            "What destination does Kennet policy use for unsealed parcels?")
        contents = _contents(env)
        self.assertTrue(any("Rowan Dock" in c for c in contents), env)
        self.assertFalse(any("Rose Yard" in c for c in contents), env)
        self.assertFalse(any("Vantor" in c for c in contents), env)


class ItemIdentityTests(unittest.TestCase):
    """PR #526 feedback (parallel-review P-F1 / FB-201): subject binding is
    item-identity aware — digit-bearing identifiers distinguish items, and
    filler words are never subject terms."""

    def test_digit_identifiers_distinguish_items(self):
        # "Invoice 42 ... Where does Invoice 17 go?" must NOT conflict-drop
        # the Invoice 17 row (pre-fix: shared generic term 'invoice' bound).
        frame = inject.analyze_prompt(
            "Invoice 42 is sent to Pine Crossing. "
            "Where does Invoice 17 go?")
        drops, _ = inject.partition_prompt_assertions(frame, [
            {"id": "inv17", "type": "fact",
             "content": "Invoice 17 is sent to Ash Register."},
        ])
        self.assertNotIn("inv17", drops)

    def test_batch_ids_distinguish_items(self):
        frame = inject.analyze_prompt(
            "Batch 7 is sent to Pine Crossing. Where does Batch 9 go?")
        drops, _ = inject.partition_prompt_assertions(frame, [
            {"id": "b9", "type": "fact",
             "content": "Batch 9 is sent to Ash Register."},
        ])
        self.assertNotIn("b9", drops)

    def test_filler_words_are_never_subject_terms(self):
        # INTA-002: 'Remember that nightly test logs ...' — 'remember' must
        # not be the shared term that binds a different item.
        self.assertNotIn("remember", inject._pa_terms("Remember that plan"))
        self.assertNotIn("please", inject._pa_terms("Please file it"))
        self.assertNotIn("know", inject._pa_terms("Do we know the route"))

    def test_generic_noun_binding_is_disclosed_residual(self):
        # Disclosed residual (PR body RECORD row): generic-noun subjects
        # ("usage reports" vs "error reports") with distinct modifiers and no
        # identifiers still bind — the over-suppression residual fails open
        # toward the pre-PR behavior and is tracked for re-evaluation.
        frame = inject.analyze_prompt(
            "Usage reports are sent to Datadog now. "
            "Where do error reports go?")
        drops, _ = inject.partition_prompt_assertions(frame, [
            {"id": "err", "type": "fact",
             "content": "Error reports are sent to Sentry."},
        ])
        self.assertIn("err", drops,
                      "disclosed residual changed: update the PR body row")

    def test_kennet_conflict_still_drops(self):
        # Regression guard: the frozen C1 shape (no identifiers) keeps the
        # conflict drop under the item-identity rules.
        frame = inject.analyze_prompt(
            "This Kennet unsealed parcel is being sent to Pine Crossing.")
        drops, _ = inject.partition_prompt_assertions(frame, [
            {"id": "rowan", "type": "fact",
             "content": "Kennet policy sends unsealed parcels to Rowan Dock."},
        ])
        self.assertEqual(drops.get("rowan"), "conflict")

    def test_question_generic_reference_still_binds_digit_item(self):
        # "the invoice" (no id) still binds Invoice 42's row: the digit rule
        # only fires when BOTH sides carry digit tokens.
        frame = inject.analyze_prompt(
            "Invoice 42 is sent to Pine Crossing. Where does the invoice go?")
        drops, _ = inject.partition_prompt_assertions(frame, [
            {"id": "inv42", "type": "fact",
             "content": "Invoice 42 is sent to Ash Register."},
        ])
        self.assertIn("inv42", drops)


class QuestionGrammarTests(unittest.TestCase):
    """PR #526 feedback (parallel-review P-F2 / FB-202): yes/no auxiliaries,
    hedged 'whether' frames, and consumed-'?' clauses never assert."""

    def test_yes_no_auxiliary_starters_never_assert(self):
        for clause in ("Is the scan sent to Rose Yard",
                       "Are logs sent to stderr",
                       "Does the CLI send errors to stderr",
                       "Do we route batches to S3",
                       "Can the parcel enter Pearl Crossing",
                       "Should the deploy notify Slack",
                       "Will the report go to Datadog",
                       "Was the shipment received at Pearl Crossing"):
            self.assertEqual(
                inject.analyze_prompt(clause)["assertions"], [],
                "aux-starter clause asserted: %r" % clause)

    def test_whether_clauses_never_assert(self):
        for clause in ("Tell me whether Ash Register is sent to Pearl "
                       "Crossing.",
                       "Do we know whether the scan goes to Rose Yard",
                       "Check whether the CLI sends errors to stderr"):
            self.assertEqual(
                inject.analyze_prompt(clause)["assertions"], [],
                "'whether' clause asserted: %r" % clause)

    def test_consumed_question_mark_non_final_clause(self):
        # The '?' of a non-final clause is consumed by the splitter; the aux
        # starter must carry the question reading.
        frame = inject.analyze_prompt(
            "Does the CLI send errors to stderr? I want to fix the test.")
        self.assertEqual(frame["assertions"], [])
        # The hook rewrite appends context tokens after the '?'.
        frame2 = inject.analyze_prompt(
            "Are logs sent to stderr? pytest cli.py")
        self.assertEqual(frame2["assertions"], [])

    def test_declarative_destination_still_asserts(self):
        frame = inject.analyze_prompt(
            "This Kennet unsealed parcel is being sent to Pine Crossing.")
        self.assertEqual(len(frame["assertions"]), 1)
        self.assertEqual(frame["assertions"][0]["relation"], "destination")


class RescueRestrictionTests(unittest.TestCase):
    """PR #526 feedback (parallel-review P-F3 / FB-203 + FB-107): the
    subject-unbound rescue fallback runs only for anaphora/ellipsis-bound
    foci, and nominees never repeat rows the ordinary gate already
    selected."""

    DANA = {"id": "dana", "type": "fact", "confidence": 0.9,
            "signal": "test",
            "content": "Dana Reyes approves Crane Repair.",
            "_rel_cos": 0.31}

    def test_explicit_subject_no_fallback(self):
        frame = inject.analyze_prompt(
            "Who approves the Ash Register shipment?")
        nominees = inject.rescue_nominees(frame, [dict(self.DANA)], [])
        self.assertEqual(nominees, {},
                         "unrelated approver row rescued for an explicit "
                         "subject with no compat row")

    def test_generic_single_token_subject_rescues(self):
        # The freight-shaped case: a single bare generic token ("the extra")
        # is not an item identity — the best relation-compatible
        # floor-clearing row may complete it.
        freight = {"id": "freight", "type": "fact", "confidence": 0.9,
                   "signal": "test",
                   "content": "Overflow freight on the night leg is routed "
                              "via Calbeck siding.", "_rel_cos": 0.40}
        frame = inject.analyze_prompt(
            "The night leg has more cargo than fits - where does the extra "
            "go?")
        nominees = inject.rescue_nominees(frame, [freight], [])
        self.assertIn("freight", nominees,
                      "single generic-token subject lost its rescue")

    def test_anaphor_bound_focus_still_rescues(self):
        frame = inject.analyze_prompt(
            "This Kennet unsealed parcel is being sent to Pine Crossing. "
            "Who approves it?")
        lyle = {"id": "lyle", "type": "fact", "confidence": 0.9,
                "signal": "test",
                "content": "Harbor master Lyle approves every Kennet "
                           "shipment.",
                "_rel_cos": 0.30}
        nominees = inject.rescue_nominees(frame, [lyle], [])
        self.assertIn("lyle", nominees,
                      "anaphor-bound approver focus lost its rescue")

    def test_ordinary_selected_never_renominated(self):
        # FB-107 / MX-PO-002 / INTA-007: a row the ordinary gate already
        # admitted must not appear in the nominees (attribute_rescued
        # over-count).
        frame = inject.analyze_prompt(
            "This Kennet unsealed parcel is being sent to Pine Crossing. "
            "Who approves the job?")
        lyle = {"id": "lyle", "type": "fact", "confidence": 0.9,
                "signal": "test",
                "content": "Harbor master Lyle approves every Kennet "
                           "shipment.",
                "_rel_cos": 0.30}
        nominees = inject.rescue_nominees(frame, [dict(lyle)], [dict(lyle)])
        self.assertNotIn("lyle", nominees,
                         "ordinary-selected row counted as rescued")


class BelowRelevanceWiringTests(unittest.TestCase):
    """PR #526 feedback (RI-1 / FB-101): the silent-reason classifier must
    see the POST-partition pool so a fully partition-dropped pool reports
    below-relevance, live and on the explain mirror."""

    def test_classifier_post_partition_rows_all_dropped(self):
        dropped_ids = {"a", "b"}
        pool = [{"id": "a", "type": "fact"}, {"id": "b", "type": "fact"}]
        post = [r for r in pool if r["id"] not in dropped_ids]
        reason = inject.classify_silent_reason(
            post, omitted=0, budget_emptied=False, lane_stats=None,
            candidate_ids=["a", "b"], post_ledger_rows=pool,
            attribute_dropped=len(dropped_ids))
        self.assertEqual(reason, "below-relevance")

    def test_classifier_survivors_fail_trust_is_below_bar(self):
        pool = [{"id": "a", "type": "fact"}]
        reason = inject.classify_silent_reason(
            pool, omitted=0, budget_emptied=False,
            lane_stats={"trust_passed": 0, "relevance_failed": 1},
            candidate_ids=["a"], post_ledger_rows=pool,
            attribute_dropped=0)
        self.assertEqual(reason, "below-bar")

    def test_live_reason_below_relevance_on_all_dropped_prompt(self):
        # Live seam: seed one conflicting default, prompt a conflicting
        # destination, the pool partitions away every row -> reason must be
        # below-relevance (pre-fix: below-bar).
        rid = _add("Kennet policy sends unsealed parcels to Rowan Dock.",
                   "kennet")
        env = _recall(
            "This Kennet unsealed parcel is being sent to Pine Crossing. "
            "What destination should be used?")
        try:
            self.assertEqual(env.get("reason"), "below-relevance", env)
            # attribute_dropped itself is keyed only on the direct
            # no-session wire (FB-102): the session-selector envelope this
            # CLI lane returns rebuilds the closed key set.
        finally:
            _cli("update", "--id", rid, "--content",
                 "Kennet policy sends unsealed parcels to Rowan Dock.",
                 "--json")

    def test_explain_reason_below_relevance_on_all_dropped_prompt(self):
        # Explain mirror: the same prompt replays through explain_recall's
        # injection lane; its envelope reason must match the live seam
        # (pre-fix: below-bar from the pre-partition pool).
        import io
        import contextlib
        from storelib import connect, recall as recall_mod
        rid = _add("Kennet policy sends unsealed parcels to Rowan Dock.",
                   "kennet")
        try:
            conn = connect()
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    recall_mod.explain_recall(
                        conn, query="This Kennet unsealed parcel is being "
                                    "sent to Pine Crossing. What destination "
                                    "should be used?", namespace=NS,
                        as_json=True, for_injection=True)
            finally:
                conn.close()
            env = json.loads(buf.getvalue())
            self.assertEqual(env.get("reason"), "below-relevance", env.get(
                "reason"))
        finally:
            _cli("update", "--id", rid, "--content",
                 "Kennet policy sends unsealed parcels to Rowan Dock.",
                 "--json")


class PagesWithholdTests(unittest.TestCase):
    """PR #526 feedback (FB-109 / parallel-review MX-UR-08): a derived page
    whose bullet embeds a partition-dropped source is withheld before
    admit_with_pages, so the suppressed fact cannot re-deliver verbatim."""

    def test_overlapping_page_withheld_clean_page_kept(self):
        dropped = {"rowan-id": "conflict"}
        overlapping = {"id": "p1", "type": "page",
                       "represented_ids": ["rowan-id", "other"],
                       "content": "Kennet policy sends unsealed parcels "
                                  "to Rowan Dock."}
        clean = {"id": "p2", "type": "page",
                 "represented_ids": ["lyle-id"],
                 "content": "Harbor master Lyle approves every Kennet "
                            "shipment."}
        kept = inject._page_rows_withhold_partition_overlaps(
            [overlapping, clean], dropped)
        self.assertEqual([p["id"] for p in kept], ["p2"])

    def test_source_ids_fallback_and_type_safety(self):
        dropped = {"s1": "redundant"}
        via_source_ids = {"id": "p3", "type": "page",
                          "source_ids": ["s1"]}
        nonstr = {"id": "p4", "type": "page", "represented_ids": [1, 2]}
        empty = {"id": "p5", "type": "page"}
        kept = inject._page_rows_withhold_partition_overlaps(
            [via_source_ids, nonstr, empty], dropped)
        self.assertEqual([p["id"] for p in kept], ["p4", "p5"])

    def test_no_drops_returns_input_unchanged(self):
        pages = [{"id": "p6", "type": "page", "source_ids": ["x"]}]
        self.assertIs(
            inject._page_rows_withhold_partition_overlaps(pages, {}),
            pages)


class QualifiedFactOrderingTests(unittest.TestCase):
    """PR #526 feedback (parallel-review MX-UIB-006): the qualified-fact
    exemption fires BEFORE the conflict loop — a row that answers the asked
    cost survives even when it also carries a conflicting destination."""

    def test_qualified_fact_survives_conflicting_destination(self):
        frame = inject.analyze_prompt(
            "The Kennet parcel goes to Pine Crossing. "
            "What does the Kennet unsealed parcel cost?")
        drops, survivors = inject.partition_prompt_assertions(frame, [
            {"id": "q", "type": "fact",
             "content": "Kennet unsealed parcels routed via Rowan Dock "
                        "cost 40 marks when the harbor wave log is clear."},
        ])
        self.assertNotIn("q", drops)
        self.assertEqual(len(survivors), 1)

    def test_destination_only_fact_still_conflict_drops(self):
        frame = inject.analyze_prompt(
            "The Kennet parcel goes to Pine Crossing. "
            "What does the Kennet unsealed parcel cost?")
        drops, _ = inject.partition_prompt_assertions(frame, [
            {"id": "d", "type": "fact",
             "content": "Kennet policy sends unsealed parcels to Rowan "
                        "Dock."},
        ])
        self.assertEqual(drops.get("d"), "conflict")


class RescueCapTests(unittest.TestCase):
    """PR #526 feedback (parallel-review TEST-002): the one-row-per-focus
    cap is load-bearing in production AND in the eval verifier."""

    QUERY = ("This Kennet unsealed parcel is being sent to Pine Crossing. "
             "Who approves the job?")

    def _two_floor_clearing(self):
        return [
            {"id": "lyle1", "type": "fact", "confidence": 0.9,
             "signal": "test",
             "content": "Harbor master Lyle approves every Kennet "
                        "shipment.", "_rel_cos": 0.40},
            {"id": "lyle2", "type": "fact", "confidence": 0.9,
             "signal": "test",
             "content": "Dock warden Lyle signs off on Kennet arrivals.",
             "_rel_cos": 0.35},
        ]

    def test_production_rescues_exactly_one(self):
        nominees = inject.rescue_nominees(
            inject.analyze_prompt(self.QUERY), self._two_floor_clearing(), [])
        self.assertEqual(len(nominees), 1,
                         "two floor-clearing candidates admitted for one "
                         "focus")

    def test_verifier_expects_exactly_one(self):
        sys.path.insert(0, str(REPO_ROOT / "skills/memory/scripts"))
        from storelib import eval_gold
        import storelib.inject as inj
        floor, gate_none, grounded = inj._gate_constants()
        expected = eval_gold._expected_selection(
            self.QUERY, self._two_floor_clearing(),
            floor=floor, gate_none_floor=gate_none, grounded=grounded)
        self.assertEqual(len(expected), 1)


class VerifierRealLaneTests(unittest.TestCase):
    """PR #526 feedback (parallel-review TEST-004): _verify_real_lane is
    invoked end to end — it accepts the exact lane envelope and raises
    BypassError on an inflated rendered set."""

    QUERY = ("This Kennet unsealed parcel is being sent to Pine Crossing. "
             "Who approves the job?")

    def _fence(self, rows):
        from storelib import recall as recall_mod
        return recall_mod._format_fenced_recall(
            rows,
            header="Relevant memories (namespace project:pa498). Consider "
                   "if they apply; ignore if not.",
            legacy_injection_wire=True)

    def test_accepts_exact_lane_envelope(self):
        from storelib import connect
        from storelib import eval_gold
        env = _recall(self.QUERY)
        rows = env.get("results") or []
        self.assertTrue(rows, "lane delivered nothing to verify")
        eval_gold._verify_real_lane(
            "fb-004-exact", rows, env, self._fence(rows),
            conn=connect(), candidate_ids=env.get("candidate_ids"),
            candidate_lanes=env.get("candidate_lanes"),
            executed_query=self.QUERY)

    def test_inflated_render_set_raises_bypass(self):
        from storelib import connect
        from storelib import eval_gold
        env = _recall(self.QUERY)
        rows = [dict(r) for r in (env.get("results") or [])]
        rowan = {"id": "rowan-extra", "type": "fact", "confidence": 0.9,
                 "signal": "test", "namespace": NS,
                 "content": "Kennet policy sends unsealed parcels to Rowan "
                            "Dock."}
        rows.append(rowan)
        with self.assertRaises(eval_gold.BypassError):
            eval_gold._verify_real_lane(
                "fb-004-inflate", rows, env, self._fence(rows),
                conn=connect(), candidate_ids=env.get("candidate_ids"),
                candidate_lanes=env.get("candidate_lanes"),
                executed_query=self.QUERY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
