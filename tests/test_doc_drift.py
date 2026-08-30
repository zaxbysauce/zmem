"""Documentation-drift ratchet (issue #56, task 1.8).

Each test pins a documentation claim that had drifted from code and was
repaired by the 0.8.7 documentation-truth pass (issue #56). If a test here
fails, a doc or tracked file regressed — fix the doc/file; do not weaken the
test. The companion schema-version pin lives in tests/test_schema_version.py.

Covered regressions:
  - SKILL.md describing vector recall as "a future optional tier" (hybrid
    `--hybrid` RRF recall already shipped)
  - SKILL.md recall usage omitting `--hybrid` / `--no-bump`
  - PLAN.md claiming userConfig.storeDirectory is unwired (it is wired via
    zmem-launch.js / CLAUDE_PLUGIN_OPTION_STOREDIRECTORY, #38 I6)
  - any tracked file claiming a 3.8-era Python floor (the floor is 3.11)
  - any tracked file embedding an absolute user home path (leaks the
    operator's username/machine into the distributable; e.g. the graphify
    cache that shipped under graphify-out/ before 0.8.7 untracked it)

Needles that would match this file's own source are built by concatenation so
the scan cannot self-match (and a test asserts exactly that).

Run: python tests/test_doc_drift.py
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "skills" / "memory" / "SKILL.md"
PLAN_MD = REPO_ROOT / "PLAN.md"

_BS = chr(92)  # backslash, built indirectly so this file cannot self-match
# One-or-more path separator as a REGEX fragment: a character class holding an
# escaped backslash and a slash. The backslash is doubled (chr(92) twice)
# because inside a class a lone backslash before ] would read as an escaped
# closing bracket, silently changing the class to {], /, +} — no backslash.
_SEP = "[" + _BS * 2 + "/]+"

# An absolute user home path: a Windows drive form (drive-letter + separator
# + Users + separator + a real-looking name), or a POSIX/macOS home dir.
# Placeholders whose first segment character is outside [A-Za-z0-9_~-] (e.g.
# an angle-bracket form after the Users segment) deliberately do NOT match —
# that is the sanctioned way to show a path shape in docs/examples.
HOME_PATH_RE = re.compile(
    "(?:[A-Za-z]:{sep}Users{sep}|{sep}home{sep}|{sep}Users{sep})"
    "[A-Za-z0-9_~-]+".format(sep=_SEP)
)

# Floor-claim pattern, assembled from parts so this file's own source cannot
# contain the contiguous literals it forbids. Covers the realistic regression
# phrasings: the plus-suffixed form, the wordy "requires <interpreter>" form,
# and the >= comparator form. Legitimate range/history text stays legal: a
# bare "3.8" (as in an en-dashed 3.8–3.10 range or "below 3.8") matches none
# of the three alternatives.
_PY = "Py" + "thon"
FLOOR_CLAIM_RE = re.compile(
    "(?:{py}\\s+3\\.8\\b|>=\\s*3\\.8\\b|3\\.8\\+)".format(py=_PY)
)


def _tracked_files():
    """Every git-tracked path. A git failure is a loud test failure, never a
    vacuous pass — the ratchet must not silently skip the scan."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(REPO_ROOT), capture_output=True, timeout=60,
        )
    except FileNotFoundError as exc:
        # git absent from PATH entirely: subprocess raises before any
        # returncode exists. Still loud (unittest reports it as an ERROR),
        # but make it the AssertionError the contract promises (PRR-013).
        raise AssertionError(
            "git binary not found — the tracked-file ratchet requires git "
            "and must fail loudly, never skip") from exc
    if out.returncode != 0:
        raise AssertionError(
            "git ls-files failed (rc=%d): %s" % (
                out.returncode, out.stderr.decode("utf-8", "replace")))
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def _read(rel):
    return (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")


class SkillDocDriftTest(unittest.TestCase):

    # -- v13 (issue #65) -----------------------------------------------------

    def test_v13_episode_cli_documented(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        for cmd in ("episode-open", "episode-add", "episode-close",
                    "episode-list"):
            self.assertIn(cmd, text, f"SKILL.md must document `{cmd}` (10.7)")
        self.assertIn("episode_memory", text)
        self.assertIn("token_count", text)

    def test_v13_session_tools_documented(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("session_start", text)
        self.assertIn("session_end", text)
        self.assertIn("NO-WRITE", text)
        self.assertIn("retrieval_count", text)

    def test_v13_scoped_tokens_documented(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("namespace_not_allowed", text)
        self.assertIn("ZMEM_MCP_TOKEN_FILE", text)
        self.assertIn("unscoped_token", text)

    def test_v13_token_budget_documented(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("ZMEM_INJECT_TOKEN_BUDGET", text)
        self.assertIn("tokens_used", text)
        self.assertIn("tokens_budget", text)

    def test_v13_read_envelope_documented(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn('"omitted"', text)
        self.assertIn("injection_risk", text)
        self.assertIn("kind", text)

    def test_no_future_optional_tier_claim(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertNotIn(
            "future optional tier", text,
            "SKILL.md must not describe vector/embedding recall as a future "
            "tier — `--hybrid` RRF recall shipped; document it instead")

    def test_recall_usage_lists_hybrid_and_no_bump(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("--hybrid", text,
                      "SKILL.md recall documentation must list --hybrid")
        self.assertIn("--no-bump", text,
                      "SKILL.md recall documentation must list --no-bump")

    def test_no_hybrid_documented(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("--no-hybrid", text,
                      "SKILL.md recall documentation must list --no-hybrid")

    def test_as_of_documented(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("--as-of", text,
                      "SKILL.md recall documentation must list --as-of")

    def test_three_floors_documented(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        # Three distinct floors with distinct env overrides. If any of
        # these three constants is missing, the floor is ungrounded.
        self.assertIn("INJECT_FLOOR_PROMPT_DEFAULT", text,
                      "SKILL.md must document INJECT_FLOOR_PROMPT_DEFAULT")
        self.assertIn("INJECT_FLOOR_RECENT_DEFAULT", text,
                      "SKILL.md must document INJECT_FLOOR_RECENT_DEFAULT")
        self.assertIn("INJECT_FLOOR_GATE_NONE", text,
                      "SKILL.md must document INJECT_FLOOR_GATE_NONE")

    # -- issue #59, 4.x: the append-only revision surface is documented -------

    def test_update_and_invalidate_documented(self):
        """SKILL.md must document the append-only `update` and the
        reason-required `invalidate` commands (and the lineage/validity columns
        they write) — a later doc edit cannot silently drop the v9 write path
        while the code and tests still ship it (test_doc_drift ratchet).
        PR-review PRR-T: the needles are the feature-specific CLI tokens, not
        the bare English words 'update'/'invalidate' (which match generic
        prose anywhere and kept the ratchet green even with the whole feature
        section deleted)."""
        text = SKILL_MD.read_text(encoding="utf-8")
        for needle in ("update --id", "invalidate --id", "--reason",
                       "update_of", "valid_until", "append-only"):
            self.assertIn(needle, text,
                          f"SKILL.md must document {needle} (issue #59 write path)")

    def test_taint_provenance_documented(self):
        """SKILL.md must document the taint ranks (there are exactly three, an
        unknown is refused), the worst-of propagation rule, and that explicit
        recall prefixes untrusted rows — otherwise operators cannot reason about
        trust on any surface."""
        text = SKILL_MD.read_text(encoding="utf-8")
        for needle in ("taint", "trusted_internal", "untrusted_tool",
                       "untrusted_web", "worst-of"):
            self.assertIn(needle, text,
                          f"SKILL.md must document the taint model ({needle})")
        self.assertNotIn("four streams", text)  # sanity: no invented rank

    def test_as_of_valid_until_exclusive_semantics_documented(self):
        """The complete --as-of contract (valid_until EXCLUSIVE end, and the
        superseded filter drop under as-of) must be documented so callers do
        not re-discover the boundary as a bug."""
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("EXCLUSIVE", text,
                      "SKILL.md must state valid_until is the EXCLUSIVE end")
        self.assertIn("valid_until > as_of", text,
                      "SKILL.md must state the as-of validity predicate")

    def test_decision_constraint_documented_as_shipped_types(self):
        """decision/constraint are FIRST-CLASS shipped types (issue #59);
        SKILL.md must list them in the add --type usage as ordinary choices —
        never demoted to a deferred/future note, or a reader would build
        against a system that already ships them."""
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("decision|constraint", text,
                      "SKILL.md add --type must list decision/constraint as "
                      "shipped choices")

    # -- issue #60, 5.7: the three-signal retrieval surface is documented ----

    def _retrieval_section(self) -> str:
        """The '## How recall works' section body (up to the next ## header).

        Section-scoped on purpose: the issue's test requirement is that THE
        RETRIEVAL SECTION mentions entity matching and MMR — whole-file
        needles would stay green if the mentions drifted into an unrelated
        section while the retrieval description went stale."""
        text = SKILL_MD.read_text(encoding="utf-8")
        start = text.find("## How recall works")
        self.assertGreaterEqual(start, 0, "SKILL.md must have a '## How recall works' section")
        end = text.find("\n## ", start + 1)
        return text[start:] if end < 0 else text[start:end]

    def test_retrieval_section_mentions_entity_matching_and_mmr(self):
        """Issue #60, 5.7: the retrieval section must document the entity
        lane (third RRF signal) and MMR diversity — the two v10 retrieval
        behaviors — so an agent reading only SKILL.md operates them."""
        section = self._retrieval_section()
        self.assertIn("entity matching", section,
                      "the retrieval section must mention entity matching "
                      "(the third RRF signal, issue #60 5.3)")
        self.assertIn("MMR", section,
                      "the retrieval section must mention MMR diversity "
                      "(issue #60 5.5)")

    def test_entity_and_mmr_surface_flags_documented(self):
        """An agent reading only SKILL.md can operate --no-mmr, entity-list,
        and entity-merge (issue #60, 5.7), and the MMR env knob is named."""
        text = SKILL_MD.read_text(encoding="utf-8")
        for needle in ("--no-mmr", "ZMEM_MMR_LAMBDA",
                       "entity-list", "entity-merge", "--confirm"):
            self.assertIn(needle, text,
                          f"SKILL.md must document {needle} (issue #60 surface)")

    # -- issue #61 (v11): the associative-link surface is documented ----------

    def test_link_surface_flags_and_commands_documented(self):
        """Issue #61, 6.3/6.5: an agent reading only SKILL.md can operate
        --link-hops/--link-budget, links, and contradict, and the link env
        knob is named."""
        text = SKILL_MD.read_text(encoding="utf-8")
        for needle in ("--link-hops", "--link-budget",
                       "ZMEM_LINK_THRESHOLD", "contradict",
                       "trust_score", "[CONTESTED LINK]"):
            self.assertIn(needle, text,
                          f"SKILL.md must document {needle} (issue #61 surface)")

    def test_retrieval_section_mentions_link_expansion(self):
        """Issue #61, 6.3: the retrieval section must document 1-hop link
        expansion so an agent reading only SKILL.md can interpret the extra
        recall rows and the [CONTESTED LINK] tag."""
        section = self._retrieval_section()
        self.assertIn("Link expansion", section,
                      "the retrieval section must mention link expansion "
                      "(issue #61 6.3)")
        self.assertIn("[CONTESTED LINK]", section)

    def test_linking_is_deterministic_and_llm_free(self):
        """Link generation is deterministic by design and ships no LLM knob
        (ZMEM_LINK_LLM deliberately does not exist) — the doc must say so."""
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("no LLM", text)
        self.assertNotIn("ZMEM_LINK_LLM=", text,
                         "SKILL.md must not document a ZMEM_LINK_LLM default "
                         "as if the knob existed — it is not shipped")

    def test_entity_extraction_is_deterministic_and_llm_free(self):
        """The extractor is deterministic by design and ships no LLM path —
        the doc must say so (an operator must not wait on a model download
        for entity identity to work)."""
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("deterministic", text)
        self.assertIn("no LLM", text)

    # -- issue #64 (v12): feedback, the promote ladder, eval, tune-weights --

    def test_feedback_command_documented(self):
        """Issue #64, 9.4: an agent reading only SKILL.md can submit feedback
        and knows the counters are CLI-only."""
        text = SKILL_MD.read_text(encoding="utf-8")
        for needle in ("### feedback", "--applied", "--violated",
                       "applied_count", "violated_count",
                       "trust_score", "0 success; 1 target missing",
                       "Hermes prefetch never advance them"):
            self.assertIn(needle, text,
                          f"SKILL.md must document {needle} (issue #64 surface)")

    def test_promote_ladder_documented(self):
        """Issue #64, 9.4: the exact ladder thresholds must be documented and
        the doc must state hooks never auto-promote."""
        text = SKILL_MD.read_text(encoding="utf-8")
        for needle in ("applied_count >= 3", "violated_count == 0",
                       "TRUST_VIOLATION_FLOOR_DROP", "0.15",
                       "nothing auto-promotes"):
            self.assertIn(needle, text,
                          f"SKILL.md must document {needle} (promote ladder)")

    def test_eval_runner_documented(self):
        """Issue #64, 9.1/9.2/9.3: an agent reading only SKILL.md can run the
        eval runner against an isolated store and know the adapters skip
        cleanly."""
        text = SKILL_MD.read_text(encoding="utf-8")
        for needle in ("scripts/eval_runner.py", "--store", "eval/gold.jsonl",
                       "hit_at_k", "mrr", "as_of_accuracy",
                       "injection_omit_rate", "eval_adapters.py",
                       "skipped: ...", "ZMEM_EMBED_PROFILE=fake",
                       "ZMEM_TEST_NOW"):
            self.assertIn(needle, text,
                          f"SKILL.md must document {needle} (eval surface)")

    def test_tune_weights_documented(self):
        """Issue #64, 9.6: the dry-run-only contract and the manual-edit
        application path must be documented."""
        text = SKILL_MD.read_text(encoding="utf-8")
        for needle in ("tune-weights --dry-run", "summing to 1.0",
                       "MANUAL edit", "no `--apply`"):
            self.assertIn(needle, text,
                          f"SKILL.md must document {needle} (tune-weights)")


class CloseoutDocDriftTest(unittest.TestCase):
    """Issue #58, 3.7: closeout must reflect that the vec0 KNN footgun
    is mitigated (still over-fetch), not unmitigated."""

    def test_closeout_knn_wording_is_mitigated(self):
        text = (REPO_ROOT / "skills" / "closeout" / "SKILL.md").read_text(encoding="utf-8")
        # The "vec0 KNN is namespace-blind" phrase is acceptable as long as
        # it is paired with "mitigated" or "still over-fetch". If a future
        # edit strips the qualifier, this ratchet fires.
        if "vec0 KNN is namespace-blind" in text:
            self.assertTrue(
                "mitigated" in text or "still over-fetch" in text,
                "closeout SKILL.md says 'vec0 KNN is namespace-blind' "
                "without 'mitigated' or 'still over-fetch' — the footgun "
                "is mitigated, not unmitigated (issue #58 3.7).",
            )


class DoctorChecksTest(unittest.TestCase):
    """Issue #58, 3.7: doctor must include the new hybrid-default and
    vec-ns-overfetch checks. Run doctor on a tmp store and assert."""

    def test_doctor_reports_hybrid_default_and_vec_ns_overfetch(self):
        import json
        import os
        import sys
        import tempfile

        scripts_dir = REPO_ROOT / "skills" / "memory" / "scripts"
        sys.path.insert(0, str(scripts_dir))
        try:
            import doctor  # noqa: F401
        except Exception:
            self.skipTest("doctor not importable in this environment")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_store = Path(tmp) / "store.sqlite"
            env = {**os.environ, "ZMEM_STORE": str(tmp_store)}
            # doctor exits 1 whenever ANY check is fail-level (e.g.
            # store-access on the nonexistent tmp store, node/bash
            # absence on a bare CI runner) — the exit code is about the
            # OVERALL report, not about our two checks. Accept 0 or 1;
            # the assertion is on the JSON check ids.
            r = subprocess.run(
                [sys.executable, str(scripts_dir / "doctor.py"), "--format", "json"],
                capture_output=True, text=True, env=env,
            )
            self.assertIn(
                r.returncode, (0, 1),
                f"doctor.py exited {r.returncode} (unexpected; stderr: "
                f"{r.stderr[:400]})",
            )
            self.assertTrue(
                r.stdout.strip(),
                f"doctor.py printed no JSON (stderr: {r.stderr[:400]})",
            )
            report = json.loads(r.stdout)
            checks_by_id = {c["id"]: c for c in report["checks"]}
            self.assertIn(
                "hybrid-default", checks_by_id,
                "doctor must report hybrid-default (issue #58 3.7)",
            )
            self.assertIn(
                "vec-ns-overfetch", checks_by_id,
                "doctor must report vec-ns-overfetch (issue #58 3.7)",
            )
            # PRR-001R regression pin: the check must report a REAL
            # availability status — never the "probe failed: NameError"
            # warn the pre-fix module-level-reference bug produced.
            hd = checks_by_id["hybrid-default"]
            self.assertIn(hd["status"], ("pass", "info"))
            self.assertNotIn(
                "NameError", hd["summary"],
                "hybrid-default must probe embeddings, not NameError into "
                "the except branch (PRR-001R)",
            )
            self.assertIn(
                "embeddings.available=", hd["summary"],
                "hybrid-default summary must state the availability verdict",
            )


class PlanDocDriftTest(unittest.TestCase):

    def test_stale_storedirectory_phrase_is_gone(self):
        text = PLAN_MD.read_text(encoding="utf-8")
        self.assertNotIn(
            "aren't wired to `ZMEM_DATA` at runtime", text,
            "PLAN.md must not carry the stale claim that userConfig values "
            "are unwired (#38 I6): zmem-launch.js consumes "
            "CLAUDE_PLUGIN_OPTION_STOREDIRECTORY. Keep the superseded note "
            "paraphrased, never verbatim.")


class CloudDocDriftTest(unittest.TestCase):
    """docs/CLOUD.md is the Tier 1 contract that `export-pack --help` points
    at; before 0.8.7 it still taught the pre-#37-L1 false claim that
    structural text was exempt from the --max-bytes budget (final-critic
    finding on this very PR — the help was corrected but the doc it
    references was not)."""

    def test_cloud_md_states_the_whole_pack_budget(self):
        text = (REPO_ROOT / "docs" / "CLOUD.md").read_text(encoding="utf-8")
        self.assertIn("whole rendered pack", text,
                      "CLOUD.md must state that --max-bytes budgets the whole "
                      "rendered pack, matching _render_pack and --help")
        self.assertNotIn(
            "is exempt", text,
            "CLOUD.md must not claim structural text is exempt from the "
            "--max-bytes budget — _render_pack counts framing toward the cap; "
            "only post-walk framing (empty-section heading + omitted-count "
            "note) can exceed it")


class TrackedFileHygieneTest(unittest.TestCase):

    def test_no_tracked_file_claims_python_3_8_floor(self):
        offenders = [
            rel for rel in _tracked_files()
            if FLOOR_CLAIM_RE.search(_read(rel))
        ]
        self.assertEqual(
            [], offenders,
            "tracked files still claim a 3.8-era Python floor; the supported "
            "floor is 3.11 (README, SKILL.md, doctor, script docstrings)")

    def test_no_tracked_file_embeds_an_absolute_home_path(self):
        offenders = [
            f"{rel}:{m.start()}" for rel in _tracked_files()
            for m in HOME_PATH_RE.finditer(_read(rel))
        ]
        self.assertEqual(
            [], offenders,
            "tracked files embed absolute user home paths — replace with a "
            "placeholder (angle-bracket segment) or an env-var form and "
            "untrack generated caches")

    def test_home_path_ratchet_does_not_self_match(self):
        # If this file itself matched the scan, test_no_tracked_file_embeds_
        # an_absolute_home_path would fail confusingly on its own source.
        self.assertIsNone(
            HOME_PATH_RE.search(Path(__file__).read_text(encoding="utf-8")),
            "tests/test_doc_drift.py itself must not contain a matchable "
            "home-path literal — build fixtures by concatenation")

    def test_floor_claim_ratchet_does_not_self_match(self):
        self.assertIsNone(
            FLOOR_CLAIM_RE.search(Path(__file__).read_text(encoding="utf-8")),
            "tests/test_doc_drift.py itself must not contain a matchable "
            "floor-claim literal — build fixtures by concatenation")

    def test_home_path_regex_positive_and_negative_controls(self):
        # Positive control (fixture assembled from parts, so the literal never
        # appears in this source): a real-shaped home path MUST match.
        sample = "C:" + _BS + "Users" + _BS + "someoperator" + _BS + ".zmem"
        self.assertIsNotNone(HOME_PATH_RE.search(sample))
        posix = "/home/" + "someoperator"
        self.assertIsNotNone(HOME_PATH_RE.search(posix))
        # Negative controls: sanctioned placeholder forms must NOT match.
        placeholder = "C:" + _BS + "Users" + _BS + "<user>" + _BS + ".zmem"
        self.assertIsNone(HOME_PATH_RE.search(placeholder))
        ellipsis = "C:" + _BS + "Users" + _BS + "..." + _BS + "plugins"
        self.assertIsNone(HOME_PATH_RE.search(ellipsis))

    def test_floor_claim_regex_positive_and_negative_controls(self):
        # Positive controls (assembled from parts): the realistic regression
        # phrasings MUST match.
        self.assertIsNotNone(FLOOR_CLAIM_RE.search(_PY + " 3.8 or newer"))
        self.assertIsNotNone(FLOOR_CLAIM_RE.search("requires " + _PY + " 3.8"))
        self.assertIsNotNone(FLOOR_CLAIM_RE.search("floor: 3.8" + "+"))
        self.assertIsNotNone(FLOOR_CLAIM_RE.search("needs >=" + " 3.8 today"))
        # Negative controls: legitimate range/history text must NOT match.
        self.assertIsNone(FLOOR_CLAIM_RE.search("hard-fail below 3.8"))
        self.assertIsNone(FLOOR_CLAIM_RE.search("silent pass on 3.8–3.10"))
        self.assertIsNone(FLOOR_CLAIM_RE.search("3.8-era " + _PY + " floor"))


class Issue82DocDriftTest(unittest.TestCase):
    """Issue #82 documentation pins: the retrieval debugger, the unfold
    contract, the honesty buckets, the self-corpus script, and the audited
    claims surface."""

    def test_claims_audit_exists_with_needles(self):
        path = REPO_ROOT / "docs" / "CLAIMS-AUDIT.md"
        self.assertTrue(path.is_file(),
                        "docs/CLAIMS-AUDIT.md must exist (issue #82)")
        text = path.read_text(encoding="utf-8")
        self.assertIn("aspirational", text,
                      "the audit must mark non-claims explicitly")
        self.assertIn("eval_runner.py", text,
                      "the audit must map eval claims to the runner")
        self.assertIn("never `add` rows",
                      text,
                      "the audit must carry the QUALIFIED hooks-write claim")

    def test_skill_md_documents_explain_and_unfold(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        for needle in ("--explain", "--target", "--no-unfold",
                       "EXPLAIN_REASONS", "vec_lane_miss",
                       "omitted_injection", "explain_unavailable",
                       "[PREVIOUSLY]", "unfold_of", "unfold_hop",
                       "ZMEM_UNFOLD_TOP_K", "ZMEM_UNFOLD_MAX_HOPS",
                       "ZMEM_UNFOLD_BUDGET", "change-intent"):
            self.assertIn(needle, text, f"SKILL.md missing {needle!r}")

    def test_skill_md_documents_honesty_buckets_and_self_corpus(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        for needle in ("retraction", "polarity", "explicit",
                       "eval_self_corpus.py", "store.py backup"):
            self.assertIn(needle, text, f"SKILL.md missing {needle!r}")

    def test_skill_md_qualifies_hooks_write_claim(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        # The overstated absolute is gone...
        self.assertNotIn(
            "hooks never write the store",
            text.replace("\n", " "),
            "the absolute 'hooks never write the store' phrasing must stay "
            "qualified (session-cadence maintenance legitimately writes)")
        # ...and the accurate claim + its one sanctioned exception are stated.
        self.assertIn("never `add` rows", text)
        self.assertIn("session-cadence", text)


class Issue87DocDriftTest(unittest.TestCase):
    """Issue #87 / #85 direction 1: the silent-inject reason split must be
    documented with exact literals so operators can diagnose an empty pool
    vs a below-bar rejection vs a budget wipe from the message alone."""

    def test_skill_md_documents_silent_reasons(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        for needle in ("reason=empty-pool", "reason=omitted",
                       "reason=below-bar", "reason=budget-drop",
                       "reason=injected", "INJECT_SILENT_REASONS",
                       "no durable memories retrieved for this prompt.",
                       "no durable memories met the inject bar."):
            self.assertIn(needle, text, f"SKILL.md missing {needle!r}")

    def test_skill_md_documents_bg_log_reason_field(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("zmem-bg.log", text)
        self.assertIn("reason=", text,
                      "SKILL.md must say zmem-bg.log carries reason=")


if __name__ == "__main__":
    unittest.main(verbosity=2)
