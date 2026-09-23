"""Optional post-MMR cross-encoder rerank (issue #63, 8.6) extended with the
checked-in mini-pair-scorer profile, a 250 ms fail-open budget, and the
opt-in passive final-set seam (issue #125).

POLICY (load-bearing — do not weaken):
- Default OFF everywhere. Enablement requires ZMEM_CROSS_ENCODER to opt in.
- Explicit rerank fires ONLY on explicit CLI `recall` runs that also mutate
  telemetry: `cli_allowed` demands recall-without---no-bump. Passive rerank
  requires a SECOND opt-in, ZMEM_CROSS_ENCODER_PASSIVE exactly "1", plus the
  passive lane itself (for_injection + --no-bump); without it every hook
  surface (UserPromptSubmit / SubagentStart / PreCompact / SessionStart) and
  the Hermes prefetch remain structurally excluded.
- The `search` subcommand never evaluates this module at all (its dispatch
  omits the enablement parameter outright). Its Hermes/MCP aliases additionally
  pin --no-hybrid by byte-stable contract, so they stay excluded even if a
  future refactor routes them through the recall argv.
- Degrade is fail-open: a missing model file, missing deps, or any scorer
  exception returns the input order UNCHANGED and never fails the recall
  that asked for it. Every scorer attempt emits exactly ONE terminal reason
  line on stderr (`[zmem] cross-encoder reason=<...>`); the trivial
  early-exits (empty rows / single row / empty query) are not attempts and
  emit nothing.
- Model resolution: ZMEM_CROSS_ENCODER_MODEL (operator-supplied, operator
  vouched, sibling tokenizer.json) wins; otherwise the checked-in
  `mini-pair-scorer` profile resolves through `cross_encoder_profiles` on
  EVERY load and its file is checksum-gated by `verify_profile_file` before
  use. The profile URL ships EMPTY — profile download is disabled until
  ZMEM_CROSS_ENCODER_MODEL_URL supplies one, and even then a download
  happens only when ZMEM_MODEL_AUTODOWNLOAD is exactly "1" (call-time, one
  attempt, digest-verified, atomic replace; the loader never executes an
  unverified binary).
- Passive reordering is NOT promoted by the #125 PR: shadow mode
  (ZMEM_CROSS_ENCODER_SHADOW=1) is the passive evaluation mode. The
  promotion gate is recorded in PASSIVE_PROMOTION_GATE below and stays
  closed until the measured values it names exist.
"""

from __future__ import annotations

import contextlib
import math
import os
import sys
import time
from pathlib import Path

import cross_encoder_profiles as _profiles

# "1" turns the feature on. Any other value (including absent) means off.
ENABLE_ENV = "ZMEM_CROSS_ENCODER"
# Local ONNX cross-encoder file (pair-scoring architecture, e.g. an exported
# ms-marco-MiniLM reranker). Required for real scoring; without it or with an
# unreadable file the feature silently degrades to no-op.
MODEL_PATH_ENV = "ZMEM_CROSS_ENCODER_MODEL"
# Second opt-in for the passive injection lane (issue #125): must be EXACTLY
# "1"; every other value, including unset, keeps passive scoring off.
PASSIVE_ENV = "ZMEM_CROSS_ENCODER_PASSIVE"
# Shadow evaluation mode for the passive final-set seam: exactly "1" scores
# the final rows, logs rank deltas, and returns the ORIGINAL order.
SHADOW_ENV = "ZMEM_CROSS_ENCODER_SHADOW"
# Explicit download source for the profile model; empty falls back to the
# profile's own url (which ships empty = download disabled).
MODEL_URL_ENV = "ZMEM_CROSS_ENCODER_MODEL_URL"
# Exact-string "1" permits ONE call-time model download; everything else
# (including unset) is off and emits the autodownload-disabled state line.
AUTODOWNLOAD_ENV = "ZMEM_MODEL_AUTODOWNLOAD"
# Wall-clock budget for one rerank attempt (milliseconds). Validated as a
# base-10 non-negative integer; invalid text falls back to the default with
# a one-line warning.
CROSS_ENCODER_BUDGET_MS = 250
CROSS_ENCODER_BUDGET_ENV = "ZMEM_CROSS_ENCODER_BUDGET_MS"

