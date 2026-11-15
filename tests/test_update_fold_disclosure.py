"""Issue #472: `store.py update`'s dedup fold must disclose the INCOMING
update content's disposition (frozen acceptance check).

Contract this check pins (storelib/write.py update_memory fold branch):
when an update folds into an existing similar live row (today any
similarity >= 0.85 detected by the dedup detector), ONLY metadata
merges into the keeper — the incoming update
content is dropped on the floor (the old row is tombstoned, the new
content never stored). Today the CLI claims the opposite:

    [zmem] update: <old-id> superseded; new content merged into
    existing live memory <keeper-id> (similarity=0.977)

(FALSE — nothing was merged), and `update --json` returns
{"id": keeper, "result": "updated", "created_new": false, "warnings": []}
— empty warnings, no disposition, so a machine consumer cannot learn its
update vanished. There is no --no-dedup escape on the update subparser.

AC1 (UpdateFoldDisclosureTest): the fold must disclose honestly on BOTH
surfaces — a human stdout line carrying the keeper id, the fold
similarity with three decimals, and a "not stored" phrasing for the
incoming content (and the false "new content merged into existing live
memory" line must be gone); and an `update --json` fold must return a
parseable JSON object whose `warnings` list is NON-empty and mentions
the content drop. The distinctive token of the dropped update must
still be unsearchable (the disclosure must be truthful — this pins the
test to the REAL fold path, not a no-fold theater).

AC2 (UpdateFoldThresholdTest, decision-gated): an update whose fold
similarity sits BELOW the update-fold decision threshold — targeted
here into the band [0.86, 0.96], strictly above the 0.85 dedup floor
and strictly below 1.0 — must land as its OWN row, and ALL THREE legs
are required together (plan-critic r2 MAJOR 3): `update --json`
reports created_new true AND a NON-keeper result id AND the search
returns the landed row itself (row id == result id, content carrying
the distinctive token). At base the fold still fires inside that band
and drops the content — that is the required RED.

Discriminating signature: RED at base (the disclosure, the warnings,
and the own-row landing do not exist), GREEN after the #472 fix lands.

Drives the REAL skills/memory/scripts/store.py CLI via subprocess
against a hermetic throwaway store (fake embed profile; the boilerplate
below is probe-verified to fold at similarity ~0.977 under that
profile, and the AC2 mid-slice variant at ~0.916).

Run: python -X utf8 tests/test_update_fold_disclosure.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable

# Boilerplate proven to fold at similarity ~0.977 under the fake embed
# profile (probe repro for #472, same corpus as the #469 check): ~70
# shared tokens, distinct tail.
BOILER = (
    "zmem ship record for workstream followup pr review session with plan critic rounds "
    "implementation reviewer and final critic gates all approve at head tree identity "
    "release version bump with eight manifests changelog newest first section dated "
    "canary report regenerated support report stamped after canary five lanes fixture "
    "regenerated release gate emit manifest after git add verification replayed frozen "
    "checks red to green mutation probes killed scan deferred clean full test loop green "
    "anchor receipt published on the target issue with semantics digest stable"
)
CONTENT_A = BOILER + " pr 467"                 # the keeper row
NEARDUP_HUMAN = BOILER + " pr 472 zetafold472"  # AC1 human-leg update (folds ~0.977)
NEARDUP_JSON = BOILER + " pr 474 zetafold473"   # AC1 json-leg update  (folds ~0.976)
UNRELATED_B = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet "
    "kilo lima mike november standby baseline unrelated filler row"
)
UNRELATED_C = (
    "oscar papa quebec romeo sierra tango uniform victor whiskey xray "
    "yankee zulu standby baseline unrelated filler row two"
)

# AC2 band fixture: alter a MATERIAL mid-slice of the shared boilerplate
# (several consecutive words replaced) and append a distinctive tail.
# Probe-verified measured fold similarity ~0.916 under the fake profile
# — inside the target band [0.86, 0.96], strictly above the 0.85 floor.
MID_SLICE = (
    "release version bump with eight manifests changelog newest first "
    "section dated"
)
REPL_SLICE = (
    "artifact promotion count across nine registries tagroll older "
    "last block stamped"
)
BAND_CONTENT = BOILER.replace(MID_SLICE, REPL_SLICE) + " okapi472band"

NS_AC1 = "project:updfold472"
NS_AC2 = "project:updband472"

SIM_RE = re.compile(r"similarity=(\d\.\d{3})")
DROP_WARN_RE = re.compile(r"not stored|content.*(drop|discard|not)",
                          re.IGNORECASE)
FALSE_PHRASE = "new content merged into existing live memory"


def _env(tmp: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env.pop("ZMEM_DATA", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["ZMEM_EMBED_PROFILE"] = "fake"
    env["PYTHONIOENCODING"] = "utf-8"
    # Strip ambient knobs that gate the behavior under test or the store
    # child's config (same discipline as the #469 check): an operator-level
    # ZMEM_DEDUP_THRESHOLD above the fold similarity suppresses the fold,
    # and an ambient ZMEM_REMOTE_URL flips the child into remote mode.
    # Plan-critic r1 (MAJOR 5): also strip the update-fold knob this issue
    # introduces — each test class pins it explicitly below.
    env.pop("ZMEM_DEDUP_THRESHOLD", None)
    env.pop("ZMEM_REMOTE_URL", None)
    env.pop("ZMEM_UPDATE_FOLD_THRESHOLD", None)
    return env


class UpdateFoldDisclosureTest(unittest.TestCase):
    """AC1: the update fold must disclose, on both surfaces, that the
    incoming update content was NOT stored."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-updfold472-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = _env(self.tmp)
        # Plan REVISION 2 (critic-endorsed T=1.0 exact-only default): the
        # near-dup fold lane this test pins is only REACHABLE when the
        # operator knob opts into near-dup folding — pin it below the
        # fixture's 0.977 similarity so the disclosure contract stays
        # deterministically exercised under the 1.0 default.
        self.env["ZMEM_UPDATE_FOLD_THRESHOLD"] = "0.95"

    def _run(self, *args):
        return subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=180,
        )

    def _add(self, content: str, ns: str) -> str:
        r = self._run("add", "--namespace", ns, "--type", "fact",
                      "--content", content, "--signal", "test")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = re.search(r"\[zmem\] added memory ([0-9a-f-]{36})", r.stdout)
        self.assertIsNotNone(
            m, f"add must report its memory id; stdout:\n{r.stdout}")
        return m.group(1)

    def test_update_fold_discloses_content_drop(self):
        """When `update` folds into an existing live row, the human
        stdout must carry an honest disclosure (keeper id + 3-decimal
        similarity + a 'not stored' phrasing for the incoming content,
        with the false 'new content merged' line gone), and a second
        fold via `update --json` must return non-empty warnings naming
        the content drop. RED at base (the disclosure and the warnings
        do not exist), GREEN after the #472 fix."""
        rinit = self._run("init")
        self.assertEqual(rinit.returncode, 0, rinit.stderr)
        mem_a_id = self._add(CONTENT_A, NS_AC1)   # the keeper
        mem_b_id = self._add(UNRELATED_B, NS_AC1)  # the update victim

        # --- AC1(a): the HUMAN fold path must disclose the drop -------
        ru = self._run("update", "--id", mem_b_id, "--namespace", NS_AC1,
                       "--type", "fact", "--content", NEARDUP_HUMAN)
        self.assertEqual(ru.returncode, 0, ru.stderr)

        # One stdout line carrying, TOGETHER: the keeper row id, the fold
        # similarity with three decimals, and a "not stored" phrasing for
        # the incoming update content. At base the only keeper-mentioning
        # line is the false "new content merged into existing live
        # memory" line, which carries none of the drop phrasing.
        keeper_lines = [ln.strip() for ln in ru.stdout.splitlines()
                        if mem_a_id in ln]
        disclosures = [
            ln for ln in keeper_lines
            if "not stored" in ln.lower()
            and re.search(r"similarity=\d+\.\d{3}", ln)
        ]
        if not keeper_lines:
            detail = ("the update printed no stdout line naming the "
                      f"keeper row {mem_a_id}; the fold did not fire "
                      "under the fake embed profile")
        elif not disclosures:
            detail = ("the stdout line(s) naming the keeper do not "
                      "disclose the incoming update content's disposition")
        else:
            detail = ""
        self.assertTrue(
            disclosures,
            f"{detail}. The update fold must print ONE stdout line that "
            f"carries the keeper row id {mem_a_id}, the fold similarity "
            f"with three decimals (similarity=d.ddd), and a phrasing "
            f"stating the update's content was NOT stored. "
            f"update stdout:\n{ru.stdout}")

        # The FALSE merged-into-keeper claim must be gone entirely — only
        # metadata merges; the content is dropped.
        self.assertNotIn(
            FALSE_PHRASE, ru.stdout,
            "the fold line must no longer claim the new content was "
            "'merged into existing live memory' (only metadata merges; "
            f"the update content is dropped); update stdout:\n{ru.stdout}")

        # --- AC1(c): the disclosure must be honest — the dropped -------
        # update content's distinctive token stays unsearchable, and the
        # search path still returns rows for STORED content (positive
        # control, so the no-match leg cannot pass on a blanket no-match
        # regression).
        rpos = self._run("search", "--text", "pr 467", "--namespace", NS_AC1)
        self.assertEqual(rpos.returncode, 0, rpos.stderr)
        self.assertIn(
            "pr 467", rpos.stdout,
            "positive control: a token of the keeper row must stay "
            f"searchable; search stdout:\n{rpos.stdout}")
        rjs = self._run("search", "--text", "zetafold472", "--namespace",
                        NS_AC1, "--json")
        self.assertEqual(rjs.returncode, 0, rjs.stderr)
        try:
            search_payload = json.loads(rjs.stdout)
        except ValueError:
            self.fail(f"search --json stdout must be one parseable JSON "
                      f"object; stdout:\n{rjs.stdout}")
        self.assertEqual(
            search_payload.get("count"), 0,
            "the dropped update content's distinctive token must return "
            f"0 results (the disclosure is honest); search stdout:\n"
            f"{rjs.stdout}")
        self.assertNotIn(
            "zetafold472", rjs.stdout,
            "the dropped update content's distinctive token must not "
            f"appear in any returned row; search stdout:\n{rjs.stdout}")

        # --- AC1(b): a SECOND fold via `update --json` (a THIRD live ---
        # row — B is tombstoned by the first fold, dead ids are refused)
        # must return machine-readable JSON with NON-empty warnings that
        # mention the content drop.
        mem_c_id = self._add(UNRELATED_C, NS_AC1)
        rj = self._run("update", "--json", "--id", mem_c_id,
                       "--namespace", NS_AC1, "--type", "fact",
                       "--content", NEARDUP_JSON)
        self.assertEqual(rj.returncode, 0, rj.stderr)
        self.assertNotIn(
            "[zmem]", rj.stdout,
            "no human [zmem] line may leak into --json stdout (human "
            f"notices go to stderr under --json); stdout:\n{rj.stdout}")
        json_lines = [ln for ln in rj.stdout.splitlines() if ln.strip()]
        self.assertTrue(
            json_lines, f"update --json must print a JSON object; "
            f"stdout:\n{rj.stdout}")
        try:
            payload = json.loads(json_lines[-1])
        except ValueError as exc:
            self.fail(f"update --json stdout must stay machine-readable "
                      f"(json.loads on the last stdout line): {exc}; "
                      f"stdout:\n{rj.stdout}")
        # Vacuity guard: this leg must really have FOLDED into the keeper
        # (an empty-warnings result on a created_new path would be honest
        # and could satisfy the warnings leg for the wrong reason).
        self.assertEqual(
            payload.get("id"), mem_a_id,
            f"the --json leg must report the keeper row id {mem_a_id} "
            f"(the fold fired); payload:\n{json.dumps(payload)}")
        warnings = payload.get("warnings")
        self.assertTrue(
            isinstance(warnings, list) and warnings,
            "an update fold under --json must return a NON-empty "
            "`warnings` list disclosing that the incoming update content "
            "was dropped — at base it is [] and a machine consumer "
            f"cannot learn its update vanished; payload:\n"
            f"{json.dumps(payload)}")
        warning_texts = [json.dumps(w) for w in warnings]
        self.assertTrue(
            any(DROP_WARN_RE.search(t) for t in warning_texts),
            "at least one warning must mention the content drop "
            "(matching e.g. 'not stored' or 'content ... dropped/"
            f"discarded'); warnings:\n" + "\n".join(warning_texts))
        # Plan-critic r1 (MAJOR 7): the warning must carry the SAME
        # disposition as the human line — the keeper id AND the formatted
        # similarity ride along (a bare 'content dropped' note is not the
        # same disposition).
        self.assertTrue(
            any(mem_a_id in t and re.search(r"similarity=\d+\.\d{3}", t)
                for t in warning_texts),
            "at least one warning must name the keeper row "
            f"{mem_a_id} and the fold similarity (similarity=d.ddd) "
            f"alongside the drop disposition; warnings:\n"
            + "\n".join(warning_texts))


