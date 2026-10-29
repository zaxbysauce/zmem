"""Acceptance tests for issue #376: subject-aware dedup.

Task-scoped facts with explicitly incompatible task identities must remain
independently addressable when high similarity would otherwise collapse them,
with ONE shared compatibility rule across add / update / ingest / maintenance
consolidation and NO general threshold reduction.

Frozen identity convention (these tests are the contract freeze the issue
demands — authored BEFORE the feature exists, per "Freeze assertions before
feature implementation"):

* A task identity is carried in the row's EXISTING public ``tags`` metadata as
  one or more ``task:<key>`` tokens — the ``tags='task:'+subject`` convention
  used by BOTH pinned preparation drivers at 2da9f44e932fbb7f50b5bc7c556670b1373ae48d
  (research/hy-memory-experiments/run_experiments.py and real_semantic.py,
  read back from local git history; that directory is not a published repo
  artifact). The companion ``source_ref='session:synthetic-<subject>'``
  scheme comes from run_experiments.py's ``add()`` helper only —
  real_semantic.py passed no source_ref — so source_ref is NOT part of the
  frozen identity rule, only a preserved-provenance field the tests pin per
  row. No new column or public field is required or assumed.
* Two rows are SUBJECT-COMPATIBLE when their task-identity sets intersect,
  or when either side carries no usable task identity (unknown-subject stays
  backward-compatibly compatible — an absent key never vetoes). A malformed
  identity token (``task:`` with an empty or whitespace-only key) is AMBIGUOUS
  and is treated as absent, never as a conflict. Arbitrary NON-task tags
  (``host:``, ``agent:``, free words) are never identities.
* Type-scoped carve-out (issue text: "Reusable lessons can generalize across
  different task origins"): ``type=lesson`` rows merge across DIFFERENT task
  identities; only ``type=fact`` (and other task-scoped types) keep the
  distinct-subject separation.
* Two task-scoped rows with explicit, DISJOINT task identities are
  subject-incompatible: dedup-on-write, update-fold, ingest-fold (exact,
  semantic and cached lookups) and consolidate admission must all refuse to
  merge them, must not contaminate the survivor's metadata, and must land the
  incoming statement as its own row. Repository-qualified keys (e.g.
  ``task:zaxbysauce/zmem#368`` vs ``task:othercorp/widget#368``) are
  DISTINCT — no cross-repository collision.
* A different session, source_ref, incidental mentioned file or shared
  entity is NOT proof of subject conflict: same-subject (or untagged)
  duplicates still merge exactly as before. The known below-threshold
  paraphrase control (pinned cosine 0.763633 < 0.85) stays a MISSED
  duplicate — merging it via a global threshold reduction fails AC7.
* Cross-task TAG contamination is frozen as covering EVERY write-path
  channel, not only the dedup fold: after a subject-incompatible add, the
  survivor's tags must still name exactly its own task identities. On the
  base, the v10/v11 link attribute-propagation channel unions the incoming
  task tag onto a subject-incompatible same-content neighbor (observed in
  the AC6 export leg) — that union is cross-task tag contamination under
  AC5's "prevent cross-task tag ... contamination" and fails these tests.
* A subject veto must record a STABLE, PRIVACY-SAFE explanation: the frozen
  machine-readable veto code is ``subject_veto`` (constant below), and no
  veto-explanation surface may echo the vetoed row's content text.
* AC6 frozen OBSERVABILITY SEAM: the implementation MUST expose
  ``storelib.write.SUBJECT_COMPAT_EVALS`` — a process-local int incremented
  once per subject-compatibility evaluation on the dedup/consolidation
  decision paths. The real-model driver reads it via getattr (absent -> 0,
  recorded as seam_absent=true) before/after each measured contested add;
  the contract asserts delta >= 2 (the incompatible NEAREST candidate and
  the compatible runner-up are both evaluated) and size-independence
  delta60 <= delta10 + 16. The KNN call count stays a SECONDARY window
  metric — KNN rows cannot observe compatibility evaluations. At base the
  attribute is absent, so the semantic cost leg is red by absence: the
  discriminating NEW-SURFACE signature.

Fixtures: the six constructed pairs plus the issue-number / task-closeout
examples are the EXACT texts frozen in the pinned preparation artifact
``research/hy-memory-experiments/real_semantic.py`` at
2da9f44e932fbb7f50b5bc7c556670b1373ae48d. Their subjects are ORACLE-SUPPLIED
via tags — they measure the identity policy, not any production subject
extractor (the issue's "disclose oracle/extracted identity differences";
AC7 emits that disclosure in its structured rubric report).

Real-model leg: the pinned MiniLM identity (Xenova/all-MiniLM-L6-v2 ONNX,
384-dim, sha256 bbd7b466...) is probed read-only from (in order)
$ZMEM_TEST_REAL_MODELS_DIR, the plugin's bundled models dir
(skills/memory/models), and the operator's shared model cache ~/.zmem/models
(MODEL FILES ONLY — the canonical store at ~/.zmem/store.sqlite is never
opened; the driver additionally asserts its resolved store equals the
scratch path). Outcomes are distinguished: MODEL_UNAVAILABLE (no verified
model / driver reported unavailability) fails the leg as INCOMPLETE;
DRIVER_ERROR (the harness crashed) is reported separately — the two are
never conflated. The real-model legs are never silently skipped, never
substituted with another model or the `fake` profile, and the no-model
deterministic fixtures never count as the real-model pass (AC1).

Proposed CI lane (no workflow edits made here): NATIVE APPROVAL of the
real-model legs REQUIRES a provisioned, checksum-verified pinned model —
download the Xenova ONNX once, verify sha256
bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5, place it
in a cached models dir, and export ZMEM_TEST_REAL_MODELS_DIR — so
AC1/AC3/AC6-semantic/AC7 run for real. On a model-absent lane the suite's
MODEL_UNAVAILABLE failures are DIAGNOSTIC evidence that the leg is
INCOMPLETE, nothing more: they can never be counted as, converted to, or
gated into an alternate pass, and no other model (and never the `fake`
profile) may be substituted. A model-absent lane may only assert that the
failure set is EXACTLY the MODEL_UNAVAILABLE signatures — a reduced pass
count is never an approval signal.

Isolation: the store environment is pinned to throwaway temp paths BEFORE
any zmem import; every store-touching leg runs against a fresh temporary
store (issue #244 rules via tests/_store_guard.py). Model downloads are
disabled everywhere (ZMEM_MODEL_AUTODOWNLOAD=0).

Shape note: the seven tests are MODULE-LEVEL functions so the issue's frozen
AC argvs (`... -q tests/test_subject_dedup_contract.py::test_distinct_subjects`)
resolve as written. The direct-python CI form (no pytest required) runs the
same functions from ``__main__``.

Run (repo convention, no pytest required):
    python tests/test_subject_dedup_contract.py
Run (the issue's frozen AC form, per AC):
    python -X utf8 -m pytest --disable-plugin-autoload --noconftest \
        -p no:cacheprovider -q tests/test_subject_dedup_contract.py::test_distinct_subjects
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
BUNDLED_MODELS_DIR = SCRIPTS_DIR.parent / "models"  # skills/memory/models
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

# ---------------------------------------------------------------------------
# Store environment pinning — BEFORE the #244 guard import and before any
# storelib/embeddings import. The operator's real default store
# (~/.zmem/store.sqlite) must never be resolved by this process or any of its
# subprocesses.
# ---------------------------------------------------------------------------
_MODULE_TMP = tempfile.mkdtemp(prefix="zmem-s376-module-")
os.environ["ZMEM_STORE"] = os.path.join(_MODULE_TMP, "store.sqlite")
os.environ.pop("ZMEM_DATA", None)
os.environ.pop("CLAUDE_PLUGIN_DATA", None)
os.environ.pop("ZCODE_PLUGIN_DATA", None)
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"  # never download a model
atexit.register(shutil.rmtree, _MODULE_TMP, True)

import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

# ---------------------------------------------------------------------------
# Frozen real-model identity (AC1: "Freeze model identity and inputs") and
# the frozen veto code (AC5: "Record a stable, privacy-safe explanation").
# ---------------------------------------------------------------------------
MODEL_HF_ID = "Xenova/all-MiniLM-L6-v2"
MODEL_DIM = 384
MODEL_SHA256 = "bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5"
SUBJECT_VETO_CODE = "subject_veto"

# AC7 pin: the dedup threshold is 0.85 (storelib write.py
# DEDUP_SIMILARITY_THRESHOLD default; ZMEM_DEDUP_THRESHOLD is scrubbed from
# every leg env so the default is what runs). The issue forbids reducing it.
DEDUP_THRESHOLD = 0.85

# Behavior-changing env that must never leak from the ambient shell into a
# leg (the fake profile in particular would substitute the real model).
_SCRUB_KEYS = (
    "ZMEM_DATA", "ZMEM_BACKUP_DIR", "ZMEM_BACKUP_INTERVAL_DAYS",
    "ZMEM_EMBED_PROFILE",  # never run the real-model leg on the fake profile
    "ZMEM_DEDUP_THRESHOLD", "ZMEM_LINK_THRESHOLD", "ZMEM_VEC_NS_OVERFETCH",
    "ZMEM_CAPTURE_MODE", "ZMEM_MODEL_URL",
)

# The pinned six-pair constructed corpus + held-out additions. Exact texts
# from research/hy-memory-experiments/real_semantic.py @2da9f44e (oracle
# subjects preserved). (label, subject_a, subject_b, text_a, text_b)
PINNED_CORPUS = (
    ("task-closeout", "issue41", "issue42",
     "Request timeout fix completed: deadline propagated, targeted checks "
     "passed, review approved.",
     "Request timeout fix completed: cancellation propagated, targeted "
     "checks passed, review approved."),
    ("issue-number", "issue41", "issue42",
     "Issue 41: deadline propagation fix passed targeted checks and review.",
     "Issue 42: deadline propagation fix passed targeted checks and review."),
    ("same-task-paraphrase", "issue41", "issue41",
     "The request timeout fix propagates the deadline through the HTTP client.",
     "The HTTP client now receives the propagated request deadline."),
    ("different-files", "file:client.py", "file:worker.py",
     "Fixed client.py timeout handling by propagating the request deadline; "
     "targeted tests passed.",
     "Fixed worker.py timeout handling by propagating the request deadline; "
     "targeted tests passed."),
    ("same-general-lesson", "lesson:deadline", "lesson:deadline",
     "Always propagate request deadlines through nested network calls.",
     "Always propagate request deadlines through nested network calls."),
    ("unknown-subject", None, "issue41",
     "Use a bounded deadline for outbound network requests.",
     "Use a bounded deadline for outbound network requests."),
)

# Held-out additions authored with this test (not from the pinned corpus):
# a distinct-subject pair that must stay separate, and a same-subject
# near-duplicate that must never be split BY SUBJECT.
HELD_OUT_PAIRS = (
    ("heldout-pr-distinct", "pr:410", "pr:411",
     "PR 410 review approved the deadline propagation fix after targeted checks.",
     "PR 411 review approved the deadline propagation fix after targeted checks."),
    ("heldout-same-subject-near-dup", "issue99", "issue99",
     "The retry budget resets once all workers acknowledge the cancellation.",
     "The retry budget resets after every worker acknowledges the cancellation."),
)

BELOW_THRESHOLD_CONTROL = "same-task-paraphrase"  # pinned cosine 0.763633


def _base_env() -> dict:
    """A scrubbed copy of this process's environment for subprocess legs."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZAXBYGRAPH")}
    for key in _SCRUB_KEYS:
        env.pop(key, None)
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["ZMEM_S376_SCRIPTS_DIR"] = str(SCRIPTS_DIR)
    return env