# Passive-reordering promotion gate (issue #125 AC7 — RECORDED, NOT CLAIMED).
# Active reordering on the passive injection lane stays disabled until ALL of:
#   - the #155 real-corpus gate decision exists (it does: PR #218,
#     eval/real-corpus-2026-09-19.json), AND
#   - a #111 gold run WITH the reranker reports
#     precision_at_k > 0.8978333333333333 (the committed baseline value), AND
#   - that run's p95 rerank latency is <= 250 ms, AND
#   - a #129 measurement reports false_injection_rate <= 0.0.
# Until a future PR records those measured values and flips
# passive_reorder_promoted(), shadow mode is the ONLY passive evaluation mode.
PASSIVE_PROMOTION_GATE = {
    "reorder_enabled": False,
    "requires": "#155 decision; #111 precision_at_k > 0.8978333333333333; "
                "p95 latency <= 250; #129 false_injection_rate <= 0.0",
}


def passive_reorder_promoted() -> bool:
    """Whether the passive lane may REORDER (it may not this PR — shadow only)."""
    return bool(PASSIVE_PROMOTION_GATE.get("reorder_enabled"))


_TRUTHY = {"1", "true", "yes", "on"}

# Test injection seam (issue #63 8.6: "tests that a fake scorer can be
# injected"). `_scorer_fn(query, texts: list[str]) -> list[float] | None`.
_scorer_fn = None

# When a capture sink is installed, `_emit_reason` records instead of
# printing — the passive seam re-emits the captured reason itself so each
# call still produces exactly one terminal line (issue #125 design 6).
_reason_sink: list | None = None


def set_scorer(fn) -> None:
    """Install a scorer override. Pass a callable `(query, texts)->scores|None`
    (tests inject deterministic fakes here). Call ``set_scorer(None)`` to
    restore production behavior."""
    global _scorer_fn
    _scorer_fn = fn


def enabled() -> bool:
    """Env parse ONLY — deliberately performs zero I/O so dispatch-time gating
    is free even for the hot hook paths that must never pay for this feature."""
    return (os.environ.get(ENABLE_ENV, "").strip().lower() in _TRUTHY)


def cli_allowed(*, no_bump: bool, no_hybrid: bool,
                for_injection: bool = False) -> bool:
    """The single decision point the recall dispatch consults.

    EXPLICIT lane (for_injection=False, the unchanged #63 truth table):
      True iff ZMEM_CROSS_ENCODER opted in, not --no-bump, not --no-hybrid.
    PASSIVE lane (for_injection=True, issue #125):
      True iff ZMEM_CROSS_ENCODER opted in AND --no-bump (the passive lane is
      always no-bump) AND ZMEM_CROSS_ENCODER_PASSIVE is exactly "1".
      `no_hybrid` does not change the passive decision — rerank changes order
      only and the shadow lane returns the original order regardless, while
      the search aliases' --no-hybrid byte-stable contract is a distinct
      explicit-lane concern.
    """
    if for_injection:
        return (enabled() and no_bump
                and os.environ.get(PASSIVE_ENV, "0") == "1")
    return enabled() and not no_bump and not no_hybrid


def _emit_stderr(line: str) -> None:
    print(line, file=sys.stderr)


def emit_reason(reason: str) -> None:
    """The single terminal reason line for one scorer attempt."""
    global _reason_sink
    if _reason_sink is not None:
        _reason_sink.append(reason)
        return
    _emit_stderr(f"[zmem] cross-encoder reason={reason}")


