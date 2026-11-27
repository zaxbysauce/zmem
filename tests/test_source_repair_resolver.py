"""C5 frozen check (Amendment A1-R2/R3, final-critic r2 credential gate).

AC2-extension contract (Amendment A1-R2/R3, owner decision 2026-11-27):
``store.py source repair`` recovers a redacted identifier from the TRUSTED
SOURCE the evidence was captured from, when the production-written evidence
excerpt is marker-only (zero hex candidates, exactly one [REDACTED_SECRET]
marker). Binding chain (D3 probe-verified): position-anchored block (the
normalized excerpt's line count starting EXACTLY at normalized line
ref_offset), capture-policy redaction of the raw block equals the excerpt,
exactly one engine detection, exactly one marker occurrence, an actual
change, and the marker-anchored raw slice fullmatches 40/64-hex. Apply
re-resolves under the lock (same candidate required). Metadata for
trusted_source repairs carries NO raw value (digest only). Frozen at base
as ERROR: at 0ada1b83 the ``source repair`` surface does not exist ->
"invalid source arguments".
"""
from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "memory" / "scripts"
STORE = SCRIPTS / "store.py"
NS = "project:resolver-502-512"

MARKER = "[REDACTED_SECRET]"

# 40-hex canary + a second 40-hex value for the ambiguity fixtures.
CANARY = "5f2a1b3c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f42"
OTHER = "0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f"

LINE1 = "release canary context line one"
LINE3 = "release canary context line three"


def _env(root: Path, extra: dict[str, str | None]) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "ZMEM_STORE": str(root / "store.sqlite"),
        "ZMEM_DATA": str(root),
        "ZMEM_MODELS_DIR": str(root / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_EMBED_PROFILE": "fake",
        "ZMEM_NAMESPACE": NS,
    })
    for key in ("ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE", "ZMEM_TRANSCRIPT",
                "ZMEM_AGENT_TRANSCRIPT", "ZMEM_CODEX_MEMORY",
                "ZMEM_ZCODE_DB", "ZMEM_HERMES_SESSIONS", "HERMES_HOME",
                "ZMEM_SOURCE_ROOT", "ZMEM_REPAIR_ROOT"):
        env.pop(key, None)
    for key, value in (extra or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


def _run(root: Path, *args: str,
         extra_env: dict[str, str | None] | None = None,
         ) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE), *args], cwd=ROOT,
        env=_env(root, extra_env or {}), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


def _init(root: Path, extra_env: dict[str, str | None]) -> None:
    result = _run(root, "init", extra_env=extra_env)
    if result.returncode != 0:
        raise AssertionError(result.stderr)


def _add(root: Path, content: str, extra_env: dict[str, str | None]) -> str:
    result = _run(root, "add", "--namespace", NS, "--type", "fact",
                  "--content", content, "--signal", "test",
                  "--confidence", "0.9", "--json", extra_env=extra_env)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return str(json.loads(result.stdout)["id"])


def _sql_one(db_path: Path, query: str, params: tuple) -> object:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchone()
    finally:
        conn.close()


