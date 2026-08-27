#!/usr/bin/env python
"""Embedding generation for ZMem using a bundled ONNX model.

Loads all-MiniLM-L6-v2 (384-dim) from the plugin models directory, generates
L2-normalized embeddings via ONNX Runtime. Runs fully offline once the model
is on disk — no network calls at embed time.

Optional dependency: if onnxruntime or tokenizers is not installed, or the model
file is missing (and cannot be lazy-downloaded — see `_try_download_model`),
all functions return None and callers should degrade gracefully to FTS5-only
recall / lexical-overlap clustering (see store.py `consolidate`).

Embedding profiles (issue #63, 8.2/8.5): ZMEM_EMBED_PROFILE selects a row from
``embed_profiles.PROFILES``. The default/unset profile (`minilm`) behaves
exactly as before this module gained profile awareness. The `fake` profile
returns deterministic 16-dim placeholder vectors with no dependencies, files,
or network — for model-absent tests/CI only (doctor warns on real stores).
Model facts (hf_id / dim / sha256) live in ``embed_profiles`` — this module
no longer owns them.

The model file itself (skills/memory/models/minilm.onnx, ~90MB) is NOT
committed to git (Phase 10, PLAN.md §7-P10) — it's gitignored and either
already present on disk from a prior install, lazy-downloaded on first use
(opt-in only, via ZMEM_MODEL_AUTODOWNLOAD=1 — never automatic, to preserve
zero-cloud-dependency by default), or simply absent (degraded mode). A full
git-history rewrite to purge the 90MB blob already committed on this
branch's history is a disruptive, user-opt-in follow-up — NOT done here
(see PLAN.md).
"""

from __future__ import annotations

import hashlib
import os
import struct
import sys
from pathlib import Path

# Profile registry (issue #63). Same directory, stdlib-only, no circular
# imports. All model identity constants below are now sourced from it.
import embed_profiles as _profiles

# --- Lazy globals (populated on first use) ---
_session = None
_tokenizer = None
_model_available: bool | None = None
# Which profile's verdict `_model_available` reflects. Profile awareness means
# the availability verdict is PER-PROFILE: without the key, an in-process env
# switch (tests, long-lived Hermes server) would serve a stale minilm verdict
# to fake or vice versa.
_profile_cache_key: str | None = None
# None = not yet checked; True = checksum verified at load; False = mismatch
# (model present but rejected). availability_status() surfaces this so doctor
# does not report a checksum-rejected model as healthy (cubic-2, #36 M15).
_model_checksum_ok: bool | None = None
# True if the model PASSED checksum but then FAILED to load (corrupt
# tokenizer.json, unparseable ONNX, etc.). Distinct from a checksum mismatch so
# availability_status can report an accurate reason (cubic-2 final-critic).
_model_load_failed: bool = False
_MODEL_DIM = 384

# sha256 of the Xenova ONNX export bundled/downloaded as minilm.onnx. Now a
# RE-EXPORT of the registry value so `embed_profiles` stays the single source
# of truth; kept as this module attribute because tests monkeypatch it here and
# `verify_checksum` resolves its default at call time via this name (#36 M15;
# issue #63 8.1 keeps that contract intact).
_MODEL_SHA256 = _profiles.MINILM_SHA256

# Default source for a lazy download when the model file is absent. This is
# the widely-used Xenova ONNX export of all-MiniLM-L6-v2 (same architecture/
# dim, but NOT guaranteed byte-identical to `_MODEL_SHA256` — different ONNX
# export toolchains/opsets produce different bytes for the same weights).
# Override with ZMEM_MODEL_URL to point at a build that matches the pinned
# checksum, or place the file manually at <models_dir>/minilm.onnx. Either
# way, a checksum mismatch never crashes — it just falls through to the
# no-embeddings path (see `_try_download_model`).
_DEFAULT_MODEL_URL = (
    "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx"
)

