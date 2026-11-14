"""Bounded nightly shadow-orchestration prototype (#377 AC5 + AC4 apply protocol).

The fixed apply sequence (plan Rev 6; critic R2-2):
  snapshot (one-shot read, cursors closed, conn closed)
  -> inference (NO db connection, NO lock held)
  -> ownership check (only the configured authoritative owner schedules; ownership is
     immutable per run, recorded in the run ledger)
  -> acquire the REAL production lock NAME ("consolidate") on the isolated store via
     host.acquire_lock with the production _lock_path scheme — FAIL-CLOSED on None or
     the degraded "unlocked" token
  -> BEGIN IMMEDIATE -> revalidate revisions/retractions INSIDE the transaction ->
     idempotent apply (admit-once run-id ledger) -> COMMIT -> release.

The shadow-scheduler lock ("curator-shadow") supplements (prevents two schedulers) —
it NEVER replaces the production lock.

Cadence fidelity WITHOUT clock patching (critic R2/R3-5): the REAL
consolidate._cadence_gate_skipped runs against scratch stores whose
`last_consolidation`/`last_consolidation_count` meta rows are seeded at controlled
ages (thresholds are pinned in resources.child_env so outcomes are deterministic); a
skipped nightly run must NOT re-arm the clock; the nightly trigger never forces the
gate (the only force= in this module is the literal force=False the gate call uses).

Install/disable: `enable` / `disable` / `dry-run` subcommands operate on a SCRATCH
config dir only — no cron registration, no production flag (out of scope by the
issue's own boundary; nothing outside the scratch dir changes).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from . import resources
except ImportError:  # standalone script mode (enable/disable/dry-run): resolve
    # the package root absolutely and import the sibling module by name.
    import sys as _sys
    _PKG = Path(__file__).resolve().parents[1]
    if str(_PKG) not in _sys.path:
        _sys.path.insert(0, str(_PKG))
    import resources as resources  # noqa: F401

SCRIPTS = resources.SCRIPTS_DIR
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from host import acquire_lock, release_lock  # noqa: E402  (production lock primitive)

PRODUCTION_LOCK_NAME = "consolidate"  # cli.py:6577/6633 — the real maintenance lock
SHADOW_LOCK_NAME = "curator-shadow"   # supplement only; never a substitute


def lock_path_for(store_dir: Path, name: str = PRODUCTION_LOCK_NAME) -> Path:
    """The production _lock_path scheme (backup.py:74-76)."""
    return Path(store_dir) / f".zmem-{name}.lock"


@dataclass
class NightWindowConfig:
    owner: str = "scratch-owner"
    timezone: str = "UTC"
    idle_window: tuple[int, int] = (2, 5)  # local hours
    min_free_mb: int = 0
    max_catchup_runs: int = 1
    namespaces: tuple[str, ...] = ("project:synthetic/curator",)
    store_dir: Path = field(default_factory=Path)

    def to_json(self) -> dict:
        return {
            "owner": self.owner, "timezone": self.timezone,
            "idle_window": list(self.idle_window), "min_free_mb": self.min_free_mb,
            "max_catchup_runs": self.max_catchup_runs,
            "namespaces": list(self.namespaces),
        }


# ---------------------------------------------------------------------------
# Run ledger (admit-once idempotence)
# ---------------------------------------------------------------------------

class RunLedger:
    """Admit-once ledger on a scratch sqlite: a run-id applies exactly once, ever."""

    def __init__(self, ledger_path: Path):
        self.path = Path(ledger_path)
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS curator_runs ("
            " run_id TEXT PRIMARY KEY, owner TEXT, applied_json TEXT, at TEXT)"
        )
        conn.commit()
        conn.close()

    def admit(self, run_id: str, owner: str, applied: dict) -> bool:
        """Returns True iff THIS call performed the apply (idempotent re-run: False)."""
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT applied_json FROM curator_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is not None:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "INSERT INTO curator_runs VALUES (?,?,?,?)",
                (run_id, owner, json.dumps(applied, sort_keys=True),
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            return True
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Lock discipline (fail-closed)
# ---------------------------------------------------------------------------

class LockUnavailable(RuntimeError):
    pass


def acquire_production_lock(store_dir: Path, stale_seconds: float = 1800.0,
                            acquire_fn: Callable[[str, float], str | None] | None = None):
    """Acquire the REAL production lock on the isolated store; FAIL-CLOSED on None
    or the degraded "unlocked" token (host.py:829). acquire_fn is injectable for the
    degraded-lock mutant probe; production code is never patched."""
    fn = acquire_fn or acquire_lock
    path = lock_path_for(store_dir)
    token = fn(str(path), stale_seconds)
    if token is None or token == "unlocked":
        raise LockUnavailable(
            f"lock_unavailable: acquire returned {token!r}; refusing to open a "
            "transaction or apply (fail-closed)"
        )
    return (path, token)


# ---------------------------------------------------------------------------
# The apply protocol (AC4)
# ---------------------------------------------------------------------------

@dataclass
class ApplyOutcome:
    status: str  # applied | already_applied | revalidation_mismatch | lock_unavailable
    detail: str = ""
    run_id: str = ""


def snapshot_store(store_path: Path, budget: resources.Budget) -> dict[str, Any]:
    """One-shot bounded snapshot read; cursors closed and conn closed before return
    (inference never holds a db connection)."""
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, namespace, content, superseded_at, ingestion_ts FROM memory "
            "ORDER BY id"
        ).fetchall()
        if len(rows) > budget.max_rows:
            raise AssertionError(
                f"budget(snapshot): {len(rows)} rows exceeds cap {budget.max_rows}"
            )
        return {
            "rows": [
                {"id": r[0], "namespace": r[1], "content": resources.redact(r[2] or ""),
                 "superseded_at": r[3], "ingestion_ts": r[4]}
                for r in rows
            ],
            "taken_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        conn.close()


def revalidate(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> bool:
    """INSIDE the writer transaction: every snapshotted row must still match its
    revision (updated_at/superseded_at unchanged)."""
    for row in snapshot["rows"]:
        cur = conn.execute(
            "SELECT ingestion_ts, superseded_at FROM memory WHERE id=?", (row["id"],)
        ).fetchone()
        if cur is None or cur[0] != row["ingestion_ts"] or cur[1] != row["superseded_at"]:
            return False
    return True


def apply_authorized_work(
    store_path: Path,
    ledger: RunLedger,
    run_id: str,
    owner: str,
    work: dict,
    acquire_fn: Callable[[str, float], str | None] | None = None,
    store_dir: Path | None = None,
) -> ApplyOutcome:
    """The fixed sequence: lock -> BEGIN IMMEDIATE -> revalidate -> admit-once apply
    -> commit -> release."""
    store_dir = store_dir or Path(store_path).parent
    try:
        path, token = acquire_production_lock(store_dir, acquire_fn=acquire_fn)
    except LockUnavailable as exc:
        return ApplyOutcome(status="lock_unavailable", detail=str(exc), run_id=run_id)
    try:
        conn = sqlite3.connect(str(store_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            snapshot_ok = revalidate(conn, {"rows": work.get("expected_rows", [])})
            if not snapshot_ok:
                conn.execute("ROLLBACK")
                return ApplyOutcome(
                    status="revalidation_mismatch",
                    detail="source rows changed during inference; bounded retry next run",
                    run_id=run_id,
                )
            first_time = ledger.admit(run_id, owner, work.get("payload", {}))
            conn.execute("COMMIT")
            return ApplyOutcome(
                status="applied" if first_time else "already_applied",
                run_id=run_id,
            )
        finally:
            conn.close()
    finally:
        release_lock(str(path), token)


# ---------------------------------------------------------------------------
# Cadence fidelity (real gate, seeded meta, never force)
# ---------------------------------------------------------------------------

def seed_cadence_meta(store_path: Path, age_days: float, live_count: int = 10) -> None:
    """Seed the production cadence clock at a controlled age (no clock patching).

    Timestamp format matches the production gate's parser ('%Y-%m-%dT%H:%M:%SZ').
    """
    ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(str(store_path))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_consolidation', ?)",
            (ts,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES "
            "('last_consolidation_count', ?)", (str(live_count),)
        )
        conn.commit()
    finally:
        conn.close()


def read_cadence_meta(store_path: Path) -> dict[str, str]:
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
    finally:
        conn.close()


def run_cadence_gate(store_path: Path, dry_run: bool = False) -> dict[str, Any]:
    """Run the REAL production cadence gate against the scratch store (never forced).

    Imports the gate by full module path: storelib's package __init__ re-exports
    the FUNCTION `consolidate`, which shadows the module attribute of the same
    name (`storelib.consolidate` is the function, not the module).
    """
    from storelib.consolidate import _cadence_gate_skipped as _gate
    conn = sqlite3.connect(str(store_path))
    try:
        skipped = _gate(
            conn, force=False, dry_run=dry_run, label="curator-shadow"
        )
        return {"skipped": bool(skipped)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Shadow scheduler (fake clock; windows / missed wakes / overlap / DST)
# ---------------------------------------------------------------------------

@dataclass
class NightDecision:
    night: int
    action: str  # run | skip_busy | skip_low_resource | coalesced | overlap_skipped | empty_input
    reason: str = ""
    run_id: str = ""


class ShadowScheduler:
    """Fake-clock scheduler over simulated nights. Single-flight via the shadow lock;
    the production lock is taken ONLY inside apply_authorized_work."""

    def __init__(self, config: NightWindowConfig, now_fn: Callable[[], datetime] | None = None):
        self.config = config
        self.now = now_fn or (lambda: datetime.now(timezone.utc))
        self._last_shadow_token: tuple[Path, str] | None = None

    def _shadow_lock_acquire(self) -> tuple[Path, str] | None:
        path = lock_path_for(self.config.store_dir, SHADOW_LOCK_NAME)
        token = acquire_lock(str(path), 600.0)
        if token is None or token == "unlocked":
            return None
        return (path, token)

    def _shadow_lock_release(self, held) -> None:
        if held:
            release_lock(str(held[0]), held[1])

    def run_nights(self, nights: list[dict], ledger: RunLedger) -> list[NightDecision]:
        """nights: [{index, busy: bool, free_mb: int, empty_input: bool,
        overlap_prev: bool, run_id}] — a scripted fake-clock scenario."""
        out: list[NightDecision] = []
        for n in nights:
            idx = n["index"]
            if n.get("overlap_prev"):
                out.append(NightDecision(idx, "overlap_skipped",
                                         "previous run still holds the shadow lock"))
                continue
            if n.get("busy"):
                out.append(NightDecision(idx, "skip_busy", "host busy in idle window"))
                continue
            if n.get("free_mb", 10**9) < self.config.min_free_mb:
                out.append(NightDecision(idx, "skip_low_resource",
                                         f"free {n.get('free_mb')}MB < min"))
                continue
            if n.get("empty_input"):
                out.append(NightDecision(idx, "empty_input", "no changed rows"))
                continue
            held = self._shadow_lock_acquire()
            if held is None:
                out.append(NightDecision(idx, "overlap_skipped",
                                         "shadow lock held by another scheduler"))
                continue
            try:
                run_id = n.get("run_id") or f"night-{idx}-{uuid.uuid4().hex[:8]}"
                out.append(NightDecision(idx, "run", "", run_id))
                ledger.admit(run_id, self.config.owner, {"night": idx})
            finally:
                self._shadow_lock_release(held)
        return out

    def coalesce_missed(self, missed_count: int) -> int:
        """Missed wakes coalesce into AT MOST max_catchup_runs catch-up runs."""
        return min(missed_count, self.config.max_catchup_runs)

    def dst_offset_check(self, local_hour_cb: Callable[[], int]) -> bool:
        """The idle window is evaluated in LOCAL hours; a DST shift that moves the
        window boundary is honored (the callback supplies the local hour)."""
        start, end = self.config.idle_window
        h = local_hour_cb()
        return start <= h < end

    def rotate_old_sample(self, previous: list[str], changed: list[str], cap: int = 20) -> list[str]:
        """Bounded rotating older sample: newest-first, capped."""
        merged = list(dict.fromkeys(changed + previous))
        return merged[:cap]


# ---------------------------------------------------------------------------
# Snapshot filter (namespace authz / retention / protection / no summaries)
# ---------------------------------------------------------------------------

PROTECTED_SIGNALS = {"reviewer", "user"}
HIGH_SIGNAL_FLOOR = 0.8


def filter_snapshot_rows(rows: list[dict], namespaces: tuple[str, ...]) -> dict[str, Any]:
    """Namespace-authorized, retention-aware, contradiction-excluding,
    protected/high-signal-preserving; summaries structurally excluded
    (mirrors organize.py:816's summary-exclusion seam)."""
    selected: list[dict] = []
    excluded: list[str] = []
    for r in rows:
        ns = r.get("namespace", "")
        if ns not in namespaces:
            excluded.append(f"{r['id']}:namespace_unauthorized")
            continue
        if r.get("kind") in ("summary", "consolidation_summary"):
            excluded.append(f"{r['id']}:summary_excluded")
            continue
        if r.get("contested"):
            excluded.append(f"{r['id']}:unresolved_contradiction_auto_excluded")
            continue
        if r.get("signal") in PROTECTED_SIGNALS or r.get("confidence", 0) >= HIGH_SIGNAL_FLOOR:
            r = dict(r, protected=True)
        selected.append(r)
    return {"selected": selected, "excluded": excluded}


def morning_report(decisions: list[NightDecision], applied: list[dict],
                   proposed: list[dict], skipped_expired: list[str],
                   cost: dict, recovery: list[str]) -> dict:
    """Fixed morning-report fields (plan R2-9)."""
    return {
        "applied_deterministic": applied,
        "proposed_semantic_changes": proposed,
        "skipped_and_expired": skipped_expired,
        "nights": [{"night": d.night, "action": d.action, "reason": d.reason}
                   for d in decisions],
        "resource_cost": cost,
        "recovery_pointers": recovery,
    }


# ---------------------------------------------------------------------------
# enable / disable / dry-run CLI (scratch config dir only)
# ---------------------------------------------------------------------------

def _cfg_path(cfg_dir: Path) -> Path:
    return Path(cfg_dir) / "nightly-curator.json"


def cmd_enable(cfg_dir: Path, config: NightWindowConfig) -> Path:
    _cfg_path(cfg_dir).parent.mkdir(parents=True, exist_ok=True)
    _cfg_path(cfg_dir).write_text(
        json.dumps({"enabled": True, **config.to_json()}, indent=1) + "\n",
        encoding="utf-8", newline="\n",
    )
    return _cfg_path(cfg_dir)


def cmd_disable(cfg_dir: Path) -> bool:
    p = _cfg_path(cfg_dir)
    if not p.exists():
        return False
    p.unlink()
    return True


def cmd_dry_run(cfg_dir: Path) -> dict:
    p = _cfg_path(cfg_dir)
    if not p.exists():
        return {"status": "disabled", "reason": "no config file (disabled)"}
    cfg = json.loads(p.read_text(encoding="utf-8"))
    if not cfg.get("enabled"):
        return {"status": "disabled", "reason": "config disabled"}
    return {
        "status": "dry-run",
        "config": cfg,
        "would_run": True,
        "note": "shadow evaluation only; no live schedule is registered (issue #377 "
                "keeps production enablement out of scope)",
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: orchestration.py {enable|disable|dry-run} <cfg-dir>", file=sys.stderr)
        return 2
    cmd, cfg_dir = argv[0], Path(argv[1])
    if cmd == "enable":
        p = cmd_enable(cfg_dir, NightWindowConfig(store_dir=cfg_dir))
        print(json.dumps({"status": "enabled", "config": str(p)}))
        return 0
    if cmd == "disable":
        print(json.dumps({"status": "disabled" if cmd_disable(cfg_dir) else "already-disabled"}))
        return 0
    if cmd == "dry-run":
        print(json.dumps(cmd_dry_run(cfg_dir)))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
