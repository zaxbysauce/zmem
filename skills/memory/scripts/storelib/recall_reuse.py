"""Correctness-preserving recall reuse layer (issue #474).

SCOPE (disclosed in the PR body and the #474 measurement report): this is an
IN-PROCESS layer at the ``recall_memory`` public seam. Hook
subprocess-per-prompt paths are cold by construction (each store.py process
starts empty); the layer serves long-lived recall callers — the Workstream V
worker (PR 3), in-process selector/library consumers, eval harnesses — and
is the surface the worker program builds on. Cross-process SERVING is a
worker-era decision on the umbrella, not this PR.

Semantics (frozen checks C6-C9 + the approved plan, rev 5):

- Reuse key = per-component sha256 digests over (store identity, query,
  session, options, resolved-config fingerprint, mutation generation, store
  incarnation). The FULL digest serves; the component map feeds the
  observation sidecar's attribution. Any unresolvable component ⇒
  conservative BYPASS (no read, no write) with the ineligibility recorded
  by observation.
- Serving is DEFAULT OFF (``ZMEM_RECALL_REUSE=1`` opts in). Observation
  (``ZMEM_RECALL_REUSE_OBSERVE=1``) runs independently and is also forced
  when serving is on, so the served workload measures itself.
- TTL default 30s (``ZMEM_RECALL_REUSE_TTL_S``; 0 disables), entry cap 64
  (LRU), deep-copy on insert AND on serve.
- In-flight coalescing: a second concurrent identical recall awaits the
  winner's outcome — envelope (own effects replayed) or the winner's raised
  failure. Failures are NEVER cached (a repeated key after a failure
  recomputes).
- Effect replay: the first call runs the UNMODIFIED implementation —
  telemetry, capture population, and stdout happen exactly where they
  always did (ordering preserved by construction). The seam RECORDS the
  ordered effects (telemetry ids/kwargs, capture snapshot, stdout text) and
  replays them on every hit/coalesced serve, so output, capture, and
  telemetry behavior are identical with and without reuse.
- A hit-side effect failure EVICTS the entry and re-raises; the next call
  recomputes (AC8).
- ``ZMEM_RECALL_REUSE_REFRESH=1`` forces miss-and-replace for that call;
  ``clear_recall_reuse_cache()`` is the programmatic surface.
- Counters: hits, misses, coalesced, invalidations by reason, refreshes,
  failures-not-cached, bypasses; timing deque (<=1024 samples of
  compute_ms/reuse_ms). NO query text anywhere.
"""

from __future__ import annotations

import collections
import copy
import hashlib
import io
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

from storelib import generation as _store_generation

SERVE_ENV = "ZMEM_RECALL_REUSE"
TTL_ENV = "ZMEM_RECALL_REUSE_TTL_S"
REFRESH_ENV = "ZMEM_RECALL_REUSE_REFRESH"
OBSERVE_ENV = "ZMEM_RECALL_REUSE_OBSERVE"

DEFAULT_TTL_S = 30.0
MAX_ENTRIES = 64

# --- Resolved-config fingerprint (approved plan R2-3) ------------------------
# ONE census of every environment knob the recall path reads. The fingerprint
# digests the RAW env strings (unset = None): any knob change produces a
# different fingerprint (a conservative miss), and a knob set to its default
# value also misses — safe by construction. The Phase 4.2 sweep re-derives
# this census mechanically; a new recall-path env read that is not listed
# here fails the census guardrail.
CONFIG_KNOBS: tuple[tuple[str, str], ...] = (
    # recall.py tiering / fusion / lanes
    ("ZMEM_MMR_LAMBDA", "recall"),
    ("ZMEM_TIER_SLOTS", "recall"),
    ("ZMEM_UNFOLD_TOP_K", "recall"),
    ("ZMEM_UNFOLD_MAX_HOPS", "recall"),
    ("ZMEM_UNFOLD_BUDGET", "recall"),
    ("ZMEM_GRAPH_SEED", "recall"),
    ("ZMEM_ARM_CAP_FTS", "recall"),
    ("ZMEM_ARM_CAP_VEC", "recall"),
    ("ZMEM_ARM_CAP_ENTITY", "recall"),
    ("ZMEM_ARM_CAP_GRAPH", "recall"),
    ("ZMEM_ARM_CAP_TEMPORAL", "recall"),
    ("ZMEM_ENTITY_KIND_WEIGHTS", "recall"),
    ("ZMEM_FENCE_OPEN", "recall"),
    ("ZMEM_FENCE_CLOSE", "recall"),
    ("ZMEM_CROSS_PROJECT", "recall"),
    ("ZMEM_CROSS_ENCODER_SHADOW", "recall"),
    ("ZMEM_UNTRUSTED_FENCE", "recall"),
    ("ZMEM_EMBED_PROFILE", "recall"),
    # injection floors / budget (selector + for-injection lane)
    ("ZMEM_INJECT_FLOOR_COS", "inject"),
    ("ZMEM_INJECT_FLOOR_ENT", "inject"),
    ("ZMEM_INJECT_FLOOR_GATE_NONE", "inject"),
    ("ZMEM_INJECT_FLOOR_GRAPH", "inject"),
    ("ZMEM_INJECT_FLOOR_LEX", "inject"),
    ("ZMEM_INJECT_FLOOR_PROMPT", "inject"),
    ("ZMEM_INJECT_FLOOR_RECENT", "inject"),
    ("ZMEM_INJECT_FLOOR_TRUST", "inject"),
    ("ZMEM_INJECT_FLOOR_USER_GLOBAL", "inject"),
    ("ZMEM_INJECT_MARGIN", "inject"),
    ("ZMEM_CTX_BUDGET", "inject"),
    ("ZMEM_INJECT_TOKEN_BUDGET", "inject"),
    ("ZMEM_CAPTURE", "inject"),
    # cross-encoder lane
    ("ZMEM_CROSS_ENCODER_MODEL", "cross_encoder"),
    ("ZMEM_CROSS_ENCODER", "cross_encoder"),
    # injection detail surfaces (evidence lookup bounds shape the
    # for-injection details output)
    ("ZMEM_EVIDENCE_CAP", "inject"),
    ("ZMEM_EVIDENCE_DAYS", "inject"),
    ("ZMEM_CROSS_ENCODER_PASSIVE", "cross_encoder"),
    ("ZMEM_CROSS_ENCODER_BUDGET_MS", "cross_encoder"),
)