# Resolved fresh on every call so tests and long-lived servers observe env
# changes without cache surprises. An unknown profile name returns "" here
# (treated as unavailable); the CLI refuses it with exit 2 long before any
# embed attempt, so this path is defense-in-depth, not primary validation.
def current_profile_name(environ=None) -> str:
    try:
        return _profiles.resolve_active_profile(environ)
    except _profiles.ProfileError:
        return ""


_fake_warned_once = False


def warn_fake_active(profile_name: str | None = None) -> None:
    """One-time-per-process operator guard for the `fake` profile.

    Called by WRITE surfaces (add/update/ingest) before they embed: hooks'
    short-lived recall processes stay quiet on read paths, but any store that
    starts accumulating fake vectors announces itself loudly on stderr — a
    forgotten ZMEM_EMBED_PROFILE=fake export must never silently corrupt a
    real store with placeholder vectors.

    `profile_name` lets EXPLICIT conversions (`reembed --all --profile fake`)
    pass the profile actually being applied — the environment may still be
    unset/minilm there, and keying the warning off env alone was the silent-
    destruction hole from the zax review round B2.
    """
    global _fake_warned_once
    target = profile_name if profile_name is not None else current_profile_name()
    if _fake_warned_once or target != "fake":
        return
    _fake_warned_once = True
    try:
        print(
            "[zmem] WARNING: ZMEM_EMBED_PROFILE=fake is active — writes will "
            "store deterministic 16-dim PLACEHOLDER vectors, not semantic "
            "embeddings. This profile is for model-absent tests/CI only; unset "
            "the variable before real use.",
            file=sys.stderr,
        )
    except Exception:
        pass


# The plugin's bundled models directory, relative to this script. Kept as a
# module-level constant so tests can redirect it deterministically without
# touching the real file layout.
_BUNDLED_MODELS_DIR = Path(__file__).parent.parent / "models"


def _models_dir_usable(path: Path | None) -> bool:
    """Cheap, never-raising check that a models dir can serve the embedding
    model. The model file is the only one whose absence makes embeddings
    unavailable at load time, so its presence is the discriminator used to
    decide whether a candidate dir is worth preferring. Everything else (token
    consistency, checksum, importability) is validated later by the loaders."""
    if path is None:
        return False
    try:
        return (path / "minilm.onnx").is_file()
    except OSError:
        return False


def _resolve_models_dir() -> Path:
    """Resolve the models directory.

    ZMEM_MODELS_DIR overrides the default models directory. It is supported
    both as a test affordance (tests point it at an empty/missing dir so they
    never touch a real installed model) AND as a production configuration
    knob (point it at a populated, checksum-verified models dir, e.g. a shared
    model cache across checkouts).

    When ZMEM_MODELS_DIR is unset, prefer the plugin's bundled models
    directory. If the bundled dir has no model file, fall back to the box-wide
    shared models cache (the 'models' sibling of the store data dir) so
    embeddings keep working without any env var on hosts where the installed
    model lives in the shared cache but not in this checkout. If neither has a
    model, return the bundled dir (the pre-fallback default) so downstream
    code reports the usual model_file_missing state instead of raising.
    """
    override = os.environ.get("ZMEM_MODELS_DIR")
    if override:
        return Path(override)
    bundled = _BUNDLED_MODELS_DIR
    if _models_dir_usable(bundled):
        return bundled
    try:
        import host  # local import: avoid import-time coupling at module load
        shared = Path(host.resolve_store_path()).parent / "models"
        if _models_dir_usable(shared):
            return shared
    except Exception:
        pass  # never raise; fall through to the bundled default below
    return bundled


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_deep_ck_cache: dict = {}


