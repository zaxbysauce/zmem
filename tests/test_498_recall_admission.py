"""Issue #498 deterministic regression: passive recall admission must
respect explicit task details and reject wrong-attribute defaults.

AC1 -- an explicit, affirmative destination/assignment in the prompt takes
precedence: the conflicting stored standing default must NOT be rendered
or counted as surfaced.  A prompt that ASKS for the standing default, or
that only mentions a different item / a hypothetical assignment, must
still retrieve it.

AC2 -- a mixed prompt (supplies a destination, asks who approves) keeps
the independent approval fact and still suppresses the conflicting
destination default.  A destination-only fact is suppressed when the
question asks an unsupported attribute (price).  A qualified fact that
actually contains the requested information is preserved.

Hermetic contract:
- Own throwaway store: ZMEM_STORE/ZMEM_DATA are pinned in EVERY child env
  before store.py runs (this file never imports storelib in-process, so
  the module-import pin is moot; subprocesses pin per-run, matching the
  repo's _store_guard discipline).
- Deterministic: lexical retrieval only.  ZMEM_MODELS_DIR points at an
  empty dir, HOME/USERPROFILE redirect into the scratch, and model
  autodownload is off -- no MiniLM, no network.
- The store CLI is driven exactly like the hook lane does:
  recall --for-injection --json --session-id=<fresh> --moment user_prompt
  --lane codex ... --no-bump, asserting on the closed envelope's
  ``rendered`` and ``results``/``candidate_ids`` surfaces.  A row counts
  as surfaced when its id appears in results[] or its marker text appears
  in rendered or any result content (candidate_ids is PRE-gate by the
  #117/#174 contract, so it is reported in failures but never asserted).

Run: python tests/test_498_recall_admission.py   (repo convention, no pytest)
"""

from __future__ import annotations
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

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

# ---------------------------------------------------------------------------
# Module-level scratch + env pin BEFORE any storelib/_store_guard import:
# storelib freezes STORE_PATH on first import and the #244 guard judges the
# ambient env at ITS import, so the pin must precede both (this file never
# imports storelib in-process, but the pin keeps the guard green on a
# developer machine with a live default store; children re-pin per-run).
# ---------------------------------------------------------------------------
_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-498-admission-"))


@atexit.register
def _cleanup_scratch():
    try:
        shutil.rmtree(_SCRATCH, ignore_errors=True)
    except Exception:
        pass


os.environ["ZMEM_STORE"] = str(_SCRATCH / "guard-pin.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)

import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

NS = "project:admission498"
NS_QUALIFIED = "project:admission498q"

# --- seed corpus (mirrors the issue #498 fixtures) ---------------------------
SEEDS = [
    ("kennet-default",
     "Kennet policy sends unsealed parcels to Rowan Dock.", "kennet,shipping"),
    ("plover-register",
     "Plover Hill day checks enter the Pearl Register.", "plover,registers"),
    ("morrowfield-scan",
     "The current Morrowfield weekend scan goes to Rose Yard.",
     "morrowfield,scans"),
    ("kennet-approver",
     "Harbor master Lyle approves every Kennet shipment.",
     "kennet,approvals"),
    ("badge-replace",
     "Security badge replacements are issued at the Vantor desk.", "badges"),
    ("overflow-freight",
     "Overflow freight on the night leg is routed via Calbeck siding.",
     "freight"),
    ("translation-revision",
     "Translation revisions for the Meridian manual go to the senior editor.",
     "manuals"),
    ("shift-coverage",
     "Weekend shift coverage is approved by the duty roster board.", "shifts"),
    ("temperature-report",
     "Cold-chain temperature readings must be logged every four hours.",
     "coldchain"),
]
QUALIFIED_PRICE = ("Kennet unsealed parcels routed via Pine Crossing cost "
                   "40 marks when the harbor wave log is clear.")

PROMPT_PINE_CROSSING = (
    "This Kennet unsealed parcel is being sent to Pine Crossing. "
    "What destination should be used?")
PROMPT_ASH_REGISTER = (
    "This Plover Hill day check belongs in the Ash Register. "
    "Which register should receive it?")
PROMPT_STANDING_DEFAULT = (
    "What destination does Kennet policy use for unsealed parcels?")
PROMPT_HYPOTHETICAL_OTHER = (
    "Hypothetically, if this were a Morrowfield scan it might go to "
    "Rose Yard. What destination does Kennet policy use for unsealed "
    "parcels?")
PROMPT_MIXED_APPROVER = (
    "This Kennet unsealed parcel is being sent to Pine Crossing. "
    "Who approves the job?")
PROMPT_PRICE = (
    "What does the Kennet unsealed parcel route to Pine Crossing cost?")

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_MODEL_AUTODOWNLOAD",
    "ZMEM_MODELS_DIR", "ZMEM_SESSION", "CLAUDE_SESSION_ID",
    "ZCODE_SESSION_ID", "ZMEM_EMBED_PROFILE", "ZMEM_QUERY_CONTEXT",
    "ZMEM_TRANSCRIPT", "ZMEM_TIER0", "ZMEM_SKILLS_DIRS", "ZMEM_PYTHON",
    "ZMEM_CROSS_PROJECT", "ZMEM_REMOTE_URL", "ZMEM_CROSS_ENCODER",
)


