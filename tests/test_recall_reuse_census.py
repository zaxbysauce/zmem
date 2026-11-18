#!/usr/bin/env python
"""Issue #474 supplementary guardrails (plan 4.2 predicates 1/3/5):

- P1: every ONNX InferenceSession construction site in the production tree
  passes an explicit bounded SessionOptions (or an explicitly-recorded
  unsupported-runtime fallback branch beside it).
- P3: every writer of a recall-read table in the recall path maps to the
  mutation-generation census (bumped) or a written disposition
  (telemetry-only / wholesale-replacement rotation / additive DDL).
- P5 (the fingerprint completeness mechanism, plan R2-3): every ZMEM_* env
  read reachable from the recall path maps to a recall_reuse.CONFIG_KNOBS
  entry or carries an explicit no-effect disposition below — a knob added
  to the recall path without extending the fingerprint fails this census.

Run:  python tests/test_recall_reuse_census.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import storelib.recall_reuse as rr  # noqa: E402

# Env names that are READ on the recall path but provably do not affect
# recall output/selection (each with its reason):
#   ZMEM_TEST_NOW        — test clock injection (test-only; deterministic
#                          offsets, not selection)
#   ZMEM_DATA/ZMEM_STORE — store identity resolution (already a key
#                          component via the resolved store path)
#   ZMEM_MODELS_DIR      — artifact location only; the profile identity is
#                          keyed and a model swap changes the generation
#                          through reembed, not mid-recall selection
#   ZMEM_CROSS_ENCODER_MODEL_URL / ZMEM_CROSS_ENCODER_CE_DEBUG_TRACEBACK /
#   ZMEM_CE_DEBUG_TRACEBACK — download source / debug traceback only
#   ZMEM_FENCE_OPEN/ZMEM_FENCE_CLOSE — render markers... NO: fence width
#      changes output bytes, so they ARE keyed (kept in CONFIG_KNOBS).
NO_EFFECT_DISPOSITIONS = {
    "ZMEM_TEST_NOW": "test clock injection (deterministic offsets)",
    "ZMEM_DATA": "store identity resolution (keyed via store path)",
    "ZMEM_STORE": "store identity resolution (keyed via store path)",
    "ZMEM_MODELS_DIR": "artifact location; profile identity is keyed",
    "ZMEM_CROSS_ENCODER_MODEL_URL": "download source only (autodownload off)",
    "ZMEM_CROSS_ENCODER_CE_DEBUG_TRACEBACK": "debug traceback only",
    "ZMEM_CE_DEBUG_TRACEBACK": "debug traceback only",
    "ZMEM_ORT_INTRA_THREADS": "resource policy, not selection (issue #473)",
    "ZMEM_ORT_INTER_THREADS": "resource policy, not selection (issue #473)",
    "ZMEM_ORT_DISABLE_SPIN": "resource policy, not selection (issue #473)",
    "ZMEM_DEDUP_THRESHOLD": "write-path only",
    "ZMEM_LINK_THRESHOLD": "write-path only",
    "ZMEM_MODEL_AUTODOWNLOAD": "download gate; observation forces it off",
    "ZMEM_UNFOLD_TOP_K_DEFAULT": "constant name fragment, not an env read",
    "ZMEM_ARM_CAP_": "prefix fragment (per-arm loop key), each full name keyed",
    "ZMEM_RECALL_REUSE": "reuse-layer self-control (serving gate)",
    "ZMEM_RECALL_REUSE_OBSERVE": "reuse-layer self-control",
    "ZMEM_RECALL_REUSE_TTL_S": "reuse-layer self-control",
    "ZMEM_RECALL_REUSE_REFRESH": "reuse-layer self-control",
    "ZMEM_SESSION": "session token feeds the keyed session component",
    "ZMEM_SESSION_ID": "session token feeds the keyed session component",
    "ZMEM_MODEL_URL": "download source only (autodownload off on reads)",
    "ZMEM_ENTITY_LLM": "write-path entity classification only",
    "ZMEM_CARD_MIN_MENTIONS": "write-path card minting only",
    "ZMEM_CARD_TOP_N": "write-path card minting only",
    "ZMEM_LINK_LLM": "write-path link classification only",
    "ZMEM_BELIEF_ADAPTER_ACTIONS": "maintenance adapter (write path) only",
}

RECALL_PATH_MODULES = [
    "storelib/recall.py",
    "storelib/recall_reuse.py",
    "storelib/inject.py",
    "storelib/cross_encoder.py",
    "embeddings.py",
    "storelib/generation.py",
    "storelib/entity.py",
    "storelib/links.py",
    "storelib/beliefs.py",
    "storelib/evidence.py",
    "storelib/cross_project.py",
]


class CensusGuardrails(unittest.TestCase):
    def test_p1_every_session_construction_passes_options(self):
        hits = []
        for mod in ("embeddings.py", "storelib/cross_encoder.py"):
            text = (SCRIPTS / mod).read_text(encoding="utf-8")
            for m in re.finditer(r"InferenceSession\(", text):
                line_no = text[:m.start()].count("\n") + 1
                context = text[max(0, m.start() - 300):m.start()]
                if "sess_options=" in text[m.start():m.start() + 200]:
                    continue  # the bounded branch itself
                if "_sess_opts is None" in context or \
                        "_resource_options" in context:
                    continue  # the guarded fallback branch beside it
                hits.append("%s:%d" % (mod, line_no))
        self.assertEqual(hits, [],
                         "unbounded InferenceSession construction sites: "
                         "%s" % hits)

    def test_p3_recall_path_writers_map_to_census(self):
        # recall.py's own memory-table writes: telemetry-only (no bump, the
        # pinned pin) + the reembed apply (bumped).
        text = (SCRIPTS / "storelib/recall.py").read_text(encoding="utf-8")
        write_sites = [
            text[:m.start()].count("\n") + 1
            for m in re.finditer(
                r"(INSERT INTO memory\b|UPDATE memory\b|DELETE FROM memory\b)",
                text)]
        self.assertEqual(len(write_sites), 4,
                         "recall.py memory-write census drift: %s"
                         % write_sites)
        in_telemetry = [ln for ln in write_sites
                        if 2740 <= ln <= 2800]  # _bump_telemetry body
        self.assertEqual(len(in_telemetry), 2,
                         "the telemetry writes must stay inside "
                         "_bump_telemetry (no-bump disposition)")
        in_reembed = [ln for ln in write_sites if ln > 6400]
        self.assertEqual(len(in_reembed), 2,
                         "the reembed-apply writes must sit inside the "
                         "bumped transaction")

    def test_p5_env_reads_covered_by_fingerprint_or_disposition(self):
        knobs = {name for name, _lane in rr.CONFIG_KNOBS}
        knobs |= {rr.HAZARD_ENV}
        uncovered = []
        for mod in RECALL_PATH_MODULES:
            path = SCRIPTS / mod
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"ZMEM_[A-Z_0-9]+", text):
                name = m.group(0)
                if name in knobs or name in NO_EFFECT_DISPOSITIONS:
                    continue
                if name.endswith(("_DEFAULT", "_ENV")):
                    continue  # constant-name fragments, not reads
                if name not in uncovered:
                    uncovered.append(name)
        self.assertEqual(
            uncovered, [],
            "recall-path env reads not in the reuse fingerprint census "
            "and carrying no no-effect disposition — extend "
            "recall_reuse.CONFIG_KNOBS or record a disposition: %s"
            % uncovered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