class UpdateFoldThresholdTest(unittest.TestCase):
    """AC2 (decision-gated): an update whose fold similarity is BELOW the
    update-fold decision threshold (targeted into [0.86, 0.96], strictly
    above the 0.85 dedup floor) must land as its OWN row."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-updband472-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = _env(self.tmp)
        # Plan REVISION 2: run at the DEFAULT update-fold threshold (1.0,
        # exact-only) — _env already stripped the ambient knob; the explicit
        # pop documents that this class must not opt into near-dup folding.
        self.env.pop("ZMEM_UPDATE_FOLD_THRESHOLD", None)

    def _run(self, *args):
        return subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=180,
        )

    def _add(self, content: str, ns: str) -> str:
        r = self._run("add", "--namespace", ns, "--type", "fact",
                      "--content", content, "--signal", "test")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = re.search(r"\[zmem\] added memory ([0-9a-f-]{36})", r.stdout)
        self.assertIsNotNone(
            m, f"add must report its memory id; stdout:\n{r.stdout}")
        return m.group(1)

    def _calibration_similarity(self) -> "float | None":
        """Independent reading of the (CONTENT_A, BAND_CONTENT) pair
        similarity via the ADD path in a throwaway store: `add` folds at
        the 0.85 floor and prints similarity=d.ddd, so the fixture's
        measured band position stays verifiable even after the update
        path stops folding inside the band (the post-fix world)."""
        tmp = tempfile.mkdtemp(prefix="zmem-updband472cal-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = _env(tmp)
        rinit = subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), "init"],
            env=env, capture_output=True, text=True, timeout=180)
        if rinit.returncode != 0:
            return None
        ra = subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), "add", "--namespace",
             "project:updband472cal", "--type", "fact", "--content",
             CONTENT_A, "--signal", "test"],
            env=env, capture_output=True, text=True, timeout=180)
        if ra.returncode != 0:
            return None
        rb = subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), "add", "--namespace",
             "project:updband472cal", "--type", "fact", "--content",
             BAND_CONTENT, "--signal", "test"],
            env=env, capture_output=True, text=True, timeout=180)
        if rb.returncode != 0:
            return None
        m = SIM_RE.search(rb.stdout) or SIM_RE.search(rb.stderr)
        return float(m.group(1)) if m else None

    def test_subthreshold_update_lands_own_row(self):
        """An update whose measured fold similarity sits inside the band
        [0.86, 0.96] (strictly above the 0.85 dedup floor, strictly below
        1.0) must land as its OWN row, ALL THREE legs together
        (plan-critic r2 MAJOR 3): `update --json` reports created_new
        true AND a non-keeper result id AND the search RETURNS the landed
        row itself (its id equals the result id and its content carries
        the distinctive token). At base the fold still fires in
        that band and drops the content — the required RED the #472
        decision must turn GREEN."""
        rinit = self._run("init")
        self.assertEqual(rinit.returncode, 0, rinit.stderr)
        mem_keep = self._add(CONTENT_A, NS_AC2)      # prospective keeper
        mem_victim = self._add(UNRELATED_B, NS_AC2)  # update victim

        rj = self._run("update", "--json", "--id", mem_victim,
                       "--namespace", NS_AC2, "--type", "fact",
                       "--content", BAND_CONTENT)
        self.assertEqual(rj.returncode, 0, rj.stderr)

        # Precondition: VERIFY the fixture's measured similarity against
        # the code's own output. Prefer the update's own fold line (under
        # --json the human line lands on stderr); once the fix stops the
        # in-band fold, fall back to an independent ADD-path reading of
        # the same content pair.
        m = SIM_RE.search(rj.stderr) or SIM_RE.search(rj.stdout)
        measured = float(m.group(1)) if m else self._calibration_similarity()
        self.assertIsNotNone(
            measured,
            "fixture calibration failed: neither the update output nor "
            "the add-path calibration printed a similarity=d.ddd line — "
            "recalibrate the AC2 fixture so its fold similarity is "
            "measurably inside [0.86, 0.96]")
        self.assertTrue(
            0.86 <= measured <= 0.96,
            f"AC2 fixture drift: measured fold similarity {measured:.3f} "
            f"is outside the target band [0.86, 0.96] (strictly above "
            f"the 0.85 dedup floor, strictly below 1.0) — recalibrate "
            f"MID_SLICE/REPL_SLICE so the update sits in-band")

        json_lines = [ln for ln in rj.stdout.splitlines() if ln.strip()]
        self.assertTrue(
            json_lines, f"update --json must print a JSON object; "
            f"stdout:\n{rj.stdout}")
        try:
            payload = json.loads(json_lines[-1])
        except ValueError as exc:
            self.fail(f"update --json stdout must stay machine-readable "
                      f"(json.loads on the last stdout line): {exc}; "
                      f"stdout:\n{rj.stdout}")
        created_new = payload.get("created_new")
        result_id = payload.get("id")

        rs = self._run("search", "--text", "okapi472band", "--namespace",
                       NS_AC2, "--json")
        self.assertEqual(rs.returncode, 0, rs.stderr)
        try:
            search_payload = json.loads(rs.stdout)
        except ValueError:
            self.fail(f"search --json stdout must be one parseable JSON "
                      f"object; stdout:\n{rs.stdout}")
        # Plan-critic r2 (MAJOR 3): 'token found' must mean the SEARCH
        # RETURNED THE LANDED ROW — a row whose id is the update's
        # non-keeper result id and whose content carries the distinctive
        # token. A bare count>0 (unrelated keeper hits) or a stdout
        # substring must NOT satisfy the predicate.
        token_found = any(
            row.get("id") == result_id and row.get("id") != mem_keep
            and "okapi472band" in (row.get("content") or "")
            for row in (search_payload.get("results") or []
                        if isinstance(search_payload, dict) else []))

        # THE CONTRACT: below the update-fold decision threshold the
        # update lands its own row. At base the in-band fold still fires,
        # created_new is false and the token is unsearchable — RED.
        # Plan-critic r1 (MAJOR 7): ALL THREE legs required — a false
        # created_new receipt over a dropped fold, or a findable token on
        # a fold receipt, each still fail the contract.
        self.assertTrue(
            created_new is True and payload.get("id") != mem_keep
            and token_found,
            f"an update folding at similarity {measured:.3f} (inside the "
            f"band [0.86, 0.96], below the update-fold decision "
            f"threshold) must land as its OWN row: update --json must "
            f"report created_new=true with a NON-keeper result id, AND "
            f"the update's distinctive token 'okapi472band' must be "
            f"findable via search. Measured: created_new={created_new!r}, "
            f"result id={payload.get('id')!r} (keeper {mem_keep}), search "
            f"count={search_payload.get('count')!r} — the content was "
            f"dropped by the in-band fold; update payload:\n"
            f"{json.dumps(payload)}\nsearch stdout:\n{rs.stdout}")

        # The keeper row must be untouched by the update landing.
        rpos = self._run("search", "--text", "pr 467", "--namespace", NS_AC2)
        self.assertEqual(rpos.returncode, 0, rpos.stderr)
        self.assertIn(
            "pr 467", rpos.stdout,
            "positive control: the keeper row's distinctive token must "
            f"stay searchable; search stdout:\n{rpos.stdout}")
        rl = self._run("list", "--json", "--namespace", NS_AC2)
        self.assertEqual(rl.returncode, 0, rl.stderr)
        try:
            listed = json.loads(rl.stdout)
        except ValueError:
            self.fail(f"list --json stdout must be one parseable JSON "
                      f"object; stdout:\n{rl.stdout}")
        keep_rows = [row for row in listed.get("memories", [])
                     if row.get("id") == mem_keep]
        self.assertTrue(
            keep_rows, f"the keeper row {mem_keep} must remain live in "
            f"the namespace; list payload:\n{json.dumps(listed)}")
        self.assertIsNone(
            keep_rows[0].get("superseded_at"),
            f"the keeper row {mem_keep} must not be tombstoned; row:\n"
            f"{json.dumps(keep_rows[0])}")
        self.assertEqual(
            keep_rows[0].get("preview"), CONTENT_A[:80],
            f"the keeper row's content must be unchanged by the update; "
            f"row:\n{json.dumps(keep_rows[0])}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