@contextlib.contextmanager
def capture_reason():
    """Record the attempt's terminal reason instead of printing it.

    Yields a list that receives exactly the reason string when (and only
    when) maybe_rerank makes a scorer attempt; stays empty for the trivial
    early-exits, which are not attempts (issue #125 A3)."""
    global _reason_sink
    prev = _reason_sink
    sink: list = []
    _reason_sink = sink
    try:
        yield sink
    finally:
        _reason_sink = prev


def resolve_model_paths() -> tuple[str | None, str | None]:
    """(model_path, tokenizer_path) per the #125 precedence chain:
    ZMEM_CROSS_ENCODER_MODEL (sibling tokenizer) first; otherwise
    ZMEM_MODELS_DIR, then the shared embedding models dir, + the profile's
    model file. (None, None) when no directory can be resolved."""
    explicit = (os.environ.get(MODEL_PATH_ENV) or "").strip()
    if explicit:
        return explicit, os.path.join(os.path.dirname(explicit),
                                      "tokenizer.json")
    models_dir = (os.environ.get("ZMEM_MODELS_DIR") or "").strip()
    if not models_dir:
        try:
            import embeddings as _embeddings
            models_dir = str(_embeddings._resolve_models_dir())
        except Exception:
            return None, None
    try:
        profile = _profiles.resolve_profile()
        model = _profiles.profile_model_path(Path(models_dir), profile)
    except ValueError:
        return None, None
    return str(model), str(model.parent / profile["tokenizer_file"])


def _download_profile_model(model_path: str, profile: dict) -> str:
    """One call-time download attempt for the profile model.

    Returns "ok" | "missing-model" | "load-error". ZMEM_MODEL_AUTODOWNLOAD
    must be EXACTLY "1" (anything else emits the state line and disables);
    the URL comes from ZMEM_CROSS_ENCODER_MODEL_URL, falling back to the
    profile url (empty = disabled). Bytes land in a unique `.part` sibling,
    are digest-checked ON DISK, and only then atomically replace the
    destination; the `.part` path is removed in `finally` on every path.
    """
    if os.environ.get(AUTODOWNLOAD_ENV, "0") != "1":
        _emit_stderr("[zmem] cross-encoder state=autodownload-disabled")
        return "missing-model"
    url = ((os.environ.get(MODEL_URL_ENV) or "").strip()
           or (profile.get("url") or ""))
    if not url:
        return "missing-model"
    from urllib.request import urlopen

    part: str | None = None
    try:
        dest = Path(model_path)
        # The models dir may not exist yet on a fresh install; create it
        # only once the download is actually permitted.
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = str(dest.with_name(
            f"{dest.name}.{os.getpid()}.{os.urandom(4).hex()}.part"))
        with urlopen(url, timeout=30) as resp:
            data = resp.read()
        with open(part, "wb") as fh:
            fh.write(data)
        digest = _stream_sha256(part)
        if digest != (profile.get("sha256") or "").lower():
            return "load-error"
        os.replace(part, model_path)
        part = None
        return "ok"
    except Exception:
        return "load-error"
    finally:
        if part is not None:
            try:
                if os.path.exists(part):
                    os.unlink(part)
            except OSError:
                pass


def _stream_sha256(path: str) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_SCORER_CACHE: dict = {}

# Why the last _local_scorer() build returned None ("missing-model" or
# "load-error") — maybe_rerank re-emits it as the terminal reason so a URL
# failure reports load-error while an absent/disabled model reports
# missing-model (issue #125 design 2/4).
_last_load_failure: str | None = None