def verify_checksum_cached(path: Path, expected: str | None = None):
    """Deep hash of `path` with POSITIVE-result memoization keyed on
    (mtime, size).

    Doctor's diagnostic pass needs an authoritative pin verdict even when no
    load has happened yet this process — but re-hashing a ~90 MB model on
    every doctor invocation is wasted I/O for the healthy case. A VERIFIED
    file whose (mtime,size) is unchanged skips the rehash; mismatches are
    NEVER cached, so tampering stays instantly visible and repairing the file
    flips back to verified once its stats change.
    Returns True / False / None (file unreadable), like a three-state probe.
    """
    try:
        stt = path.stat()
        key = (str(path), stt.st_mtime_ns, stt.st_size)
    except OSError:
        return None
    cached = _deep_ck_cache.get(key)
    if cached is not None:
        return cached
    verdict = bool(verify_checksum(path, expected))
    if verdict:
        _deep_ck_cache[key] = True
    return verdict


def verify_checksum(path: Path, expected: str | None = None) -> bool:
    """True iff `path` exists and its sha256 matches `expected`. Never raises.

    `expected` defaults to the live value of the module-level `_MODEL_SHA256`
    (looked up at call time, NOT bound at function-definition time — this is
    what lets tests override it via monkeypatching the module attribute).
    """
    if expected is None:
        expected = _MODEL_SHA256
    try:
        return path.is_file() and _sha256_file(path) == expected
    except OSError:
        return False


def _redact_url_for_logging(url: str) -> str:
    """Return `url` with any credentials/tokens stripped, safe to print/log.

    `ZMEM_MODEL_URL` is user/environment-controlled and can legitimately carry
    credentials (`https://user:token@host/path`) or a presigned-style query
    string (`?sig=...`, `?token=...`). Neither should ever reach stderr/logs.
    Keeps the message actionable (scheme, host, and path survive) while
    dropping userinfo entirely and redacting the query STRING AS A WHOLE.
    Uses `urllib.parse` rather than a regex so this can't be fooled by
    unusual-but-valid URL syntax. Never raises: an unparseable `url` is
    returned as a fixed placeholder rather than echoed verbatim.

    The WHOLE query goes, not just the values. An earlier version preserved
    parameter names and replaced only values (`?sig=REDACTED`), which still
    leaks a valueless query token: `?<token>` contains no `=`, so the secret
    IS the parameter name and was echoed verbatim. Parameter names are
    user-controlled and cannot be assumed non-sensitive, and knowing which
    parameters were present is not worth having to reason about which of them
    happen to be safe.
    """
    import urllib.parse

    try:
        parts = urllib.parse.urlsplit(url)
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        redacted_query = "REDACTED" if parts.query else ""
        return urllib.parse.urlunsplit(
            (parts.scheme, netloc, parts.path, redacted_query, "")
        )
    except Exception:
        return "<redacted>"