HAZARD_ENV = "ZMEM_CROSS_PROJECT_HAZARD_VERBS"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def config_fingerprint(environ=None) -> dict:
    """Component subdigest of every census knob's raw env value."""
    env = os.environ if environ is None else environ
    parts = {name: _sha(repr(env.get(name))) for name, _lane in CONFIG_KNOBS}
    try:
        from storelib import cross_project as _cp
        parts[_cp.CROSS_PROJECT_HAZARD_VERBS_ENV] = _sha(
            repr(env.get(_cp.CROSS_PROJECT_HAZARD_VERBS_ENV)))
    except Exception:
        parts[HAZARD_ENV] = _sha(repr(env.get(HAZARD_ENV)))
    return parts


def _store_path(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    if not row or len(row) < 3 or not row[2]:
        return None
    # review SP-003: normalize case/separators so Windows spelling
    # variants of one store (C:/x vs c:\x) hash to one reuse key instead
    # of fragmenting the cache; normcase is a no-op on POSIX.
    return os.path.normcase(str(row[2]))


def _session_token() -> str:
    for name in ("ZMEM_SESSION", "ZMEM_SESSION_ID"):
        value = os.environ.get(name)
        if value:
            return value
    return "<process>"


def _canonical(value) -> str:
    """Stable repr for key components: dicts sorted, lists order-preserving
    (order-insensitivity is only claimed where a test proves it), and
    SET-LIKE values (frozenset/set) as sorted lists — their repr iteration
    order varies across processes, which would key identical logical calls
    differently (review finding 6)."""
    def _default(obj):
        if isinstance(obj, (frozenset, set)):
            return sorted(str(item) for item in obj)
        return repr(obj)
    try:
        return json.dumps(value, sort_keys=True, default=_default)
    except (TypeError, ValueError):
        return repr(value)


def key_components(conn, call_kwargs: dict) -> dict | None:
    """Full component map, or None when any component is unresolvable
    (conservative bypass)."""
    try:
        store = _store_path(conn)
        if not store:
            return None
        query = call_kwargs.get("query")
        if not isinstance(query, str):
            return None
        options = {k: v for k, v in call_kwargs.items() if k != "_capture"}
        return {
            "store": _sha(store),
            "query": _sha(query),
            "session": _sha(_fence_or_session(call_kwargs)),
            "options": _sha(_canonical(options)),
            "config": _sha(_canonical(config_fingerprint())),
            "generation": _sha(_store_generation.read_mutation_generation(conn)),
            "incarnation": _sha(_store_generation.read_store_incarnation(conn)),
        }
    except Exception:
        return None


def _fence_or_session(call_kwargs: dict) -> str:
    fence = call_kwargs.get("_fence_id")
    if fence:
        return "fence:%s" % fence
    return "session:%s" % _session_token()


def base_digest(components: dict) -> str:
    """Stable comparison identity (R3-1): store/user + query ONLY — every
    invalidation dimension (session, options, config, generation,
    incarnation) is excluded so its change is attributable."""
    return _sha(components["store"] + ":" + components["query"])


def full_digest(components: dict) -> str:
    return _sha(_canonical({k: components[k] for k in sorted(components)}))


# --- Serving state -----------------------------------------------------------

class _ReuseEntry:
    __slots__ = ("result", "stdout_text", "capture_snapshot",
                 "telemetry_calls", "ts", "components")

    def __init__(self, result, stdout_text, capture_snapshot,
                 telemetry_calls, components):
        self.result = result
        self.stdout_text = stdout_text
        self.capture_snapshot = capture_snapshot
        self.telemetry_calls = telemetry_calls
        self.ts = time.monotonic()
        self.components = components


_reuse_cache: "collections.OrderedDict[str, _ReuseEntry]" = \
    collections.OrderedDict()
_reuse_lock = threading.RLock()
_reuse_inflight: dict[str, dict] = {}
# Issue #474 (review RP-002): the winner's effect recorders swap the
# PROCESS-GLOBAL telemetry hook and sys.stdout — two distinct-key winners
# computing concurrently would nest/restore them out of order, corrupting
# recorded effects and the restored globals. Concurrency contract: EVERY
# seam-level impl() execution (winner compute, bypass, observe-only) and
# every hit/loser effect replay serializes on this lock, so no recall-seam
# effects can enter a winner's recorded script. Code outside this seam
# (unrelated process threads writing sys.stdout or bumping telemetry while
# a winner records) is outside the seam's control; supported consumers
# (CLI, hooks) invoke the seam from a single thread per process.
_COMPUTE_LOCK = threading.RLock()

_counters: dict = collections.defaultdict(int)
_counters["invalidation_reasons"] = collections.defaultdict(int)
_timing_compute = collections.deque(maxlen=1024)
_timing_reuse = collections.deque(maxlen=1024)


def _ttl_s() -> float:
    raw = os.environ.get(TTL_ENV)
    if raw is None or raw == "":
        return DEFAULT_TTL_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TTL_S
    return max(0.0, value)


def _serving_on() -> bool:
    return (os.environ.get(SERVE_ENV, "") == "1"
            and _ttl_s() > 0.0)


def _observing() -> bool:
    return (os.environ.get(OBSERVE_ENV, "") == "1") or _serving_on()


def _refresh_requested() -> bool:
    return os.environ.get(REFRESH_ENV, "") == "1"


class _Tee:
    """Write-through stdout tee: the real stream still receives every write
    (first-call output unchanged); the text is recorded for replay."""

    def __init__(self, real):
        self._real = real
        self.chunks: list[str] = []

    def write(self, s):
        self.chunks.append(s)
        return self._real.write(s)

    def flush(self):
        return self._real.flush()

    def isatty(self):
        try:
            return self._real.isatty()
        except Exception:
            return False

    @property
    def buffer(self):
        # A sys.stdout.buffer.write would bypass the text tee and never
        # replay on a hit (review finding 7) — record it too. Today the
        # recall path prints via print() only; this closes the latent gap.
        real = getattr(self._real, "buffer", None)
        if real is None:
            return None
        tee = self

        class _BytesTee:
            def write(self, data):
                tee.chunks.append(data.decode(
                    getattr(tee._real, "encoding", "utf-8") or "utf-8",
                    "replace"))
                return real.write(data)

            def flush(self):
                return real.flush()

            def __getattr__(self, name):
                return getattr(real, name)

        return _BytesTee()

    def __getattr__(self, name):
        # Delegate every other stream attribute (encoding, errors, ...)
        # to the real stream dynamically. _Tee deliberately does NOT
        # subclass io.TextIOBase: the base class DEFINES encoding/errors
        # as None, which would shadow this delegation.
        return getattr(self._real, name)


class _TelemetryRecorder:
    """Records (ids, kwargs) per _bump_telemetry call and forwards to the
    real implementation — the first call applies its own telemetry exactly
    where it always did; the recording is the replay script."""

    def __init__(self, recall_module):
        self._mod = recall_module
        self.calls: list[tuple[list, dict]] = []
        self._real = recall_module._bump_telemetry

    def install(self):
        mod = self._mod
        recorder = self

        def _rec(conn, ids, **kwargs):
            recorder.calls.append((list(ids), dict(kwargs)))
            return recorder._real(conn, ids, **kwargs)

        mod._bump_telemetry = _rec

    def restore(self):
        self._mod._bump_telemetry = self._real


def recall_with_reuse(conn, call_kwargs: dict, impl, capture_dict):
    """The recall_memory seam. ``impl`` is a zero-arg callable running the
    UNMODIFIED implementation (effects land where they always did);
    ``capture_dict`` is the caller's ``_capture`` dict (identity excluded
    from the key; content recorded for replay)."""
    # Issue #474 (review RP-001): when neither serving nor observation is
    # on, no consumer of the reuse key exists — short-circuit BEFORE the
    # PRAGMA/meta reads and the ~34-digest fingerprint so the default-off
    # path pays nothing beyond two cheap env checks.
    serving = _serving_on()
    observing = _observing()
    if not serving and not observing:
        return impl()

    components = key_components(conn, call_kwargs)
    if components is None:
        if observing:
            _observe(conn, None, None, "bypass-unresolvable", ok=None)
        with _reuse_lock:
            _counters["bypass"] += 1
        # review closeout (RP-002 residual): while a winner records, its
        # recorders own the process-global telemetry hook and sys.stdout.
        # Every seam-level impl() execution — including bypass and
        # observe-only — must serialize under _COMPUTE_LOCK, or its effects
        # land inside the winner's recorded script and are replayed on
        # later hits of that key.
        with _COMPUTE_LOCK:
            return impl()

    key = full_digest(components)
    observe_sidecar = observing

    if not serving:
        if observe_sidecar:
            _observe(conn, components, None, None, ok=True, pre=True)
        with _COMPUTE_LOCK:
            return impl()

    ttl = _ttl_s()
    refresh = _refresh_requested()
    if refresh:
        with _reuse_lock:
            _counters["refresh"] += 1

    with _reuse_lock:
        entry = _reuse_cache.get(key)
        if entry is not None and not refresh:
            age = time.monotonic() - entry.ts
            if age <= ttl:
                _reuse_cache.move_to_end(key)
            else:
                _counters["invalidation_reasons"]["ttl"] += 1
                del _reuse_cache[key]
                entry = None
                if observe_sidecar:
                    _observe(conn, components, "ttl", None, ok=True)
        else:
            if entry is not None and refresh:
                del _reuse_cache[key]
            entry = None

    if entry is not None:
        # HIT: replay this caller's effects from the recorded script.
        t0 = time.monotonic()
        try:
            _apply_effects(conn, entry, capture_dict)
        except Exception:
            with _reuse_lock:
                _reuse_cache.pop(key, None)
                _counters["hit_effect_failures"] += 1
            raise
        with _reuse_lock:
            _counters["hits"] += 1
        _timing_reuse.append((time.monotonic() - t0) * 1000.0)
        if observe_sidecar:
            _observe(conn, components, None, None, ok=True, so="hit")
        return copy.deepcopy(entry.result)

    # MISS: coalesce or compute.
    with _reuse_lock:
        slot = _reuse_inflight.get(key)
        if slot is None:
            slot = {"event": threading.Event(), "error": None,
                    "done": False}
            _reuse_inflight[key] = slot
            winner = True
        else:
            winner = False

    if not winner:
        # review Stage-B r3: an explicit waiter-entry counter — the AC2
        # contract test polls it for deterministic proof the loser reached
        # the waiter branch (thread scheduling alone cannot prove it).
        with _reuse_lock:
            _counters["waiters_joined"] += 1
        timed_out = not slot["event"].wait(timeout=ttl + 30.0)
        with _reuse_lock:
            # Issue #474 (review CS-001/RP-005): pop only the slot we
            # actually waited on — a timed-out waiter or a waiter waking
            # after a winner failure must never delete a successor's slot.
            if _reuse_inflight.get(key) is slot:
                _reuse_inflight.pop(key, None)
            cached = _reuse_cache.get(key)
        if cached is not None:
            with _reuse_lock:
                _counters["coalesced"] += 1
            try:
                _apply_effects(conn, cached, capture_dict)
            except Exception:
                # Same eviction contract as the hit path (review finding 5):
                # a poisoned entry must not survive to the next caller.
                with _reuse_lock:
                    _reuse_cache.pop(key, None)
                    _counters["hit_effect_failures"] += 1
                raise
            if observe_sidecar:
                _observe(conn, components, None, None, ok=True,
                         so="coalesced")
            return copy.deepcopy(cached.result)
        if slot["error"] is not None:
            with _reuse_lock:
                _counters["coalesced"] += 1
            raise slot["error"]
        # Winner finished between our checks without publishing (evicted,
        # effect-failed, or — review RP-005 — our wait timed out and the
        # slot was already reclaimed): recompute rather than synthesize
        # anything.
        with _reuse_lock:
            _counters["misses"] += 1
        return recall_with_reuse(conn, call_kwargs, impl, capture_dict)

    # Winner: run the UNMODIFIED implementation under the recorders.
    # Issue #474 (review RP-002): the telemetry recorder and stdout tee are
    # PROCESS-GLOBAL swaps — two distinct-key winners computing
    # concurrently would nest/restores them out of order, corrupting both
    # callers' recorded effects and the restored globals. Serialize the
    # record→compute→restore window per process (the lock is released
    # before effect replay/publication, so hits never block on it).
    with _COMPUTE_LOCK:
        with _reuse_lock:
            _counters["misses"] += 1
        t0 = time.monotonic()
        import storelib.recall as _recall_mod
        recorder = _TelemetryRecorder(_recall_mod)
        tee = _Tee(sys.stdout)
        saved_stdout = sys.stdout
        try:
            recorder.install()
            sys.stdout = tee
            result = impl()
        except BaseException as exc:
            with _reuse_lock:
                _counters["failures_not_cached"] += 1
            slot["error"] = exc
            slot["done"] = True
            slot["event"].set()
            with _reuse_lock:
                if _reuse_inflight.get(key) is slot:
                    _reuse_inflight.pop(key, None)
            if observe_sidecar:
                _observe(conn, components, None, None, ok=False)
            raise
        finally:
            sys.stdout = saved_stdout
            recorder.restore()
    compute_ms = (time.monotonic() - t0) * 1000.0
    _timing_compute.append(compute_ms)

    capture_snapshot = None
    if capture_dict is not None:
        try:
            capture_snapshot = copy.deepcopy(dict(capture_dict))
        except Exception:
            capture_snapshot = None

    try:
        entry_obj = _ReuseEntry(
            copy.deepcopy(result), "".join(tee.chunks), capture_snapshot,
            list(recorder.calls), components)
        with _reuse_lock:
            _reuse_cache[key] = entry_obj
            _reuse_cache.move_to_end(key)
            while len(_reuse_cache) > MAX_ENTRIES:
                _reuse_cache.popitem(last=False)
    except BaseException:
        # The computation SUCCEEDED but publication failed (e.g. a
        # non-copyable result). The slot must still complete or every
        # later identical recall blocks as a loser until the wait timeout
        # (implementation-review finding 2; round-2 residual 2 hardened
        # Exception -> BaseException for symmetry with the impl guard):
        # no entry is published and the failure propagates like a compute
        # failure.
        with _reuse_lock:
            slot["error"] = sys.exc_info()[1]
            slot["done"] = True
            slot["event"].set()
            if _reuse_inflight.get(key) is slot:
                _reuse_inflight.pop(key, None)
        raise
    with _reuse_lock:
        slot["done"] = True
        slot["event"].set()
        if _reuse_inflight.get(key) is slot:
            _reuse_inflight.pop(key, None)
    if observe_sidecar:
        _observe(conn, components, None, None, ok=True, so="miss",
                 ms=compute_ms)
    return result


def _apply_effects(conn, entry: _ReuseEntry, capture_dict) -> None:
    """Replay the recorded effect script for THIS caller: telemetry on this
    caller's connection with the recorded ids/kwargs, capture population,
    and the recorded stdout bytes verbatim.

    Review Stage-B FIX-3: effect replay writes through the PROCESS-GLOBAL
    telemetry hook and sys.stdout — the same globals a concurrent winner's
    recorder has swapped. Taking _COMPUTE_LOCK here means a hit that lands
    during a winner's record window waits for the winner to restore the
    globals first, so its telemetry/stdout can never enter (or be corrupted
    by) the winner's recorded script. Hit replay is fast (bounded telemetry
    writes + one stdout write), so the serialization costs nothing."""
    import storelib.recall as _recall_mod
    with _COMPUTE_LOCK:
        for ids, kwargs in entry.telemetry_calls:
            _recall_mod._bump_telemetry(conn, ids, **kwargs)
        if capture_dict is not None and entry.capture_snapshot is not None:
            capture_dict.update(copy.deepcopy(entry.capture_snapshot))
        if entry.stdout_text:
            sys.stdout.write(entry.stdout_text)
            sys.stdout.flush()


def clear_recall_reuse_cache() -> None:
    """Programmatic explicit-refresh surface (called by the reembed
    completion path — vectors rebuilt under a new profile free the
    cached envelopes immediately instead of waiting out the TTL)."""
    with _reuse_lock:
        _reuse_cache.clear()


def reuse_stats() -> dict:
    """Bounded, anonymized serving counters (AC9). No query text."""
    with _reuse_lock:
        stats = {k: v for k, v in _counters.items() if k != "invalidation_reasons"}
        stats["invalidations"] = dict(_counters["invalidation_reasons"])
        stats["entries"] = len(_reuse_cache)
        stats["timing_samples_compute"] = len(_timing_compute)
        stats["timing_samples_reuse"] = len(_timing_reuse)
    return stats


# --- Observation sidecar (approved plan rev 5) --------------------------------

SIDECAR_NAME = "reuse-observe.jsonl"
DROPS_NAME = "reuse-observe.drops"
MAX_RECORDS = 4096
AGGREGATE_HEADER = "# aggregate v1"
_SIDECAR_LOCK = threading.Lock()


def _sidecar_paths(conn) -> tuple[Path, Path] | None:
    store = _store_path(conn)
    if not store:
        return None
    parent = Path(store).parent / "ops"
    return parent / SIDECAR_NAME, parent / DROPS_NAME


def _now_utc() -> str:
    # Millisecond resolution: second-resolution timestamps hide sub-TTL
    # intervals (a 0.08s gap under a 0.05s TTL reads as 0s -> "within").
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    return "%s.%03dZ" % (base, int(time.time() * 1000) % 1000)


def _observe(conn, components, invalidation_reason, eligibility, *,
             ok, so=None, ms=None, pre=False) -> None:
    """Cross-process shadow observation. Runtime FAIL-OPEN (a hook never
    fails because observation cannot write); coverage is established
    independently by the reference driver + the drops journal, and the
    report fails closed (see observation_report)."""
    try:
        paths = _sidecar_paths(conn)
        if paths is None:
            return
        sidecar, drops = paths
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "v": 2,
            "t": _now_utc(),
            "ok": bool(ok) if ok is not None else None,
            "so": so,
            "ms": round(ms, 3) if ms is not None else None,
        }
        if eligibility:
            record["el"] = eligibility
        if invalidation_reason:
            record["inv"] = invalidation_reason
        if components is not None:
            record["kf"] = full_digest(components)
            record["kb"] = base_digest(components)
            record["cs"] = dict(components)
            record["g"] = components["generation"]
            record["i"] = components["incarnation"]
        _append_observation(sidecar, drops, record)
    except Exception:
        return


