"""Issue #472 supplementary guards: --no-dedup escape hatch, the embedding
COLLISION matrix (normalized-content equality, never similarity), the
update-fold knob's strict parsing, and remote-mode refusal.

The frozen acceptance contract lives in tests/test_update_fold_disclosure.py
(C1/C2). This file pins the surrounding guardrails the plan-critic ladder
required:

- papa/romeo collision (plan-critic r2, executed): distinct strings share a
  16-dim feature-hash embedding under the fake profile, so similarity=1.000
  does NOT mean identical content. Exactness must be decided by normalized
  content equality:
  * UPDATE at the default gate: the colliding update LANDS as its own
    searchable row and never mutates the keeper.
  * UPDATE with the knob lowered below 1.0: the fold fires with the
    NON-exact content-drop disclosure + a --json warning (never the
    identical-text lane).
  * ADD: the colliding add gets the near-dup drop disclosure, never the
    "already contains this exact content" claim.
- T>1.0 (valid, exact-only): exact content still folds; non-exact lands.
- ZMEM_DEDUP_THRESHOLD above the knob: the detector never returns the
  in-between candidate — no fold.
- Knob parsing: garbage/empty/nan/inf/zero/negative refuse the update
  (exit 2) with NOTHING written and unrelated commands unaffected; >1.0 is
  valid exact-only.
- Local --no-dedup bypass: the update lands its own row at similarity 1.000.
- Remote mode: an explicit --no-dedup REFUSES (exit 2) naming the flag,
  queues no op, and creates no local store (the queued-op payload cannot
  carry the bypass).
- State-keeper regression (plan-critic r1 BLOCKER 1): an in-band state
  update (knob lowered) leaves the keeper's memory_expiry byte-identical
  while the replacement row receives the resolved pair.

Drives the REAL skills/memory/scripts/store.py CLI via subprocess against
hermetic throwaway stores (fake embed profile).

Run: python -X utf8 tests/test_update_fold_no_dedup.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable

# Fake-profile collision pair (plan-critic r2 probe; verified locally):
# distinct 5-letter words whose 16-dim feature-hash embeddings collide, so
# the detector reports similarity=1.000 for DIFFERENT content.
KEEPER_WORD = "papa"
OTHER_WORD = "romeo"

NS = "project:fold472supp"


def _env(tmp: str, **extra: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env.pop("ZMEM_DATA", None)
    # PR #485 broad-review r2 (F-08/TI-2): the store-resolution precedence
    # also honors the plugin-data dirs, so a host that exports them must not
    # leak into the sandboxed child.
    env.pop("CLAUDE_PLUGIN_DATA", None)
    env.pop("ZCODE_PLUGIN_DATA", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["ZMEM_EMBED_PROFILE"] = "fake"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("ZMEM_DEDUP_THRESHOLD", None)
    env.pop("ZMEM_REMOTE_URL", None)
    env.pop("ZMEM_UPDATE_FOLD_THRESHOLD", None)
    env.update({k: v for k, v in extra.items() if v is not None})
    for k, v in extra.items():
        if v is None:
            env.pop(k, None)
    return env


class _CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-472supp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, *args, env=None):
        return subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            env=env or _env(self.tmp), capture_output=True, text=True,
            timeout=180, cwd=str(REPO_ROOT))

    def _add(self, content, ns=NS, env=None):
        r = self._run("add", "--namespace", ns, "--type", "fact",
                      "--content", content, "--signal", "test", env=env)
        assert r.returncode == 0, (r.stdout, r.stderr)
        import re
        m = re.search(r"\[zmem\] added memory ([0-9a-f-]{36})", r.stdout)
        assert m, r.stdout
        return m.group(1), r

    def _list(self, ns=NS, env=None):
        r = self._run("list", "--json", "--namespace", ns, env=env)
        assert r.returncode == 0, (r.stdout, r.stderr)
        return json.loads(r.stdout).get("memories", [])

    def _search(self, text, ns=NS, env=None):
        r = self._run("search", "--text", text, "--json",
                      "--namespace", ns, env=env)
        assert r.returncode == 0, (r.stdout, r.stderr)
        return json.loads(r.stdout)


class CollisionMatrixTest(_CliCase):
    """similarity=1.000 with DIFFERENT content: exactness must come from
    normalized content equality, never similarity."""

    def _setup(self, **knob):
        keeper, _ = self._add(KEEPER_WORD, env=_env(self.tmp, **knob))
        victim, _ = self._add("unrelated third row for the update victim",
                              env=_env(self.tmp, **knob))
        return keeper, victim

    def test_collision_update_at_default_lands_own_row(self):
        keeper, victim = self._setup()
        r = self._run("update", "--id", victim, "--namespace", NS,
                      "--type", "fact", "--content", OTHER_WORD)
        self.assertEqual(r.returncode, 0, r.stderr)
        combined = r.stdout + r.stderr
        self.assertIn("update fold skipped", combined, combined)
        self.assertNotIn("identical text", combined, combined)
        # The colliding content LANDED — searchable on its own row.
        found = self._search(OTHER_WORD)
        self.assertTrue(
            any(row.get("content") == OTHER_WORD
                for row in found.get("results") or []),
            f"collision content must land as its own searchable row; "
            f"search: {json.dumps(found)[:400]}")
        # The keeper is untouched.
        rows = {row["id"]: row for row in self._list()}
        self.assertIsNone(rows[keeper].get("superseded_at"))
        self.assertEqual(rows[keeper].get("preview"), KEEPER_WORD)

    def test_collision_update_with_lowered_knob_discloses_drop(self):
        keeper, victim = self._setup(ZMEM_UPDATE_FOLD_THRESHOLD="0.95")
        rj = self._run("update", "--json", "--id", victim, "--namespace", NS,
                       "--type", "fact", "--content", OTHER_WORD,
                       env=_env(self.tmp,
                                ZMEM_UPDATE_FOLD_THRESHOLD="0.95"))
        self.assertEqual(rj.returncode, 0, rj.stderr)
        payload = json.loads(rj.stdout.strip().splitlines()[-1])
        self.assertEqual(payload.get("id"), keeper)
        self.assertFalse(payload.get("created_new"))
        warnings = payload.get("warnings") or []
        self.assertTrue(
            any(keeper in json.dumps(w) and "not" in json.dumps(w)
                and "stored" in json.dumps(w) for w in warnings),
            f"a collision fold must warn with the drop disposition "
            f"(keeper + not-stored); payload: {json.dumps(payload)}")
        # The disclosure on stderr (human lane under --json) must use the
        # NON-exact lane — never the identical-text claim.
        self.assertNotIn("identical text", rj.stderr, rj.stderr)
        self.assertIn("was NOT stored", rj.stderr, rj.stderr)

    def test_collision_add_never_claims_exact_content(self):
        _keeper, _ = self._add(KEEPER_WORD)
        # NOTE: a folding add prints no "[zmem] added memory" line, so this
        # leg drives the CLI directly instead of via _add.
        r = self._run("add", "--namespace", NS, "--type", "fact",
                      "--content", OTHER_WORD, "--signal", "test")
        self.assertEqual(r.returncode, 0, r.stderr)
        combined = r.stdout + r.stderr
        self.assertIn("was NOT stored", combined, combined)
        self.assertNotIn("already contains this exact content", combined,
                         combined)
        found = self._search(OTHER_WORD)
        self.assertEqual(found.get("count"), 0)


class ThresholdBoundaryTest(_CliCase):
    def test_exact_content_still_folds_above_one(self):
        keeper, _ = self._add("alpha beta exact gate content")
        victim, _ = self._add("unrelated row to be updated now")
        r = self._run("update", "--id", victim, "--namespace", NS,
                      "--type", "fact", "--content",
                      "alpha beta exact gate content",
                      env=_env(self.tmp,
                               ZMEM_UPDATE_FOLD_THRESHOLD="1.1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("equivalent text (normalized-equal) is already stored in that row",
                      r.stdout, r.stdout)
        rows = {row["id"]: row for row in self._list()}
        self.assertIsNone(rows[keeper].get("superseded_at"))

    def test_non_exact_lands_above_one(self):
        _keeper, _ = self._add("alpha beta exact gate content")
        victim, _ = self._add("unrelated row to be updated now")
        new_text = "alpha beta exact gate contents modified tail"
        r = self._run("update", "--id", victim, "--namespace", NS,
                      "--type", "fact", "--content", new_text,
                      env=_env(self.tmp,
                               ZMEM_UPDATE_FOLD_THRESHOLD="1.1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        # Landed as its own searchable row (no fold either way — the
        # expectation is landing, whatever the detector returned).
        found = self._search("modified tail",
                             env=_env(self.tmp,
                                      ZMEM_UPDATE_FOLD_THRESHOLD="1.1"))
        self.assertTrue(
            any(new_text in (row.get("content") or "")
                for row in found.get("results") or []),
            f"non-exact content must land as its own row; "
            f"search: {json.dumps(found)[:400]}")

    def test_detector_floor_above_knob_means_no_fold(self):
        # Knob 0.90, dedup floor 0.98: a candidate whose similarity (0.934
        # under the fake profile, MEASURED via fake_embed cosine) clears the
        # knob but not the detector's admission floor never becomes a dedup
        # candidate at all — the update lands. The discriminating seam is the
        # DETECTOR FLOOR (write.py's `similarity >= threshold` filter): a
        # mutant that drops the floor check admits this candidate and the
        # 0.90 knob then folds it, turning this test RED; deleting the
        # update-fold gate alone cannot (the detector still filters first).
        # A companion control leg (floor 0.90, same fixture) folds at
        # similarity=0.934, proving the fixture CAN fold and the no-fold
        # outcome is attributable to the floor alone. (Broad-review r2 F-08:
        # the old fixture measured 0.623, below BOTH thresholds, so no
        # mutation of either seam could turn it red.)
        _keeper, _ = self._add(
            "shared prefix tokens for the detector floor probe row one")
        victim, _ = self._add("totally different victim row content here")
        new_text = ("shared prefix tokens for the detector floor probe "
                    "row one tail")
        r = self._run("update", "--id", victim, "--namespace", NS,
                      "--type", "fact", "--content", new_text,
                      env=_env(self.tmp, ZMEM_UPDATE_FOLD_THRESHOLD="0.90",
                               ZMEM_DEDUP_THRESHOLD="0.98"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("FOLDED into existing live memory",
                         r.stdout + r.stderr, r.stdout + r.stderr)
        found = self._search(
            "row one tail",
            env=_env(self.tmp, ZMEM_UPDATE_FOLD_THRESHOLD="0.90",
                     ZMEM_DEDUP_THRESHOLD="0.98"))
        self.assertTrue(
            any(new_text in (row.get("content") or "")
                for row in found.get("results") or []),
            f"the detector-floor-blocked update must land; "
            f"search: {json.dumps(found)[:400]}")


class ExactFoldPolarityGuardTest(_CliCase):
    """Stage-B reviewer counterexample (broad-review r2 R1 follow-up): the
    polarity signature strips only NEWLINE-FREE quoted spans
    (consolidate.py `r'"[^"\\n]*"'`) while normalize_content collapses
    whitespace, so `"not\\nx"` and `"not x"` are NORMALIZED-EQUAL yet
    polarity-CONFLICTING. The exact fold must therefore run the polarity
    guard on the final selected keeper — an exact-content update whose
    keeper disagrees on polarity lands as its own row (the fold declines),
    never merging a contradiction into the keeper."""

    def test_exact_content_with_newline_quote_conflict_lands(self):
        # Real newline in the keeper content (not a literal backslash-n) so
        # normalize_content collapses it and the update below is
        # content_norm-equal to the keeper through the EXACT lane
        # deterministically — while the polarity signature strips the
        # quoted span only from the newline-free update text.
        keeper, _ = self._add('"not\nx" is the guard probe', ns=NS)
        victim, _ = self._add("unrelated victim row for the polarity leg",
                              ns=NS)
        r = self._run("update", "--id", victim, "--namespace", NS,
                      "--type", "fact", "--content", '"not x" is the guard probe')
        self.assertEqual(r.returncode, 0, r.stderr)
        combined = r.stdout + r.stderr
        # The fold must have been DECLINED (polarity disagreement with the
        # keeper despite normalized-equal content) and the update landed as
        # its own NEW live row (append-only: the victim is tombstoned, the
        # replacement row is live, and the keeper was never merged into).
        self.assertIn("polarity disagreement", combined, combined)
        rows = {row["id"]: row for row in self._list(ns=NS)}
        self.assertIsNone(
            rows[keeper].get("superseded_at"),
            "the keeper must stay live and untouched by a conflicting "
            f"update; rows={json.dumps(rows)[:400]}")
        landed = [row for row in rows.values()
                  if row["id"] != keeper and row.get("superseded_at") is None]
        self.assertEqual(
            len(landed), 1,
            f"exactly ONE new live row (the landed update) expected; "
            f"rows={json.dumps(rows)[:400]}")
        self.assertNotIn("folded", combined.lower(), combined)


class KnobParsingTest(_CliCase):
    def _refusals(self):
        for i, bad in enumerate(("garbage", "", "nan", "inf", "0", "-0.5")):
            with self.subTest(bad=bad):
                # Setup adds use --no-dedup so near-identical victim rows
                # never fold during SETUP (a folded add prints no added-id).
                r = self._run("add", "--namespace", NS, "--type", "fact",
                              "--content",
                              f"victim row {i} for knob parsing leg {bad!r}",
                              "--signal", "test", "--no-dedup")
                self.assertEqual(r.returncode, 0, r.stderr)
                import re
                m = re.search(r"\[zmem\] added memory ([0-9a-f-]{36})",
                              r.stdout)
                self.assertIsNotNone(m, r.stdout)
                victim = m.group(1)
                r = self._run("update", "--id", victim, "--namespace", NS,
                              "--type", "fact", "--content",
                              "content that must never be written " + repr(bad),
                              env=_env(self.tmp,
                                       ZMEM_UPDATE_FOLD_THRESHOLD=bad))
                self.assertEqual(r.returncode, 2, (r.stdout, r.stderr))
                self.assertIn("ZMEM_UPDATE_FOLD_THRESHOLD", r.stderr, r.stderr)
                # NOTHING written: the victim is still live.
                rows = {row["id"]: row for row in self._list()}
                self.assertIsNone(rows[victim].get("superseded_at"))
        # Unrelated commands unaffected by the ambient garbage knob.
        r = self._run("list", "--json", "--namespace", NS,
                      env=_env(self.tmp,
                               ZMEM_UPDATE_FOLD_THRESHOLD="garbage"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_refusals(self):
        self._refusals()


class NoDedupBypassTest(_CliCase):
    def test_local_no_dedup_lands_own_row_at_similarity_one(self):
        keeper, _ = self._add("bypass probe identical content text")
        victim, _ = self._add("unrelated victim for the bypass leg")
        r = self._run("update", "--id", victim, "--namespace", NS,
                      "--type", "fact", "--content",
                      "bypass probe identical content text", "--no-dedup")
        self.assertEqual(r.returncode, 0, r.stderr)
        # The update target is superseded by design (append-only update) --
        # --no-dedup guarantees the REPLACEMENT landed as its own row
        # instead of folding into the keeper.
        rows = {row["id"]: row for row in self._list()}
        self.assertIn(keeper, rows)
        self.assertIsNone(rows[keeper].get("superseded_at"),
                          "the keeper must not be the fold target")
        live = [r_ for r_ in rows.values()
                if r_.get("preview") == "bypass probe identical content text"]
        self.assertEqual(len(live), 2,
                         "both the keeper and the bypassed replacement must "
                         f"live; rows: {json.dumps(list(rows.values()))[:400]}")


class RemoteRefusalTest(_CliCase):
    def test_remote_no_dedup_refuses_loudly(self):
        # Strip ZMEM_STORE/ZMEM_DATA (the config combo refuses BEFORE write
        # flags — the refusal under test is the FLAG's), dead-localhost hub.
        # R5-3: sandbox HOME/USERPROFILE at a throwaway dir so the
        # no-default-store assertion cannot flake on the operator's LIVE
        # home store being touched by concurrent hooks mid-run (the mtime
        # equality form measured the real store; this form measures a store
        # that must never exist).
        sandbox_home = Path(tempfile.mkdtemp(prefix="zmem-472remote-"))
        self.addCleanup(shutil.rmtree, sandbox_home, True)
        env = {**os.environ}
        env.pop("ZMEM_STORE", None)
        env.pop("ZMEM_DATA", None)
        # Broad-review r2 F-08: the plugin-data dirs outrank home in
        # host.resolve_store_path — scrub them too, so the no-default-store
        # assertion measures every store-resolution limb the child could use.
        env.pop("CLAUDE_PLUGIN_DATA", None)
        env.pop("ZCODE_PLUGIN_DATA", None)
        env["HOME"] = str(sandbox_home)
        env["USERPROFILE"] = str(sandbox_home)
        env["ZMEM_REMOTE_URL"] = "http://127.0.0.1:9/zmem"
        env["ZMEM_MCP_TOKEN"] = "test-token-local-only"
        env["PYTHONIOENCODING"] = "utf-8"
        store_default = sandbox_home / ".zmem" / "store.sqlite"
        mtime_before = store_default.exists()
        r = subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), "update", "--id",
             "00000000-0000-0000-0000-000000000000", "--namespace", NS,
             "--type", "fact", "--content", "remote refusal probe",
             "--no-dedup"],
            env=env, capture_output=True, text=True, timeout=180,
            cwd=str(REPO_ROOT))
        self.assertEqual(r.returncode, 2, (r.stdout, r.stderr))
        self.assertIn("--no-dedup", r.stderr, r.stderr)
        self.assertIn("not supported in remote mode", r.stderr, r.stderr)
        # The refusal created no local store and did not touch an existing
        # one (before/after mtime — an operator box carries a real default
        # store that exists regardless of this run).
        self.assertFalse(store_default.exists(),
                         "the remote refusal must not create any local "
                         "store under the sandboxed HOME")


class StateKeeperRegressionTest(_CliCase):
    def test_in_band_state_update_leaves_keeper_expiry_untouched(self):
        env = _env(self.tmp, ZMEM_UPDATE_FOLD_THRESHOLD="0.95")
        r = self._run("add", "--namespace", NS, "--type", "state",
                      "--content", "state keeper for the fold gate probe",
                      "--expiry-class", "endpoint", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = self._list(env=env)
        keeper = next(row["id"] for row in rows
                      if "state keeper" in (row.get("preview") or ""))
        conn = sqlite3.connect(os.path.join(self.tmp, "store.sqlite"))
        conn.row_factory = sqlite3.Row
        before = dict(conn.execute(
            "SELECT inject_until, expiry_class FROM memory_expiry "
            "WHERE memory_id=?", (keeper,)).fetchone() or {})
        conn.close()
        victim, _ = self._add("plain victim row for the state leg", env=env)
        r = self._run("update", "--id", victim, "--namespace", NS,
                      "--type", "state", "--content",
                      "state keeper for the fold gate probe amended tail",
                      "--expiry-class", "metric", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("update fold skipped", r.stdout + r.stderr,
                      r.stdout + r.stderr)
        conn = sqlite3.connect(os.path.join(self.tmp, "store.sqlite"))
        conn.row_factory = sqlite3.Row
        after = conn.execute(
            "SELECT inject_until, expiry_class FROM memory_expiry "
            "WHERE memory_id=?", (keeper,)).fetchone()
        # Keeper's side row byte-identical (same pair, nothing added).
        self.assertIsNotNone(after, "keeper expiry row must survive")
        self.assertEqual(dict(after), before)
        # The REPLACEMENT row received the resolved pair.
        repl = conn.execute(
            "SELECT memory_id FROM memory_expiry WHERE expiry_class=?",
            ("metric",)).fetchall()
        self.assertTrue(repl, "replacement row must carry the resolved pair")
        self.assertNotEqual(repl[0]["memory_id"], keeper)
        conn.close()




class TopOneBlindspotRegressionTest(_CliCase):
    """PR #485 review round (swarm-pr-review explorers + reviewer, both
    probe-verified): the fold gate's exactness must be decided against ANY
    live row (content_norm re-rank), never only the detector's top-ranked
    candidate — a colliding row outranking the true exact keeper used to
    (a) refuse an exact update its fold (duplicate live rows) and (b) make
    the add path claim 'not stored anywhere' while the identical text WAS
    live."""

    def _setup_collision_beside_keeper(self):
        keeper, _ = self._add(KEEPER_WORD)
        # The colliding row must EXIST as a separate live row (--no-dedup
        # add) so it can outrank the exact keeper in the KNN ranking.
        r = self._run("add", "--namespace", NS, "--type", "fact",
                      "--content", OTHER_WORD, "--signal", "test",
                      "--no-dedup")
        self.assertEqual(r.returncode, 0, r.stderr)
        victim, _ = self._add("unrelated victim row for the blindspot leg")
        return keeper, victim

    def test_exact_update_folds_despite_colliding_outranker(self):
        keeper, victim = self._setup_collision_beside_keeper()
        r = self._run("update", "--id", victim, "--namespace", NS,
                      "--type", "fact", "--content", KEEPER_WORD)
        self.assertEqual(r.returncode, 0, r.stderr)
        combined = r.stdout + r.stderr
        self.assertIn("dedup re-ranked", combined, combined)
        self.assertNotIn("update fold skipped", combined, combined)
        # EXACTLY ONE live row carries the keeper word — no duplicate.
        live = [row for row in self._list()
                if (row.get("preview") or "").startswith(KEEPER_WORD)]
        self.assertEqual(len(live), 1,
                         f"an exact-content update must fold, not duplicate; "
                         f"live rows: {json.dumps(live)[:400]}")
        self.assertEqual(live[0]["id"], keeper)

    def test_add_behind_colliding_outranker_never_claims_not_stored(self):
        _keeper, _victim = self._setup_collision_beside_keeper()
        r = self._run("add", "--namespace", NS, "--type", "fact",
                      "--content", KEEPER_WORD, "--signal", "test")
        self.assertEqual(r.returncode, 0, r.stderr)
        combined = r.stdout + r.stderr
        self.assertIn("dedup re-ranked", combined, combined)
        self.assertNotIn("not stored anywhere", combined,
                         "the add path must never claim the content is not "
                         "stored while an identical live row exists;"
                         f" output: {combined[-400:]}")

    def test_exact_lane_json_warning_pinned(self):
        """PR #485 review (T1): the EXACT lane's --json warnings entry was
        unpinned — deleting its warns.append passed every test."""
        keeper, victim = self._setup_collision_beside_keeper()
        rj = self._run("update", "--json", "--id", victim, "--namespace", NS,
                       "--type", "fact", "--content", KEEPER_WORD)
        self.assertEqual(rj.returncode, 0, rj.stderr)
        payload = json.loads(rj.stdout.strip().splitlines()[-1])
        self.assertEqual(payload.get("id"), keeper)
        self.assertFalse(payload.get("created_new"))
        warnings = payload.get("warnings") or []
        self.assertTrue(
            any("normalized-equal" in json.dumps(w)
                and "nothing new was stored" in json.dumps(w)
                for w in warnings),
            f"the exact-lane --json warnings entry must be pinned; "
            f"payload: {json.dumps(payload)}")


class OrganizeKnobRefusalTest(_CliCase):
    """PR #485 review (I2, rebuilt per broad-review r2 R5/F-01): a malformed
    ambient ZMEM_UPDATE_FOLD_THRESHOLD must refuse `store.py organize` with
    a clean exit 2. The rebuilt test FORCES the refusal path: the knob is
    now validated at organize() entry (before any stage writes), so ANY
    non-dry-run organize with a garbage knob exits 2 — no fixture-shape
    dependence on reaching update_memory, and the rc==2 assertion is
    unconditional (the old version passed vacuously when its fixture never
    reached the compression stage; deleting the CLI handler kept it green)."""

    def test_organize_refuses_cleanly_on_garbage_knob(self):
        # The knob parse is env-side and needs no store state at all, but
        # seeding two rows makes the NOTHING-COMMITTED oracle discriminating:
        # a mutant that moves the pre-flight AFTER consolidation (final-critic
        # round-5 mutant, executed) still exits 2 but arms the cadence clock —
        # caught by the meta.last_consolidation assertion below.
        long_a = ("alpha beta gamma delta epsilon zeta eta theta iota kappa "
                  "lambda mu nu xi omicron pi rho sigma tau upsilon phi chi "
                  "psi omega repeated tail one for the organize leg")
        long_b = ("alpha beta gamma delta epsilon zeta eta theta iota kappa "
                  "lambda mu nu xi omicron pi rho sigma tau upsilon phi chi "
                  "psi omega repeated tail two differs here at the end")
        r1 = self._run("add", "--namespace", NS, "--type", "lesson",
                       "--content", long_a, "--signal", "test")
        r2 = self._run("add", "--namespace", NS, "--type", "lesson",
                       "--content", long_b, "--signal", "test")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        r = self._run("organize", "--force",
                      env=_env(self.tmp,
                               ZMEM_UPDATE_FOLD_THRESHOLD="garbage"))
        # UNCONDITIONAL: the pre-flight refusal fires before any stage
        # writes, so exit 2 is guaranteed by the fixture-independent entry
        # check — the old else-branch (rc==0, "refusal path not exercised")
        # is gone by construction.
        self.assertEqual(r.returncode, 2, (r.stdout, r.stderr))
        self.assertIn("ZMEM_UPDATE_FOLD_THRESHOLD", r.stderr, r.stderr)
        self.assertNotIn("Traceback", r.stderr, r.stderr)
        # NOTHING was committed: the cadence clock must NOT be armed by the
        # refused run (an after-consolidation mutant arms it — this is the
        # R6/F-06 ordering oracle), and both seeded rows must still be live.
        import sqlite3 as _sq
        conn = _sq.connect(os.path.join(self.tmp, "store.sqlite"))
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        conn.close()
        self.assertNotIn(
            "last_consolidation", meta,
            f"the refused run must not arm the cadence clock; meta keys: "
            f"{sorted(meta)[:20]}")
        self.assertNotIn(
            "last_consolidation_count", meta,
            f"the refused run must not write consolidation counts; meta "
            f"keys: {sorted(meta)[:20]}")
        rows = self._list()
        live_contents = sorted(
            (row.get("preview") or "")[:40] for row in rows)
        self.assertEqual(
            len(rows), 2,
            f"both seeded rows must still be live on a refused run; "
            f"live={json.dumps(live_contents)[:400]}")

    def test_organize_valid_knob_still_runs(self):
        # The pre-flight validation must not break a healthy organize run.
        r = self._run("organize", "--force",
                      env=_env(self.tmp, ZMEM_UPDATE_FOLD_THRESHOLD="0.95"))
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        r2 = self._run("organize", "--force", env=_env(self.tmp))
        self.assertEqual(r2.returncode, 0, (r2.stdout, r2.stderr))




class UpdateRerankLessonCarveoutTest(_CliCase):
    """Final-critic r5 R5-2 falsifier (r6-corrected shape): an update
    WITHOUT --type whose EXACT keeper sits BEHIND a colliding outranker
    must reach the re-rank and admit the lesson/lesson keeper through the
    #376 carve-out. The raw-type_ form printed a FALSE subject_veto and
    left a duplicate live row; the fixture MUST drive the outranker so the
    re-rank code path (the only _subject_admit call with the type
    argument) actually executes."""

    def test_lesson_carveout_update_folds_without_type_flag(self):
        ns = "project:fold472carve"
        env = _env(self.tmp)
        # Keeper: the short collision word, typed lesson, tagged task:alpha.
        rk = self._run("add", "--namespace", ns, "--type", "lesson",
                       "--content", KEEPER_WORD, "--tags", "task:alpha",
                       "--signal", "test", "--no-dedup", env=env)
        self.assertEqual(rk.returncode, 0, rk.stderr)
        keeper = re.search(r"\[zmem\] added memory ([0-9a-f-]{36})",
                           rk.stdout).group(1)
        # Decoy: the colliding outranker (same embedding bucket).
        rd_ = self._run("add", "--namespace", ns, "--type", "fact",
                        "--content", OTHER_WORD, "--signal", "test",
                        "--no-dedup", env=env)
        self.assertEqual(rd_.returncode, 0, rd_.stderr)
        # Victim: a lesson tagged with a DISJOINT task id.
        rv = self._run("add", "--namespace", ns, "--type", "lesson",
                       "--content", "victim row for the carveout rerank leg",
                       "--tags", "task:beta", "--signal", "test",
                       "--no-dedup", env=env)
        victim = re.search(r"\[zmem\] added memory ([0-9a-f-]{36})",
                           rv.stdout).group(1)
        # Update to the EXACT keeper text with NO --type: type_eff resolves
        # to the victim's 'lesson', the outranker outranks the exact keeper,
        # and the re-rank must fire and admit lesson/lesson.
        r = self._run("update", "--id", victim, "--namespace", ns,
                      "--content", KEEPER_WORD, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        combined = r.stdout + r.stderr
        self.assertIn("dedup re-ranked", combined,
                      "the fixture must DRIVE the re-rank (outranker beside "
                      f"the exact keeper); output: {combined[-400:]}")
        self.assertNotIn("subject_veto", combined,
                         "the lesson/lesson carve-out must admit the exact "
                         f"keeper (type_eff, not raw type_); output: "
                         f"{combined[-400:]}")
        self.assertNotIn("lands as its own live row", combined, combined)
        live = [row for row in self._list(ns=ns, env=env)
                if row["id"] == keeper]
        self.assertTrue(live and live[0].get("superseded_at") is None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