class Admission498Base(unittest.TestCase):
    """Shared hermetic store + CLI helpers."""

    @classmethod
    def setUpClass(cls):
        cls.scratch = _SCRATCH
        empty_models = cls.scratch / "models-empty"
        empty_models.mkdir(exist_ok=True)
        redir_home = cls.scratch / "home"
        redir_home.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env.update({
            "ZMEM_STORE": str(cls.scratch / "store.sqlite"),
            "ZMEM_DATA": str(cls.scratch),
            "ZMEM_INJECT": "1",
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_MODELS_DIR": str(empty_models),
            "HOME": str(redir_home),
            "USERPROFILE": str(redir_home),
            "PYTHONUTF8": "1",
        })
        cls.env = env
        cls.row_ids = {}
        for label, content, tags in SEEDS:
            cls.row_ids[label] = cls._add(NS, content, tags)
        # Qualified-fact namespace: ONE row that genuinely carries the
        # requested (price) information for the same subject.
        cls.row_ids["qualified-price"] = cls._add(
            NS_QUALIFIED, QUALIFIED_PRICE, "kennet,pricing")

    @classmethod
    def _cli(cls, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY)] + list(args),
            capture_output=True, text=True, env=cls.env,
            cwd=str(REPO_ROOT), timeout=180)

    @classmethod
    def _add(cls, namespace, content, tags):
        r = cls._cli("add", "--namespace", namespace, "--type", "fact",
                     "--content", content, "--tags", tags,
                     "--signal", "test", "--confidence", "0.9", "--json")
        if r.returncode != 0:
            raise RuntimeError("seed add failed (%d): %s%s"
                               % (r.returncode, r.stdout[-200:],
                                  r.stderr[-200:]))
        return json.loads(r.stdout)["id"]

    def _recall(self, query, namespace=NS):
        """Drive the exact hook-lane store argv with a fresh session id."""
        r = self._cli(
            "recall", "--for-injection", "--json",
            "--session-id", "adm498-" + uuid.uuid4().hex[:12],
            "--moment", "user_prompt", "--lane", "codex",
            "--namespace", namespace, "--query", query,
            "--limit", "5", "--include-global", "--global-limit", "3",
            "--no-bump")
        self.assertEqual(
            r.returncode, 0,
            "recall exited %d; stderr=%s" % (r.returncode,
                                             r.stderr.strip()[:400]))
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            self.fail("recall stdout is not JSON: %r" % r.stdout[:300])

    # -- assertion helpers ---------------------------------------------------
    def _results_ids(self, envelope):
        return [row.get("id") for row in envelope.get("results", [])
                if isinstance(row, dict)]

    def _results_contents(self, envelope):
        return "\n".join(str(row.get("content") or "")
                         for row in envelope.get("results", [])
                         if isinstance(row, dict))

    def _diag(self, envelope):
        return ("candidate_ids=%r rendered_head=%r"
                % (envelope.get("candidate_ids"),
                   (envelope.get("rendered") or "")[:200]))

    def assertNotSurfaced(self, envelope, row_id, marker, why):
        """The row must be neither rendered nor counted as surfaced."""
        rendered = envelope.get("rendered") or ""
        contents = self._results_contents(envelope)
        self.assertNotIn(
            marker, rendered,
            "%s: conflicting marker %r rendered. %s"
            % (why, marker, self._diag(envelope)))
        self.assertNotIn(
            marker, contents,
            "%s: conflicting marker %r in delivered results content. %s"
            % (why, marker, self._diag(envelope)))
        self.assertNotIn(
            row_id, self._results_ids(envelope),
            "%s: conflicting default row id %r counted as surfaced. %s"
            % (why, row_id, self._diag(envelope)))

    def assertSurfaced(self, envelope, marker, why):
        rendered = envelope.get("rendered") or ""
        contents = self._results_contents(envelope)
        self.assertTrue(
            marker in rendered or marker in contents,
            "%s: expected marker %r missing from rendered and results. %s"
            % (why, marker, self._diag(envelope)))