def _local_scorer():
    """Build and cache a pair-logit scorer from the resolved model
    (cache keyed by (resolved_path, st_mtime_ns); value carries
    (mtime_ns, score) so the eviction seam can still match the closure).

    Returns None — rather than raising — on every failure mode: no
    resolvable model, file missing (after at most one profile-path
    autodownload attempt), checksum-gate refusal on the profile path,
    missing onnxruntime/tokenizers, session error. Negative results are NOT
    cached (the operator may install/replace the model between commands);
    positive results are cached per exact (path, mtime_ns) and a replaced
    model creates a new value.

    SCORING-SHAPE CONTRACT (unchanged): candidates are PAIR-encoded jointly
    with the query (`tok.encode(query, candidate)`), inputs are the
    `input_ids`/`attention_mask` tensors, padding/truncation 128. Degrade
    semantics are unchanged: any build/score failure returns the input
    order untouched.
    """
    global _last_load_failure
    model_path, tok_path = resolve_model_paths()
    if not model_path:
        _last_load_failure = "missing-model"
        return None
    explicit = bool((os.environ.get(MODEL_PATH_ENV) or "").strip())
    _last_load_failure = None
    if not explicit:
        # Profile path: checksum-gate BEFORE load, and offer exactly one
        # call-time download attempt when the file is absent.
        try:
            profile = _profiles.resolve_profile()
        except ValueError:
            _last_load_failure = "missing-model"
            return None
        if not os.path.isfile(model_path):
            outcome = _download_profile_model(model_path, profile)
            if outcome != "ok" or not os.path.isfile(model_path):
                _last_load_failure = outcome if outcome != "ok" else "load-error"
                return None
        if not _profiles.verify_profile_file(Path(model_path), profile):
            _last_load_failure = "load-error"
            return None
    elif not os.path.isfile(model_path):
        _last_load_failure = "missing-model"
        return None
    try:
        mtime_ns = os.stat(model_path).st_mtime_ns
    except OSError:
        _last_load_failure = "missing-model"
        return None
    key = (model_path, mtime_ns)
    cached = _SCORER_CACHE.get(key)
    if cached is not None:
        return cached[1]

    # Imports live in THIS scope (not a nested builder) so the `score`
    # closure below can see them: closure cells resolve per-frame, and an
    # import hidden inside a helper would make every score() call raise
    # NameError -> silent no-rerank forever (final-critic finding).
    #
    # PAIR ENCODING CONTRACT (zax-review round B1): a cross-encoder scores the
    # QUERY+CANDIDATE SEQUENCE JOINTLY. The candidate's ids MUST reach the
    # session, or every row yields the identical query-only score vector and
    # rerank is a permanent no-op behind the degrade swallow. We therefore use
    # the tokenizers library's pair form tok.encode(query, t) exactly as
    # MS-MARCO-class exporters expect. Inputs are plain python int lists —
    # ORT converts them, and mocks/tests stay trivially inspectable.
    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer
        # TOCTOU parity with embeddings.py (zax-review follow-up): stat for
        # cache freshness, then load ONE buffer for the session so a swap
        # between freshness check and construction cannot take effect here.
        with open(model_path, "rb") as fh:
            model_bytes = fh.read()
        sess = ort.InferenceSession(model_bytes)
        if not tok_path or not os.path.isfile(tok_path):
            return None
        tok = Tokenizer.from_file(tok_path)
        tok.enable_padding(length=128)
        tok.enable_truncation(max_length=128)

        def score(query: str, texts: list[str]):
            pair_ids: list[list[int]] = []
            pair_mask: list[list[int]] = []
            for t in texts:
                enc = tok.encode(query, t)
                pair_ids.append(list(enc.ids))
                pair_mask.append(list(enc.attention_mask))
            out = sess.run(None, {
                "input_ids": pair_ids,
                "attention_mask": pair_mask,
            })
            logits = out[0]
            rows = logits.tolist() if hasattr(logits, "tolist") else logits
            if rows and isinstance(rows[0], (list, tuple)):
                return [float(r[0]) for r in rows]
            return [float(r) for r in rows]

        # A replaced model (new mtime_ns) must not accumulate stale keys.
        for stale in [k for k in _SCORER_CACHE if k[0] == model_path]:
            _SCORER_CACHE.pop(stale, None)
        _SCORER_CACHE[key] = (mtime_ns, score)
        return score
    except Exception:
        # Opt-in operator diagnostics without weakening the degrade contract.
        if os.environ.get("ZMEM_CE_DEBUG_TRACEBACK") == "1":
            import traceback as _tb
            _tb.print_exc()
        _last_load_failure = "load-error"
        return None