def _append_observation(sidecar: Path, drops: Path, record: dict) -> None:
    """Locked read-modify-write with count-preserving compaction (R3-2/R4-2).

    The sidecar carries a HARD bound of MAX_RECORDS key records (open
    windows included). Per-key state is one JSON line updated at append time
    with running counters (n/dup/rt/ttl/rf/inv map); when the bound is hit,
    closed TTL windows fold into a versioned aggregate header line and, if
    still at the bound (all-open burst), the OLDEST key record early-closes
    into the aggregate with the ``ovf`` counter — later duplicates of an
    early-closed key classify as first observations AND ``ovf`` marks the
    report incomplete (fail-closed, never silently approximate).
    """
    lock_path = sidecar.with_suffix(".lock")
    with _SIDECAR_LOCK:
        import tempfile
        fd, tmp_name = tempfile.mkstemp(dir=str(sidecar.parent),
                                        prefix=".reuse-obs-")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            lock_fd = os.open(str(lock_path),
                              os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Issue #474 (review RP-003 + Stage-B FIX-4): a holder killed
            # before its finally-cleanup leaves the lock pathname behind.
            # Age-based reclaim was rejected — a suspended live holder or a
            # check-then-unlink race can delete a live/successor lock, and
            # concurrent RMW loses observations. Instead: bounded short
            # wait (150 ms — a live holder's RMW is an in-memory parse plus
            # one bounded rewrite, far under this), then journal a durable
            # drop and fail open. A dead holder suppresses observation
            # (fail-VISIBLE via drops in the report) until the operator
            # removes ops/reuse-observe.lock; no automatic reclaim can
            # delete a live holder's lock.
            deadline = time.monotonic() + 0.15
            while time.monotonic() < deadline:
                try:
                    lock_fd = os.open(str(lock_path),
                                      os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    break
                except FileExistsError:
                    time.sleep(0.02)
            else:
                try:
                    with open(drops, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(
                            {"t": _now_utc(), "reason": "lock-timeout"}
                        ) + "\n")
                except Exception:
                    # review Stage-B r2 FIX-4: an unwritable journal must
                    # not silently zero the drop count — surface it in the
                    # in-process stats so reuse_stats/report can flag it.
                    with _reuse_lock:
                        _counters["drop_journal_failures"] += 1
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return
        try:
            records: dict[str, dict] = {}
            aggregate = {"n": 0, "dup": 0, "rt": 0, "ttl": 0, "rf": 0,
                         "ovf": 0, "inv": {}, "keys": 0}
            order: list[str] = []
            if sidecar.exists():
                try:
                    with open(sidecar, "r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("#"):
                                try:
                                    aggregate = json.loads(
                                        line[len(AGGREGATE_HEADER):])
                                except ValueError:
                                    continue
                                continue
                            try:
                                row = json.loads(line)
                            except ValueError:
                                aggregate.setdefault("corrupt",
                                                     0)
                                aggregate["corrupt"] = \
                                    aggregate.get("corrupt", 0) + 1
                                continue
                            if not isinstance(row, dict):
                                # review UI-002: a valid-JSON scalar/array
                                # line must count as corrupt, not crash
                                # row.get below
                                aggregate["corrupt"] = \
                                    aggregate.get("corrupt", 0) + 1
                                continue
                            kf = row.get("kf")
                            if not kf:
                                continue
                            records[kf] = row
                            order.append(kf)
                except OSError:
                    records = {}
                    order = []

            _fold_record(record, records, aggregate)
            kf = record.get("kf")
            if kf and kf not in order:
                order.append(kf)
                _attribute_invalidation(records, record)

            # compaction with the hard bound
            while len(records) > MAX_RECORDS:
                if not order:
                    break
                oldest_kf = order.pop(0)
                row = records.pop(oldest_kf)
                aggregate["ovf"] += 1
                for k in ("n", "dup", "rt", "ttl", "rf"):
                    aggregate[k] += int(row.get(k, 0) or 0)
                for reason, count in (row.get("inv") or {}).items():
                    aggregate["inv"][reason] = \
                        aggregate["inv"].get(reason, 0) + count

            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(AGGREGATE_HEADER + json.dumps(
                    aggregate, sort_keys=True) + "\n")
                for kf in order:
                    if kf in records:
                        fh.write(json.dumps(records[kf], sort_keys=True)
                                 + "\n")
            try:
                os.replace(tmp, sidecar)
            except OSError:
                # review SP-002: a failed install (e.g. Windows destination
                # held open) must not orphan the staging temp — the
                # observation is dropped (fail-open by contract) but the
                # file is cleaned.
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
        finally:
            os.close(lock_fd)
            try:
                lock_path.unlink()
            except OSError:
                pass


def _fold_record(record: dict, records: dict, aggregate: dict) -> None:
    """Append-time classification (R3-1): update per-key running counters
    against the stored previous state for this key. Previous-state fields
    are snapshotted BEFORE mutation — ``prev`` and the updated row are the
    same object, so reading them after the update misclassifies every
    sequence."""
    kf = record.get("kf")
    if not kf:
        return
    now_ts = record.get("t")
    prev = records.get(kf)
    prev_ok = prev.get("ok") if prev is not None else None
    prev_t = prev.get("t") if prev is not None else None
    row = prev or {
        "v": 2, "kf": kf, "kb": record.get("kb"),
        "cs": record.get("cs"), "n": 0, "dup": 0, "rt": 0, "ttl": 0,
        "rf": 0, "inv": {},
    }
    row["n"] = int(row.get("n", 0)) + 1
    row["t"] = now_ts
    if record.get("el"):
        row["el"] = record["el"]
    if record.get("so"):
        row["last_so"] = record["so"]
    if record.get("ok") is not None:
        row["ok"] = record.get("ok")

    if prev is not None:
        ttl_s = _ttl_s()
        try:
            prev_ts = _parse_utc(prev_t)
            now_dt = _parse_utc(now_ts)
            within = (prev_ts is not None and now_dt is not None
                      and (now_dt - prev_ts) <= ttl_s)
        except Exception:
            within = False
        if record.get("inv"):
            row["inv"][record["inv"]] = row["inv"].get(record["inv"], 0) + 1
        elif within and record.get("rf"):
            row["rf"] += 1
        elif within and prev_ok is False:
            row["rt"] += 1
        elif within and record.get("ok") is False:
            # a FAILED arrival after a successful one flips the state for
            # the next attribution; it is not reusable work, so it counts
            # in neither dup nor rt (R3-1)
            pass
        elif within:
            row["dup"] += 1
        else:
            row["ttl"] += 1
    records[kf] = row


_INV_PRIORITY = ("generation", "incarnation", "session", "options", "config")


def _attribute_invalidation(records: dict, record: dict) -> None:
    """R3-1 attribution: a NEW kf sharing a kb with a DIFFERENT established
    kf means an invalidation dimension changed — attribute by diffing the
    component digests (priority order; query/store cannot differ under the
    same kb)."""
    kb = record.get("kb")
    kf = record.get("kf")
    if not kb or not kf:
        return
    cs = record.get("cs") or {}
    for other_kf, other in records.items():
        if other_kf == kf or other.get("kb") != kb:
            continue
        other_cs = other.get("cs") or {}
        for comp in _INV_PRIORITY:
            if cs.get(comp) != other_cs.get(comp):
                row = records[kf]
                row["inv"][comp] = row["inv"].get(comp, 0) + 1
                return


def _ms_of(text) -> float:
    """Millisecond fraction of an ISO timestamp ('...SS.mmmZ'), 0.0 when
    absent."""
    try:
        frac = text[19:]
        if frac.startswith(".") and len(frac) >= 4:
            return float(frac[1:4])
    except (TypeError, ValueError):
        pass
    return 0.0


def _parse_utc(text):
    if not text:
        return None
    import calendar
    try:
        return calendar.timegm(time.strptime(text[:19],
                                             "%Y-%m-%dT%H:%M:%S")) + (
            _ms_of(text) / 1000.0)
    except ValueError:
        return None


def observation_report(conn) -> dict:
    """Report-time aggregation with FAIL-CLOSED coverage. The reference
    driver's own attempt count is the authoritative census; this report
    carries the observed sidecar state and REJECTS itself as AC11 evidence
    when drops/corrupt/ovf are nonzero (the caller reconciles attempted ==
    observed + ineligible + dropped + corrupt + ovf)."""
    paths = _sidecar_paths(conn)

    def _dropped_count(drops_path) -> int | None:
        """None = the journal exists but is unreadable — the report must
        treat that as INCOMPLETE (review Stage-B r2 FIX-4), never as zero
        drops."""
        if not drops_path.exists():
            return 0
        try:
            with open(drops_path, "r", encoding="utf-8") as fh:
                return sum(1 for line in fh if line.strip())
        except OSError:
            return None

    if paths is None:
        return {"schema_version": 2, "present": False, "complete": False,
                "reason": "store-path-unresolvable",
                "coverage": {"observed_n": 0, "ineligible": 0,
                             "dropped": 0, "corrupt": 0, "ovf": 0}}
    sidecar, drops = paths
    if not sidecar.exists():
        dropped = _dropped_count(drops)
        return {"schema_version": 2, "present": False,
                "complete": False,
                "reason": ("sidecar-absent" if dropped == 0
                           else "sidecar-absent-with-drops"),
                "coverage": {"observed_n": 0, "ineligible": 0,
                             "dropped": dropped, "corrupt": 0, "ovf": 0}}
    aggregate = {"n": 0, "dup": 0, "rt": 0, "ttl": 0, "rf": 0, "ovf": 0,
                 "inv": {}, "keys": 0, "corrupt": 0}
    per_key = {}
    with open(sidecar, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                try:
                    aggregate.update(json.loads(line[len(AGGREGATE_HEADER):]))
                except ValueError:
                    pass
                continue
            try:
                row = json.loads(line)
            except ValueError:
                aggregate["corrupt"] += 1
                continue
            if not isinstance(row, dict):
                aggregate["corrupt"] += 1
                continue
            if row.get("kf"):
                per_key[row["kf"]] = row
    dropped = _dropped_count(drops)
    if dropped is None:
        # review Stage-B r2 FIX-4: an unreadable drops journal is an
        # incomplete-evidence condition, never silently zero drops.
        return {"schema_version": 2, "present": True, "complete": False,
                "reason": "drops-journal-unreadable",
                "coverage": {"observed_n": 0, "ineligible": 0,
                             "dropped": 0, "corrupt": 0, "ovf": 0}}
    # The aggregate header carries COMPACTED windows; live per-key rows
    # carry the open ones. Totals = aggregate + live rows.
    live = {"n": 0, "dup": 0, "rt": 0, "ttl": 0, "rf": 0}
    live_inv: dict = {}
    for row in per_key.values():
        for k in live:
            live[k] += int(row.get(k, 0) or 0)
        for reason, count in (row.get("inv") or {}).items():
            live_inv[reason] = live_inv.get(reason, 0) + count
    observed = aggregate.get("n", 0) + live["n"]
    dup = aggregate.get("dup", 0) + live["dup"]
    ineligible = sum(1 for row in per_key.values() if row.get("el"))
    complete = (aggregate.get("ovf", 0) == 0 and dropped == 0
                and aggregate.get("corrupt", 0) == 0
                # review Stage-B r3: a known in-process journal-write
                # failure means an observation was lost — coverage is not
                # complete even though the journal itself reads clean.
                and _counters.get("drop_journal_failures", 0) == 0)
    eligible_n = observed - ineligible
    invalidations = dict(aggregate.get("inv") or {})
    for reason, count in live_inv.items():
        invalidations[reason] = invalidations.get(reason, 0) + count
    return {
        "schema_version": 2,
        "present": True,
        "complete": complete,
        "coverage": {"observed_n": observed, "ineligible": ineligible,
                     "dropped": dropped,
                     "corrupt": aggregate.get("corrupt", 0),
                     "ovf": aggregate.get("ovf", 0)},
        "hook_potential": {
            "duplicate_equivalent": dup,
            "eligible_n": eligible_n,
            "rate": (dup / eligible_n if eligible_n > 0 else None),
        },
        "realizable_in_process": reuse_stats(),
        "invalidations": invalidations,
        "retries_after_failure": aggregate.get("rt", 0) + live["rt"],
        "ttl_recomputes": aggregate.get("ttl", 0) + live["ttl"],
        "refreshes": aggregate.get("rf", 0) + live["rf"],
        "keys": len(per_key),
    }
