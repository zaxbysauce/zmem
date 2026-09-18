"""Maintainer-only generator for the committed #155 replay fixture.

The generator deliberately builds into a fresh scratch directory, then
canonicalizes only fixture-owned runtime metadata.  Tests consume committed
bytes and never invoke this module to create their own oracle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
import runpy


ROOT = Path(__file__).resolve().parents[3]
EVAL_FIXTURE = Path(__file__).resolve().parents[1] / "eval_store.py"
EVAL_PIN_TS = "2026-06-01T00:00:00Z"
BASE_TS = 1780272000
DECISION_IDS = ("e0000000-0000-4000-8000-000000000065", "e0000000-0000-4000-8000-000000000066")
LANES = ("claude", "hermes-provider")
MOMENTS = ("session_start", "user_prompt", "pretool", "precompact")


def _env(store: Path, scratch: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("ZMEM_") or key in {"CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"}:
            env.pop(key, None)
    env.update({
        "ZMEM_STORE": str(store),
        "ZMEM_DATA": str(scratch / "data"),
        "ZMEM_HOME": str(scratch / "home"),
        "ZMEM_MODELS_DIR": str(scratch / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_EMBED_PROFILE": "fake",
        "ZMEM_TEST_NOW": EVAL_PIN_TS,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _expected_memory_contents() -> dict[int, str]:
    """Materialize the actual eval-store builder's complete 70-row contract."""
    fixture = runpy.run_path(str(EVAL_FIXTURE), run_name="zmem_eval_store_fixture")
    expected: dict[int, str] = {}

    for rowid, row in enumerate(fixture["_asof_rows"](), start=1):
        expected[rowid] = row["content"]
    for start, rows, content_index in (
        (11, fixture["_injection_adds"](), 3),
        (21, fixture["_namespace_adds"](), 3),
        (31, fixture["_contested_adds"](), 3),
    ):
        for rowid, row in enumerate(rows, start=start):
            expected[rowid] = row[content_index]
    for rowid, row in enumerate(fixture["ENTITY_ROWS"], start=41):
        expected[rowid] = row[1]
    for rowid, row in enumerate(fixture["FTS_ROWS"], start=46):
        expected[rowid] = row[0]
    for rowid, content, _window, _reason in fixture["RETRACT_ROWS"]:
        expected[rowid] = content
    for rowid, content in fixture["POLARITY_ROWS"]:
        expected[rowid] = content
    for rowid, (old_content, new_content) in zip(
        fixture["CHANGE_CHAIN_PREDS"], fixture["CHANGE_CHAINS"]
    ):
        expected[rowid] = old_content
    for rowid, (_old_content, new_content) in zip(
        fixture["CHANGE_CHAIN_HEADS"], fixture["CHANGE_CHAINS"]
    ):
        expected[rowid] = new_content
    for rowid, content, _ops in fixture["DECISION_ROWS"]:
        expected[rowid] = content
    if set(expected) != set(range(1, 71)):
        raise RuntimeError("eval-store memory corpus contract does not cover rowids 1..70")
    return expected


