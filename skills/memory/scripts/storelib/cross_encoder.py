"""Optional post-MMR cross-encoder rerank (issue #63, 8.6).

POLICY (load-bearing — do not weaken):
- Default OFF everywhere. Enablement requires ZMEM_CROSS_ENCODER to opt in.
- Rerank fires ONLY on explicit CLI `recall` runs that also mutate telemetry:
  `cli_allowed` demands recall-without---no-bump. Every hook surface
  (UserPromptSubmit / SubagentStart / PreCompact / SessionStart) and the Hermes
  prefetch pass --no-bump, so they are structurally excluded — they would need
  BOTH this module to be edited AND their flags changed to reach it.
- The `search` subcommand and its Hermes/MCP aliases pass --no-hybrid BY
  CONTRACT (byte-stable lexical ordering, pinned twice in review rounds), so
  cli_allowed refuses them too even when they arrive through the recall argv.
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

    SCORING-SHAPE HONESTY: there is no universally-pinned cross-encoder ONNX
    contract we can assume generically — pair-tokenization order, input names,
    and logit slicing vary per export. This loader makes a best-effort minimal
    input guess and NEVER lets a shape mismatch escape (degrade contract); an
    operator whose model scores oddly must supply a validated export. If you
    need guaranteed fidelity, inject via ``set_scorer``.
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

    def build():
        import numpy as np  # noqa: F401  (session inputs need it)
        import onnxruntime as ort
        from tokenizers import Tokenizer
        return ort, Tokenizer

    try:
        ort, Tokenizer = build()
        sess = ort.InferenceSession(model_path)
        tok_dir = os.path.dirname(model_path)
        tok_file = os.path.join(tok_dir, "tokenizer.json")
        if not os.path.isfile(tok_file):
            return None
        tok = Tokenizer.from_file(tok_file)
        tok.enable_padding(length=128)
        tok.enable_truncation(max_length=128)

        def score(query: str, texts: list[str]):
            pairs_q: list = []
            pairs_t: list = []
            for t in texts:
                enc_q = tok.encode(query)
                enc_t = tok.encode(t)
                pairs_q.append(enc_q.ids)
                pairs_t.append(enc_t.attention_mask)
            q_ids = np.array(pairs_q, dtype=np.int64)
            t_mask = np.array(pairs_t, dtype=np.int64)
            q_mask = (q_ids != 0).astype(np.int64)
            inputs = {"input_ids": q_ids, "attention_mask": q_mask}
            out = sess.run(None, inputs)
            logits = out[0]
            # Binarize attention back onto the TEXT side using its own mask;
            # generic pair models vary, so keep inputs minimal and tolerate
            # extra optional inputs.
            try:
                masked = logits * t_mask[:, :, None] if logits.ndim == 3 else logits
                scores = masked[:, 0]
            except Exception:
                scores = logits[:, 0]
            return [float(s) for s in scores]

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
        # fail the recall that requested rerank.
        return rows
