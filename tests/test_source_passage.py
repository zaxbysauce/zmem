"""AC2 + AC3 (#502/#512) frozen acceptance checks - evidence-backed repair and
quoted-passage verification through the store CLI.

Both checks drive ``skills/memory/scripts/store.py`` as a subprocess against a
hermetic temporary store (env-pinned; never a real store). They are RED at base
0ada1b83 as RUNTIME failures (the subcommands/flags do not exist yet); the
files COLLECT cleanly because no not-yet-existing symbol is imported.

AC2 CONTRACT - ``store.py source repair`` (new subcommand of ``source``)::

    store.py source repair --id <memory-id> --evidence-id <evidence-id> \
                           (--preview | --apply)

- ``--evidence-id`` selects the evidence row; it is REQUIRED when the memory
  has more than one association, and an id not associated with the memory
  refuses.
- Success prints ONE JSON object as the final stdout line::
      {"memory_id": str, "evidence_id": str, "mode": "preview"|"apply",
       "restore_value": str|null, "verified": bool, "applied": bool}
- Refusals exit 1 with a first stderr line starting
  ``[zmem] source repair: refused`` and NEVER mutate the row, for: a changed
  quotation (the evidence excerpt no longer corresponds to the memory's
  redacted context), failed object verification, unknown memory/evidence, or a
  missing ``ZMEM_REPAIR_ROOT``.
- ``ZMEM_REPAIR_ROOT`` (env pin): trusted directory root. The evidence row's
  ``ref_path`` is a bare relative name (``_safe_ref`` rules: no separators,
  drives, schemes, or ``..``) resolved to ``<ZMEM_REPAIR_ROOT>/<ref_path>``,
  which names the git repository whose object database the candidate value is
  verified against (the restored 40-hex must exist as an object there).
- ``--preview`` reports ``restore_value``/``verified`` and never mutates.
- ``--apply`` restores ONLY the redaction span the selected quotation
  verifies; every other redaction span in the row stays ``[REDACTED_SECRET]``;
  the output JSON retains the evidence id (provenance).

AC3 CONTRACT - ``store.py source show`` (new explicit ``show`` subcommand of
``source``, parallel to ``scan``; the direct single-anchor ``source --id``
form is unchanged)::

    store.py source show --id <memory-id> [--evidence-id <evidence-id>]

- Success prints the ``source`` JSON shape plus:
  - ``evidence_ids``: ALL evidence ids associated with the memory (the field
    already exists in source.py's result dict; selection stops hiding it).
  - ``verification``: ``{"outcome": "exact"|"relocated"|"ambiguous"|"missing",
    "evidence_id": "<selected id>"}`` computed by matching the selected
    evidence row's excerpt against the CURRENT bytes of the quoted project
    file: exact = verbatim line present at/near the recorded ``ref_offset``
    (0-based line index); relocated = present exactly once elsewhere;
    ambiguous = multiple distinct occurrences; missing = absent. Outcomes are
    REPORTS (exit 0); only trust-boundary failures refuse.
- Without ``--evidence-id`` on a multi-anchor memory: structured ambiguity
  refusal - exit 1, stderr starting ``[zmem] source: refused`` and naming the
  candidate evidence ids; never a silent pick (#139 invariant kept).
- ``--evidence-id`` naming an id not associated with the memory (or an orphan
  association) refuses (#139 invariant).
- ``ZMEM_SOURCE_ROOT`` (env pin): trusted directory root under which the
  evidence ``ref_path`` (bare relative filename) resolves to the quoted
  project file.
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
NS = "project:passage-502-512"


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
                "ZMEM_HERMES_SESSIONS", "HERMES_HOME"):
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


def _set_excerpt(db_path: Path, evidence_id: str, excerpt: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE evidence SET excerpt=? WHERE id=?",
                     (excerpt, evidence_id))
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


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _init_git_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    (repo / "README.txt").write_text(
        "fixture repository for repair verification\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "fixture commit")
    return _git(repo, "rev-parse", "HEAD")


def _last_json(stdout_text: str) -> dict:
    lines = [line for line in stdout_text.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("no JSON line on stdout")
    return json.loads(lines[-1])


class SourcePassageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zmem-passage-502-512-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_rescan_restores_only_a_verified_local_commit(self) -> None:
        root = self.root
        repo = root / "repo"
        sha = _init_git_repo(repo)
        repair_env: dict[str, str | None] = {"ZMEM_REPAIR_ROOT": str(root)}
        content = (f"canary fails rollback {sha} immediately; "
                   "password=An0therSecret!42 for the service account.")
        _init(root, repair_env)
        memory_id = _add(root, content, repair_env)
        db = root / "store.sqlite"

        # Base behavior anchor: the capture path redacted both spans (the
        # unlabeled 40-hex run and the password value).
        stored = _memory_content(db, memory_id)
        self.assertIn("[REDACTED_SECRET]", stored)
        self.assertNotIn(sha, stored)
        self.assertNotIn("An0therSecret!42", stored)

        evidence_id = "ev-repair-1"
        _insert_evidence(db, memory_id, [(
            evidence_id, "sess-repair", "codex", "pretool", "turn",
            "2026-11-26T12:00:00Z", "h1",
            f"canary fails rollback {sha} immediately", "repo", 0)])

        # Fail-closed without the trusted-root pin: refusal, no mutation.
        unrooted = _run(root, "source", "repair", "--id", memory_id,
                        "--evidence-id", evidence_id, "--preview",
                        extra_env={"ZMEM_REPAIR_ROOT": None})
        self.assertEqual(unrooted.returncode, 1, unrooted.stderr)
        self.assertTrue(
            unrooted.stderr.startswith("[zmem] source repair: refused"),
            unrooted.stderr)
        self.assertNotIn(sha, _memory_content(db, memory_id))

        # Preview: reports the proposed restoration, changes nothing.
        preview = _run(root, "source", "repair", "--id", memory_id,
                       "--evidence-id", evidence_id, "--preview",
                       extra_env=repair_env)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        payload = _last_json(preview.stdout)
        self.assertEqual(payload["restore_value"], sha)
        self.assertTrue(payload["verified"])
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["evidence_id"], evidence_id)
        self.assertEqual(payload["mode"], "preview")
        after_preview = _memory_content(db, memory_id)
        self.assertNotIn(sha, after_preview)
        self.assertIn("[REDACTED_SECRET]", after_preview)

        # Apply: restores the verified commit only; the unlinked redaction
        # stays; the result retains the evidence id.
        apply_result = _run(root, "source", "repair", "--id", memory_id,
                            "--evidence-id", evidence_id, "--apply",
                            extra_env=repair_env)
        self.assertEqual(apply_result.returncode, 0, apply_result.stderr)
        payload = _last_json(apply_result.stdout)
        self.assertEqual(payload["restore_value"], sha)
        self.assertTrue(payload["verified"])
        self.assertTrue(payload["applied"])
        self.assertEqual(payload["evidence_id"], evidence_id)
        self.assertEqual(payload["mode"], "apply")
        repaired = _memory_content(db, memory_id)
        self.assertIn(sha, repaired)
        self.assertIn("[REDACTED_SECRET]", repaired)
        self.assertNotIn("An0therSecret!42", repaired)

        # Refusals on a second, independent store: a TAMPERED quotation and a
        # context-matching but unverified object both refuse without mutation.
        store2: dict[str, str | None] = {"ZMEM_STORE": str(root / "store2.sqlite"),
                                         "ZMEM_REPAIR_ROOT": str(root)}
        db2 = root / "store2.sqlite"
        _init(root, store2)
        memory2 = _add(root, content, store2)
        _insert_evidence(db2, memory2, [(
            "ev-tamper", "sess-repair", "codex", "pretool", "turn",
            "2026-11-26T12:00:00Z", "h2",
            f"canary fails rollforward {sha} immediately", "repo", 0)])
        tampered = _run(root, "source", "repair", "--id", memory2,
                        "--evidence-id", "ev-tamper", "--apply",
                        extra_env=store2)
        self.assertEqual(tampered.returncode, 1, tampered.stderr)
        self.assertTrue(
            tampered.stderr.startswith("[zmem] source repair: refused"),
            tampered.stderr)
        self.assertNotIn(sha, _memory_content(db2, memory2))

        bogus = "deadbeef" * 5
        _set_excerpt(db2, "ev-tamper",
                     f"canary fails rollback {bogus} immediately")
        unverified = _run(root, "source", "repair", "--id", memory2,
                          "--evidence-id", "ev-tamper", "--apply",
                          extra_env=store2)
        self.assertEqual(unverified.returncode, 1, unverified.stderr)
        self.assertTrue(
            unverified.stderr.startswith("[zmem] source repair: refused"),
            unverified.stderr)
        self.assertNotIn(bogus, _memory_content(db2, memory2))
        self.assertIn("[REDACTED_SECRET]", _memory_content(db2, memory2))

    def test_cli_checks_a_quoted_project_file_after_independent_edits(self) -> None:
        root = self.root
        trusted = root / "trusted"
        trusted.mkdir()
        notes = trusted / "deploy-notes.txt"
        quoted = "staging rollout completes in ten minutes"
        show_env: dict[str, str | None] = {"ZMEM_SOURCE_ROOT": str(trusted)}
        _init(root, show_env)
        memory_id = _add(
            root,
            "ops note: staging rollout completes in ten minutes (from deploy notes)",
            show_env)
        db = root / "store.sqlite"
        ev1, ev2 = "ev-passage-1", "ev-passage-2"
        # Two DIFFERENT anchors (ts, excerpt, offset) on one memory: the exact
        # case that refuses at base (source.py:153-155).
        _insert_evidence(db, memory_id, [
            (ev1, "sess-passage", "codex", "pretool", "turn",
             "2026-11-26T10:00:00Z", "h1", "# deploy notes",
             "deploy-notes.txt", 0),
            (ev2, "sess-passage", "codex", "pretool", "turn",
             "2026-11-26T11:00:00Z", "h2", quoted, "deploy-notes.txt", 2),
        ])

        def write_notes(lines: list[str]) -> None:
            notes.write_text("\n".join(lines) + "\n", encoding="utf-8")

        def show(*extra_args: str) -> subprocess.CompletedProcess:
            return _run(root, "source", "show", "--id", memory_id,
                        *extra_args, extra_env=show_env)

        # exact: selection honored, all evidence ids listed.
        write_notes(["# deploy notes", "", quoted, "retention: 30 days"])
        exact = show("--evidence-id", ev2)
        self.assertEqual(exact.returncode, 0, exact.stderr)
        payload = json.loads(exact.stdout)
        self.assertIn(ev1, payload["evidence_ids"])
        self.assertIn(ev2, payload["evidence_ids"])
        self.assertEqual(payload["verification"]["outcome"], "exact")
        self.assertEqual(payload["verification"]["evidence_id"], ev2)

        # No selector on a multi-anchor memory: structured ambiguity refusal
        # naming the candidates (never a silent pick).
        unselected = show()
        self.assertEqual(unselected.returncode, 1)
        self.assertTrue(
            unselected.stderr.startswith("[zmem] source: refused"),
            unselected.stderr)
        self.assertIn(ev1, unselected.stderr)
        self.assertIn(ev2, unselected.stderr)

        # ambiguous: a duplicate occurrence of the quoted line elsewhere.
        write_notes(["# deploy notes", "", quoted, "retention: 30 days", quoted])
        duplicated = show("--evidence-id", ev2)
        self.assertEqual(duplicated.returncode, 0, duplicated.stderr)
        self.assertEqual(
            json.loads(duplicated.stdout)["verification"]["outcome"],
            "ambiguous")

        # relocated: duplicate removed, unrelated lines inserted above.
        write_notes(["editor: two new lines", "changelog: unrelated edit",
                     "# deploy notes", "", quoted, "retention: 30 days"])
        moved = show("--evidence-id", ev2)
        self.assertEqual(moved.returncode, 0, moved.stderr)
        moved_payload = json.loads(moved.stdout)
        self.assertEqual(moved_payload["verification"]["outcome"], "relocated")
        self.assertIn(ev1, moved_payload["evidence_ids"])
        self.assertEqual(moved_payload["verification"]["evidence_id"], ev2)

        # missing: the quoted line itself is gone.
        write_notes(["editor: two new lines", "changelog: unrelated edit",
                     "# deploy notes", "",
                     "staging rollout completes in twenty minutes",
                     "retention: 30 days"])
        gone = show("--evidence-id", ev2)
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertEqual(
            json.loads(gone.stdout)["verification"]["outcome"], "missing")

        # Orphan preservation (#139): an unknown selector still refuses.
        orphan = show("--evidence-id", "ev-does-not-exist")
        self.assertEqual(orphan.returncode, 1)
        self.assertTrue(
            orphan.stderr.startswith("[zmem] source: refused"),
            orphan.stderr)


if __name__ == "__main__":
    unittest.main()
