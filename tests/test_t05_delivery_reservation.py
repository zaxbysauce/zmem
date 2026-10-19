"""Issue #284 — delivery reservation (T-05) acceptance checks, Phase 2.5.

Turns the issue's acceptance criteria into executable checks against the
REAL surface (store.py CLI subprocess legs; the absorbed #362 admit-once
concurrency half lives in tests/test_delivery_ledger_concurrency.py).
Every check names its standing at both ends:

- AC1 ``test_unacked_selection_is_not_suppressed_on_retry`` —
  ``prefetch --reserve`` selects WITHOUT recording those ids in the
  delivery ledger; an identical retry (same session id, same query)
  inside the reservation TTL returns the SAME ids (count > 0, same
  candidate ids), not the ``already-delivered`` silent turn; and the
  reserving envelope carries a reservation identifier the ack flow can
  use. RED at base (``unrecognized arguments: --reserve``), GREEN after
  the fix.
- AC2 ``test_ack_commits_once_and_is_idempotent`` — after AC1's
  reserving call, ``prefetch-ack --reservation <id>`` records EXACTLY
  the reserved ids in the ledger once; a duplicate ack is a no-op
  (ledger ids unchanged, exit 0). RED at base (the reserving leg fails
  on ``--reserve``; the ack leg would fail with ``invalid choice:
  'prefetch-ack'`` — the subcommand is absent), GREEN after the fix.
- AC3 ``test_ledger_is_keyed_by_principal_harness_and_session`` — two
  callers with DIFFERENT principal/harness identity but the SAME native
  session id keep SEPARATE ledgers: delivery to one never suppresses
  the other's identical query, and the two deliveries land in distinct
  ledger files. RED at base (the identity inputs do not exist:
  ``unrecognized arguments: --principal ...``), GREEN after the fix.

Authored CLI contract (the implementer must satisfy it):
- ``store.py prefetch ... --reserve`` — the same closed selector
  envelope plus a reservation identifier under a reservation-named key
  (``reservation`` / ``reservation_id`` / a nested ``reserve`` dict … —
  the check does not guess the exact name, it demands SOME non-empty
  string handle the retry/ack flow can use). Selection under --reserve
  records NOTHING in the delivery-ledger family
  (``ops/<sha256(session_id)[:32]>.ledger`` plus any
  ``.ledger.seg.*`` segments, the shapes frozen by
  tests/test_delivery_ledger.py), and the reservation does not expire
  within the span of an immediate retry.
- ``store.py prefetch-ack --reservation <id>`` — commits exactly the
  reserved ids into the session-keyed delivery ledger; the reservation
  id alone identifies the reservation (env-pinned ZMEM_DATA/ZMEM_STORE
  resolve the sidecar state); a duplicate ack exits 0 and changes
  nothing.
- ``store.py prefetch ... --principal P --harness H`` — identity
  inputs keying the delivery ledger by (principal, harness, session id)
  so two identities sharing one native session id never share one
  ledger file.

Env discipline: every test is a module-level function that pins a fresh
GUID scratch dir (ZMEM_HOME=repo root, ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR
plus ZMEM_MODEL_AUTODOWNLOAD=0 and PYTHONIOENCODING=utf-8) BEFORE any
store.py subprocess, strips every delivery-affecting ambient override
(kill switch, inject budget, ledger window/cap, namespace, cross-project
env, MCP token config, session/host identity), and restores the saved
environment afterwards. Each test seeds its own store (init + add at
confidence 0.9, above the selector's 0.25 floor) and uses unique session
ids.

AC4/AC8 (preserving): tests/test_delivery_ledger.py — including
LedgerModuleTest::test_record_delivered_roundtrip_and_upsert — is the
frozen check and is not touched by this file.

Runs standalone: python tests/test_t05_delivery_reservation.py
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_PY = str(REPO_ROOT / "skills" / "memory" / "scripts" / "store.py")

_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_INJECT",
    "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    "ZMEM_NAMESPACE", "ZMEM_CROSS_PROJECT", "ZMEM_CROSS_PROJECT_HAZARD_VERBS",
    "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE",
    "ZMEM_HOST", "ZMEM_SESSION", "ZMEM_QUERY_CONTEXT",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID", "ZMEM_PENDING_SIDECAR",
)


@contextlib.contextmanager
def _pinned_scratch(prefix: str):
    """Fresh GUID scratch + env pin (per-test; module-level functions)."""
    tmp = tempfile.mkdtemp(prefix=f"zmem-t05-{prefix}-{uuid.uuid4().hex}-")
    saved = {k: os.environ.get(k) for k in _PIN_KEYS}
    for key in _PIN_KEYS:
        os.environ.pop(key, None)
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_store(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, STORE_PY, *args],
        env=dict(os.environ), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


def _init_store() -> None:
    r = _run_store("init")
    assert r.returncode == 0, f"store.py init failed: {r.stderr}"


def _seed_row(namespace: str, content: str) -> str:
    """Seed one grounded high-confidence row; returns its minted id."""
    r = _run_store("add", "--namespace", namespace, "--type", "lesson",
                   "--content", content, "--signal", "test",
                   "--confidence", "0.9", "--json")
    assert r.returncode == 0, f"store.py add failed: {r.stderr}"
    return json.loads(r.stdout)["id"]


# --- delivery-ledger file readers (the shapes frozen by #117/#252) --------


def _session_stem(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def _ledger_entry_ids(path: Path) -> set:
    """Delivered ids in one ledger-family file.

    Tolerates both durable shapes: the base ledger (one JSON doc
    ``{"entries": [...]}``) and a #252 segment (JSONL entry dicts).
    """
    ids = set()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ids
    entries = None
    try:
        doc = json.loads(raw)
        if isinstance(doc, dict) and isinstance(doc.get("entries"), list):
            entries = doc["entries"]
    except ValueError:
        entries = None
    if entries is None:
        entries = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                entries.append(e)
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("id"), str):
            ids.add(e["id"])
    return ids


def _session_ledger_ids(data_dir: str, session_id: str) -> set:
    """Ids in the session's delivery ledger: the base ``.ledger`` file plus
    any live ``.ledger.seg.*`` segments (the #252 concurrent-writer shapes).
    """
    ops = Path(data_dir) / "ops"
    stem = _session_stem(session_id)
    ids = _ledger_entry_ids(ops / (stem + ".ledger"))
    if ops.is_dir():
        for p in ops.glob(stem + ".ledger.seg.*"):
            ids |= _ledger_entry_ids(p)
    return ids


def _ledger_family_files(data_dir: str) -> list:
    """Every delivery-ledger family file under ops (any session key):
    ``*.ledger`` bases and ``*.ledger.seg.*`` segments. Reservation state
    files are not this family and never match these patterns."""
    ops = Path(data_dir) / "ops"
    if not ops.is_dir():
        return []
    return sorted(p for p in ops.iterdir()
                  if p.name.endswith(".ledger")
                  or ".ledger.seg." in p.name)


def _reservation_token(envelope: dict) -> str:
    """A reservation identifier from the envelope: any non-empty string
    value under a reservation-named key, at the top level or one dict
    level down (``reservation`` / ``reservation_id`` / a nested
    ``reserve`` object). The check does not guess the exact key name; it
    demands that SOME handle exists for the retry/ack flow."""
    found: list = []

    def _scan(value: dict, depth: int = 0) -> None:
        for key, item in value.items():
            if "reserv" in str(key).lower():
                if isinstance(item, str) and item.strip():
                    found.append(item)
                elif isinstance(item, dict):
                    # a reservation-named container: any non-empty string
                    # value inside it is a handle ({"token": "..."} ...)
                    for sub in item.values():
                        if isinstance(sub, str) and sub.strip():
                            found.append(sub)
            elif isinstance(item, dict) and depth < 2:
                _scan(item, depth + 1)

    _scan({k: v for k, v in envelope.items() if k != "results"})
    assert found, (
        "the --reserve envelope carries no reservation identifier (no "
        "non-empty string under any reservation-named key) — the retry/ack "
        f"flow has no handle: {sorted(envelope)}")
    return found[0]


def test_unacked_selection_is_not_suppressed_on_retry():
    """AC1 (issue #284): ``prefetch --reserve`` selects WITHOUT recording
    those ids in the delivery ledger; an identical retry (same session id,
    same query) within the reservation TTL returns the SAME ids (count >
    0, same candidate ids — not the ``already-delivered`` silent turn),
    and the reserving envelope carries a reservation identifier. RED at
    base (``unrecognized arguments: --reserve``), GREEN after the fix."""
    marker = f"t05-ac1-{uuid.uuid4().hex}"
    session_id = f"t05-ac1-{uuid.uuid4()}"
    with _pinned_scratch("ac1") as tmp:
        _init_store()
        _seed_row("project:t05-ac1", f"{marker} reservation row one")
        _seed_row("project:t05-ac1", f"{marker} reservation row two")
        argv = ("prefetch", "--query", marker,
                "--namespace", "project:t05-ac1",
                "--session-id", session_id, "--moment", "user_prompt",
                "--reserve", "--json")
        first = _run_store(*argv)
        assert first.returncode == 0, (
            "the reserving prefetch failed (at base argparse rejects the "
            f"reservation flag): rc={first.returncode} "
            f"stderr={first.stderr.strip()}")
        env1 = json.loads(first.stdout)
        first_ids = list(env1.get("candidate_ids") or [])
        assert env1.get("count", 0) > 0, (
            f"the reserving prefetch selected nothing: {env1}")
        assert first_ids, f"no candidate_ids on the reserving call: {env1}"

        # The reserving call must NOT have written the ids into the ledger.
        session_leak = _session_ledger_ids(tmp, session_id) & set(first_ids)
        assert not session_leak, (
            "the reserving prefetch recorded the selected ids in the "
            f"session delivery ledger: leaked {sorted(session_leak)}")
        family_leak = {}
        for p in _ledger_family_files(tmp):
            leak = _ledger_entry_ids(p) & set(first_ids)
            if leak:
                family_leak[p.name] = sorted(leak)
        assert not family_leak, (
            "the reserving prefetch recorded the selected ids in some "
            f"delivery-ledger family file: {family_leak}")

        # The envelope must carry a reservation identifier for the ack flow.
        token = _reservation_token(env1)
        assert token and token.strip(), env1

        # Identical retry (same session id, same query) inside the TTL:
        # the SAME ids come back — not the already-delivered silent turn.
        retry = _run_store(*argv)
        assert retry.returncode == 0, (
            f"the identical retry failed: rc={retry.returncode} "
            f"stderr={retry.stderr.strip()}")
        env2 = json.loads(retry.stdout)
        retry_ids = list(env2.get("candidate_ids") or [])
        assert env2.get("count", 0) > 0, (
            "the identical retry inside the reservation TTL came back "
            f"silent (reason={env2.get('reason')!r}) — an un-acked "
            f"reservation must not suppress re-selection: {env2}")
        assert env2.get("reason") != "already-delivered", env2
        assert (set(retry_ids) == set(first_ids)
                and len(retry_ids) == len(first_ids)), (
            f"the retry returned different ids: first={first_ids} "
            f"retry={retry_ids}")


def test_ack_commits_once_and_is_idempotent():
    """AC2 (issue #284): after the reserving call, ``prefetch-ack
    --reservation <id>`` records EXACTLY the reserved ids in the ledger
    once; a duplicate ack is a no-op (ledger ids unchanged, exit 0). RED
    at base (the reserving leg fails on ``--reserve``; the ack leg would
    fail with ``invalid choice: 'prefetch-ack'``), GREEN after the fix."""
    marker = f"t05-ac2-{uuid.uuid4().hex}"
    session_id = f"t05-ac2-{uuid.uuid4()}"
    with _pinned_scratch("ac2") as tmp:
        _init_store()
        row_id = _seed_row("project:t05-ac2", f"{marker} ack row")
        reserve = _run_store(
            "prefetch", "--query", marker, "--namespace", "project:t05-ac2",
            "--session-id", session_id, "--moment", "user_prompt",
            "--reserve", "--json")
        assert reserve.returncode == 0, (
            "the reserving prefetch failed (at base argparse rejects the "
            f"reservation flag): rc={reserve.returncode} "
            f"stderr={reserve.stderr.strip()}")
        env1 = json.loads(reserve.stdout)
        reserved = list(env1.get("candidate_ids") or [])
        assert reserved, f"no reserved ids on the reserving call: {env1}"
        token = _reservation_token(env1)

        # pre-ack: the reservation itself recorded nothing
        assert row_id not in _session_ledger_ids(tmp, session_id), (
            "the reserving prefetch recorded the row before any ack")

        ack = _run_store("prefetch-ack", "--reservation", token)
        assert ack.returncode == 0, (
            "prefetch-ack failed (at base the subcommand is absent: "
            f"invalid choice): rc={ack.returncode} "
            f"stderr={ack.stderr.strip()}")
        committed = _session_ledger_ids(tmp, session_id)
        assert committed == set(reserved), (
            f"the ack must commit exactly the reserved ids "
            f"{sorted(reserved)}; the ledger holds {sorted(committed)}")

        dup = _run_store("prefetch-ack", "--reservation", token)
        assert dup.returncode == 0, (
            f"the duplicate ack must exit 0: rc={dup.returncode} "
            f"stderr={dup.stderr.strip()}")
        after = _session_ledger_ids(tmp, session_id)
        assert after == committed, (
            "the duplicate ack mutated the ledger "
            f"({sorted(committed)} -> {sorted(after)}); it must be a no-op")


def test_ledger_is_keyed_by_principal_harness_and_session():
    """AC3 (issue #284): two callers with DIFFERENT principal/harness
    identity but the SAME native session id keep SEPARATE ledgers —
    delivery to one never suppresses the other's identical query, and the
    two deliveries land in distinct ledger files. RED at base (the
    identity inputs do not exist: ``unrecognized arguments: --principal
    ...``), GREEN after the fix."""
    marker = f"t05-ac3-{uuid.uuid4().hex}"
    session_id = f"t05-ac3-{uuid.uuid4()}"  # the SAME native session id
    ns = "project:t05-ac3"
    with _pinned_scratch("ac3") as tmp:
        _init_store()
        row_id = _seed_row(ns, f"{marker} keyed ledger row")

        def caller(principal: str, harness: str) -> dict:
            r = _run_store("prefetch", "--query", marker,
                           "--namespace", ns, "--session-id", session_id,
                           "--moment", "user_prompt",
                           "--principal", principal, "--harness", harness,
                           "--json")
            assert r.returncode == 0, (
                "the prefetch identity inputs were rejected (at base "
                "argparse does not know --principal/--harness): "
                f"rc={r.returncode} stderr={r.stderr.strip()}")
            return json.loads(r.stdout)

        env_a = caller("t05-principal-a", "claude-code")
        env_b = caller("t05-principal-b", "codex-cli")

        # Caller B (same session id, identical query) must NOT be
        # suppressed by caller A's delivery.
        assert env_a.get("count", 0) > 0, f"caller A selected nothing: {env_a}"
        assert env_b.get("count", 0) > 0, (
            "caller B was suppressed by caller A's delivery under the SAME "
            f"session id (reason={env_b.get('reason')!r}): {env_b}")
        assert env_b.get("reason") != "already-delivered", env_b
        ids_a = set(env_a.get("candidate_ids") or [])
        ids_b = set(env_b.get("candidate_ids") or [])
        assert row_id in ids_a, f"caller A missed the row: {env_a}"
        assert ids_a == ids_b and row_id in ids_b, (
            f"caller B's selection diverged: a={sorted(ids_a)} "
            f"b={sorted(ids_b)}")

        # Separate ledgers: the two deliveries must land in DISTINCT
        # ledger files, each carrying its own caller's delivery record.
        holders = [p for p in _ledger_family_files(tmp)
                   if row_id in _ledger_entry_ids(p)]
        assert len(holders) >= 2, (
            "the two callers' deliveries share one ledger file — the "
            "ledger must be keyed by (principal, harness, session), not "
            f"the bare session id; ledger family: "
            f"{[p.name for p in _ledger_family_files(tmp)]}")


def test_identity_dimensions_isolate_independently():
    """Review round 2 (finding 6): AC3 varies principal AND harness
    TOGETHER, so a single-dimension drop — principal lost from the
    sidecar, harness lost at commit, either lost from _session_key —
    passes it while two callers' deliveries alias into one ledger. This
    pin varies the dimensions independently (same harness / different
    principal; same principal / different harness), and checks the
    bare-session ledger stays empty under keyed callers (no aliasing to
    the legacy session-only key). Also pins the unknown- and malformed-id
    ack refusals (exit 2)."""
    marker = f"t05-ac3b-{uuid.uuid4().hex}"
    session_id = f"t05-ac3b-{uuid.uuid4()}"  # the SAME native session id
    ns = "project:t05-ac3b"
    with _pinned_scratch("ac3b") as tmp:
        _init_store()
        row_id = _seed_row(ns, f"{marker} identity isolation row")

        def caller(principal=None, harness=None) -> dict:
            argv = ["prefetch", "--query", marker,
                    "--namespace", ns, "--session-id", session_id,
                    "--moment", "user_prompt", "--json"]
            if principal is not None:
                argv += ["--principal", principal]
            if harness is not None:
                argv += ["--harness", harness]
            r = _run_store(*argv)
            assert r.returncode == 0, (
                f"prefetch failed rc={r.returncode}: {r.stderr.strip()}")
            return json.loads(r.stdout)

        # Same harness, DIFFERENT principal.
        env_a = caller("t05-id-principal-a", "claude-code")
        env_b = caller("t05-id-principal-b", "claude-code")
        # Same principal, DIFFERENT harness.
        env_c = caller("t05-id-principal-a", "codex-cli")
        # Bare session (no identity): the legacy session-only key.
        env_bare = caller()

        for name, env in (("a", env_a), ("b", env_b), ("c", env_c),
                          ("bare", env_bare)):
            assert env.get("count", 0) > 0, (
                f"caller {name} selected nothing: {env}")
            assert env.get("reason") != "already-delivered", (
                f"caller {name} was suppressed by an earlier caller under "
                f"the same native session id (single-dimension key drop): "
                f"{env}")

        # Distinct ledgers: all four deliveries land in distinct ledger
        # files — the two keyed dimensions isolate independently AND the
        # keyed callers never touch the bare session-only ledger.
        holders = [p for p in _ledger_family_files(tmp)
                   if row_id in _ledger_entry_ids(p)]
        assert len(holders) >= 4, (
            "the four callers' deliveries collapsed into fewer ledgers — "
            f"the identity dimensions must isolate independently; family: "
            f"{[p.name for p in _ledger_family_files(tmp)]}")

        # Review round 2: unknown-id ack is the exit-2 refusal, and the
        # id shape check keeps a crafted path id from resolving outside
        # ops/ (it reports the same refusal).
        r = _run_store("prefetch-ack", "--reservation", "0" * 32)
        assert r.returncode == 2, (
            f"unknown-but-well-formed reservation must exit 2: "
            f"rc={r.returncode} stderr={r.stderr.strip()}")
        r2 = _run_store("prefetch-ack", "--reservation", "../../evil")
        assert r2.returncode == 2, (
            f"malformed reservation id must exit 2 (never resolve outside "
            f"ops/): rc={r2.returncode} stderr={r2.stderr.strip()}")


_TESTS = (
    test_unacked_selection_is_not_suppressed_on_retry,
    test_ack_commits_once_and_is_idempotent,
    test_ledger_is_keyed_by_principal_harness_and_session,
    test_identity_dimensions_isolate_independently,
)


if __name__ == "__main__":
    failures = []
    for test in _TESTS:
        name = test.__name__
        try:
            test()
        except unittest.SkipTest as exc:
            print(f"skip  {name}: {exc}")
            continue
        except AssertionError as exc:
            failures.append((name, f"AssertionError: {exc}"))
            print(f"FAIL  {name}: AssertionError: {exc}")
            continue
        except Exception as exc:  # a crashed check is a failed check
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            continue
        print(f"ok    {name}")
    if failures:
        print(f"\n{len(failures)} failing test(s):")
        for name, error in failures:
            print(f"  {name}: {error}")
        sys.exit(1)
    print("\nall tests passed")
    sys.exit(0)