def _try_download_model(
    model_path: Path, url: str | None = None, expected_sha256: str | None = None
) -> bool:
    """Best-effort lazy download of the model file with checksum verification.

    Fail-open by design: any exception, a network error, or a checksum
    mismatch returns False and leaves the caller to degrade to the
    no-embeddings path. Never raises, never leaves a partial/corrupt file at
    `model_path` (downloads to a uniquely-named .part sibling first — unique
    per attempt via pid + random suffix, so concurrent download attempts
    from separate processes never collide on the same intermediate file —
    only renamed into place after the checksum passes).
    """
    import urllib.request

    resolved_url = url or os.environ.get("ZMEM_MODEL_URL", _DEFAULT_MODEL_URL)
    safe_url = _redact_url_for_logging(resolved_url)
    # Unique per-attempt temp name (pid + random suffix) so two concurrent
    # download attempts (e.g. multiple hook processes racing on first use)
    # never write to the same intermediate file.
    tmp_path = model_path.with_suffix(
        f"{model_path.suffix}.{os.getpid()}.{os.urandom(4).hex()}.part"
    )
    try:
        model_path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(resolved_url, timeout=30) as resp:
            with open(tmp_path, "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
        if not verify_checksum(tmp_path, expected_sha256):
            # Loud, actionable failure (safe-by-default preserved: we discard
            # the mismatched file and fall through to no-embeddings, NEVER load
            # an unverified binary). The default Xenova URL is documented (PLAN
            # §7-P10, #37 L14) as NOT byte-identical to the pinned checksum, so
            # a fresh-clone autodownload will fail-closed by design. Surface the
            # expected sha256 so the operator can verify their own replacement,
            # and name the two working remediation paths (#37 L14).
            expected = expected_sha256 or _MODEL_SHA256
            print(
                f"WARNING: zmem embedding model unavailable — downloaded model "
                f"from {safe_url} failed checksum verification "
                f"(expected sha256 {expected}). This is a KNOWN state, not a "
                f"bug in your setup: the default download URL is not "
                f"byte-identical to the pinned checksum (different ONNX export "
                f"toolchains produce different bytes for the same weights; see "
                f"PLAN.md §7-P10). zmem will run in degraded (no-embeddings) "
                f"mode — recall falls back to FTS5, consolidate to lexical "
                f"clustering. To enable embeddings, either (a) set "
                f"ZMEM_MODEL_URL to a build whose sha256 matches {expected}, "
                f"or (b) place a verified minilm.onnx at {model_path}.",
                file=sys.stderr,
            )
            return False
        if not model_path.is_file():
            # Avoid unnecessary churn if another concurrent attempt already
            # completed successfully; both would be verified-correct copies
            # of the same nominal model, so either landing is fine.
            tmp_path.replace(model_path)
        return True
    except Exception:
        return False
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _check_available() -> bool:
    """Check if onnxruntime + tokenizers + model files are all present.

    If the model file is missing, attempts a lazy download (checksum-
    verified) ONLY when explicitly opted in via ZMEM_MODEL_AUTODOWNLOAD=1.
    This is opt-in, not opt-out: by default zmem never makes an unsolicited
    network call, to honor the project's local-first / zero-cloud-dependency
    promise. Download failure of any kind is silent and falls through to
    the no-embeddings path; it never raises.

    Profile-aware (issue #63): the `fake` profile needs neither deps nor
    files — it short-circuits to available. The cached verdict is keyed on
    the active profile so an env switch is never masked by a stale bool.
    """
    global _model_available, _profile_cache_key
    prof = current_profile_name()
    if _model_available is not None and _profile_cache_key == prof:
        return _model_available
    # NOTE (PRR-011): _profile_cache_key is committed ONLY after every probe
    # below succeeds — assigning it up-front could pin a stale verdict for
    # this profile if a probe raises mid-way.
    if not prof:
        # Unknown ZMEM_EMBED_PROFILE value. Unreachable via the CLI (it
        # refuses at dispatch); degrade here so library callers can't be
        # crashed by env content.
        _model_available = False
        return False
    if prof == "fake":
        _profile_cache_key = prof
        _model_available = True
        return True
    try:
        import onnxruntime  # noqa: F401
        from tokenizers import Tokenizer  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        _profile_cache_key = prof
        _model_available = False
        return False
    models_dir = _resolve_models_dir()
    model_path = models_dir / "minilm.onnx"
    tok_path = models_dir / "tokenizer.json"
    if not model_path.is_file() and os.environ.get("ZMEM_MODEL_AUTODOWNLOAD", "0") == "1":
        _try_download_model(model_path)
    ok = model_path.is_file() and tok_path.is_file()
    _profile_cache_key = prof
    _model_available = ok
    return ok


def _ensure_loaded():
    """Lazy-load the ONNX session and tokenizer. Called on first embed."""
    global _session, _tokenizer, _model_available, _model_checksum_ok, _model_load_failed
    if _session is not None:
        return
    if current_profile_name() == "fake":
        return  # fake embeds never touch the session/tokenizer stack
    if not _check_available():
        return
    import onnxruntime as ort
    from tokenizers import Tokenizer

    models_dir = _resolve_models_dir()
    model_path = models_dir / "minilm.onnx"
    # Verify the model's integrity on the LOAD path too, not just the download
    # path. _check_available only tests is_file(); without this gate, an attacker
    # able to write to ZMEM_MODELS_DIR (or control the env var) could swap in an
    # arbitrary ONNX binary that onnxruntime would load and execute. The pinned
    # _MODEL_SHA256 is the trust root (a code constant, not attacker-writable).
    # On mismatch we fail OPEN to the degraded no-embeddings path rather than
    # executing an unverified model. (#36 M15.)
    #
    # TOCTOU closure (zax-review round): read the file ONCE and verify/load the
    # same buffer — hashing a path and then re-opening it for the session left a
    # swap-in window between the two operations.
    try:
        model_bytes = model_path.read_bytes()
        actual_sha = hashlib.sha256(model_bytes).hexdigest()
    except OSError:
        _model_available = False
        _model_checksum_ok = False
        return
    if actual_sha != _MODEL_SHA256:  # call-time attr: tests patch it
        _model_available = False
        _model_checksum_ok = False
        try:
            print(
                "[zmem] WARNING: refusing to load minilm.onnx — checksum mismatch "
                "(the file at the models dir does not match the pinned SHA-256). "
                "Falling back to degraded FTS5/lexical operation. Re-install the "
                "model or set ZMEM_MODEL_URL to a source matching the pinned "
                "checksum plus ZMEM_MODEL_AUTODOWNLOAD=1.",
                file=sys.stderr,
            )
        except Exception:
            pass
        return
    # Load the session + tokenizer. A corrupt/unreadable tokenizer.json (or any
    # load-time error) must fail OPEN to the degraded path, not propagate into
    # unguarded callers (add/hybrid-search hard-failing). This mirrors the
    # checksum-mismatch fail-open above (cubic-1/2, #36 M15 residual).
    try:
        _session = ort.InferenceSession(model_bytes)
        _tokenizer = Tokenizer.from_file(str(models_dir / "tokenizer.json"))
        _tokenizer.enable_padding(length=128)
        _tokenizer.enable_truncation(max_length=128)
        _model_checksum_ok = True
    except Exception as exc:
        _model_available = False
        # The checksum PASSED but the load failed (corrupt tokenizer.json,
        # unparseable ONNX). Record a DISTINCT flag so availability_status can
        # report an accurate reason (model_load_failed, not a checksum mismatch)
        # rather than conflating the two (cubic-2 final-critic).
        _model_load_failed = True
        try:
            print(
                "[zmem] WARNING: failed to load the embedding model/tokenizer "
                f"({type(exc).__name__}); falling back to degraded FTS5/lexical "
                "operation. Re-install the model files or check ZMEM_MODELS_DIR.",
                file=sys.stderr,
            )
        except Exception:
            pass


def embed_text(text: str) -> bytes | None:
    """Generate an L2-normalized embedding blob for the given text.

    Dimensions follow the active profile: 384-d (1536 bytes) for `minilm`,
    16-d (64 bytes) for `fake`. Returns a packed float32 blob suitable for
    sqlite-vec, or None if the embedding infrastructure is unavailable.
    """
    if not text or not text.strip():
        return None
    if current_profile_name() == "fake":
        return _profiles.fake_embed(text)
    _ensure_loaded()
    if _session is None or _tokenizer is None:
        return None

    import numpy as np

    encoded = _tokenizer.encode(text)
    input_ids = np.array([encoded.ids], dtype=np.int64)
    attention_mask = np.array([encoded.attention_mask], dtype=np.int64)

    inputs = {"input_ids": input_ids, "attention_mask": attention_mask}
    if len(_session.get_inputs()) > 2:
        inputs["token_type_ids"] = np.zeros_like(input_ids)

    outputs = _session.run(None, inputs)
    last_hidden = outputs[0]  # [1, seq_len, 384]

    # Mean pooling with attention mask
    mask = attention_mask[:, :, None].astype(np.float32)
    summed = (last_hidden * mask).sum(axis=1)
    counts = np.clip(mask.sum(axis=1), 1e-8, None)
    pooled = summed / counts

    # L2 normalize
    norm = np.linalg.norm(pooled, axis=1, keepdims=True)
    pooled = pooled / np.clip(norm, 1e-8, None)

    return struct.pack(f"{_MODEL_DIM}f", *pooled[0])


def cosine_similarity_from_blob(blob1: bytes, blob2: bytes) -> float:
    """Compute cosine similarity between two float32 blobs."""
    import numpy as np

    v1 = np.frombuffer(blob1, dtype=np.float32)
    v2 = np.frombuffer(blob2, dtype=np.float32)
    if len(v1) != len(v2):
        return 0.0
    return float(np.dot(v1, v2))


def is_available() -> bool:
    """Public API: check if embeddings are available without loading the model."""
    return _check_available()


def availability_status() -> dict:
    """Structured, shallow availability diagnostic for stats/doctor/warnings.

    Returns a dict describing WHY embeddings are (un)available, computed fresh
    on each call (NOT cached) so it reflects the current filesystem/import
    state at the call site:

        {
          "available": bool,            # True iff deps import AND both files exist
                                        #   (AND the model passed checksum if loaded)
          "reason": str | None,         # 'ok' | 'imports_missing' |
                                        # 'model_file_missing' | 'tokenizer_missing' |
                                        # 'model_checksum_mismatch' |
                                        # 'model_load_failed' | 'unknown_profile' | None
          "missing_imports": list[str], # subset of onnxruntime/tokenizers/numpy
          "models_dir": str,            # resolved models dir (ZMEM_MODELS_DIR or default)
          "interpreter": str,           # sys.executable — which Python is resolving deps
          "model_file": bool,           # minilm.onnx present?
          "tokenizer_file": bool,       # tokenizer.json present?
          "checksum_ok": bool | None,   # None=not yet checked, True=verified,
                                        #   False=rejected at load (M15)
          "load_failed": bool,          # load failure flag (distinct from mismatch)
          "profile": str,               # active embedding profile name (''=invalid env)
          "dim": int | None,            # that profile's vector dim (None if invalid)
          "note": str,                  # only present on model_checksum_mismatch:
                                        # names the Xenova-ONNX vs PyTorch-weights
                                        # distinction so operators never "fix" a
                                        # mismatch by disabling verification (#63 8.1)
        }

    Presence-only: checks importability + file existence. NEVER hashes the
    model, NEVER loads the ONNX session, NEVER triggers autodownload, NEVER
    raises. This is deliberately distinct from the cached `is_available()`/
    `_check_available()` fast path used by the embed hot-path: that one caches
    a bool for speed; this one inspects current state for diagnostics. In a
    short-lived CLI process they never disagree; in a long-lived process
    (e.g. the Hermes server) this reflects install state at call time, which
    is exactly what startup/diagnostic logging needs.
    """
    missing: list[str] = []
    for mod in ("onnxruntime", "tokenizers", "numpy"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    models_dir = _resolve_models_dir()
    # Guard the file probes: Path.is_file() can raise OSError (e.g. a
    # permission-denied or otherwise inaccessible models dir) on Python 3.11+.
    # The function's contract is "NEVER raises" — treat any FS error as
    # "file not available" so stats/doctor/warning paths stay diagnostic.
    try:
        model_file = (models_dir / "minilm.onnx").is_file()
    except (OSError, ValueError):
        model_file = False
    try:
        tokenizer_file = (models_dir / "tokenizer.json").is_file()
    except (OSError, ValueError):
        tokenizer_file = False

    # Resolve the profile WITHOUT crashing on an unknown value: the raw env
    # string goes into the report so the operator sees exactly what they set.
    try:
        prof = _profiles.resolve_active_profile()
    except _profiles.ProfileError:
        prof = ""
    if not prof:
        return {
            "available": False,
            "reason": "unknown_profile",
            "missing_imports": missing,
            "models_dir": str(models_dir),
            "interpreter": sys.executable or "",
            "model_file": model_file,
            "tokenizer_file": tokenizer_file,
            "checksum_ok": None,
            "load_failed": _model_load_failed,
            "profile": (_profiles.get_env_raw() or "").strip().lower(),
            "dim": None,
        }

    prof_entry = _profiles.PROFILES[prof]
    base_note: dict = {"profile": prof, "dim": prof_entry["dim"]}

    if prof == "fake":
        # No deps/files needed for the hash embedder; probe results are
        # informational only. Checked BEFORE the deps gate so a fake-profile
        # CI box without onnxruntime still reads available=True — matching
        # `_check_available` exactly.
        return {**base_note, "available": True, "reason": "ok",
                "missing_imports": [], "models_dir": str(models_dir),
                "interpreter": sys.executable or "", "model_file": model_file,
                "tokenizer_file": tokenizer_file, "checksum_ok": None,
                "load_failed": _model_load_failed}
    if missing:
        return {**base_note, "available": False, "reason": "imports_missing",
                "missing_imports": missing, "models_dir": str(models_dir),
                "interpreter": sys.executable or "", "model_file": model_file,
                "tokenizer_file": tokenizer_file, "checksum_ok": None,
                "load_failed": _model_load_failed}
    if not model_file:
        return {**base_note, "available": False, "reason": "model_file_missing",
                "missing_imports": missing, "models_dir": str(models_dir),
                "interpreter": sys.executable or "", "model_file": model_file,
                "tokenizer_file": tokenizer_file, "checksum_ok": None,
                "load_failed": _model_load_failed}
    if not tokenizer_file:
        return {**base_note, "available": False, "reason": "tokenizer_missing",
                "missing_imports": missing, "models_dir": str(models_dir),
                "interpreter": sys.executable or "", "model_file": model_file,
                "tokenizer_file": tokenizer_file, "checksum_ok": None,
                "load_failed": _model_load_failed}
    if _model_load_failed:
        # Checksum PASSED but the model/tokenizer failed to load (corrupt
        # tokenizer.json, unparseable ONNX). Distinct from a checksum mismatch
        # so the diagnostic is accurate (cubic-2 final-critic).
        return {**base_note, "available": False, "reason": "model_load_failed",
                "missing_imports": missing, "models_dir": str(models_dir),
                "interpreter": sys.executable or "", "model_file": model_file,
                "tokenizer_file": tokenizer_file, "checksum_ok": True,
                "load_failed": _model_load_failed}
    if _model_checksum_ok is False:
        # Files present, but the load path already REJECTED the model on a
        # checksum mismatch (M15). Report unavailable so doctor does not show a
        # checksum-rejected model as healthy (cubic-2). `_model_checksum_ok` is
        # None until the first load attempt; a short-lived CLI process that has
        # not yet embedded leaves it None (treated as 'ok' here — the checksum
        # gate fires on first embed).
        return {**base_note, "available": False,
                "reason": "model_checksum_mismatch", "note": prof_entry["notes"],
                "missing_imports": missing, "models_dir": str(models_dir),
                "interpreter": sys.executable or "", "model_file": model_file,
                "tokenizer_file": tokenizer_file, "checksum_ok": False,
                "load_failed": _model_load_failed}
    return {**base_note, "available": True, "reason": "ok",
            "missing_imports": missing, "models_dir": str(models_dir),
            "interpreter": sys.executable or "", "model_file": model_file,
            "tokenizer_file": tokenizer_file, "checksum_ok": _model_checksum_ok,
            "load_failed": _model_load_failed}