def _nomodel_env(store_path: str) -> dict:
    """Env for a store.py subprocess pinned to a throwaway store with the
    embedding model forced absent (fast deterministic legs; repo convention
    from tests/test_backup.py::_base_env)."""
    env = _base_env()
    env["ZMEM_STORE"] = str(store_path)
    env["ZMEM_MODELS_DIR"] = str(Path(store_path).parent / "no-such-models")
    return env


def _last_json(stdout: str) -> dict:
    """The last parseable JSON object on a store.py --json stdout (human
    [zmem] lines may precede it)."""
    obj = None
    for line in stdout.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
    if not isinstance(obj, dict):
        raise AssertionError(f"no JSON object on store.py stdout: {stdout!r}")
    return obj


# ---------------------------------------------------------------------------
# Real-model resolution: read-only probe for the pinned MiniLM. The
# operator's shared MODEL cache (~/.zmem/models) is read for its model files
# only — exactly what the pinned preparation run itself did — and the
# canonical store next to it is never opened.
# ---------------------------------------------------------------------------
def _probe_real_models_dir():
    candidates = []
    explicit = os.environ.get("ZMEM_TEST_REAL_MODELS_DIR", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(BUNDLED_MODELS_DIR)
    candidates.append(Path.home() / ".zmem" / "models")
    probed = []
    for cand in candidates:
        model_file = cand / "minilm.onnx"
        entry = {"dir": str(cand),
                 "minilm_onnx": model_file.is_file(),
                 "sha256": None}
        if entry["minilm_onnx"]:
            try:
                h = hashlib.sha256()
                with model_file.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                entry["sha256"] = h.hexdigest()
            except OSError as exc:
                entry["error"] = str(exc)
        if entry["minilm_onnx"] and entry["sha256"] == MODEL_SHA256:
            return cand, probed + [entry]
        probed.append(entry)
    return None, probed


# ---------------------------------------------------------------------------
# Driver for the real-model legs, executed as a SUBPROCESS so the loaded ONNX
# session and embeddings state can never leak into (or from) this test
# process or a sibling test. Mirrors the pinned real_semantic.py shape:
# schema.connect() (native sqlite-vec), init+migrate, one store, per-case
# namespaces, write.add_memory.
# ---------------------------------------------------------------------------
_REAL_MODEL_DRIVER = r'''
import contextlib, hashlib, io, json, math, os, sqlite3, struct, sys, time
from pathlib import Path

sys.path.insert(0, os.environ["ZMEM_S376_SCRIPTS_DIR"])
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import embeddings
import embed_profiles

status = embeddings.availability_status()
probe = embeddings.embed_text("subject dedup contract probe") if embeddings.is_available() else None
model_path = Path(os.environ["ZMEM_MODELS_DIR"]) / "minilm.onnx"
model_sha = None
if model_path.is_file():
    with model_path.open("rb") as fh:
        model_sha = hashlib.file_digest(fh, "sha256").hexdigest()
if probe is None or not status.get("available"):
    print(json.dumps({"model_unavailable": {**status, "resolved_sha256": model_sha}}))
    raise SystemExit(3)
if model_sha != embed_profiles.MINILM_SHA256:
    print(json.dumps({"model_unavailable": {"reason": "sha_mismatch",
                                            "resolved_sha256": model_sha,
                                            "pinned": embed_profiles.MINILM_SHA256}}))
    raise SystemExit(3)
if len(probe) != 384 * 4:  # embed_text returns 384 float32 as 1536 bytes
    print(json.dumps({"model_unavailable": {"reason": "dim", "dim_bytes": len(probe)}}))
    raise SystemExit(3)

from storelib import evidence, schema, write

assert Path(schema.STORE_PATH).resolve() == Path(os.environ["ZMEM_STORE"]).resolve()
conn = schema.connect()
conn.row_factory = sqlite3.Row
schema.init_db(conn)
schema.migrate(conn)
native_vec = conn.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE name='memory_vec'").fetchone()[0] == 1


def unpack(blob):
    return struct.unpack("<" + "f" * (len(blob) // 4), blob)


def cosine(a, b):
    av, bv = unpack(a), unpack(b)
    return sum(x * y for x, y in zip(av, bv)) / (
        math.sqrt(sum(x * x for x in av)) * math.sqrt(sum(y * y for y in bv)))


def embed(text):
    return embeddings.embed_text(text)


def ev(label):
    eid = evidence.write_evidence(
        conn, session_id="s376-real-model", lane="zcode", moment="pretool",
        kind="tool_call", ts="2026-10-17T00:00:00Z", excerpt=label,
        ref_path="synthetic/s376-driver.txt", ref_offset=0)
    conn.commit()
    return eid


def tags_for(subject):
    return ("task:" + subject) if subject else ""


def add(ns, text, subject=None, *, eids=(), skip=False, kind="fact"):
    with contextlib.redirect_stdout(io.StringIO()):
        res = write.add_memory(
            conn, namespace=ns, type_=kind, content=text, tags=tags_for(subject),
            source_ref="session:synthetic-" + (subject or "unknown"),
            signal="test", link_attr_propagate=False,
            evidence_ids=list(eids) or None, skip_dedup=skip)
    return {"id": str(res), "deduped": bool(res.deduped)}


def live(ns):
    out = []
    for r in conn.execute(
            "SELECT id, content, tags, source_ref, retrieval_count FROM memory "
            "WHERE namespace=? AND superseded_at IS NULL", (ns,)):
        out.append({"id": r["id"], "content": r["content"], "tags": r["tags"],
                    "source_ref": r["source_ref"],
                    "retrieval_count": r["retrieval_count"],
                    "evidence": evidence.evidence_ids_for_memory(conn, r["id"])})
    return out


def live_count(ns):
    return conn.execute(
        "SELECT COUNT(*) FROM memory WHERE namespace=? AND superseded_at IS NULL",
        (ns,)).fetchone()[0]


report = {"model": {"hf_id": "Xenova/all-MiniLM-L6-v2", "sha256": model_sha,
                    "dim": 384, "native_vec": native_vec,
                    "profile": status.get("profile")},
          "cases": {}, "corpus": [], "heldout": [], "cost_semantic": {}}

# AC1 — distinct-subject examples with per-row evidence and source refs.
for name, sa, sb, ta, tb in (
    ("issue-number-distinct", "issue41", "issue42",
     "Issue 41: deadline propagation fix passed targeted checks and review.",
     "Issue 42: deadline propagation fix passed targeted checks and review."),
    ("task-closeout-distinct", "issue41", "issue42",
     "Request timeout fix completed: deadline propagated, targeted checks "
     "passed, review approved.",
     "Request timeout fix completed: cancellation propagated, targeted "
     "checks passed, review approved."),
):
    ns = "project:s376/" + name
    ea, eb = ev(name + " evidence A"), ev(name + " evidence B")
    first = add(ns, ta, sa, eids=[ea])
    second = add(ns, tb, sb, eids=[eb])
    report["cases"][name] = {
        "cosine": round(cosine(embed(ta), embed(tb)), 6),
        "texts": [ta, tb], "evidence_ids": [ea, eb],
        "first": first, "second": second,
        "live_rows": live_count(ns), "rows": live(ns)}

# AC2 — incidental mentioned files under ONE subject must still merge.
ns = "project:s376/same-subject-file-mention"
fa = ("Fixed client.py timeout handling by propagating the request deadline; "
      "targeted tests passed.")
fb = ("Fixed worker.py timeout handling by propagating the request deadline; "
      "targeted tests passed.")
report["cases"]["same-subject-file-mention"] = {
    "cosine": round(cosine(embed(fa), embed(fb)), 6),
    "first": add(ns, fa, "issue41"), "second": add(ns, fb, "issue41"),
    "live_rows": live_count(ns), "rows": live(ns)}

# AC3 leg 1 — STRICT ranking: the incompatible candidate is strictly the
# nearest neighbor; the compatible row is the runner-up. X is a one-word
# edit of A's text (nearest) but carries B's subject.
ns = "project:s376/runner-up-strict"
ta = "Issue 41: deadline propagation fix passed targeted checks and review."
tb = "Issue 42: deadline propagation fix passed targeted checks and review."
tx = "Issue 41: deadline propagation fix passed targeted checks and final review."
a = add(ns, ta, "issue41")
b = add(ns, tb, "issue42", skip=True)
x = add(ns, tx, "issue42")
report["cases"]["runner-up-strict"] = {
    "cos_xa": round(cosine(embed(tx), embed(ta)), 6),
    "cos_xb": round(cosine(embed(tx), embed(tb)), 6),
    "adds": [a, b, x], "live_rows": live_count(ns), "rows": live(ns)}

# AC3 leg 2 — the TIE case kept separate: equal-content A/B both nearest to
# X; the compatible B must win the fold, A must stay untouched.
ns = "project:s376/runner-up-tie"
T = ("The shared closeout checklist for the release train covers deadline "
     "propagation and cancellation draining.")
X = T + " Confirmed by both owners."
report["cases"]["runner-up-tie"] = {
    "cosine_tx": round(cosine(embed(T), embed(X)), 6),
    "adds": [add(ns, T, "issue41"), add(ns, T, "issue42", skip=True),
             add(ns, X, "issue42")],
    "live_rows": live_count(ns), "rows": live(ns)}

# AC3 leg — the polarity GUARD needs a dedup hit with a negation flip,
# which only the semantic path can produce (exact-equal texts cannot
# disagree on polarity). Same-subject AND unknown-identity variants: the
# guard must fire in both states and record the contradicts link.
for name, sub in (("polarity-guard-same", "issue41"),
                  ("polarity-guard-unknown", None)):
    ns = "project:s376/" + name
    pa = "The request timeout fix propagates the deadline through the HTTP client."
    pb = "The request timeout fix never propagates the deadline through the HTTP client."
    r1, r2 = add(ns, pa, sub), add(ns, pb, sub)
    pair_links = conn.execute(
        "SELECT COUNT(*) FROM memory_link WHERE relation='contradicts' "
        "AND ((src_id=? AND dst_id=?) OR (src_id=? AND dst_id=?))",
        (r1["id"], r2["id"], r2["id"], r1["id"])).fetchone()[0]
    report["cases"][name] = {
        "cosine": round(cosine(embed(pa), embed(pb)), 6),
        "adds": [r1, r2], "live_rows": live_count(ns),
        "pair_contradicts_links": pair_links}

# AC7 — the frozen six-pair constructed corpus (oracle subjects).
for label, sa, sb, ta, tb in %CORPUS_TUPLE%:
    ns = "project:s376/corpus/" + label
    cos = round(cosine(embed(ta), embed(tb)), 6)
    r1, r2 = add(ns, ta, sa), add(ns, tb, sb)
    report["corpus"].append({"case": label, "subjects": [sa, sb], "cosine": cos,
                             "same_id": r1["id"] == r2["id"],
                             "second_deduped": r2["deduped"],
                             "live_rows": live_count(ns)})

# AC7 — held-out reviewed additions.
for label, sa, sb, ta, tb in %HELDOUT_TUPLE%:
    ns = "project:s376/heldout/" + label
    cos = round(cosine(embed(ta), embed(tb)), 6)
    r1, r2 = add(ns, ta, sa), add(ns, tb, sb)
    report["heldout"].append({"case": label, "subjects": [sa, sb], "cosine": cos,
                              "live_rows": live_count(ns)})

# AC6 — semantic-path CANDIDATE work. PRIMARY: the frozen observability
# seam ``storelib.write.SUBJECT_COMPAT_EVALS`` (process-local int,
# incremented once per subject-compatibility evaluation on
# dedup/consolidation decision paths), read via getattr before/after the
# measured contested add (absent -> 0 with seam_absent=true) — KNN rows
# cannot observe compatibility evaluations. SECONDARY: the KNN window
# metric wraps ``storelib.write._vec_knn_in_namespace`` — the module
# attribute the dedup/link paths resolve — the same seam the pinned
# preparation experiments patched (unittest.mock.patch of write
# internals); it is NOT a public API.
#
# Fixture per size n: n same-subject filler rows + an ABOVE-THRESHOLD
# INCOMPATIBLE nearest candidate (exact text of the measured row, different
# subject) + a COMPATIBLE runner-up (near-identical text, measured
# subject). ALL seeding is NON-DESTRUCTIVE via the sanctioned skip-dedup
# bypass — without it the near-identical same-theme fillers dedup-fold
# during seeding and BOTH sizes collapse to the same live-row count,
# making the size-independence bound vacuous. The driver PRECONDITIONS
# exactly n + 2 live rows before the measured add and PINS the contested
# top-two (identities + similarities) in the report so fixture drift
# cannot silently invalidate the contested shape. The measured add
# therefore faces a contested candidate field at BOTH sizes — its top hit
# must be rejected and the runner-up considered without scanning the
# namespace.
MEASURED = ("A brand new closeout memo about the quasar lane, unlike every "
            "seeded page, reviewed by both owners.")
RUNNER_UP = ("A brand new closeout memo about the quasar lane, unlike every "
             "seeded page, confirmed by both owners.")
THEMES = (
    "the telemetry module wiring notes",
    "the backup rotation runbook chapter",
    "the fleet token refresh procedure",
    "the evidence redaction lane summary",
    "the episode closeout cadence rules",
    "the namespace rekey guard rails",
    "the inject token budget table",
    "the correction queue harvest notes",
    "the passive prefetch fence policy",
    "the doctor drift map findings",
)
original_knn = write._vec_knn_in_namespace
knn_probe = {"calls": 0, "candidates": 0}


def counting_knn(conn, embedding, *, namespaces, k, overfetch=None):
    rows = original_knn(conn, embedding, namespaces=namespaces, k=k,
                        overfetch=overfetch)
    knn_probe["calls"] += 1
    knn_probe["candidates"] += len(rows)
    return rows


for n in (10, 60):
    ns = "project:s376/cost-sem/n%d" % n
    with contextlib.redirect_stdout(io.StringIO()):
        # Fillers seed NON-DESTRUCTIVELY (skip-dedup bypass): seeded
        # normally, the near-identical same-theme fillers dedup-fold during
        # seeding and both sizes collapse to the same live-row count.
        for i in range(n):
            write.add_memory(conn, namespace=ns, type_="fact",
                             content="Page %d of %s, revision %d, reviewed by owner %d." % (
                                 i, THEMES[i % len(THEMES)], i % 7, i % 5),
                             tags="task:issue41", source_ref="session:s376-cost",
                             signal="test", link_attr_propagate=False,
                             skip_dedup=True)
        # Above-threshold incompatible nearest + compatible runner-up.
        incompatible_id = str(write.add_memory(
            conn, namespace=ns, type_="fact", content=MEASURED,
            tags="task:issue41", source_ref="session:s376-cost",
            signal="test", link_attr_propagate=False))
        compatible_id = str(write.add_memory(
            conn, namespace=ns, type_="fact", content=RUNNER_UP,
            tags="task:issue42", source_ref="session:s376-cost",
            signal="test", link_attr_propagate=False,
            skip_dedup=True))
    # PRECONDITION: non-destructive seeding must leave exactly n + 2 live
    # rows (n fillers + the incompatible nearest + the compatible
    # runner-up) before the measured add — fail loudly otherwise.
    live_before = live_count(ns)
    assert live_before == n + 2, (
        "cost fixture precondition: %s must hold exactly n+2=%d live rows "
        "before the measured add (%d fillers + incompatible nearest + "
        "compatible runner-up); got %d — seeding is no longer "
        "non-destructive, re-freeze the fixture" % (ns, n + 2, n, live_before))
    # PIN the contested top-two against fixture drift: the nearest live row
    # to the measured embedding must be the INCOMPATIBLE exact row and the
    # runner-up the COMPATIBLE row, both above threshold, at BOTH sizes.
    measured_emb = embed(MEASURED)
    scored = sorted(
        ((cosine(measured_emb, r["embedding"]), r["id"], r["tags"])
         for r in conn.execute(
             "SELECT id, tags, embedding FROM memory "
             "WHERE namespace=? AND superseded_at IS NULL", (ns,))),
        reverse=True)
    contested_top2 = [{"cosine": round(c, 6), "id": rid, "tags": tags}
                      for c, rid, tags in scored[:2]]
    assert contested_top2[0]["id"] == incompatible_id, (
        "cost fixture drift at %s: the nearest candidate is not the seeded "
        "incompatible exact row (top2=%s, expected id=%s)"
        % (ns, contested_top2, incompatible_id))
    assert contested_top2[0]["tags"] == "task:issue41", contested_top2
    assert contested_top2[1]["id"] == compatible_id, (
        "cost fixture drift at %s: the runner-up is not the seeded "
        "compatible row (top2=%s, expected id=%s)"
        % (ns, contested_top2, compatible_id))
    assert contested_top2[1]["tags"] == "task:issue42", contested_top2
    assert contested_top2[0]["cosine"] >= write.DEDUP_SIMILARITY_THRESHOLD, (
        "cost fixture drift at %s: the incompatible nearest must sit above "
        "the dedup threshold (top2=%s)" % (ns, contested_top2))
    assert contested_top2[1]["cosine"] >= write.DEDUP_SIMILARITY_THRESHOLD, (
        "cost fixture drift at %s: the compatible runner-up must sit above "
        "the dedup threshold (top2=%s)" % (ns, contested_top2))
    selects = []

    def trace(statement):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement.split("\n")[0][:120])

    conn.set_trace_callback(trace)
    write._vec_knn_in_namespace = counting_knn
    knn_probe["calls"] = knn_probe["candidates"] = 0
    # Frozen AC6 seam: read via getattr; absent -> 0 and seam_absent=true.
    seam_absent = not hasattr(write, "SUBJECT_COMPAT_EVALS")
    seam_before = getattr(write, "SUBJECT_COMPAT_EVALS", 0)
    t0 = time.perf_counter()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            write.add_memory(conn, namespace=ns, type_="fact",
                             content=MEASURED, tags="task:issue42",
                             source_ref="session:s376-cost",
                             signal="test", link_attr_propagate=False)
    finally:
        write._vec_knn_in_namespace = original_knn
        conn.set_trace_callback(None)
    seam_after = getattr(write, "SUBJECT_COMPAT_EVALS", 0)
    report["cost_semantic"]["n%d" % n] = {
        "selects": len(selects),
        "knn_calls": knn_probe["calls"],
        "candidates": knn_probe["candidates"],
        "seconds": round(time.perf_counter() - t0, 6),
        "first_selects": selects[:6],
        "live_before": live_before,
        "live_after": live_count(ns),
        "contested_top2": contested_top2,
        "seam_before": seam_before,
        "seam_after": seam_after,
        "seam_absent": seam_absent}

first_emb = conn.execute(
    "SELECT embedding IS NOT NULL FROM memory "
    "WHERE namespace='project:s376/issue-number-distinct' LIMIT 1").fetchone()
report["first_row_embedded"] = bool(first_emb) and first_emb[0] == 1
# The EFFECTIVE runtime threshold (env-scrubbed default), reported so the
# test asserts the real value rather than a local constant.
report["effective_dedup_threshold"] = write.DEDUP_SIMILARITY_THRESHOLD
conn.close()
print(json.dumps(report))
'''

# The corpus tuples are injected as repr() so the driver string carries the
# exact frozen fixture texts (single source of truth: the constants above).
_REAL_MODEL_DRIVER = _REAL_MODEL_DRIVER.replace(
    "%CORPUS_TUPLE%", repr(tuple(PINNED_CORPUS))).replace(
    "%HELDOUT_TUPLE%", repr(tuple(HELD_OUT_PAIRS)))

_REAL_MODEL_CACHE: dict = {}


def _real_model_report():
    """Run (once per process) the real-model driver in a subprocess against a
    throwaway store. Returns the parsed report dict; None when the pinned
    model is unavailable ('unavailable' details cached) and raises nothing —
    callers distinguish via the cache keys: 'unavailable' (model missing or
    driver-reported unavailability) vs 'error' (the harness itself failed;
    NEVER reported as MODEL_UNAVAILABLE)."""
    if "report" in _REAL_MODEL_CACHE:
        return _REAL_MODEL_CACHE["report"]
    if "unavailable" in _REAL_MODEL_CACHE or "error" in _REAL_MODEL_CACHE:
        return None
    models_dir, probed = _probe_real_models_dir()
    if models_dir is None:
        _REAL_MODEL_CACHE["unavailable"] = {
            "reason": "no checksum-verified minilm.onnx at any probed dir",
            "pinned_sha256": MODEL_SHA256, "probed": probed}
        return None
    tmp = tempfile.mkdtemp(prefix="zmem-s376-real-")
    atexit.register(shutil.rmtree, tmp, True)
    env = _base_env()
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env["ZMEM_MODELS_DIR"] = str(models_dir)
    env["TOKENIZERS_PARALLELISM"] = "false"
    env["OMP_NUM_THREADS"] = "2"
    env["OPENBLAS_NUM_THREADS"] = "2"
    try:
        proc = subprocess.run(
            [PYTHON, "-X", "utf8", "-c", _REAL_MODEL_DRIVER],
            env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=900)
    except subprocess.TimeoutExpired:
        _REAL_MODEL_CACHE["error"] = {
            "reason": "driver timeout after 900s", "probed": probed}
        return None
    if proc.returncode == 3:
        # The driver's own model-unavailability verdict.
        try:
            detail = _last_json(proc.stdout)
            if "model_unavailable" not in detail:
                raise AssertionError
        except AssertionError:
            detail = {"stdout": proc.stdout[-2000:],
                      "stderr": proc.stderr[-2000:]}
        _REAL_MODEL_CACHE["unavailable"] = detail.get("model_unavailable", detail)
        return None
    # Everything else: parse the report; a crash or a print-but-no-JSON bug
    # is DRIVER_ERROR, never MODEL_UNAVAILABLE.
    try:
        report = _last_json(proc.stdout)
    except AssertionError:
        _REAL_MODEL_CACHE["error"] = {
            "reason": f"driver exit {proc.returncode} with no report JSON",
            "stderr": proc.stderr[-2000:], "stdout": proc.stdout[-2000:]}
        return None
    if proc.returncode != 0:
        _REAL_MODEL_CACHE["error"] = {
            "reason": f"driver exit {proc.returncode} (report parsed)",
            "stderr": proc.stderr[-2000:]}
        return None
    _REAL_MODEL_CACHE["report"] = report
    return report


def _require_real_model_report() -> dict:
    """AC1/AC7 discipline: a missing pinned real model FAILS the leg with
    MODEL_UNAVAILABLE (reported as incomplete) — never a skip, never the
    no-model fixture standing in for the real-model pass. A harness crash is
    reported as DRIVER_ERROR so the two are never conflated."""
    report = _real_model_report()
    if report is None:
        if "error" in _REAL_MODEL_CACHE:
            detail = json.dumps(_REAL_MODEL_CACHE["error"], indent=1)
            raise AssertionError(
                "DRIVER_ERROR: the real-model driver subprocess failed "
                f"(this is a harness failure, not model unavailability): "
                f"{detail}")
        detail = json.dumps(_REAL_MODEL_CACHE.get("unavailable"), indent=1)
        raise AssertionError(
            "MODEL_UNAVAILABLE: the pinned real model ("
            f"{MODEL_HF_ID}, sha256 {MODEL_SHA256}) could not be loaded for "
            "the real-model acceptance leg. This leg is INCOMPLETE, not "
            f"passed. Probe detail: {detail}")
    return report


# ---------------------------------------------------------------------------
# Cost driver (AC6, lexical path): query-count + latency instrumentation on
# the public storelib write API, model-absent, throwaway store. Same
# contested-candidate shape as the semantic leg: an above-threshold (exact-
# equal) INCOMPATIBLE row plus a COMPATIBLE runner-up are seeded at both
# sizes before the measured add. The lexical candidate exposure is the
# indexed exact lookup itself (one candidate by construction), so the
# bounded observable here is SELECT work; the candidate-count bound lives
# on the semantic leg where the KNN window actually widens.
# ---------------------------------------------------------------------------
_COST_DRIVER = r'''
import contextlib, io, json, os, sqlite3, sys, time
from pathlib import Path

sys.path.insert(0, os.environ["ZMEM_S376_SCRIPTS_DIR"])
from storelib import schema, write

assert Path(schema.STORE_PATH).resolve() == Path(os.environ["ZMEM_STORE"]).resolve()
conn = schema.connect()
conn.row_factory = sqlite3.Row
schema.init_db(conn)
schema.migrate(conn)
out = {}
MEASURED = "cost probe NEW row unlike every seeded row"
for n in (10, 60):
    ns = "project:s376/cost/n%d" % n
    with contextlib.redirect_stdout(io.StringIO()):
        for i in range(n):
            write.add_memory(conn, namespace=ns, type_="fact",
                             content="cost probe seeded row %d on the issue41 lane" % i,
                             tags="task:issue41", source_ref="session:s376-cost",
                             signal="test", link_attr_propagate=False)
        write.add_memory(conn, namespace=ns, type_="fact", content=MEASURED,
                         tags="task:issue41", source_ref="session:s376-cost",
                         signal="test", link_attr_propagate=False)
        write.add_memory(conn, namespace=ns, type_="fact", content=MEASURED,
                         tags="task:issue42", source_ref="session:s376-cost",
                         signal="test", link_attr_propagate=False,
                         skip_dedup=True)
    selects = []

    def trace(statement):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement.split("\n")[0][:120])

    conn.set_trace_callback(trace)
    t0 = time.perf_counter()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            write.add_memory(conn, namespace=ns, type_="fact",
                             content=MEASURED, tags="task:issue42",
                             source_ref="session:s376-cost",
                             signal="test", link_attr_propagate=False)
    finally:
        conn.set_trace_callback(None)
    out["n%d" % n] = {"selects": len(selects),
                      "seconds": round(time.perf_counter() - t0, 6),
                      "first_selects": selects[:6]}
print(json.dumps(out))
'''


# ---------------------------------------------------------------------------
# Shared CLI-subprocess harness (the public store.py surface). Module-level
# functions (not unittest classes) so the issue's frozen AC argvs resolve.
# ---------------------------------------------------------------------------
class _ScratchStore:
    """A throwaway store driven through the real store.py CLI, model absent."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-s376-")
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = _nomodel_env(self.store)

    def close(self):
        shutil.rmtree(self.tmp, True)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    # -- process helpers ----------------------------------------------------
    def run_store(self, *args, env=None, stdin=None, timeout=240):
        return subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            env=env or self.env, input=stdin, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout)

    def add(self, content, *, ns="project:s376/t", type_="fact", tags="",
            source_ref="", signal="test", confidence=None, taint=None,
            evidence=(), no_dedup=False):
        args = ["add", "--namespace", ns, "--type", type_, "--content", content,
                "--tags", tags, "--source-ref", source_ref, "--signal", signal,
                "--json"]
        if confidence is not None:
            args += ["--confidence", str(confidence)]
        if taint is not None:
            args += ["--taint", taint]
        if evidence:
            args += ["--evidence", ",".join(evidence)]
        if no_dedup:
            args += ["--no-dedup"]
        result = self.run_store(*args)
        assert result.returncode == 0, (
            f"add failed: {result.stderr}\n{result.stdout}")
        payload = _last_json(result.stdout)
        assert payload.get("ok"), payload
        return payload

    def write_evidence(self, label):
        payload = json.dumps({
            "session_id": "s376-acceptance", "lane": "zcode", "moment": "pretool",
            "kind": "tool_call", "ts": "2026-10-17T00:00:00Z", "excerpt": label,
            "ref_path": "synthetic/s376.txt", "ref_offset": 0})
        result = self.run_store("evidence", "write", stdin=payload)
        assert result.returncode == 0, (
            f"evidence write failed: {result.stderr}\n{result.stdout}")
        eid = result.stdout.strip().splitlines()[-1].strip()
        assert eid, result.stdout
        return eid

    def write_jsonl(self, name, records):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        return path

    # -- store inspection ---------------------------------------------------
    def _connect(self, store=None):
        conn = sqlite3.connect(store or self.store)
        conn.row_factory = sqlite3.Row
        return conn

    def live_rows(self, ns=None, store=None):
        conn = self._connect(store)
        try:
            if ns is None:
                return conn.execute(
                    "SELECT * FROM memory WHERE superseded_at IS NULL").fetchall()
            return conn.execute(
                "SELECT * FROM memory WHERE namespace=? AND superseded_at IS NULL "
                "ORDER BY ingestion_ts, id", (ns,)).fetchall()
        finally:
            conn.close()

    def live_count(self, ns=None, store=None):
        conn = self._connect(store)
        try:
            if ns is None:
                return conn.execute(
                    "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL"
                ).fetchone()[0]
            return conn.execute(
                "SELECT COUNT(*) FROM memory WHERE namespace=? AND "
                "superseded_at IS NULL", (ns,)).fetchone()[0]
        finally:
            conn.close()

    def evidence_for(self, mid, store=None):
        conn = self._connect(store)
        try:
            return sorted(r["evidence_id"] for r in conn.execute(
                "SELECT evidence_id FROM memory_evidence WHERE memory_id=?",
                (mid,)))
        finally:
            conn.close()

    def contradicts_link_between(self, id_a, id_b, store=None):
        """Pair-scoped contradicts-link probe (memory_link stores one row per
        direction)."""
        conn = self._connect(store)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM memory_link WHERE relation='contradicts' "
                "AND ((src_id=? AND dst_id=?) OR (src_id=? AND dst_id=?))",
                (id_a, id_b, id_b, id_a)).fetchone()[0]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# AC1 — distinct explicitly identified task facts stay separate.
# ---------------------------------------------------------------------------
def test_distinct_subjects():
    # ---- Leg 1: real MiniLM + native sqlite-vec (issue #376 AC1) ----------
    report = _require_real_model_report()
    assert report["model"]["hf_id"] == MODEL_HF_ID
    assert report["model"]["sha256"] == MODEL_SHA256
    assert report["model"]["dim"] == MODEL_DIM
    assert report["model"]["native_vec"], (
        "real-model leg must run the supported sqlite-vec path (memory_vec "
        f"present); got {report['model']}")
    assert report["first_row_embedded"], (
        "real-model leg rows must actually carry embeddings")
    for case in ("issue-number-distinct", "task-closeout-distinct"):
        data = report["cases"][case]
        assert data["cosine"] >= DEDUP_THRESHOLD, (
            f"{case}: pinned pair must sit above the dedup threshold for the "
            f"case to be meaningful (cosine={data['cosine']})")
        assert not data["second"]["deduped"], (
            f"{case}: the second distinct-subject add folded into the first "
            f"(cosine={data['cosine']}) — distinct task facts must stay "
            "independently addressable")
        assert data["live_rows"] == 2, (
            f"{case}: expected 2 live rows for distinct subjects, got "
            f"{data['live_rows']}")
        rows = sorted(data["rows"], key=lambda r: r["tags"])
        text_a, text_b = data["texts"]
        ev_a, ev_b = data["evidence_ids"]
        # EXACT original content preserved on each row.
        assert rows[0]["content"] == text_a, (
            f"{case}: row A content was not preserved verbatim: "
            f"{rows[0]['content']!r}")
        assert rows[1]["content"] == text_b, (
            f"{case}: row B content was not preserved verbatim: "
            f"{rows[1]['content']!r}")
        assert rows[0]["tags"] == "task:issue41", rows[0]
        assert rows[0]["source_ref"] == "session:synthetic-issue41", rows[0]
        assert rows[1]["tags"] == "task:issue42", rows[1]
        assert rows[1]["source_ref"] == "session:synthetic-issue42", rows[1]
        # EXACT evidence-id mapping: A's evidence on A, B's on B.
        assert rows[0]["evidence"] == [ev_a], (
            f"{case}: row A must carry exactly its own evidence {ev_a}, got "
            f"{rows[0]['evidence']}")
        assert rows[1]["evidence"] == [ev_b], (
            f"{case}: row B must carry exactly its own evidence {ev_b}, got "
            f"{rows[1]['evidence']}")
        assert rows[0]["id"] != rows[1]["id"]

    # ---- Leg 2: no-model deterministic fixture, same identity boundary -----
    with _ScratchStore() as s:
        ns = "project:s376/ac1-deterministic"
        content = "Issue 41 fix landed"
        first = s.add(content, ns=ns, tags="task:issue41",
                      source_ref="session:synthetic-issue41")
        assert first["result"] == "stored", first
        second = s.add(content, ns=ns, tags="task:issue42",
                       source_ref="session:synthetic-issue42")
        rows = s.live_rows(ns)
        assert len(rows) == 2, (
            "no-model leg: exact-equal content with explicitly disjoint task "
            f"identities must stay 2 live rows (got {len(rows)}; second add "
            f"result={second['result']})")
        by_tag = {r["tags"]: r for r in rows}
        assert "task:issue41" in by_tag, f"identity lost: {by_tag}"
        assert "task:issue42" in by_tag, f"identity lost: {by_tag}"
        assert by_tag["task:issue41"]["retrieval_count"] == 0, (
            "the first row must not be refreshed by a subject-incompatible "
            f"add: {dict(by_tag['task:issue41'])}")
        assert by_tag["task:issue41"]["source_ref"] == "session:synthetic-issue41"
        assert by_tag["task:issue42"]["source_ref"] == "session:synthetic-issue42"
        assert by_tag["task:issue41"]["content"] == content
        assert by_tag["task:issue42"]["content"] == content


# ---------------------------------------------------------------------------
# AC2 — the documented identity policy (no blanket partitioning).
# ---------------------------------------------------------------------------
def test_identity_controls():
    content = "closeout checklist completed for the deadline fix"
    with _ScratchStore() as s:
        # (1) same-task duplicates still merge — no blanket partition.
        ns = "project:s376/ac2-same-task"
        s.add(content, ns=ns, tags="task:issue41")
        second = s.add(content, ns=ns, tags="task:issue41")
        assert s.live_count(ns) == 1, (
            "same-task duplicate must merge (live=1)")
        assert second["result"] == "deduped", second

        # (2) multi-subject rows: shared subject = compatible; disjoint = not.
        ns = "project:s376/ac2-multi-shared"
        s.add(content, ns=ns, tags="task:issue41,task:issue90")
        s.add(content, ns=ns, tags="task:issue90")
        assert s.live_count(ns) == 1, (
            "a multi-subject row is compatible with an incoming row sharing "
            "any subject")
        ns = "project:s376/ac2-multi-disjoint"
        s.add(content, ns=ns, tags="task:issue41,task:issue90")
        s.add(content, ns=ns, tags="task:issue42")
        assert s.live_count(ns) == 2, (
            "a multi-subject row with a disjoint incoming subject set is "
            "subject-incompatible: both statements must survive")

        # (3) repository-qualified identities: identical numbers, distinct
        # repositories — no cross-repository id collision.
        ns = "project:s376/ac2-xrepo"
        s.add(content, ns=ns, tags="task:zaxbysauce/zmem#368",
              source_ref="session:synthetic-zmem368")
        s.add(content, ns=ns, tags="task:othercorp/widget#368",
              source_ref="session:synthetic-widget368")
        assert s.live_count(ns) == 2, (
            "repository-qualified identical issue numbers are DISTINCT task "
            "identities and must not merge")

        # (4) missing/ambiguous identity stays backward compatible: an
        # untagged duplicate of a tagged row still merges.
        ns = "project:s376/ac2-unknown"
        s.add(content, ns=ns, tags="task:issue41")
        s.add(content, ns=ns)
        assert s.live_count(ns) == 1, (
            "unknown-subject compatibility: an absent identity must not veto "
            "(backward compatible merge)")

        # (4b) AMBIGUOUS MALFORMED identity tokens (empty / whitespace key)
        # are treated as absent, never as a conflict, and never crash.
        ns = "project:s376/ac2-malformed"
        s.add(content, ns=ns, tags="task:issue41")
        s.add(content, ns=ns, tags="task:")
        assert s.live_count(ns) == 1, (
            "a malformed task token (empty key) is ambiguous, not a subject "
            "conflict: exact duplicates still merge")
        ns = "project:s376/ac2-malformed-ws"
        s.add(content, ns=ns, tags="task:   ")
        s.add(content, ns=ns)
        assert s.live_count(ns) == 1, (
            "a whitespace-only task key is ambiguous, not a conflict")

        # (4c) ARBITRARY non-task tags are never identities.
        ns = "project:s376/ac2-arbitrary"
        s.add(content, ns=ns, tags="host:box1")
        s.add(content, ns=ns, tags="agent:zeta")
        assert s.live_count(ns) == 1, (
            "arbitrary non-task tags (host:/agent:/free words) must never "
            "partition duplicates")
        ns = "project:s376/ac2-arbitrary-mix"
        s.add(content, ns=ns, tags="task:issue41,host:box1")
        s.add(content, ns=ns, tags="task:issue41,agent:zeta")
        assert s.live_count(ns) == 1, (
            "differing arbitrary tags alongside the SAME task identity must "
            "not veto the merge")

        # (5) historical transitions: a superseded row does not block the
        # same identity from writing fresh history.
        ns = "project:s376/ac2-transition"
        first = s.add(content, ns=ns, tags="task:issue41")
        result = s.run_store("supersede", "--id", first["id"])
        assert result.returncode == 0, result.stderr
        s.add(content, ns=ns, tags="task:issue41")
        rows = s.live_rows(ns)
        assert len(rows) == 1, (
            "after supersede, a re-add of the same identity and content must "
            "produce exactly one new live row")

        # (6) generic lessons still merge — no per-session / per-source
        # partition — and REUSABLE LESSONS GENERALIZE ACROSS TASK ORIGINS
        # (issue text): a lesson observed under two DIFFERENT task
        # identities is one lesson, not two task facts.
        lesson = ("Always propagate request deadlines through nested network "
                  "calls.")
        ns = "project:s376/ac2-lesson"
        s.add(lesson, ns=ns, type_="lesson", source_ref="session:alpha")
        s.add(lesson, ns=ns, type_="lesson", source_ref="session:beta")
        assert s.live_count(ns) == 1, (
            "a reusable lesson generalizes across sessions: a different "
            "source_ref is NOT proof of subject conflict")
        ns = "project:s376/ac2-lesson-origins"
        s.add(lesson, ns=ns, type_="lesson", tags="task:issue41")
        s.add(lesson, ns=ns, type_="lesson", tags="task:issue42")
        assert s.live_count(ns) == 1, (
            "a reusable LESSON generalizes across different task origins — "
            "the distinct-subject separation is for task-scoped FACTS, and "
            "must not fork one general lesson per task")
        ns = "project:s376/ac2-per-source"
        s.add(content, ns=ns, tags="task:issue41",
              source_ref="session:synthetic-issue41")
        s.add(content, ns=ns, tags="task:issue41",
              source_ref="session:synthetic-other")
        assert s.live_count(ns) == 1, (
            "same task identity + different source_ref must merge")

    # (7) incidental file mentions are not identities: with the real model,
    # same-subject rows differing only in a mentioned file (pinned
    # different-files texts, cosine ~0.90) must STILL merge.
    report = _require_real_model_report()
    data = report["cases"]["same-subject-file-mention"]
    assert data["cosine"] >= DEDUP_THRESHOLD, (
        "same-subject file-mention pair must sit above the dedup threshold "
        f"to be meaningful (cosine={data['cosine']})")
    assert data["live_rows"] == 1, (
        "a merely mentioned file is not a subject: same-subject rows "
        f"differing only in an incidental file must merge (cosine="
        f"{data['cosine']}, live_rows={data['live_rows']}) — no universal "
        "different-file veto")


# ---------------------------------------------------------------------------
# AC3 — compatible runner-up through semantic, exact and cached lookups.
# ---------------------------------------------------------------------------
def test_compatible_runner_up():
    content = "runner up probe content for the closeout lane"

    # ---- Leg 1: STRICT ranking, real model — the INCOMPATIBLE candidate is
    # strictly nearest; the compatible row is the runner-up that must win.
    report = _require_real_model_report()
    data = report["cases"]["runner-up-strict"]
    assert data["cos_xa"] > data["cos_xb"], (
        "strict-ranking fixture is broken: the incoming row must be "
        f"STRICTLY nearer the incompatible candidate (cos_xa="
        f"{data['cos_xa']}) than the compatible one (cos_xb="
        f"{data['cos_xb']}) — re-freeze the fixture if the pinned model "
        "disagrees")
    assert data["cos_xb"] >= DEDUP_THRESHOLD, (
        f"the compatible runner-up must itself be above the threshold "
        f"(cos_xb={data['cos_xb']})")
    tags_present = sorted(r["tags"] for r in data["rows"])
    assert tags_present == ["task:issue41", "task:issue42"], (
        "strict-ranking case: each surviving row must carry EXACTLY its own "
        f"task identity (got {tags_present}) — a folded or unioned tag set "
        "means the veto leaked metadata")
    by_tag = {r["tags"]: r for r in data["rows"]}
    assert by_tag["task:issue41"]["retrieval_count"] == 0, (
        "the STRICTLY-NEAREST but INCOMPATIBLE candidate (task:issue41) "
        "must not be refreshed: a rejected top candidate must not hide the "
        f"compatible runner-up (adds={data['adds']})")
    assert by_tag["task:issue42"]["retrieval_count"] >= 1, (
        "the compatible runner-up (task:issue42) must absorb the fold "
        f"(rows={data['rows']})")
    assert data["adds"][2]["deduped"], (
        "the incoming add must dedup into the compatible runner-up, not "
        f"insert a third row (adds={data['adds']})")

    # ---- Leg 2: the TIE case (kept separate from strict ranking): both
    # candidates equidistant; compatibility, not tie order, picks the fold.
    data = report["cases"]["runner-up-tie"]
    assert data["cosine_tx"] >= DEDUP_THRESHOLD, (
        f"runner-up tie pair must be above threshold (cosine="
        f"{data['cosine_tx']})")
    assert len(data["rows"]) == 2, (
        f"after the compatible fold exactly A and B stay live (rows={data['rows']})")
    tags_present = sorted(r["tags"] for r in data["rows"])
    assert tags_present == ["task:issue41", "task:issue42"], (
        f"tie case: each surviving row must carry EXACTLY its own task "
        f"identity (got {tags_present})")
    by_tag = {r["tags"]: r for r in data["rows"]}
    assert by_tag["task:issue41"]["retrieval_count"] == 0, (
        "in a tie the INCOMPATIBLE candidate must not be refreshed "
        f"(adds={data['adds']})")
    assert by_tag["task:issue42"]["retrieval_count"] >= 1, (
        "in a tie the COMPATIBLE candidate must absorb the fold "
        f"(rows={data['rows']})")
    assert data["adds"][2]["deduped"], (
        f"the tie fold must dedup, not mint a third row (adds={data['adds']})")

    # ---- Leg 1c: the polarity GUARD on the real-model path (the only path
    # where a dedup hit with a negation flip exists). It must fire under a
    # KNOWN subject and under UNKNOWN identity — subject logic may never
    # bypass it — and must keep recording the pair-scoped contradicts link.
    for name in ("polarity-guard-same", "polarity-guard-unknown"):
        guard = report["cases"][name]
        assert guard["cosine"] >= DEDUP_THRESHOLD, (
            f"{name}: the negation-flip pair must be above the dedup "
            f"threshold for the guard case to be meaningful (cosine="
            f"{guard['cosine']})")
        assert not guard["adds"][1]["deduped"], (
            f"{name}: a polarity-disagreeing duplicate must NEVER merge, "
            "whatever the identity state")
        assert guard["live_rows"] == 2, (
            f"{name}: polarity-disagreeing duplicates stay 2 live rows "
            f"(got {guard['live_rows']})")
        assert guard["pair_contradicts_links"] >= 1, (
            f"{name}: the established contradicts link between the pair "
            "must survive any subject-aware logic")

    with _ScratchStore() as s:
        # ---- Leg 3: exact-match fallback (the indexed LIMIT 1 lookup must
        # respect the identity rule when several equal-content rows live).
        ns = "project:s376/ac3-exact"
        s1 = s.add(content, ns=ns, tags="task:issue41")
        s2 = s.add(content, ns=ns, tags="task:issue42", no_dedup=True)
        assert s.live_count(ns) == 2
        third = s.add(content, ns=ns, tags="task:issue42")
        rows = {r["id"]: r for r in s.live_rows(ns)}
        assert len(rows) == 2, (
            "the exact fallback must fold into the compatible row, never "
            "mint a third equal-content row")
        assert rows[s1["id"]]["retrieval_count"] == 0, (
            "the incompatible first-inserted row must not be the exact-fold "
            f"target: {dict(rows[s1['id']])}")
        assert rows[s2["id"]]["retrieval_count"] == 1, (
            "the compatible same-subject row must be the exact-fold target: "
            f"{dict(rows[s2['id']])}")
        assert third["id"] == s2["id"], (third, s2)

        # ---- Leg 4: per-ingest CACHED lookup. The ingest cache pre-scan
        # (observed on base) serves the LAST-inserted equal-content row, so
        # an incoming task:issue41 row meets an INCOMPATIBLE cached id (the
        # task:issue42 row inserted second). The leg is self-validating: it
        # first proves a cache hit happened (exactly one row refreshed, no
        # third row), then pins WHICH row the fold belongs to.
        ns = "project:s376/ac3-cache"
        s1 = s.add(content, ns=ns, tags="task:issue41")
        s2 = s.add(content, ns=ns, tags="task:issue42", no_dedup=True)
        path = s.write_jsonl("ingest-compatible.jsonl", [{
            "id": str(uuid.uuid4()), "namespace": ns, "type": "fact",
            "content": content, "tags": "task:issue41",
            "source_ref": "session:synthetic-issue41", "signal": "test",
            "confidence": 0.9, "ingestion_ts": "2026-10-28T00:00:00Z"}])
        result = s.run_store("ingest-jsonl", "--in", path)
        assert result.returncode == 0, result.stderr + result.stdout
        rows = {r["id"]: r for r in s.live_rows(ns)}
        refreshed = [rid for rid, r in rows.items()
                     if r["retrieval_count"] > 0]
        assert len(rows) == 2, (
            "cached ingest leg: the incoming row must fold (cache hit), "
            "never mint a third equal-content row — if this fails the "
            "fixture no longer exercises the cache (re-freeze)")
        assert len(refreshed) == 1, (
            "cache verification: exactly ONE existing row may be refreshed "
            f"by the cached lookup (refreshed={refreshed})")
        # The discriminating pin: the fold must land on the row whose task
        # identity is COMPATIBLE with the incoming row (s1/task:issue41),
        # not on the incompatible id the cache serves.
        assert refreshed == [s1["id"]], (
            "the cached (ns, content_norm) entry pointed at a "
            "subject-INCOMPATIBLE row and was folded into anyway: the "
            "per-ingest cache must respect the same identity contract and "
            "find the compatible duplicate instead "
            f"(refreshed={refreshed}, compatible={s1['id']}, "
            f"incompatible={s2['id']})")
        # Second ingest, the OTHER subject: symmetrically must fold into s2.
        path = s.write_jsonl("ingest-incompatible.jsonl", [{
            "id": str(uuid.uuid4()), "namespace": ns, "type": "fact",
            "content": content, "tags": "task:issue42",
            "source_ref": "session:synthetic-issue42", "signal": "test",
            "confidence": 0.9, "ingestion_ts": "2026-10-28T00:00:01Z"}])
        result = s.run_store("ingest-jsonl", "--in", path)
        assert result.returncode == 0, result.stderr + result.stdout
        rows = {r["id"]: r for r in s.live_rows(ns)}
        assert len(rows) == 2, "the second cached ingest must not mint a row"
        assert rows[s1["id"]]["retrieval_count"] == 1, (
            "s1 must keep exactly its one compatible refresh — the "
            "task:issue42 ingest must not touch it")
        assert rows[s2["id"]]["retrieval_count"] == 1, (
            "the task:issue42 ingest must fold into its compatible row s2 "
            f"(rows={ {k: v['retrieval_count'] for k, v in rows.items()} })")

        # ---- Leg 5: invalid/unknown identity must not bypass the polarity
        # guard. On the NO-MODEL path the only robust observable is the
        # two-rows separation: the polarity GUARD itself needs a dedup hit
        # with a negation flip, which exact-match cannot produce (equal
        # texts cannot disagree on polarity), and contradicts links are not
        # reliably established for short no-model pairs (verified on base).
        # The guard and its pair-scoped contradicts links are exercised on
        # the real-model path — see the polarity-guard cases in leg 1 — and
        # at maintenance time in test_all_write_surfaces' contested leg.
        for label, tags_a, tags_b in (
                ("tagged", "task:issue41", "task:issue41"),
                ("untagged", "", "")):
            ns = "project:s376/ac3-negation-" + label
            s.add("always propagate the request deadline", ns=ns,
                  tags=tags_a)
            s.add("never propagate the request deadline", ns=ns,
                  tags=tags_b)
            rows = s.live_rows(ns)
            assert len(rows) == 2, (
                "polarity-disagreeing duplicates must stay separate rows "
                "under every identity state (the polarity guard outranks any "
                f"subject decision): got {len(rows)}")


# ---------------------------------------------------------------------------
# AC4 — one identity decision across add, update, ingest, consolidate/organize.
# ---------------------------------------------------------------------------
def test_all_write_surfaces():
    content = "add surface probe for subject dedup consistency"
    with _ScratchStore() as s:
        # ---- add ----------------------------------------------------------
        ns = "project:s376/ac4-add"
        s.add(content, ns=ns, tags="task:issue41")
        s.add(content, ns=ns, tags="task:issue42")
        assert s.live_count(ns) == 2, (
            "add: distinct subjects must stay 2 rows")

        # ---- update, TWO-HOP lineage (append-only replacement must not
        # fold into an incompatible row; the update_of chain survives both
        # hops with the subject identity intact) ---------------------------
        ns = "project:s376/ac4-update"
        c1 = "update surface original statement for issue 41"
        c2 = "update surface replacement statement shared by both issues"
        c3 = "update surface second replacement unique to issue 41"
        r3 = s.add(c2, ns=ns, tags="task:issue42",
                   source_ref="session:synthetic-issue42")
        r1 = s.add(c1, ns=ns, tags="task:issue41",
                   source_ref="session:synthetic-issue41")
        # Hop 1: replacement content collides with the incompatible row r3.
        result = s.run_store("update", "--id", r1["id"], "--content", c2,
                             "--json")
        assert result.returncode == 0, result.stderr + result.stdout
        rows = s.live_rows(ns)
        replacement = [r for r in rows
                       if r["content"] == c2 and "task:issue41" in r["tags"]]
        assert replacement, (
            "update hop 1: the replacement row (content c2, task:issue41) "
            "must exist — the update path must not fold it into the "
            f"subject-incompatible row (rows={[dict(r) for r in rows]})")
        r1b = replacement[0]["id"]
        by_id = {r["id"]: r for r in rows}
        assert by_id[r3["id"]]["retrieval_count"] == 0, (
            "update hop 1: the incompatible row must not be refreshed")
        assert by_id[r3["id"]]["tags"] == "task:issue42", (
            "update hop 1: the incompatible row must not absorb the "
            f"replacement identity's tags through any channel: {dict(by_id[r3['id']])}")
        # Hop 2: update the replacement again; the lineage chain must read
        # r1c.update_of == r1b and r1b.update_of == r1.
        result = s.run_store("update", "--id", r1b, "--content", c3, "--json")
        assert result.returncode == 0, result.stderr + result.stdout
        conn = s._connect()
        try:
            chain = {row["id"]: row["update_of"] for row in conn.execute(
                "SELECT id, update_of FROM memory WHERE namespace=?", (ns,))}
        finally:
            conn.close()
        r1c = next(rid for rid, parent in chain.items()
                   if parent == r1b and rid != r1)
        assert chain.get(r1b) == r1["id"], (
            f"two-hop update lineage broken: hop-1 replacement {r1b} must "
            f"record update_of == {r1['id']}, got {chain.get(r1b)!r}")
        final = [r for r in s.live_rows(ns)
                 if r["content"] == c3 and "task:issue41" in r["tags"]]
        assert final and final[0]["id"] == r1c, (
            "update hop 2: the second replacement must be the live "
            f"task:issue41 row (live={ [dict(r) for r in s.live_rows(ns)] })")

        # ---- ingest: distinct-subject duplicate must land separately ------
        ns = "project:s376/ac4-ingest"
        s1 = s.add(content, ns=ns, tags="task:issue41")
        path = s.write_jsonl("ac4-ingest.jsonl", [{
            "id": str(uuid.uuid4()), "namespace": ns, "type": "fact",
            "content": content, "tags": "task:issue42",
            "source_ref": "session:synthetic-issue42", "signal": "test",
            "confidence": 0.9, "ingestion_ts": "2026-10-28T00:00:00Z"}])
        result = s.run_store("ingest-jsonl", "--in", path)
        assert result.returncode == 0, result.stderr + result.stdout
        rows = s.live_rows(ns)
        assert len(rows) == 2, (
            "ingest: a distinct-subject duplicate must not fold into the "
            "local row")
        by_id = {r["id"]: r for r in rows}
        assert by_id[s1["id"]]["retrieval_count"] == 0
        assert by_id[s1["id"]]["tags"] == "task:issue41"

        # ---- duplicate-id replay: re-ingesting the same record is
        # idempotent and never mutates an incompatible row ----------------
        ns = "project:s376/ac4-replay"
        rid = str(uuid.uuid4())
        path = s.write_jsonl("ac4-replay.jsonl", [{
            "id": rid, "namespace": ns, "type": "fact", "content": content,
            "tags": "task:issue41", "source_ref": "session:synthetic-issue41",
            "signal": "test", "confidence": 0.9,
            "ingestion_ts": "2026-10-28T00:00:00Z"}])
        for _ in range(2):
            result = s.run_store("ingest-jsonl", "--in", path)
            assert result.returncode == 0, result.stderr + result.stdout
        rows = s.live_rows(ns)
        assert len(rows) == 1, (
            "duplicate-id replay must stay exactly one live row")
        assert rows[0]["id"] == rid
        assert rows[0]["tags"] == "task:issue41"

        # ---- consolidate: maintenance admission must respect the rule ----
        ns = "project:s376/ac4-consolidate"
        c1 = s.add(content, ns=ns, tags="task:issue41")
        c2 = s.add(content, ns=ns, tags="task:issue42", no_dedup=True)
        assert s.live_count(ns) == 2
        result = s.run_store("consolidate", "--force", "--namespace", ns,
                             "--json")
        assert result.returncode == 0, result.stderr + result.stdout
        rows = s.live_rows(ns)
        assert len(rows) == 2, (
            "consolidate must not merge subject-incompatible rows: "
            "destructive candidate admission follows the same identity rule "
            f"(rows={[r['id'] for r in rows]})")
        for row in rows:
            assert not (row["merged_from"] or ""), (
                "no subject-incompatible row may be recorded as merged into "
                "a survivor (contested/generated-summary rows keep their own "
                f"identity): merged_from={row['merged_from']!r} for "
                f"{row['id']}")

        # ---- real CONTESTED cluster (mixed negation polarity, same
        # subject): reported, never merged — the polarity guard survives
        # subject-awareness at maintenance time.
        ns = "project:s376/ac4-contested"
        a = s.add("always drain the pending queue before closeout", ns=ns,
                  tags="task:issue41")
        b = s.add("never drain the pending queue before closeout", ns=ns,
                  tags="task:issue41")
        result = s.run_store("consolidate", "--force", "--namespace", ns,
                             "--json")
        assert result.returncode == 0, result.stderr + result.stdout
        report_json = _last_json(result.stdout)
        contested = report_json.get("contested_clusters") or []
        assert contested, (
            "the polarity-contested pair must be REPORTED as a contested "
            f"cluster by consolidate (json={report_json})")
        assert all(not cluster.get("merged") for cluster in contested), (
            f"contested clusters must never merge by default: {contested}")
        assert s.live_count(ns) == 2, (
            "the contested polarity pair must stay 2 live rows")
        assert s.contradicts_link_between(a["id"], b["id"]) >= 1, (
            "the contested pair must keep its contradicts link")

        # ---- genuine >=3-member GENERATED SUMMARY (organize contract
        # preserved): three SAME-subject rows sharing a distinctive entity
        # form one topic; organize creates the summary row over the
        # compatible members and keeps every member live with its identity.
        # A subject-incompatible same-entity row is the NO-DESTRUCTIVE-ABSORB
        # guard: organize may never delete, supersede or retag it. The
        # shared #376 rule binds destructive candidate admission; organize
        # summaries are non-destructive.
        ns = "project:s376/ac4-summary"
        members = []
        for text in (
                "The XylathonQueue retry policy was rewritten during the "
                "autumn hardening week",
                "Load tests showed the XylathonQueue consumer lagging behind "
                "producers every night",
                "Documentation for the XylathonQueue was moved into the "
                "runbook chapter on queues"):
            members.append(s.add(text, ns=ns, tags="task:issue55"))
        incompatible = s.add(
            "The XylathonQueue incident postmortem belongs to a different "
            "task entirely", ns=ns, tags="task:issue66")
        result = s.run_store("organize", "--force", "--json")
        assert result.returncode == 0, result.stderr + result.stdout
        organize_json = _last_json(result.stdout)
        summaries = (organize_json.get("summaries") or {}).get("created", 0)
        assert summaries >= 1, (
            "organize must still generate the topic summary for a genuine "
            f">=3-member same-subject topic (json={organize_json})")
        rows = s.live_rows(ns)
        summaries_rows = [r for r in rows
                          if r["tags"] == "summary,topic"
                          and (r["source_ref"] or "").startswith("organize:")]
        assert summaries_rows, (
            "the generated summary row must be identifiable by its stable "
            "shape: tags 'summary,topic' and an 'organize:'-prefixed "
            f"source_ref (rows={[dict(r) for r in rows]})")
        summary = summaries_rows[0]
        member_ids = set((summary["source_ref"][len("organize:"):]).split(","))
        # Summary membership composition is not constrained by this contract.
        live_by_id = {r["id"]: r for r in rows}
        for m in members:
            assert m["id"] in member_ids, (
                "the generated summary must cover all >=3 COMPATIBLE "
                f"members (members={member_ids})")
            assert m["id"] in live_by_id, (
                "every summarized member must still be live after organize")
            assert live_by_id[m["id"]]["tags"] == "task:issue55", (
                "every summarized member must keep its task identity")
        # The no-destructive-absorb guard: the subject-incompatible
        # same-entity row remains LIVE (superseded_at IS NULL) with exactly
        # its own task tag.
        assert incompatible["id"] in live_by_id, (
            "the subject-incompatible same-entity row must remain LIVE "
            "(superseded_at IS NULL) — organize may never delete or "
            "supersede it")
        assert live_by_id[incompatible["id"]]["tags"] == "task:issue66", (
            "the subject-incompatible row must keep exactly its own task "
            f"identity: {live_by_id[incompatible['id']]['tags']!r}")
        # The summary row itself remains live.
        assert summary["id"] in live_by_id, (
            "the generated summary row itself must remain live "
            f"(summary={dict(summary)})")

        # ---- organize boundary leg (id-scoped: organize may legitimately
        # CREATE summary rows; the invariant is the survival and identity of
        # BOTH subject-incompatible rows from the consolidate ns).
        rows_after = {r["id"]: r for r in s.live_rows(
            "project:s376/ac4-consolidate")}
        for rid, expected_tags in ((c1["id"], "task:issue41"),
                                   (c2["id"], "task:issue42")):
            assert rid in rows_after, (
                "organize must not supersede or absorb a subject-"
                f"incompatible row ({rid} left the live set; survivors="
                f"{sorted(rows_after)})")
            assert rows_after[rid]["tags"] == expected_tags, (
                "both task identities must survive organize with their own "
                f"tags (row {rid}: {rows_after[rid]['tags']!r})")


# ---------------------------------------------------------------------------
# AC5 — no cross-task metadata contamination; taint floor and provenance kept;
# stable privacy-safe veto code.
# ---------------------------------------------------------------------------
def test_metadata_integrity():
    with _ScratchStore() as s:
        # ---- rejected cross-subject merge leaves the survivor untouched ---
        ns = "project:s376/ac5-veto"
        content = "metadata integrity probe statement"
        ev1 = s.write_evidence("s376 survivor evidence")
        ev2 = s.write_evidence("s376 incoming evidence")
        survivor = s.add(content, ns=ns, tags="task:issue41",
                         source_ref="session:synthetic-issue41",
                         confidence=0.6, taint="trusted_internal",
                         evidence=(ev1,))
        # The vetoed add runs through run_store so stdout/stderr stay
        # inspectable for the veto-code and privacy-safety assertions.
        veto_args = ["add", "--namespace", ns, "--type", "fact",
                     "--content", content, "--tags", "task:issue42",
                     "--source-ref", "session:synthetic-issue42",
                     "--signal", "reviewer", "--confidence", "0.9",
                     "--taint", "untrusted_web", "--evidence", ev2, "--json"]
        result = s.run_store(*veto_args)
        assert result.returncode == 0, result.stderr + result.stdout
        incoming = _last_json(result.stdout)
        combined_output = result.stdout + result.stderr
        warnings = incoming.get("warnings") or []
        warning_types = [w.get("type", "") for w in warnings if isinstance(w, dict)]
        # Stable, privacy-safe veto code (issue #376 scope: "Record a
        # stable, privacy-safe explanation for a subject veto").
        assert SUBJECT_VETO_CODE in warning_types or SUBJECT_VETO_CODE in combined_output, (
            f"a subject veto must record the STABLE machine-readable code "
            f"'{SUBJECT_VETO_CODE}' (frozen contract) in the write result "
            f"warnings or the command output; got warning types "
            f"{warning_types} and output: {combined_output[-400:]}")
        assert content not in combined_output, (
            "the veto explanation must be PRIVACY-SAFE: no veto surface may "
            "echo the vetoed row's content text")

        rows = s.live_rows(ns)
        assert len(rows) == 2, (
            "a rejected cross-subject merge must preserve the incoming "
            "content as its own live row")
        by_id = {r["id"]: r for r in rows}
        row_s = by_id[survivor["id"]]
        row_i = by_id[incoming["id"]]
        assert row_s["tags"] == "task:issue41", (
            "a subject veto must not union the incoming tags onto the "
            "survivor through ANY channel (dedup fold or link attribute "
            f"propagation): {dict(row_s)}")
        assert row_s["confidence"] == 0.6, (
            "a subject veto must not lift the survivor's confidence")
        assert row_s["signal"] == "test", row_s
        assert row_s["taint"] == "trusted_internal", (
            "a subject veto must not absorb the incoming taint (worst-of is "
            "for GENUINE dedups only)")
        assert row_s["retrieval_count"] == 0, (
            "a subject veto must not bump the survivor's retrieval counter")
        assert row_s["trust_score"] == 1.0, (
            "a subject veto must not move the survivor's trust")
        assert s.evidence_for(row_s["id"]) == [ev1], (
            "a subject veto must not attach the incoming evidence to the "
            "survivor")
        assert row_i["tags"] == "task:issue42", row_i
        assert row_i["content"] == content, (
            "the vetoed incoming statement must land verbatim")
        assert row_i["source_ref"] == "session:synthetic-issue42", row_i
        assert row_i["taint"] == "untrusted_web", row_i
        assert row_i["confidence"] == 0.9, row_i
        assert s.evidence_for(row_i["id"]) == [ev2], (
            "the vetoed incoming row keeps its OWN evidence")

        # ---- genuine dedup keeps the taint floor and provenance -----------
        ns = "project:s376/ac5-genuine"
        content = "genuine duplicate probe statement"
        ev3 = s.write_evidence("s376 genuine original evidence")
        ev4 = s.write_evidence("s376 genuine incoming evidence")
        keeper = s.add(content, ns=ns, confidence=0.6,
                       taint="trusted_internal", evidence=(ev3,))
        s.add(content, ns=ns, confidence=0.9, taint="untrusted_web",
              evidence=(ev4,))
        rows = s.live_rows(ns)
        assert len(rows) == 1, (
            "a genuine (identity-compatible) duplicate still merges")
        row = rows[0]
        assert row["id"] == keeper["id"]
        assert row["confidence"] == 0.9, (
            "genuine dedup upgrades confidence (unchanged behavior)")
        assert row["taint"] == "untrusted_web", (
            "genuine dedup preserves the established taint floor: the keeper "
            "absorbs the worst-of taint")
        assert s.evidence_for(row["id"]) == sorted([ev3, ev4]), (
            "genuine dedup preserves the ORIGINAL evidence provenance and "
            "attaches the incoming evidence")


# ---------------------------------------------------------------------------
# AC6 — bounded candidate work (semantic AND lexical), query counts, latency
# compared across namespace sizes; schema/sync round trip.
# ---------------------------------------------------------------------------
def _assert_cost_bound(cost, lane):
    sel10, sel60 = cost["n10"]["selects"], cost["n60"]["selects"]
    lat10, lat60 = cost["n10"]["seconds"], cost["n60"]["seconds"]
    assert sel60 <= sel10 + 8, (
        f"{lane}: per-add SELECT work must not scale with namespace size "
        f"(n10={sel10}, n60={sel60}; first selects n60="
        f"{cost['n60']['first_selects']})")
    assert sel60 <= 40, (
        f"{lane}: per-add SELECT work must stay bounded (n60={sel60}; "
        f"first selects={cost['n60']['first_selects']})")
    assert lat10 < 2.0 and lat60 < 2.0, (
        f"{lane}: a single in-process add against a seeded namespace must "
        f"stay under 2s (n10={lat10}s, n60={lat60}s)")
    assert lat60 <= lat10 + 1.0, (
        f"{lane}: latency must not scale with namespace size (n10={lat10}s, "
        f"n60={lat60}s)")
    if "candidates" in cost["n60"]:
        cand10 = cost["n10"]["candidates"]
        cand60 = cost["n60"]["candidates"]
        assert cand60 <= cand10 + 16, (
            f"{lane}: CANDIDATE evaluations must not scale with namespace "
            f"size even with an incompatible top candidate and a compatible "
            f"runner-up both above threshold (n10={cand10}, n60={cand60}; "
            f"knn calls n10={cost['n10']['knn_calls']} "
            f"n60={cost['n60']['knn_calls']}) — measured at the "
            "write._vec_knn_in_namespace seam, see the driver disclosure")
        assert cand60 <= 80, (
            f"{lane}: per-add CANDIDATE evaluations must stay bounded "
            f"(n60={cand60}) — a subject veto may widen the window by a "
            "constant, never scan the namespace")


def test_compatibility_and_cost():
    # ---- leg 1: LEXICAL (model-absent) candidate work ---------------------
    tmp = tempfile.mkdtemp(prefix="zmem-s376-cost-")
    try:
        env = _nomodel_env(os.path.join(tmp, "store.sqlite"))
        proc = subprocess.run(
            [PYTHON, "-X", "utf8", "-c", _COST_DRIVER], env=env,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600)
        assert proc.returncode == 0, proc.stderr + proc.stdout
        lexical = _last_json(proc.stdout)
    finally:
        shutil.rmtree(tmp, True)
    _assert_cost_bound(lexical, "lexical")

    # ---- leg 2: SEMANTIC (real-model) candidate work ----------------------
    report = _require_real_model_report()
    # The EFFECTIVE runtime threshold, read from the writer the driver
    # actually exercised (env-scrubbed default), not this file's constant.
    assert report["effective_dedup_threshold"] == 0.85, (
        "the real-model driver ran against a NON-default dedup threshold "
        f"(effective={report['effective_dedup_threshold']}); the frozen "
        "rubric is defined against the 0.85 storelib default — scrub "
        "ZMEM_DEDUP_THRESHOLD and re-run")
    assert report["effective_dedup_threshold"] == DEDUP_THRESHOLD, (
        "the local rubric pin and the runtime threshold drifted apart")
    _assert_cost_bound(report["cost_semantic"], "semantic")

    # ---- AC6 PRIMARY seam: storelib.write.SUBJECT_COMPAT_EVALS ------------
    # The frozen contract requires the implementation to expose
    # storelib.write.SUBJECT_COMPAT_EVALS (process-local int, incremented
    # once per subject-compatibility evaluation on the dedup/consolidation
    # decision paths); the KNN window metric above cannot observe
    # compatibility evaluations. The driver read it via getattr (absent ->
    # 0, recorded as seam_absent=true) before/after the measured contested
    # add. At base the attribute is absent, so this leg is red by absence —
    # the discriminating signature.
    seam = report["cost_semantic"]
    for key in ("n10", "n60"):
        entry = seam[key]
        assert not entry.get("seam_absent"), (
            f"semantic {key}: the frozen observability seam "
            "storelib.write.SUBJECT_COMPAT_EVALS is ABSENT — the "
            "implementation must expose this process-local counter (one "
            "increment per subject-compatibility evaluation on the "
            "dedup/consolidation decision paths)")
        delta = entry["seam_after"] - entry["seam_before"]
        assert delta >= 2, (
            f"semantic {key}: the measured contested add must evaluate "
            "subject compatibility at least twice — the incompatible "
            f"NEAREST candidate AND the compatible runner-up "
            f"(SUBJECT_COMPAT_EVALS delta={delta}, "
            f"before={entry['seam_before']}, after={entry['seam_after']})")
    delta10 = seam["n10"]["seam_after"] - seam["n10"]["seam_before"]
    delta60 = seam["n60"]["seam_after"] - seam["n60"]["seam_before"]
    assert delta60 <= delta10 + 16, (
        "semantic: subject-compatibility evaluations must not scale with "
        "namespace size (SUBJECT_COMPAT_EVALS delta10="
        f"{delta10}, delta60={delta60})")

    with _ScratchStore() as s:
        # ---- leg 3: CLI add latency stays within a generous bound ---------
        t0 = time.perf_counter()
        result = s.run_store(
            "add", "--namespace", "project:s376/cost/cli", "--type", "fact",
            "--content", "cli latency probe cold store", "--tags",
            "task:issue41", "--signal", "test", "--json")
        assert result.returncode == 0, result.stderr
        cli_seconds = time.perf_counter() - t0
        assert cli_seconds < 30.0, (
            "a single public-path add must stay well under 30s wall clock "
            f"(took {cli_seconds:.2f}s; lexical seeded adds: "
            f"n10={lexical['n10']['seconds']}s n60={lexical['n60']['seconds']}s"
            f"; semantic seeded adds: "
            f"n10={report['cost_semantic']['n10']['seconds']}s "
            f"n60={report['cost_semantic']['n60']['seconds']}s)")

        # ---- leg 4: schema/sync round trip preserves the identity boundary --
        ns = "project:s376/ac6-roundtrip"
        content = "round trip identity probe statement"
        s.add(content, ns=ns, tags="task:issue41",
              source_ref="session:synthetic-issue41")
        s.add(content, ns=ns, tags="task:issue42",
              source_ref="session:synthetic-issue42", no_dedup=True)
        s.add("round trip unique row with no task identity", ns=ns)
        out_path = os.path.join(s.tmp, "roundtrip.jsonl")
        result = s.run_store("export-jsonl", "--out", out_path)
        assert result.returncode == 0, result.stderr + result.stdout
        with open(out_path, encoding="utf-8") as fh:
            exported = [json.loads(line) for line in fh if line.strip()]
        exported_tags = sorted(
            str(rec.get("tags", "")) for rec in exported
            if rec.get("namespace") == ns)
        joined_tags = " ".join(exported_tags)
        assert "task:issue41" in joined_tags, (
            "export must carry the task identity metadata")
        assert "task:issue42" in joined_tags, (
            "export must carry BOTH distinct task identities "
            f"(exported tags: {exported_tags})")

        fresh_store = os.path.join(s.tmp, "roundtrip-store.sqlite")
        fresh_env = _nomodel_env(fresh_store)
        result = s.run_store("ingest-jsonl", "--in", out_path, env=fresh_env)
        assert result.returncode == 0, result.stderr + result.stdout
        live_after = s.live_rows(ns, store=fresh_store)
        assert len(live_after) == 3, (
            "the export/ingest round trip must preserve all three rows — "
            "re-ingesting a distinct-subject pair into a fresh store must "
            "not fold the second identity into the first")
        assert sorted(r["tags"] for r in live_after) == [
            "", "task:issue41", "task:issue42"], (
            "each row must round-trip with its OWN task identity intact "
            f"(got {sorted(r['tags'] for r in live_after)})")


# ---------------------------------------------------------------------------
# AC7 — frozen constructed + held-out corpus: false merges vs missed
# duplicates reported separately as a STRUCTURED report; the below-threshold
# paraphrase control stays a disclosed miss (a threshold-only change fails).
# ---------------------------------------------------------------------------
def test_held_out_evaluation():
    # Threshold pin: the issue forbids a general threshold reduction; 0.85 is
    # the storelib default and ZMEM_DEDUP_THRESHOLD is scrubbed in every leg.
    assert DEDUP_THRESHOLD == 0.85, (
        "the frozen rubric is defined against the 0.85 storelib default; a "
        "changed threshold means the rubric itself must be re-frozen")

    report = _require_real_model_report()
    # The rubric threshold is cross-checked against the EFFECTIVE runtime
    # value the real-model driver exercised (asserted in AC6; re-asserted
    # here because the rubric's classifications are threshold-relative).
    assert report["effective_dedup_threshold"] == DEDUP_THRESHOLD, (
        "the rubric classifications are defined against the 0.85 default; "
        f"the driver ran with effective={report['effective_dedup_threshold']}")
    corpus = {entry["case"]: entry for entry in report["corpus"]}
    held = {entry["case"]: entry for entry in report["heldout"]}
    assert set(corpus) == {p[0] for p in PINNED_CORPUS}, (
        "the frozen six-pair constructed corpus must be evaluated whole")

    distinct_expect_separate = {
        "task-closeout", "issue-number", "different-files",
        "heldout-pr-distinct"}

    rubric = {
        "threshold": DEDUP_THRESHOLD,
        "model": report["model"],
        "false_merges": [],
        "missed_duplicates": [],
        "below_threshold_controls": {},
        "oracle_vs_extracted": (
            "Subjects are ORACLE-SUPPLIED by the frozen fixtures (tags); "
            "no production subject extractor is measured here — extractor "
            "quality is #377's curator evaluation, not this rubric"),
    }
    for label, entry in sorted({**corpus, **held}.items()):
        cos, live_rows = entry["cosine"], entry["live_rows"]
        if label in distinct_expect_separate:
            if cos < DEDUP_THRESHOLD:
                raise AssertionError(
                    f"{label}: pinned distinct-subject pair fell below the "
                    f"dedup threshold (cosine={cos}) — the frozen model "
                    "inputs drifted; re-freeze before interpreting")
            if live_rows < 2 or entry.get("same_id"):
                rubric["false_merges"].append(
                    {"case": label, "cosine": cos, "live_rows": live_rows})
        elif label == BELOW_THRESHOLD_CONTROL:
            rubric["below_threshold_controls"][label] = {
                "cosine": cos, "live_rows": live_rows,
                "known_missed_paraphrase": True}
        else:  # same-subject pairs: merge exactly when above threshold
            expected = 1 if cos >= DEDUP_THRESHOLD else 2
            if live_rows != expected:
                rubric["missed_duplicates"].append({
                    "case": label, "cosine": cos, "live_rows": live_rows,
                    "expected_live_rows": expected})

    # The frozen control: pinned cosine 0.763633 stays a MISSED duplicate —
    # disclosed, never claimed as recall improvement.
    control = rubric["below_threshold_controls"][BELOW_THRESHOLD_CONTROL]
    assert control["cosine"] < DEDUP_THRESHOLD, (
        f"{BELOW_THRESHOLD_CONTROL} is the frozen below-threshold control "
        f"(pinned cosine 0.763633); got {control['cosine']}")
    assert control["live_rows"] == 2, (
        f"{BELOW_THRESHOLD_CONTROL} must REMAIN a disclosed missed duplicate "
        "(live_rows=2); merging it means the threshold was lowered, which "
        "does not satisfy issue #376")

    # Structured disclosure FIRST (always emitted — even on a failing run —
    # so the false-merge / missed-duplicate evidence is inspectable). stdout
    # is captured by plain pytest but visible with -s and in the
    # direct-python lane.
    print("\nsubject-dedup rubric report:\n" + json.dumps(rubric, indent=1))

    assert rubric["false_merges"] == [], (
        "FALSE MERGES (distinct explicit subjects collapsed): "
        f"{rubric['false_merges']} — every distinct-subject pair above the "
        "threshold must stay separate")
    assert rubric["missed_duplicates"] == [], (
        "MISSED DUPLICATES (same-subject pairs above threshold that stayed "
        f"separate): {rubric['missed_duplicates']}")

    # Oracle disclosure: these subjects were supplied by the frozen fixtures
    # (tags), not measured from a production extractor.
    for label, entry in sorted({**corpus, **held}.items()):
        assert "subjects" in entry, (
            f"{label}: the rubric must keep disclosing the ORACLE subject "
            "pair next to each measurement (oracle vs extracted identity "
            "differences stay visible)")


_ALL_TESTS = (
    test_distinct_subjects,
    test_identity_controls,
    test_compatible_runner_up,
    test_all_write_surfaces,
    test_metadata_integrity,
    test_compatibility_and_cost,
    test_held_out_evaluation,
)


if __name__ == "__main__":
    # Direct-python CI form (repo convention: no pytest required). The
    # pytest form runs the same module-level functions via the frozen AC
    # argvs; this loop gives the identical outcomes a pytest-free lane.
    #
    # Model-absent lane contract (documented above): when the pinned real
    # model is unavailable, the real-model legs fail with MODEL_UNAVAILABLE
    # signatures. On this DIAGNOSTIC lane the run exits 0 ONLY when every
    # failure is a MODEL_UNAVAILABLE signature and nothing else failed —
    # the documented reduced pass, never native approval (the provisioned
    # lane with the verified model is the only native pass). Any other
    # failure still exits 1.
    failures = []
    model_unavailable_only = True
    for fn in _ALL_TESTS:
        name = fn.__name__
        try:
            fn()
        except AssertionError as exc:
            # F-04/UR-1: classify by structured PREFIX, never substring —
            # only the unavailable branch raises messages that start with
            # "MODEL_UNAVAILABLE:"; a DRIVER_ERROR (or any other failure)
            # is a hard failure regardless of its message text.
            if not str(exc).startswith("MODEL_UNAVAILABLE:"):
                model_unavailable_only = False
            print(f"FAIL {name}: {exc}")
            traceback.print_exc()
        except Exception as exc:  # unexpected — still a failure, never a skip
            failures.append(name)
            model_unavailable_only = False
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\nRan {len(_ALL_TESTS)} tests")
    if failures:
        if model_unavailable_only:
            print(
                "MODEL_UNAVAILABLE-DIAGNOSTIC: every failure is a "
                "MODEL_UNAVAILABLE signature; the real-model legs did NOT "
                "run (diagnostic/incomplete lane — native approval requires "
                "the provisioned pinned model; see the docstring)."
            )
            sys.exit(0)
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        sys.exit(1)
    print("OK")
