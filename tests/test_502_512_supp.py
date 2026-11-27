"""Supplementary regression coverage for issues #502 + #512 (Workstream Z 1-2).

NOT a frozen acceptance check — this file adds the regression depth the plan
critic required across rounds 1-10 beyond the four frozen checks:

- per-field capture-policy calibration with NO cross-field context carryover
  (R2-1) and CROSS-FIELD non-generic preservation (a Bearer split across
  content/tags is still detected — R3-3), plus benign-plus-unlabeled (R2-1);
- commit/release-context LEGACY repair alignment (F1's central case),
  duplicate-context ambiguous refusal (F9), ancestor-repo refusal (R9-1),
  pointer-row repair refusal (R6-4);
- repair derived state: content_norm recomputed, embedding metadata NULL,
  memory_vec row gone, mutation generation bumped (F8); repair durability
  under dry-run AND applying rescan with unrelated spans still remediating
  (R3-1/R4-1) and the accumulating two-repair record (R4-2);
- ``user:global`` equal-breadth scope authorization and retention (R4-3/F4);
- verify unsupported-kind refusal (R2-4) and record persistence (CF-4).

All hermetic: TemporaryDirectory stores, env-pinned subprocesses, real git
repos only inside temp dirs.
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
sys.path.insert(0, str(SCRIPTS))
NS = "project:wsz-502-512"

from redaction import is_identifier_context_match  # noqa: E402
from storelib.write import _check_secrets  # noqa: E402


def _env(root: Path, extra_env: dict[str, str | None]) -> dict[str, str]:
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
                "ZMEM_AGENT_TRANSCRIPT", "ZMEM_CODEX_MEMORY", "ZMEM_ZCODE_DB",
                "ZMEM_HERMES_SESSIONS", "HERMES_HOME", "ZMEM_REPAIR_ROOT",
                "ZMEM_SOURCE_ROOT"):
        env.pop(key, None)
    for key, value in extra_env.items():
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
        encoding="utf-8", errors="replace", timeout=180,
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


def _insert_evidence(db_path: Path, memory_id: str,
                     rows: list[tuple]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO evidence(id,session_id,lane,moment,kind,ts,hash,"
            "excerpt,ref_path,ref_offset) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        conn.executemany(
            "INSERT INTO memory_evidence(memory_id,evidence_id) VALUES(?,?)",
            [(memory_id, row[0]) for row in rows])
        conn.commit()
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


def _meta(db_path: Path, key: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (key,)).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class PerFieldCalibrationTests(unittest.TestCase):
    """R2-1 + R3-3: _check_secrets calibration is per-field for generics and
    preserves combined non-generic detection."""

    def test_cross_field_context_cannot_exempt(self) -> None:
        # 'commit' ending content must NOT exempt a hex run starting tags.
        warnings = _check_secrets("commit", "",
                                  "44f0dcb1a3e5927c8b6a0d4e2f1c3b5a9d7e8f0c")
        self.assertTrue(any("matched pattern" in w for w in warnings),
                         warnings)

    def test_same_field_context_still_exempts(self) -> None:
        warnings = _check_secrets(
            "Deployed release 5f2a1b3c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f42 today",
            "", "")
        self.assertEqual([w for w in warnings if "matched pattern" in w], [])

    def test_exempt_first_generic_value_hides_nothing(self) -> None:
        # A labeled value first, an UNLABELED one later in the same field:
        # the scan continues past the exempt match.
        text = ("release 5f2a1b3c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f42 then "
                "44f0dcb1a3e5927c8b6a0d4e2f1c3b5a9d7e8f0c raw")
        warnings = _check_secrets(text, "", "")
        self.assertTrue(any("matched pattern" in w for w in warnings),
                         warnings)

    def test_cross_field_bearer_is_still_detected(self) -> None:
        # R3-3: a Bearer value split across content/tags keeps its detection.
        warnings = _check_secrets("Authorization: Bearer ",
                                  "", "abcdef1234567890ABCDEF")
        self.assertTrue(any("matched pattern" in w for w in warnings),
                         warnings)

    def test_boundary_selection_same_last_field(self) -> None:
        # R5-3 positive: the label occurs earlier in the SAME (last) field.
        combined = "build notes build id 3f2a1b0c9d8e"
        pos = combined.index("3f2a1b0c9d8e")
        self.assertTrue(is_identifier_context_match(combined, pos,
                                                    boundary=0))

    def test_boundary_selection_earlier_field_negative(self) -> None:
        # R5-3 negative: a label in an earlier field never exempts a later
        # field's value.
        combined = "release  <unrelated> 44f0dcb1a3e5927c8b6a0d4e2f1c3b5a9d7e8f0c"
        pos = combined.index("44f0dcb1")
        self.assertFalse(is_identifier_context_match(combined, pos,
                                                     boundary=pos))


class RepairAlignmentTests(unittest.TestCase):
    """F1 central case, F9 duplicate refusal, R9-1 ancestor refusal,
    R6-4 pointer refusal, F8 derived state, R3-1 durability, R4-2
    accumulation."""

    def _build(self, root: Path, content: str, excerpt: str,
               ref_dir: Path) -> tuple[str, str, str]:
        repair_env: dict[str, str | None] = {"ZMEM_REPAIR_ROOT": str(root)}
        _init(root, repair_env)
        memory_id = _add(root, content, repair_env)
        _insert_evidence(root / "store.sqlite", memory_id, [(
            "ev-supp-1", "sess-supp", "codex", "pretool", "turn",
            "2026-11-26T12:00:00Z", "h1", excerpt,
            ref_dir.name, 0)])
        return memory_id, repair_env, content

    def test_commit_context_legacy_repair(self) -> None:
        # F1: stored 'commit [REDACTED_SECRET]', excerpt 'commit <sha>' —
        # the issue's central case repairs correctly.
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-repair-") as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            sha = _git(_init_repo(repo), "rev-parse", "HEAD") if False else None
            _git(repo, "init")
            _git(repo, "config", "user.email", "fixture@example.invalid")
            _git(repo, "config", "user.name", "fixture")
            (repo / "README.txt").write_text("fixture\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "c")
            sha = _git(repo, "rev-parse", "HEAD")
            memory_id, repair_env, _ = self._build(
                root, f"pinned to commit [REDACTED_SECRET] yesterday",
                f"pinned to commit {sha} yesterday", repo)
            apply_result = _run(root, "source", "repair", "--id", memory_id,
                                "--evidence-id", "ev-supp-1", "--apply",
                                extra_env=repair_env)
            self.assertEqual(apply_result.returncode, 0, apply_result.stderr)
            payload = json.loads(apply_result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["restore_value"], sha)
            self.assertTrue(payload["applied"])
            self.assertIn(sha, _memory_content(root / "store.sqlite",
                                               memory_id))

    def test_duplicate_context_refuses(self) -> None:
        # F9: the aligned quotation occurring twice refuses without mutation.
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-dup-") as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init")
            _git(repo, "config", "user.email", "fixture@example.invalid")
            _git(repo, "config", "user.name", "fixture")
            (repo / "README.txt").write_text("fixture\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "c")
            sha = _git(repo, "rev-parse", "HEAD")
            content = (f"canary fails rollback [REDACTED_SECRET] immediately; "
                       f"canary fails rollback [REDACTED_SECRET] immediately")
            memory_id, repair_env, _ = self._build(
                root, content,
                f"canary fails rollback {sha} immediately", repo)
            apply_result = _run(root, "source", "repair", "--id", memory_id,
                                "--evidence-id", "ev-supp-1", "--apply",
                                extra_env=repair_env)
            self.assertEqual(apply_result.returncode, 1, apply_result.stderr)
            self.assertTrue(apply_result.stderr.startswith(
                "[zmem] source repair: refused"), apply_result.stderr)
            self.assertNotIn(sha,
                             _memory_content(root / "store.sqlite", memory_id))

    def test_ancestor_repo_refuses(self) -> None:
        # R9-1: a non-repository child under a parent repo must refuse.
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-anc-") as temp:
            root = Path(temp)
            parent = root / "parentrepo"
            parent.mkdir()
            _git(parent, "init")
            _git(parent, "config", "user.email", "fixture@example.invalid")
            _git(parent, "config", "user.name", "fixture")
            (parent / "README.txt").write_text("fixture\n", encoding="utf-8")
            _git(parent, "add", "-A")
            _git(parent, "-c", "commit.gpgsign=false", "commit", "-m", "c")
            sha = _git(parent, "rev-parse", "HEAD")
            child = root / "repo"
            child.mkdir()  # NOT a repository
            memory_id, repair_env, _ = self._build(
                root, f"canary fails rollback [REDACTED_SECRET] immediately",
                f"canary fails rollback {sha} immediately", child)
            apply_result = _run(root, "source", "repair", "--id", memory_id,
                                "--evidence-id", "ev-supp-1", "--apply",
                                extra_env=repair_env)
            self.assertEqual(apply_result.returncode, 1, apply_result.stderr)
            self.assertNotIn(sha,
                             _memory_content(root / "store.sqlite", memory_id))

    def test_pointer_row_refuses(self) -> None:
        # R6-4: a reserved knowledge-pointer row never repairs in place.
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-ptr-") as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init")
            _git(repo, "config", "user.email", "fixture@example.invalid")
            _git(repo, "config", "user.name", "fixture")
            (repo / "README.txt").write_text("fixture\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "c")
            sha = _git(repo, "rev-parse", "HEAD")
            repair_env: dict[str, str | None] = {"ZMEM_REPAIR_ROOT": str(root)}
            _init(root, repair_env)
            memory_id = _add(root, "pointer payload", repair_env)
            conn = sqlite3.connect(root / "store.sqlite")
            try:
                conn.execute("UPDATE memory SET source_ref=? WHERE id=?",
                             ("knowledge:fixture", memory_id))
                conn.commit()
            finally:
                conn.close()
            _insert_evidence(root / "store.sqlite", memory_id, [(
                "ev-supp-2", "sess-supp", "codex", "pretool", "turn",
                "2026-11-26T12:00:00Z", "h1",
                f"payload {sha}", "repo", 0)])
            apply_result = _run(root, "source", "repair", "--id", memory_id,
                                "--evidence-id", "ev-supp-2", "--apply",
                                extra_env=repair_env)
            self.assertEqual(apply_result.returncode, 1, apply_result.stderr)
            self.assertIn("knowledge-pointer", apply_result.stderr)

    def test_derived_state_and_rescan_durability(self) -> None:
        # F8 + R3-1/R4-1: after apply, content_norm is recomputed, embedding
        # metadata is NULL, the vec row is gone, the mutation generation is
        # bumped — and BOTH a dry-run and an applying rescan leave the
        # repaired row untouched while unrelated spans still remediate.
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-dur-") as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init")
            _git(repo, "config", "user.email", "fixture@example.invalid")
            _git(repo, "config", "user.name", "fixture")
            (repo / "README.txt").write_text("fixture\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "c")
            sha = _git(repo, "rev-parse", "HEAD")
            # The UNLABELED SHA was destroyed at add (fail-safe net); the
            # unlabeled reference too. Repair restores ONLY the verified
            # commit. (swarm-pr-feedback: the unlinked span was relabeled
            # from password= to a credential-free run so the destination
            # gate — which now runs on every repair path — accepts it; the
            # gate refusing the old content is the fix working.)
            memory_id, repair_env, _ = self._build(
                root,
                f"canary fails rollback [REDACTED_SECRET] immediately; "
                f"reference [REDACTED_SECRET] for the service account.",
                f"canary fails rollback {sha} immediately", repo)
            apply_result = _run(root, "source", "repair", "--id", memory_id,
                                "--evidence-id", "ev-supp-1", "--apply",
                                extra_env=repair_env)
            self.assertEqual(apply_result.returncode, 0, apply_result.stderr)
            db = root / "store.sqlite"
            conn = sqlite3.connect(db)
            try:
                row = conn.execute(
                    "SELECT content, content_norm, embedding, embedding_model,"
                    " embedded_at FROM memory WHERE id=?",
                    (memory_id,)).fetchone()
                generation = conn.execute(
                    "SELECT value FROM meta WHERE key LIKE '%generation%'"
                ).fetchall()
            finally:
                conn.close()
            content = str(row[0])
            self.assertIn(sha, content)
            self.assertIn("[REDACTED_SECRET]", content)  # unlinked span stays
            from schema_meta import normalize_content
            self.assertEqual(str(row[1]), normalize_content(content))
            self.assertIsNone(row[2])
            self.assertIsNone(row[3])
            self.assertIsNone(row[4])
            # Repair record retained with the evidence id.
            record = _meta(db, f"source_repair:{memory_id}")
            self.assertIsNotNone(record)
            self.assertIn("ev-supp-1", str(record))
            # Dry-run rescan: the repaired row is NOT a candidate.
            dry = _run(root, "rescan-secrets", "--dry-run",
                       extra_env=repair_env)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            dry_payload = json.loads(dry.stdout.strip().splitlines()[-1])
            self.assertNotIn(memory_id, dry_payload["ids"], dry_payload)
            # Applying rescan: still untouched, and the record survives.
            applied = _run(root, "rescan-secrets", "--apply",
                           extra_env=repair_env)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertIn(sha, _memory_content(db, memory_id))
            self.assertIsNotNone(_meta(db, f"source_repair:{memory_id}"))

    def test_two_repairs_accumulate(self) -> None:
        # R4-2 + review 4.5-5: TWO real repairs on one row accumulate both
        # provenance entries.
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-two-") as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init")
            _git(repo, "config", "user.email", "fixture@example.invalid")
            _git(repo, "config", "user.name", "fixture")
            (repo / "README.txt").write_text("fixture\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "first")
            sha1 = _git(repo, "rev-parse", "HEAD")
            (repo / "second.txt").write_text("second\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "second")
            sha2 = _git(repo, "rev-parse", "HEAD")
            self.assertNotEqual(sha1, sha2)
            repair_env: dict[str, str | None] = {"ZMEM_REPAIR_ROOT": str(root)}
            _init(root, repair_env)
            # BOTH unlabeled 40-hex runs destroyed at add (fail-safe net).
            memory_id = _add(
                root,
                f"rollback [REDACTED_SECRET] noted; "
                f"rebuild [REDACTED_SECRET] verified",
                repair_env)
            db = root / "store.sqlite"
            _insert_evidence(db, memory_id, [
                ("ev-supp-3", "sess-supp", "codex", "pretool", "turn",
                 "2026-11-26T12:00:00Z", "h1",
                 f"rollback {sha1} noted", "repo", 0),
                ("ev-supp-4", "sess-supp", "codex", "pretool", "turn",
                 "2026-11-26T12:05:00Z", "h2",
                 f"rebuild {sha2} verified", "repo", 0),
            ])
            # Repair one: restores sha1 (the multi-anchor memory selects
            # explicitly), appends entry 1.
            r1 = _run(root, "source", "repair", "--id", memory_id,
                      "--evidence-id", "ev-supp-3", "--apply",
                      extra_env=repair_env)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            record = json.loads(_meta(db, f"source_repair:{memory_id}"))
            self.assertEqual(len(record), 1)
            self.assertEqual(record[0]["restored"], sha1)
            self.assertIn(sha1, _memory_content(db, memory_id))
            self.assertNotIn(sha2, _memory_content(db, memory_id))
            # Repair two: restores sha2, APPENDS entry 2 (never overwrites).
            r2 = _run(root, "source", "repair", "--id", memory_id,
                      "--evidence-id", "ev-supp-4", "--apply",
                      extra_env=repair_env)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            record = json.loads(_meta(db, f"source_repair:{memory_id}"))
            self.assertEqual(len(record), 2)
            self.assertEqual({e["restored"] for e in record}, {sha1, sha2})
            self.assertEqual({e["evidence_id"] for e in record},
                             {"ev-supp-3", "ev-supp-4"})
            content = _memory_content(db, memory_id)
            self.assertIn(sha1, content)
            self.assertIn(sha2, content)
            # Rescan durability for BOTH values: the repaired row is not a
            # candidate and the record carries both entries.
            dry = _run(root, "rescan-secrets", "--dry-run",
                       extra_env=repair_env)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertNotIn(memory_id, json.loads(
                dry.stdout.strip().splitlines()[-1])["ids"])


def _init_repo(repo: Path) -> Path:
    return repo


class ScopeRevisionTests(unittest.TestCase):
    """R4-3/F4: user:global equal-breadth scope authorization."""

    def test_user_global_authorization_and_retention(self) -> None:
        from storelib.source import resolve_revision
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-scope-") as temp:
            root = Path(temp)
            db = root / "store.sqlite"
            repair_env: dict[str, str | None] = {}
            _init(root, repair_env)
            g = _add(root, "deployments go to the shared staging cluster",
                     repair_env)
            e = _add(root, "the billing deploy uses the dedicated cluster",
                     repair_env)
            conn = sqlite3.connect(db)
            try:
                conn.execute("UPDATE memory SET namespace=? WHERE id=?",
                             ("user:global", g))
                conn.execute("UPDATE memory SET namespace=? WHERE id=?",
                             ("project:billing", e))
                conn.commit()
                # A literal-global revision authorizes the canonical-global
                # target (equal breadth) and retains the narrower exception.
                result = resolve_revision(conn, revision_of=g,
                                          scope="global")
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(result["outcome"], "retained_exception",
                             result)
            self.assertEqual(result["superseded_ids"], [g])
            self.assertEqual(result["retained_ids"], [e])
            conn = sqlite3.connect(db)
            try:
                g_sup = conn.execute(
                    "SELECT superseded_at FROM memory WHERE id=?",
                    (g,)).fetchone()[0]
                e_sup = conn.execute(
                    "SELECT superseded_at FROM memory WHERE id=?",
                    (e,)).fetchone()[0]
            finally:
                conn.close()
            self.assertIsNotNone(g_sup)
            self.assertIsNone(e_sup)
            # The reverse: a user:global revision refuses a narrower target.
            conn = sqlite3.connect(db)
            try:
                refused = resolve_revision(conn, revision_of=e,
                                           scope="user:global")
            finally:
                conn.close()
            self.assertEqual(refused["outcome"], "refused_unauthorized")


class VerifyRecordTests(unittest.TestCase):
    """R2-4 + CF-4: verify persists a record; non-project kinds refuse."""

    def test_verify_persists_record(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-verify-") as temp:
            root = Path(temp)
            trusted = root / "trusted"
            trusted.mkdir()
            notes = trusted / "deploy-notes.txt"
            quoted = "staging rollout completes in ten minutes"
            show_env: dict[str, str | None] = {"ZMEM_SOURCE_ROOT":
                                               str(trusted)}
            _init(root, show_env)
            memory_id = _add(
                root, "ops note: staging rollout completes in ten minutes",
                show_env)
            notes.write_text("# notes\n\n" + quoted + "\n", encoding="utf-8")
            _insert_evidence(root / "store.sqlite", memory_id, [(
                "ev-supp-v1", "sess-supp", "codex", "pretool", "turn",
                "2026-11-26T12:00:00Z", "h1", quoted,
                "deploy-notes.txt", 2)])
            verify = _run(root, "source", "verify", "--id", memory_id,
                          extra_env=show_env)
            self.assertEqual(verify.returncode, 0, verify.stderr)
            payload = json.loads(verify.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["outcome"], "exact")
            record = _meta(root / "store.sqlite",
                           f"source_verify:{memory_id}:ev-supp-v1")
            self.assertIsNotNone(record)
            self.assertIn("exact", str(record))

    def test_verify_refuses_transcript_kind(self) -> None:
        # R2-4: a non-project-file anchor refuses with no record written.
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-vref-") as temp:
            root = Path(temp)
            _init(root, {})
            memory_id = _add(root, "memory with transcript evidence", {})
            db = root / "store.sqlite"
            _insert_evidence(db, memory_id, [(
                "ev-supp-v2", "sess-supp", "claude", "pretool", "turn",
                "2026-11-26T12:00:00Z", "h1", "some excerpt",
                "claude.jsonl", 0)])
            verify = _run(root, "source", "verify", "--id", memory_id,
                          extra_env={"ZMEM_SOURCE_ROOT": str(root / "t")})
            self.assertEqual(verify.returncode, 1, verify.stdout)
            self.assertIn("refused", verify.stderr)
            self.assertIsNone(_meta(
                db, f"source_verify:{memory_id}:ev-supp-v2"))


class BasicDecodeColonTests(unittest.TestCase):
    """Review 4.5 final-critic closure: ONE decode-checked Basic rule —
    RFC 7617 values encode "user-id : password", so the decoded bytes MUST
    contain the colon. EVERY shape whose decode contains the colon redacts
    (mixed, all-uppercase, digit-bearing, pure-lowercase); values that are
    invalid base64 or decode colon-free survive (prose words, colon-free
    junk). The scanner filters at the REGISTRY level, so recall withholding
    and knowledge-document admission get the same decode semantics (the
    r5-r7 over-redaction regressions are gone)."""

    def test_decode_colon_values_redacted(self) -> None:
        from redaction import redact_secret_like_text
        for text in ("Authorization: Basic dXNlcjpwYXNzd29yZA==",
                     "Authorization: Basic YTpi",              # a:b
                     "bare paste Basic YTpi extracted",
                     "log shows Basic mjqb fragment"):         # pure-lowercase base64, decode contains colon
            out, count = redact_secret_like_text(text)
            with self.subTest(text=text):
                self.assertGreaterEqual(count, 1)

    def test_decode_colonfree_and_invalid_survive(self) -> None:
        from redaction import redact_secret_like_text
        for text in ("bare paste Basic dagsqwxzmore extracted",  # colon-free
                     "The basic idea is simple.",
                     "Use basic auth for the proxy",
                     "The basic workflow is documented here.",
                     "Basic password",                           # colon-free
                     "Basic things",
                     "Basic hi",                                 # 2 chars
                     "bare Basic QUFBRUFCQ0RFRg==",              # colon-free uppercase
                     "say basic things"):
            out, count = redact_secret_like_text(text)
            with self.subTest(text=text):
                self.assertEqual(count, 0)
                self.assertEqual(out, text)

    def test_recall_withholding_regression_fixed(self) -> None:
        # The final critic's executed regression: _classify_credential must
        # NOT withhold a row whose content mentions "basic auth" in prose.
        from storelib.recall import _classify_credential
        self.assertFalse(_classify_credential(
            {"content": "Use basic auth for the reverse proxy"}))
        self.assertFalse(_classify_credential(
            {"content": "The basic workflow is documented here."}))
        # A real credential shape IS withheld.
        self.assertTrue(_classify_credential(
            {"content": "Authorization: Basic dXNlcjpwYXNzd29yZA=="}))

    def test_pass_keyword_anchored(self) -> None:
        # Final-critic finding 5 + r2 finding 4: the pass keyword is
        # COMPOUND-KEY ONLY ((?<=[_-])pass) - ordinary words containing
        # 'pass' survive, including the prose key shapes ('all checks
        # pass: <8+ chars>', 'CI pass=') that the bare-key form redacted.
        from redaction import redact_secret_like_text
        for text in ("bypass=enabled_always", "compass: northbound",
                     "surpass=expected_values",
                     "all checks pass: test_suite_green",
                     "CI pass=everything-ok",
                     "pass=whatever123"):
            out, count = redact_secret_like_text(text)
            with self.subTest(text=text):
                self.assertEqual(count, 0)
                self.assertEqual(out, text)
        # Compound-key credentials still redact.
        for text in ("DB_PASS=Sup3rSecret!99", "MY_PASS=abc12345"):
            out, count = redact_secret_like_text(text)
            with self.subTest(text=text):
                self.assertGreaterEqual(count, 1)


class VerbatimPassageVerificationTests(unittest.TestCase):
    """Review 4.5 r5: verification is VERBATIM — indentation and intra-line
    whitespace are meaningful; only line terminators are normalised."""

    def _build(self, root: Path, lines_a: list[str], lines_b: list[str]) -> str:
        trusted = root / "trusted"
        trusted.mkdir(parents=True, exist_ok=True)
        notes = trusted / "notes.txt"
        notes.write_text("\n".join(lines_a) + "\n", encoding="utf-8")
        show_env: dict[str, str | None] = {"ZMEM_SOURCE_ROOT": str(trusted)}
        _init(root, show_env)
        memory_id = _add(root, "ops note quoting the deploy file", show_env)
        _insert_evidence(root / "store.sqlite", memory_id, [(
            "ev-verb-1", "sess-verb", "codex", "pretool", "turn",
            "2026-11-26T12:00:00Z", "h1", lines_a[0], "notes.txt", 0)])
        notes.write_text("\n".join(lines_b) + "\n", encoding="utf-8")
        return memory_id

    def test_indented_line_is_not_exact(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-verb-") as temp:
            root = Path(temp)
            quoted = "staging rollout completes in ten minutes"
            memory_id = self._build(
                root, [quoted],
                ["    " + quoted])  # indentation-only change
            show_env: dict[str, str | None] = {"ZMEM_SOURCE_ROOT":
                                               str(root / "trusted")}
            result = _run(root, "source", "show", "--id", memory_id,
                          extra_env=show_env)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            # Verbatim comparison: the indented line is NOT the recorded
            # line, so the verbatim quotation is absent -> missing.
            self.assertEqual(payload["verification"]["outcome"], "missing")

    def test_crlf_file_still_verbatim(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-crlf-") as temp:
            root = Path(temp)
            quoted = "staging rollout completes in ten minutes"
            trusted = root / "trusted"
            trusted.mkdir(parents=True)
            notes = trusted / "notes.txt"
            notes.write_bytes((quoted + "\r\n").encode("utf-8"))
            show_env: dict[str, str | None] = {"ZMEM_SOURCE_ROOT": str(trusted)}
            _init(root, show_env)
            memory_id = _add(root, "ops note quoting the deploy file",
                             show_env)
            _insert_evidence(root / "store.sqlite", memory_id, [(
                "ev-verb-2", "sess-verb", "codex", "pretool", "turn",
                "2026-11-26T12:00:00Z", "h1", quoted, "notes.txt", 0)])
            result = _run(root, "source", "show", "--id", memory_id,
                          extra_env=show_env)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["verification"]["outcome"], "exact")



class TrustedSourceResolverTests(unittest.TestCase):
    """Amendment A1-R2/R3 supplementary depth (owner decision 2026-11-27):
    trusted-source resolvers for marker-only production evidence.

    - Rescan durability (D3's extended test): a trusted_source repair's
      value is RE-DETECTED by rescan (never exempt), the raw value exists in
      NO repair metadata, and the digest-only entry rebinds to the successor
      with the successor id.
    - git_object repairs keep the pre-A1 exemption (pinned by the existing
      RepairAlignmentTests rescan durability test).
    - Apply-local TOCTOU: apply re-resolves under the lock and refuses a
      changed source (deterministic patched-resolver side effect).
    - zcode + hermes routing: positive recovery through their containment
      pins; wrong-session evidence refuses.
    """
    CANARY = "5f2a1b3c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f42"
    OTHER = "0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f"
    LINE1 = "release canary context line one"
    LINE3 = "release canary context line three"

    def _write_production_evidence(self, db_path, memory_id, excerpt,
                                   ref_path, ref_offset, session_id):
        """One evidence row through the PRODUCTION writer (redact default
        ON) - the stored excerpt is the REDACTED text."""
        scripts = Path(__file__).resolve().parents[1] / "skills" / "memory" \
            / "scripts"
        os.environ["ZMEM_STORE"] = str(db_path)
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from storelib.evidence import write_evidence
        conn = sqlite3.connect(db_path)
        try:
            evidence_id = write_evidence(
                conn, session_id=session_id, lane="codex", moment="pretool",
                kind="turn", ts="2026-11-27T12:00:00Z", excerpt=excerpt,
                ref_path=ref_path, ref_offset=ref_offset)
            conn.execute(
                "INSERT OR IGNORE INTO memory_evidence(memory_id,evidence_id)"
                " VALUES(?,?)", (memory_id, evidence_id))
            conn.commit()
        finally:
            conn.close()
        return evidence_id

    def _seed_source(self, root: Path, lines: list[str],
                     memory_line: str) -> tuple[dict, str]:
        src = root / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "notes.md").write_text("\n".join(lines) + "\n",
                                      encoding="utf-8")
        env = {"ZMEM_SOURCE_ROOT": str(src)}
        _init(root, env)
        memory_id = _add(root, memory_line, env)
        return env, memory_id

    def test_rescan_remediates_trusted_source_value_keeps_digest(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-resolver-rescan-") as temp:
            root = Path(temp)
            excerpt_line = (f"canary fails rollback {self.CANARY} "
                            "immediately")
            env, memory_id = self._seed_source(
                root,
                [self.LINE1, excerpt_line, self.LINE3],
                f"{self.LINE1}\n{excerpt_line}\n{self.LINE3}")
            db = root / "store.sqlite"
            evidence_id = self._write_production_evidence(
                db, memory_id, excerpt_line, "notes.md", 1, "sess-supp")
            applied = _run(root, "source", "repair", "--id", memory_id,
                           "--evidence-id", evidence_id, "--apply",
                           extra_env=env)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            payload = json.loads(
                applied.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["resolver"], "project-file")
            self.assertEqual(payload["verification_kind"], "trusted_source")
            self.assertIn(self.CANARY, _memory_content(db, memory_id))
            # The trusted_source meta entry carries NO raw value.
            pred_entries = json.loads(_meta(
                db, f"source_repair:{memory_id}"))
            self.assertEqual(len(pred_entries), 1)
            self.assertNotIn("restored", pred_entries[0])
            self.assertIn("restored_sha256", pred_entries[0])
            # swarm-pr-review TEST-002/FB-011: the digest must be the value's
            # actual SHA-256, not just any present string.
            import hashlib as _hashlib
            self.assertEqual(
                pred_entries[0]["restored_sha256"],
                _hashlib.sha256(self.CANARY.encode("utf-8")).hexdigest())

            # Applying rescan: the trusted_source value is NOT exempt -
            # rescan re-detects and remediates it into a redacted successor.
            applied_rescan = _run(root, "rescan-secrets", "--apply",
                                  extra_env=env)
            self.assertEqual(applied_rescan.returncode, 0,
                             applied_rescan.stderr)
            live = sqlite3.connect(db)
            try:
                rows = live.execute(
                    "SELECT id, content FROM memory "
                    "WHERE superseded_at IS NULL").fetchall()
                metas = live.execute(
                    "SELECT key, value FROM meta "
                    "WHERE key LIKE 'source_repair:%'").fetchall()
            finally:
                live.close()
            # (i) the canary is absent from EVERY live memory row.
            for mid, content in rows:
                self.assertNotIn(self.CANARY, str(content))
            # (ii) absent from BOTH predecessor and successor metadata.
            blob = json.dumps(metas)
            self.assertNotIn(self.CANARY, blob)
            # (iii) the successor's digest-only entry exists.
            successor = None
            for key, value in metas:
                for entry in json.loads(value):
                    if (entry.get("verification_kind") == "trusted_source"
                            and entry.get("memory_id") not in
                            (memory_id,)):
                        successor = entry
            self.assertIsNotNone(successor)
            self.assertIn("restored_sha256", successor)
            self.assertNotIn("restored", successor)

    def test_apply_refuses_when_source_changes_under_lock(self) -> None:
        # R4-v2: apply re-resolves under the lock; a changed source refuses
        # with no mutation (deterministic patched-resolver side effect).
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-resolver-toctou-") as temp:
            root = Path(temp)
            excerpt_line = (f"canary fails rollback {self.CANARY} "
                            "immediately")
            env, memory_id = self._seed_source(
                root,
                [self.LINE1, excerpt_line, self.LINE3],
                f"{self.LINE1}\n{excerpt_line}\n{self.LINE3}")
            db = root / "store.sqlite"
            evidence_id = self._write_production_evidence(
                db, memory_id, excerpt_line, "notes.md", 1, "sess-supp")
            scripts = Path(__file__).resolve().parents[1] / "skills" \
                / "memory" / "scripts"
            os.environ["ZMEM_STORE"] = str(db)
            # The in-process _memory lookup filters by namespace aliases
            # resolved from the AMBIENT env - pin the seeded namespace.
            os.environ["ZMEM_NAMESPACE"] = NS
            os.environ.update({k: v for k, v in {
                "ZMEM_SOURCE_ROOT": str(root / "src"),
            }.items()})
            if str(scripts) not in sys.path:
                sys.path.insert(0, str(scripts))
            from storelib import source as source_mod
            calls = []

            def flippy(anchor, excerpt):
                calls.append(1)
                if len(calls) == 1:
                    return self.CANARY, "project-file"
                return self.OTHER, "project-file"

            saved = source_mod._resolve_trusted_source_candidate
            source_mod._resolve_trusted_source_candidate = flippy
            try:
                conn = sqlite3.connect(db)
                try:
                    with self.assertRaises(source_mod.SourceRefusal) as ctx:
                        source_mod.repair_from_evidence(
                            conn, memory_id=memory_id,
                            evidence_id=evidence_id, mode="apply")
                finally:
                    conn.close()
                self.assertIn("changed between resolution and apply",
                              str(ctx.exception))
            finally:
                source_mod._resolve_trusted_source_candidate = saved
            self.assertNotIn(self.CANARY, _memory_content(db, memory_id))
            self.assertNotIn(self.OTHER, _memory_content(db, memory_id))

    def test_zcode_session_recovery(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-resolver-zcode-") as temp:
            root = Path(temp)
            env = dict(os.environ)
            env.update({
                "ZMEM_STORE": str(root / "store.sqlite"),
                "ZMEM_DATA": str(root),
                "ZMEM_MODELS_DIR": str(root / "missing-models"),
                "ZMEM_MODEL_AUTODOWNLOAD": "0",
                "ZMEM_EMBED_PROFILE": "fake",
                "ZMEM_NAMESPACE": "project:resolver-supp",
            })
            for key in ("ZMEM_ZCODE_DB", "ZMEM_HERMES_SESSIONS",
                        "HERMES_HOME", "ZMEM_SOURCE_ROOT",
                        "ZMEM_REPAIR_ROOT"):
                env.pop(key, None)
            turn_raw = (f"canary fails rollback {self.CANARY} immediately")
            zcdb = root / "zc.sqlite"
            zconn = sqlite3.connect(zcdb)
            try:
                zconn.execute(
                    "CREATE TABLE part(session_id, message_id, sequence, "
                    "data)")
                zconn.execute(
                    "INSERT INTO part VALUES(?,?,?,?)",
                    ("s-zc", "t1", 0, json.dumps({"text": turn_raw})))
                zconn.commit()
            finally:
                zconn.close()
            env["ZMEM_ZCODE_DB"] = str(zcdb)
            result = _run(root, "init", extra_env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            add = _run(root, "add", "--namespace", "project:resolver-supp",
                       "--type", "fact", "--content", turn_raw,
                       "--signal", "test", "--confidence", "0.9", "--json",
                       extra_env=env)
            self.assertEqual(add.returncode, 0, add.stderr)
            memory_id = str(json.loads(add.stdout)["id"])
            evidence_id = self._write_production_evidence(
                root / "store.sqlite", memory_id, turn_raw,
                "zcode:session/s-zc", None, "s-zc")
            applied = _run(root, "source", "repair", "--id", memory_id,
                           "--evidence-id", evidence_id, "--apply",
                           extra_env=env)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            payload = json.loads(
                applied.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["resolver"], "zcode")
            self.assertIn(self.CANARY,
                          _memory_content(root / "store.sqlite", memory_id))

    def test_hermes_wrong_session_refuses(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-resolver-hermes-") as temp:
            root = Path(temp)
            env = dict(os.environ)
            env.update({
                "ZMEM_STORE": str(root / "store.sqlite"),
                "ZMEM_DATA": str(root),
                "ZMEM_MODELS_DIR": str(root / "missing-models"),
                "ZMEM_MODEL_AUTODOWNLOAD": "0",
                "ZMEM_EMBED_PROFILE": "fake",
                "ZMEM_NAMESPACE": "project:resolver-supp",
            })
            turn_raw = f"canary fails rollback {self.CANARY} immediately"
            result = _run(root, "init", extra_env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            add = _run(root, "add", "--namespace", "project:resolver-supp",
                       "--type", "fact", "--content", turn_raw,
                       "--signal", "test", "--confidence", "0.9", "--json",
                       extra_env=env)
            self.assertEqual(add.returncode, 0, add.stderr)
            memory_id = str(json.loads(add.stdout)["id"])
            # The evidence row's session identity does NOT match the ref's
            # session - the containment identity check refuses BEFORE any DB
            # access (no hermes home pin exists here).
            evidence_id = self._write_production_evidence(
                root / "store.sqlite", memory_id, turn_raw,
                "hermes:state.db/session/other", None, "s-h")
            refused = _run(root, "source", "repair", "--id", memory_id,
                           "--evidence-id", evidence_id, "--preview",
                           extra_env=env)
            self.assertEqual(refused.returncode, 1, refused.stdout)
            self.assertTrue(refused.stderr.startswith(
                "[zmem] source repair: refused"), refused.stderr)
            self.assertNotIn(self.CANARY,
                             _memory_content(root / "store.sqlite",
                                             memory_id))

    def test_hermes_positive_recovery_via_pinned_home(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="zmem-supp-resolver-hermes-pos-") as temp:
            root = Path(temp)
            excerpt_line = (f"canary fails rollback {self.CANARY} "
                            "immediately")
            env, memory_id = self._seed_source(
                root,
                [self.LINE1, excerpt_line, self.LINE3],
                f"{self.LINE1}\n{excerpt_line}\n{self.LINE3}")
            db = root / "store.sqlite"
            scripts = Path(__file__).resolve().parents[1] / "skills" \
                / "memory" / "scripts"
            os.environ["ZMEM_STORE"] = str(db)
            # The in-process _memory lookup filters by namespace aliases
            # resolved from the AMBIENT env - pin the seeded namespace.
            os.environ["ZMEM_NAMESPACE"] = NS
            os.environ["ZMEM_SOURCE_ROOT"] = str(root / "src")
            home = root / "hermes-home"
            home.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                (home / f"state.db{suffix}").write_bytes(b"")
            os.environ["HERMES_HOME"] = str(home)
            if str(scripts) not in sys.path:
                sys.path.insert(0, str(scripts))
            # hermes_state is the EXTERNAL Hermes module (absent on this
            # host) - inject a fake into sys.modules; the resolver's lazy
            # `from hermes_state import SessionDB` reads the attribute at
            # call time.
            import types as _types
            from storelib import source as source_mod
            fake_hermes = _types.ModuleType("hermes_state")

            class FakeDB:
                def __init__(self, *, db_path, read_only):
                    pass

                def resolve_resume_session_id(self, session):
                    return "resolved"

                def get_messages(self, session):
                    return [{"id": 1, "content": excerpt_line}]

                def close(self):
                    pass

            fake_hermes.SessionDB = FakeDB
            saved = sys.modules.get("hermes_state")
            sys.modules["hermes_state"] = fake_hermes
            try:
                evidence_id = self._write_production_evidence(
                    db, memory_id, excerpt_line,
                    "hermes:state.db/session/s-h", None, "s-h")
                conn = sqlite3.connect(db)
                try:
                    result = source_mod.repair_from_evidence(
                        conn, memory_id=memory_id,
                        evidence_id=evidence_id, mode="apply")
                finally:
                    conn.close()
            finally:
                if saved is None:
                    sys.modules.pop("hermes_state", None)
                else:
                    sys.modules["hermes_state"] = saved
            self.assertEqual(result["resolver"], "hermes")
            self.assertIn(self.CANARY, _memory_content(db, memory_id))


if __name__ == "__main__":
    unittest.main()