class AC1ExplicitDetailTests(Admission498Base):
    """AC1: explicit destination/assignment beats the standing default."""

    def test_explicit_kennet_destination_suppresses_rowan_dock(self):
        env = self._recall(PROMPT_PINE_CROSSING)
        self.assertNotSurfaced(
            env, self.row_ids["kennet-default"], "Rowan Dock",
            "AC1 pine-crossing: the prompt explicitly sends the parcel to "
            "Pine Crossing, so the stored Rowan Dock standing default must "
            "not be delivered")

    def test_explicit_ash_register_suppresses_pearl_register(self):
        env = self._recall(PROMPT_ASH_REGISTER)
        self.assertNotSurfaced(
            env, self.row_ids["plover-register"], "Pearl Register",
            "AC1 ash-register: the prompt explicitly assigns the check to "
            "the Ash Register, so the stored Pearl Register default must "
            "not be delivered")

    def test_asking_for_standing_default_still_surfaces_it(self):
        env = self._recall(PROMPT_STANDING_DEFAULT)
        self.assertSurfaced(
            env, "Rowan Dock",
            "AC1 standing-default-ask: a prompt asking what the standing "
            "default IS must still retrieve the Rowan Dock fact")

    def test_hypothetical_other_item_does_not_suppress_standing_default(self):
        env = self._recall(PROMPT_HYPOTHETICAL_OTHER)
        self.assertSurfaced(
            env, "Rowan Dock",
            "AC1 hypothetical/different-item: a hypothetical Rose Yard "
            "assignment for a DIFFERENT item (Morrowfield scan) must not "
            "wrongly suppress the Kennet standing default the prompt asks "
            "for")


class AC2AttributeScopeTests(Admission498Base):
    """AC2: independent attributes survive; unsupported ones do not leak."""

    def test_mixed_prompt_preserves_approval_and_suppresses_default(self):
        env = self._recall(PROMPT_MIXED_APPROVER)
        self.assertSurfaced(
            env, "Lyle",
            "AC2 mixed-approver: the independent approval fact must be "
            "delivered when the prompt asks who approves the job")
        self.assertNotSurfaced(
            env, self.row_ids["kennet-default"], "Rowan Dock",
            "AC2 mixed-approver: supplying Pine Crossing must still "
            "suppress the conflicting destination default")

    def test_price_question_does_not_deliver_destination_only_fact(self):
        env = self._recall(PROMPT_PRICE)
        self.assertNotSurfaced(
            env, self.row_ids["kennet-default"], "Rowan Dock",
            "AC2 price-attribute: the question asks about cost; the "
            "destination-only standing default carries no price and must "
            "not be delivered")

    def test_qualified_fact_containing_price_is_preserved(self):
        env = self._recall(PROMPT_PRICE, namespace=NS_QUALIFIED)
        self.assertSurfaced(
            env, "40 marks",
            "AC2 qualified-fact: the qualified row genuinely contains the "
            "requested price information and must stay retrievable on the "
            "price question")


if __name__ == "__main__":
    unittest.main()