def _verify_corpus(conn: sqlite3.Connection) -> None:
    """Fail generation if canonicalization changes the builder's corpus contract."""
    fixture = runpy.run_path(str(EVAL_FIXTURE), run_name="zmem_eval_store_fixture")
    expected_content = _expected_memory_contents()
    rows = conn.execute("SELECT rowid, id, content FROM memory ORDER BY rowid").fetchall()
    expected_ids = {rowid: f"e0000000-0000-4000-8000-{rowid:012d}" for rowid in range(1, 71)}
    actual_ids = {rowid: memory_id for rowid, memory_id, _content in rows}
    actual_content = {rowid: content for rowid, _memory_id, content in rows}
    if actual_ids != expected_ids:
        raise RuntimeError("canonical replay store changed the builder memory IDs")
    if actual_content != expected_content:
        raise RuntimeError("canonical replay store changed the builder memory content")

    # Historical as-of windows are an explicit contract: they must survive
    # runtime-only timestamp pinning exactly, rather than being flattened to
    # the pinned clock.
    asof_rows = fixture["_asof_rows"]()
    for rowid, expected in enumerate(asof_rows, start=1):
        actual = conn.execute(
            "SELECT namespace, type, signal, confidence, content, tags, "
            "valid_from, valid_until, superseded_at, ingestion_ts "
            "FROM memory WHERE rowid=?",
            (rowid,),
        ).fetchone()
        wanted = (
            expected["namespace"], expected["type"], expected["signal"],
            expected["confidence"], expected["content"], expected["tags"],
            expected["valid_from"], expected["valid_until"],
            expected["superseded_at"], expected["ingestion_ts"],
        )
        if actual is None or tuple(actual) != wanted:
            raise RuntimeError(f"as-of corpus row {rowid} changed during canonicalization")
    for rowid, content, window, reason in fixture["RETRACT_ROWS"]:
        actual = conn.execute(
            "SELECT content, valid_from, valid_until, superseded_at, supersede_reason "
            "FROM memory WHERE rowid=?",
            (rowid,),
        ).fetchone()
        wanted = (content, window[0], window[1], window[1], reason)
        if actual is None or tuple(actual) != wanted:
            raise RuntimeError(f"retraction corpus row {rowid} changed during canonicalization")

    memory_ids = set(expected_ids.values())
    entity_ids = {row[0] for row in conn.execute("SELECT id FROM entity")}
    for entity_id in entity_ids:
        try:
            uuid.UUID(entity_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise RuntimeError("canonical entity IDs must remain UUID-shaped") from exc
    aliases = conn.execute(
        "SELECT entity_id, alias_norm FROM entity_alias"
    ).fetchall()
    if len(aliases) != 25 or any(
        entity_id not in entity_ids or not isinstance(alias_norm, str) or not alias_norm
        for entity_id, alias_norm in aliases
    ):
        raise RuntimeError("entity_alias contains an orphaned or empty canonical reference")
    for memory_id, entity_id, _role in conn.execute(
        "SELECT memory_id, entity_id, role FROM memory_entity"
    ):
        if memory_id not in memory_ids or entity_id not in entity_ids:
            raise RuntimeError("memory_entity contains an orphaned canonical reference")
    update_refs = conn.execute(
        "SELECT id, update_of FROM memory WHERE update_of <> ''"
    ).fetchall()
    if len(update_refs) != 3 or any(
        memory_id == update_of or update_of not in memory_ids
        for memory_id, update_of in update_refs
    ):
        raise RuntimeError("memory update_of contains an orphaned canonical reference")
    links = conn.execute("SELECT src_id, dst_id, relation FROM memory_link").fetchall()
    if len(links) != 16 or any(src not in memory_ids or dst not in memory_ids for src, dst, _relation in links):
        raise RuntimeError("memory_link relationship corpus changed during canonicalization")
    for memory_id, evidence_id in conn.execute(
        "SELECT memory_id, evidence_id FROM memory_evidence"
    ):
        if memory_id not in memory_ids or conn.execute(
            "SELECT 1 FROM evidence WHERE id=?", (evidence_id,)
        ).fetchone() is None:
            raise RuntimeError("memory_evidence contains an orphaned canonical reference")


def _canonicalize(store: Path, scratch: Path) -> None:
    """Normalize entity IDs/timestamps and finalize a standalone SQLite file."""
    conn = sqlite3.connect(store)
    try:
        conn.execute("BEGIN")
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "entity" in tables:
            entity_rows = conn.execute(
                "SELECT id, kind, canonical_name FROM entity ORDER BY id"
            ).fetchall()
            mapping: dict[str, str] = {}
            for old_id, kind, name in entity_rows:
                semantic = f"zmem-replay/entity/{kind}/{name}".encode("utf-8")
                mapping[old_id] = str(uuid.uuid5(uuid.NAMESPACE_URL, semantic.decode("utf-8")))
            # Avoid primary-key collisions while remapping in one transaction.
            for index, old_id in enumerate(mapping):
                temporary = f"00000000-0000-4000-8000-{index + 1:012d}"
                conn.execute("UPDATE entity SET id=? WHERE id=?", (temporary, old_id))
                if "entity_alias" in tables:
                    conn.execute("UPDATE entity_alias SET entity_id=? WHERE entity_id=?", (temporary, old_id))
                if "memory_entity" in tables:
                    conn.execute("UPDATE memory_entity SET entity_id=? WHERE entity_id=?", (temporary, old_id))
            for index, old_id in enumerate(mapping):
                temporary = f"00000000-0000-4000-8000-{index + 1:012d}"
                new_id = mapping[old_id]
                conn.execute(
                    "UPDATE entity SET id=?, created_at=?, updated_at=? WHERE id=?",
                    (new_id, EVAL_PIN_TS, EVAL_PIN_TS, temporary),
                )
                if "entity_alias" in tables:
                    conn.execute("UPDATE entity_alias SET entity_id=? WHERE entity_id=?", (new_id, temporary))
                if "memory_entity" in tables:
                    conn.execute("UPDATE memory_entity SET entity_id=? WHERE entity_id=?", (new_id, temporary))
        # The shared eval builder deliberately pins historical validity windows
        # but the real write path's ingestion/embedding/link timestamps use the
        # wall clock.  Normalize only values later than the pinned runtime;
        # historical dates remain untouched and continue to exercise as-of
        # semantics.
        def pin_runtime(value: object) -> object:
            if not isinstance(value, str) or not value:
                return value
            try:
                raw = value.replace("Z", "+00:00")
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt > datetime.fromisoformat(EVAL_PIN_TS.replace("Z", "+00:00")):
                    return EVAL_PIN_TS
            except ValueError:
                pass
            return value
        if "memory" in tables:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
            timestamp_columns = {
                column for column in (
                    "ingestion_ts", "embedded_at", "valid_from", "valid_until",
                    "superseded_at", "last_retrieved", "last_surfaced",
                    "consolidated_at",
                ) if column in columns
            }
            for column in sorted(timestamp_columns):
                rows = conn.execute(f"SELECT rowid, {column} FROM memory").fetchall()
                for rowid, value in rows:
                    normalized = pin_runtime(value)
                    if normalized != value:
                        conn.execute(f"UPDATE memory SET {column}=? WHERE rowid=?", (normalized, rowid))
        # sqlite-vec keeps a packed metadata-chunk dictionary in addition to
        # the readable metadata-text shadow table.  Updating only the latter
        # leaves wall-clock UUIDs embedded in the packed blob, so two fresh
        # eval-store builds remain byte-different.  Reinsert the actual
        # canonical vectors through the extension to rebuild every vec shadow
        # table deterministically.  A builder that produced a vec table must
        # have the same optional extension available when this maintainer-only
        # generator runs; silently retaining a nondeterministic blob would
        # invalidate the fixture reproducibility contract.
        if "memory_vec" in tables:
            try:
                import sqlite_vec
            except ImportError as exc:
                raise RuntimeError("sqlite_vec is required to canonicalize memory_vec") from exc
            conn.enable_load_extension(True)
            try:
                sqlite_vec.load(conn)
                vector_rows = conn.execute(
                    "SELECT rowid, id, embedding FROM memory "
                    "WHERE embedding IS NOT NULL ORDER BY rowid"
                ).fetchall()
                conn.execute("DELETE FROM memory_vec")
                for rowid, memory_id, embedding in vector_rows:
                    conn.execute(
                        "INSERT INTO memory_vec(rowid, embedding, memory_id) "
                        "VALUES (?, ?, ?)",
                        (rowid, embedding, memory_id),
                    )
                expected_vector_refs = {
                    rowid: memory_id for rowid, memory_id, _embedding in vector_rows
                }
                actual_vector_refs = dict(
                    conn.execute("SELECT rowid, memory_id FROM memory_vec").fetchall()
                )
                if actual_vector_refs != expected_vector_refs:
                    raise RuntimeError(
                        "rebuilt memory_vec metadata references do not match memory"
                    )
            finally:
                conn.enable_load_extension(False)
        _verify_corpus(conn)
        for table, column in (("memory_link", "created_at"), ("episode", "started_at"), ("episode", "ended_at")):
            if table not in tables:
                continue
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                continue
            rows = conn.execute(f"SELECT rowid, {column} FROM {table}").fetchall()
            for rowid, value in rows:
                normalized = pin_runtime(value)
                if normalized != value:
                    conn.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (normalized, rowid))
        # Runtime-only metadata must not depend on the wall clock.  Historical
        # memory/episode windows are intentionally left untouched.
        if "meta" in tables:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='created_at'",
                (EVAL_PIN_TS,),
            )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            (store.parent / (store.name + suffix)).unlink()
        except FileNotFoundError:
            pass


