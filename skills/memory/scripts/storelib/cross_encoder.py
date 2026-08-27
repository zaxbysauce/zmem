"""Optional post-MMR cross-encoder rerank (issue #63, 8.6).

POLICY (load-bearing — do not weaken):
- Default OFF everywhere. Enablement requires ZMEM_CROSS_ENCODER to opt in.
- Rerank fires ONLY on explicit CLI `recall` runs that also mutate telemetry:
  `cli_allowed` demands recall-without---no-bump. Every hook surface
  (UserPromptSubmit / SubagentStart / PreCompact / SessionStart) and the Hermes
  prefetch pass --no-bump, so they are structurally excluded — they would need
  BOTH this module to be edited AND their flags changed to reach it.
- The `search` subcommand never evaluates this module at all (its dispatch
  omits the enablement parameter outright), and its Hermes/MCP aliases pin
  --no-hybrid by byte-stable contract, so the recall-argv gate refuses those
  too if they ever route differently.
- Degrade is silent and total: a missing model file, missing deps, or any
  scorer exception returns the input order UNCHANGED and never fails the
  recall that asked for it.
- No named public model profile ships here: nothing on Hugging Face offers a
  locally-verifiable cross-encoder artifact we could pin like `embed_profiles`
  does, so per the issue this stays an operator-supplied LOCAL FILE via
  ZMEM_CROSS_ENCODER_MODEL. No network access exists anywhere in this module.
"""

from __future__ import annotations

import os

# "1" turns the feature on. Any other value (including absent) means off.
ENABLE_ENV = "ZMEM_CROSS_ENCODER"
# Local ONNX cross-encoder file (pair-scoring architecture, e.g. an exported
# ms-marco-MiniLM reranker). Required for real scoring; without it or with an
# unreadable file the feature silently degrades to no-op.
MODEL_PATH_ENV = "ZMEM_CROSS_ENCODER_MODEL"

_TRUTHY = {"1", "true", "yes", "on"}

# Test injection seam (issue #63 8.6: "tests that a fake scorer can be
# injected"). `_scorer_fn(query, texts: list[str]) -> list[float] | None`.
_scorer_fn = None


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


def cli_allowed(*, no_bump: bool, no_hybrid: bool) -> bool:
    """The single decision point the recall dispatch consults.

    True iff the current invocation is an EXPLICIT hybrid-capable recall:
      - ZMEM_CROSS_ENCODER opted in, AND
      - not --no-bump  (excludes every passive hook/prefetch caller), AND
      - not --no-hybrid (excludes search & its aliases' byte-stable contract).
    Both exclusions are belt-and-suspenders: rerank changes ORDER only, but the
    passive surfaces promise deterministic byte-stable envelopes, and the bump
    surfaces are exactly where 'explicit user ask' semantics live.
    """
    return enabled() and not no_bump and not no_hybrid


_SCORER_CACHE: dict = {}


def _local_scorer():
    """Build and cache a pair-logit scorer from the configured local ONNX
    model (dict keyed by resolved path; value carries (mtime, score)).
    configured local ONNX model.

    Returns None — rather than raising — on every failure mode: env unset,
    file missing/unreadable, missing onnxruntime/tokenizers, session error.
    Negative results are NOT cached (the operator may install/replace the
    model between commands); positive results are cached and invalidated by
    file mtime changes.

    SCORING-SHAPE CONTRACT: candidates are PAIR-encoded jointly with the query
    (`tok.encode(query, candidate)`), so the session receives every candidate's
    own ids — a query-only feed is the permanent-no-op bug class this round
    closes. Input names are pinned to the two tensors virtually every
    exported bert/reranker accepts (`input_ids`, `attention_mask`); a model
    needing different names should be wrapped via ``set_scorer`` instead.
    Degrade semantics are unchanged: any build/score failure returns the
    input order untouched.
    """
    model_path = (os.environ.get(MODEL_PATH_ENV) or "").strip()
    if not model_path or not os.path.isfile(model_path):
        return None
    try:
        mtime = os.stat(model_path).st_mtime
    except OSError:
        return None
    cached = _SCORER_CACHE.get(model_path)
    if cached is not None and cached[0] == mtime:
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
        tok_dir = os.path.dirname(model_path)
        tok_file = os.path.join(tok_dir, "tokenizer.json")
        if not os.path.isfile(tok_file):
            return None
        tok = Tokenizer.from_file(tok_file)
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

        _SCORER_CACHE[model_path] = (mtime, score)
        return score
    except Exception:
        return None


def maybe_rerank(query: str, rows: list):
    """Rerank `rows` (recall result dicts exposing 'content') by cross-encoder
    score. All-or-nothing: valid scores reorder descending (stable, ties keep
    original order); anything unavailable/invalid returns `rows` unchanged.
    Scores NEVER leak into the returned dicts — outputs stay byte-shaped as if
    rerank never ran."""
    if not rows or len(rows) == 1 or not query:
        return rows
    fn = _scorer_fn
    if fn is None:
        fn = _local_scorer()
        if fn is None:
            return rows
    try:
        texts = [r.get("content") or "" for r in rows]
        raw = fn(query, texts)
        if raw is None or len(raw) != len(rows):
            return rows
        scored = list(zip(raw, range(len(rows)), rows))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [r for _s, _i, r in scored]
    except Exception:
        # Degrade unconditionally — issue 8.6: missing/broken model must not
        # fail the recall that requested rerank. Additionally evict a
        # PRODUCTION scorer entry that just threw: a build that succeeds but
        # scores wrong-shaped models must not stay pinned forever (reviewer
        # round: cache-pinning gap).
        if fn is not _scorer_fn:
            for key in [k for k, val in _SCORER_CACHE.items() if val[1] is fn]:
                _SCORER_CACHE.pop(key, None)
        return rows