def _parse_budget_ms() -> int:
    """ZMEM_CROSS_ENCODER_BUDGET_MS as a base-10 non-negative integer.
    Unset -> 250 silently; empty/non-integer/non-finite/negative text ->
    the exact warning line and 250."""
    raw = os.environ.get(CROSS_ENCODER_BUDGET_ENV)
    if raw is None:
        return CROSS_ENCODER_BUDGET_MS
    try:
        value = int(raw.strip(), 10)
    except (TypeError, ValueError):
        value = -1
    if value < 0:
        _emit_stderr("[zmem] WARNING: invalid ZMEM_CROSS_ENCODER_BUDGET_MS; "
                     "using 250 ms")
        return CROSS_ENCODER_BUDGET_MS
    return value


def maybe_rerank(query: str, rows: list, *, clock=None) -> list:
    """Rerank `rows` (recall result dicts exposing 'content') by cross-encoder
    score. All-or-nothing: valid scores reorder descending (stable, ties keep
    original order); anything unavailable/invalid returns `rows` unchanged.
    Scores NEVER leak into the returned dicts — outputs stay byte-shaped as if
    rerank never ran.

    Issue #125 additions: a 250 ms (default, env-overridable) wall-clock
    budget sampled from `clock` (default time.monotonic) before scorer
    resolution, after resolution, after the result, and after each
    score-vector element; all-or-nothing FINITE-real validation; and exactly
    one terminal reason line per attempt
    (`[zmem] cross-encoder reason=<applied|missing-model|load-error|invalid-score|budget-exhausted>`).
    A zero budget emits one
    `budget-exhausted` reason and returns the original list without any
    model construction."""
    if not rows or len(rows) == 1 or not query:
        return rows
    budget_ms = _parse_budget_ms()
    if clock is None:
        now = time.monotonic
    elif callable(clock):
        now = clock
    else:
        samples = iter(clock)

        def now():
            return next(samples)

    start = now()
    if budget_ms <= 0:
        emit_reason("budget-exhausted")
        return list(rows)

    def expired() -> bool:
        return (now() - start) >= budget_ms / 1000.0

    fn = _scorer_fn
    if fn is None:
        fn = _local_scorer()
        if expired():
            emit_reason("budget-exhausted")
            return rows
        if fn is None:
            emit_reason(_last_load_failure or "missing-model")
            return rows
    try:
        texts = [r.get("content") or "" for r in rows]
        raw = fn(query, texts)
        if expired():
            emit_reason("budget-exhausted")
            return rows
        if raw is None or len(raw) != len(rows):
            emit_reason("invalid-score")
            return rows
        try:
            scores = [float(x) for x in raw]
        except (TypeError, ValueError):
            emit_reason("invalid-score")
            return rows
        for value in scores:
            if not math.isfinite(value):
                emit_reason("invalid-score")
                return rows
            if expired():
                emit_reason("budget-exhausted")
                return rows
        scored = list(zip(scores, range(len(rows)), rows))
        scored.sort(key=lambda t: (-t[0], t[1]))
        emit_reason("applied")
        return [r for _s, _i, r in scored]
    except Exception:
        # Degrade unconditionally — issue 8.6: missing/broken model must not
        # fail the recall that requested rerank. Additionally evict a
        # PRODUCTION scorer entry that just threw: a build that succeeds but
        # scores wrong-shaped models must not stay pinned forever (reviewer
        # round: cache-pinning gap).
        if fn is not _scorer_fn:
            for key in [k for k, val in _SCORER_CACHE.items()
                        if val[1] is fn]:
                _SCORER_CACHE.pop(key, None)
        emit_reason("load-error")
        return rows