def _decision_log(version: str) -> bytes:
    lines: list[str] = []
    for lane, sid, row_id in (
        ("claude", "replay-claude", DECISION_IDS[0]),
        ("hermes-provider", "replay-hermes", DECISION_IDS[1]),
    ):
        for moment, timing in zip(MOMENTS, (10, 20, 30, 40)):
            if moment == "session_start":
                status, reason, ids, all_ids = "injected", "injected", [row_id], [row_id]
            elif moment == "user_prompt":
                status, reason, ids, all_ids = "silent", "already-delivered", [], [row_id]
            elif moment == "pretool":
                status, reason, ids, all_ids = "silent", "empty-pool", [], []
            else:
                status, reason, ids, all_ids = "injected", "injected", [row_id], [row_id]
            lines.append(
                f"[{BASE_TS}] zmem-hook status={status} reason={reason} ids={ids!r} "
                f"all={all_ids!r} sid={sid} moment={moment} lane={lane} "
                f"ver={version} t_ms={timing}\n"
            )
    return "".join(lines).encode("utf-8")


def _version(manifest: Path) -> str:
    try:
        value = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read release manifest: {exc}") from exc
    if not isinstance(value, str) or not value:
        raise RuntimeError("release manifest has no version")
    return value


def generate(manifest: Path, store_out: Path, log_out: Path, expected_out: Path, baseline_out: Path | None = None) -> dict[str, object]:
    for destination in (store_out, log_out, expected_out, baseline_out):
        if destination is not None and destination.exists():
            raise RuntimeError(f"refusing to overwrite existing fixture: {destination}")
    version = _version(manifest)
    with tempfile.TemporaryDirectory(prefix="zmem-replay-build-") as raw:
        scratch = Path(raw)
        candidate = scratch / "store.sqlite"
        env = _env(candidate, scratch)
        env["ZMEM_TEST_NOW"] = EVAL_PIN_TS
        result = subprocess.run(
            [sys.executable, str(EVAL_FIXTURE), str(candidate)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=180,
        )
        if result.returncode:
            raise RuntimeError(f"eval store builder failed ({result.returncode}): {result.stderr}")
        _canonicalize(candidate, scratch)
        log_candidate = scratch / "decisions.log"
        log_candidate.write_bytes(_decision_log(version))
        report_candidate = scratch / "expected.json"
        evaluator = ROOT / "scripts" / "eval_replay.py"
        result = subprocess.run(
            [sys.executable, str(evaluator), "--store", str(candidate), "--log", str(log_candidate),
             "--days", "30", "--json-out", str(report_candidate)],
            # Keep the builder's explicit store env out of the evaluator's
            # operator-alias refusal check: the candidate is a disposable
            # snapshot, while the evaluator must still reject the real
            # operator path in normal invocations.
            cwd=ROOT,
            env={**env, "ZMEM_STORE": str(scratch / "ambient.sqlite")},
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode:
            raise RuntimeError(f"replay evaluator failed ({result.returncode}): {result.stderr}")
        store_out.parent.mkdir(parents=True, exist_ok=True)
        log_out.parent.mkdir(parents=True, exist_ok=True)
        expected_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate, store_out)
        log_out.write_bytes(log_candidate.read_bytes())
        expected_out.write_bytes(report_candidate.read_bytes())
        if baseline_out is not None:
            baseline_out.parent.mkdir(parents=True, exist_ok=True)
            baseline_out.write_bytes(report_candidate.read_bytes())
    runtime: dict[str, str] = {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
    }
    try:
        import sqlite_vec
    except ImportError:
        runtime["sqlite_vec"] = "unavailable"
    else:
        runtime["sqlite_vec"] = sqlite_vec.__version__
    return {
        "store_sha256": hashlib.sha256(store_out.read_bytes()).hexdigest(),
        "log_sha256": hashlib.sha256(log_out.read_bytes()).hexdigest(),
        "expected_sha256": hashlib.sha256(expected_out.read_bytes()).hexdigest(),
        "runtime": runtime,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="tests/fixtures/replay/generate.py")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--expected", required=True, type=Path)
    parser.add_argument("--baseline", type=Path, default=None)
    args = parser.parse_args()
    try:
        result = generate(args.manifest, args.store, args.log, args.expected, args.baseline)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"replay fixture generation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