def _memory_content(db_path: Path, memory_id: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT content FROM memory WHERE id=?",
                           (memory_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise AssertionError(f"memory {memory_id} missing from {db_path}")
    return str(row[0])


def _meta_rows(db_path: Path, memory_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (f"source_repair:{memory_id}",)).fetchone()
    finally:
        conn.close()
    if row is None:
        return []
    try:
        entries = json.loads(row[0])
        return entries if isinstance(entries, list) else []
    except (TypeError, ValueError):
        return []


def _write_production_evidence(db_path: Path, memory_id: str, excerpt: str,
                               ref_path: str, ref_offset: int | None,
                               session_id: str = "sess-c5") -> str:
    """Write one evidence row through the PRODUCTION writer (write_evidence,
    redact default ON) exactly as the capture lanes do — the excerpt stored
    is the REDACTED text, which is the whole point of this check."""
    sys.path.insert(0, str(SCRIPTS))
    os.environ["ZMEM_STORE"] = str(db_path)
    from storelib.evidence import write_evidence
    conn = sqlite3.connect(db_path)
    try:
        evidence_id = write_evidence(
            conn, session_id=session_id, lane="codex", moment="pretool",
            kind="turn", ts="2026-11-27T12:00:00Z", excerpt=excerpt,
            ref_path=ref_path, ref_offset=ref_offset)
        conn.execute(
            "INSERT OR IGNORE INTO memory_evidence(memory_id,evidence_id) "
            "VALUES(?,?)", (memory_id, evidence_id))
        conn.commit()
    finally:
        conn.close()
    return evidence_id


def _last_json(stdout_text: str) -> dict:
    lines = [line for line in stdout_text.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("no JSON line on stdout")
    return json.loads(lines[-1])


class SourceRepairResolverTests(unittest.TestCase):
    """C5 (Amendment A1-R2/R3): trusted-source recovery for marker-only
    production evidence."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zmem-resolver-c5-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _seed(self, source_lines: list[str], memory_content: str):
        """Shared fixture: source dir + project file + hermetic store with a
        redacted memory and production-written marker-only evidence."""
        src = self.root / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "notes.md").write_text("\n".join(source_lines) + "\n",
                                      encoding="utf-8")
        env = {"ZMEM_SOURCE_ROOT": str(src)}
        _init(self.root, env)
        memory_id = _add(self.root, memory_content, env)
        return env, memory_id

    def test_production_marker_only_evidence_repairs_from_trusted_source(self) -> None:
        # The producer-gap class: an unlabeled 40-hex canary in the memory;
        # the capture policy redacts BOTH the memory content and the
        # production-written evidence excerpt.
        line2 = f"canary fails rollback {CANARY} immediately"
        env, memory_id = self._seed(
            [LINE1, line2, LINE3],
            f"{LINE1}\ncanary fails rollback {CANARY} immediately\n{LINE3}")
        db = self.root / "store.sqlite"
        stored = _memory_content(db, memory_id)
        self.assertIn(MARKER, stored)
        self.assertNotIn(CANARY, stored)

        evidence_id = _write_production_evidence(
            db, memory_id, line2, "notes.md", 1)
        row = _sql_one(db, "SELECT excerpt FROM evidence WHERE id=?",
                       (evidence_id,))
        self.assertNotIn(CANARY, row[0])
        self.assertIn(MARKER, row[0])

        # Preview: resolver reports the trusted-source recovery, mutates
        # nothing, and needs NO repair root.
        preview = _run(self.root, "source", "repair", "--id", memory_id,
                       "--evidence-id", evidence_id, "--preview",
                       extra_env=env)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        payload = _last_json(preview.stdout)
        self.assertEqual(payload["restore_value"], CANARY)
        self.assertEqual(payload["resolver"], "project-file")
        self.assertEqual(payload["verification_kind"], "trusted_source")
        self.assertTrue(payload["verified"])
        self.assertFalse(payload["applied"])
        self.assertNotIn(CANARY, _memory_content(db, memory_id))

        # Apply: restores the canary into the memory content only; the
        # evidence excerpt stays redacted; metadata carries NO raw value.
        applied = _run(self.root, "source", "repair", "--id", memory_id,
                       "--evidence-id", evidence_id, "--apply",
                       extra_env=env)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        payload = _last_json(applied.stdout)
        self.assertEqual(payload["restore_value"], CANARY)
        self.assertEqual(payload["resolver"], "project-file")
        self.assertEqual(payload["verification_kind"], "trusted_source")
        self.assertTrue(payload["applied"])
        repaired = _memory_content(db, memory_id)
        self.assertIn(CANARY, repaired)
        ev_row = _sql_one(db, "SELECT excerpt FROM evidence WHERE id=?",
                          (evidence_id,))
        self.assertNotIn(CANARY, ev_row[0])
        entries = _meta_rows(db, memory_id)
        self.assertEqual(len(entries), 1)
        self.assertNotIn("restored", entries[0])
        self.assertEqual(entries[0].get("verification_kind"), "trusted_source")
        self.assertIn("restored_sha256", entries[0])

    def test_wrapper_spans_refuse(self) -> None:
        # D2-C1 executed bypasses: the redacted span must BE the hex run.
        for line in (f"token=[{CANARY}",
                     f"token={CANARY}]",
                     f"token=[{CANARY}]"):
            with self.subTest(line=line):
                env, memory_id = self._seed(
                    [LINE1, line, LINE3],
                    f"{LINE1}\n{line}\n{LINE3}")
                db = self.root / "store.sqlite"
                evidence_id = _write_production_evidence(
                    db, memory_id, line, "notes.md", 1)
                result = _run(self.root, "source", "repair", "--id",
                              memory_id, "--evidence-id", evidence_id,
                              "--preview", extra_env=env)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertTrue(result.stderr.startswith(
                    "[zmem] source repair: refused"), result.stderr)
                self.assertNotIn(CANARY, _memory_content(db, memory_id))

    def test_position_anchor_refuses_when_line_no_longer_quotes(self) -> None:
        # ref_offset anchors the block. Current-source substitution binds
        # whatever verifiable identifier sits at the ANCHORED position (the
        # disclosed semantic); refusal comes when the anchored line no
        # longer quotes the excerpt, and a value ELSEWHERE never substitutes
        # (no whole-file search exists).
        excerpt_line = f"canary fails rollback {CANARY} immediately"
        memory_content = f"{LINE1}\n{excerpt_line}\n{LINE3}"
        # The anchored line was rewritten after capture; the SAME excerpt
        # context sits at another offset (a whole-file scan would bind it —
        # only the mandatory position anchor refuses). A value elsewhere
        # must never substitute; there is no whole-file search.
        env, memory_id = self._seed(
            [LINE1, "totally different line now", LINE3, excerpt_line],
            memory_content)
        db = self.root / "store.sqlite"
        evidence_id = _write_production_evidence(
            db, memory_id, excerpt_line, "notes.md", 1)
        result = _run(self.root, "source", "repair", "--id", memory_id,
                      "--evidence-id", evidence_id, "--preview",
                      extra_env=env)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn(CANARY, _memory_content(db, memory_id))

        # A WRONG position (ref_offset=0: the context line, no redactable
        # span) also refuses - the anchor is mandatory and exact.
        evidence_wrong = _write_production_evidence(
            db, memory_id, excerpt_line, "notes.md", 0,
            session_id="sess-c5-wrongpos")
        result = _run(self.root, "source", "repair", "--id", memory_id,
                      "--evidence-id", evidence_wrong, "--preview",
                      extra_env=env)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn(CANARY, _memory_content(db, memory_id))

    def test_multi_detection_block_refuses(self) -> None:
        # sshpass double-count class: one marker, engine count == 2 ->
        # conservative refusal (M9c discriminator).
        line = f"sshpass -p {CANARY} ssh deploy@box"
        env, memory_id = self._seed([LINE1, line, LINE3],
                                    f"{LINE1}\n{line}\n{LINE3}")
        db = self.root / "store.sqlite"
        evidence_id = _write_production_evidence(db, memory_id, line,
                                                 "notes.md", 1)
        result = _run(self.root, "source", "repair", "--id", memory_id,
                      "--evidence-id", evidence_id, "--preview",
                      extra_env=env)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn(CANARY, _memory_content(db, memory_id))

    def test_credential_labelled_spans_refuse(self) -> None:
        # Final-critic r2 finding 1: a value the redactor reached through a
        # credential rule was already classified as a secret - ASK-2
        # approved recovering IDENTIFIERS, not labelled credentials. Only
        # the generic identifier patterns may produce a binder.
        for line in (f"password: {CANARY}",
                     f"secret = {CANARY}",
                     f"api_token={CANARY}",
                     f"Authorization: Bearer {CANARY}"):
            with self.subTest(line=line):
                env, memory_id = self._seed(
                    [LINE1, line, LINE3],
                    f"{LINE1}\n{line}\n{LINE3}")
                db = self.root / "store.sqlite"
                evidence_id = _write_production_evidence(
                    db, memory_id, line, "notes.md", 1)
                result = _run(self.root, "source", "repair", "--id",
                              memory_id, "--evidence-id", evidence_id,
                              "--preview", extra_env=env)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertTrue(result.stderr.startswith(
                    "[zmem] source repair: refused"), result.stderr)
                self.assertNotIn(CANARY, _memory_content(db, memory_id))

    def test_non_routable_ref_keeps_historical_refusal(self) -> None:
        # Absolute/transcript-unroutable refs keep the exact historical
        # refusal text (byte-identical refusal surface).
        env, memory_id = self._seed(
            [LINE1, f"canary fails rollback {CANARY} immediately", LINE3],
            f"{LINE1}\ncanary fails rollback {CANARY} immediately\n{LINE3}")
        db = self.root / "store.sqlite"
        excerpt_line = f"canary fails rollback {CANARY} immediately"
        evidence_id = _write_production_evidence(
            db, memory_id, excerpt_line, "../outside.md", 1)
        result = _run(self.root, "source", "repair", "--id", memory_id,
                      "--evidence-id", evidence_id, "--preview",
                      extra_env=env)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(result.stderr.startswith(
            "[zmem] source repair: refused"), result.stderr)
        self.assertNotIn(CANARY, _memory_content(db, memory_id))

    def test_source_env_unset_refuses(self) -> None:
        env, memory_id = self._seed(
            [LINE1, f"canary fails rollback {CANARY} immediately", LINE3],
            f"{LINE1}\ncanary fails rollback {CANARY} immediately\n{LINE3}")
        db = self.root / "store.sqlite"
        excerpt_line = f"canary fails rollback {CANARY} immediately"
        evidence_id = _write_production_evidence(
            db, memory_id, excerpt_line, "notes.md", 1)
        result = _run(self.root, "source", "repair", "--id", memory_id,
                      "--evidence-id", evidence_id, "--preview",
                      extra_env={"ZMEM_SOURCE_ROOT": None})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn(CANARY, _memory_content(db, memory_id))


if __name__ == "__main__":
    unittest.main()
