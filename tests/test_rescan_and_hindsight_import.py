"""Issue #181 (Workstream L PR 2): rescan-secrets + Hindsight JSONL import.

Focused end-to-end tests for the two new operator surfaces:

- ``store.py rescan-secrets`` — dry-run reports exactly the fixture's live
  credential ids with byte-exact JSON and leaves the scanned store
  byte-identical; apply creates two append-only update lineages (tombstone +
  [REDACTED_SECRET] successor each) and never leaks credential bytes; a
  policy refusal exits 2 with the store untouched; the mode flags are a
  required mutually-exclusive pair.
- ``import-store.py --source hindsight`` — the ten-record fixture imports
  deterministically (10 fact rows, exact tags, canonical JSONL byte-identical
  to the committed expected file, reported digest equal to its sha256), and
  every malformed/duplicate/unsupported input fails with exit 1 BEFORE a
  destination exists (and leaves a seeded destination byte-identical, even
  with --force).

Isolation: the module pins the four store-isolation variables (ZMEM_STORE,
ZMEM_DATA, ZMEM_MODELS_DIR pointing at a nonexistent child,
ZMEM_MODEL_AUTODOWNLOAD=0) BEFORE any storelib import, every CLI run is a
subprocess against a per-test scratch copy, and direct sqlite connections
run PRAGMA foreign_keys=ON and assert it took. No test touches the
operator's ~/.zmem.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
IMPORT_PY = SCRIPTS_DIR / "import-store.py"

SECRETS_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "secrets"
HINDSIGHT_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "hindsight"

# Split literals so the raw token shape never appears verbatim in source
# (house precedent: tests/test_r04_queue_add_fb.py).
TOKEN_ONE = "ghp_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
TOKEN_TWO = "ghp_" + "zYxWvUtSrQpOnMlKjIhGfEdCbA9876543210"
ID_ONE = "00000000-0000-4000-8000-000000000181"
ID_TWO = "00000000-0000-4000-8000-000000000182"

# Module-level isolation pin: set BEFORE any storelib import anywhere in
# this module (STORE_PATH freezes at first storelib import).
_MODULE_TMP = tempfile.mkdtemp(prefix="zmem-181-tests-")
os.environ["ZMEM_DATA"] = os.path.join(_MODULE_TMP, "data")
os.environ["ZMEM_STORE"] = os.path.join(_MODULE_TMP, "data", "store.sqlite")
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_MODULE_TMP, "models-absent")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

sys.path.insert(0, str(SCRIPTS_DIR))

import atexit  # noqa: E402

atexit.register(shutil.rmtree, _MODULE_TMP, True)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _connect_verified(path: Path) -> sqlite3.Connection:
    """Read/write connection with the foreign-keys contract asserted."""
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1, f"PRAGMA foreign_keys=ON did not take (got {fk!r})"
    except BaseException:
        conn.close()
        raise
    return conn


class _ScratchCase(unittest.TestCase):
    """Per-test scratch directory + isolated CLI env + helpers."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-181-case-")
        self.addCleanup(self.tmp.cleanup)
        self.scratch = Path(self.tmp.name)

    def _env(self, store: Path | None = None) -> dict:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["ZMEM_DATA"] = str(self.scratch)
        env["ZMEM_STORE"] = str(store if store is not None
                                else self.scratch / "store.sqlite")
        env["ZMEM_MODELS_DIR"] = str(self.scratch / "models-absent")
        env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        return env

    def _run(self, argv: list, env: dict, timeout: int = 180):
        return subprocess.run(
            [sys.executable, str(STORE_PY)] + argv,
            env=env, capture_output=True, timeout=timeout)

    def _run_import(self, argv: list, env: dict, timeout: int = 300):
        return subprocess.run(
            [sys.executable, str(IMPORT_PY)] + argv,
            env=env, capture_output=True, timeout=timeout)


class RescanSecretsTests(_ScratchCase):

    def test_dry_run_lists_exact_fixture_ids_and_preserves_sha256(self):
        fixture = SECRETS_FIXTURES / "store.sqlite"
        expected = (SECRETS_FIXTURES / "expected-dry-run.json").read_bytes()
        store = self.scratch / "store.sqlite"
        shutil.copyfile(fixture, store)
        before = _sha256(store)

        proc = self._run(["rescan-secrets", "--dry-run"],
                         self._env(store))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Byte-exact stdout (LF-pinned; no text-mode newline translation).
        self.assertEqual(proc.stdout, expected)
        self.assertEqual(_sha256(store), before,
                         "dry-run must leave the scanned store byte-identical")
        combined = proc.stdout + proc.stderr
        self.assertNotIn(b"ghp_", combined)
        self.assertNotIn(b"AbCdEfGh", combined)
        self.assertNotIn(b"zYxWvU", combined)

    def test_apply_creates_two_update_lineages_and_tombstones_originals(self):
        fixture = SECRETS_FIXTURES / "store.sqlite"
        store = self.scratch / "store.sqlite"
        shutil.copyfile(fixture, store)

        proc = self._run(["rescan-secrets", "--apply"], self._env(store))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Exactly one machine-readable line on stdout; progress goes to
        # stderr only.
        self.assertEqual(proc.stdout.count(b"\n"), 1)
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(payload["mode"], "apply")
        self.assertEqual(payload["rows_scanned"], 2)
        self.assertEqual(payload["rows_needing_review"], 2)
        self.assertEqual(payload["ids"], [ID_ONE, ID_TWO])

        conn = _connect_verified(store)
        try:
            q = lambda s: conn.execute(s).fetchone()[0]  # noqa: E731
            self.assertEqual(q("SELECT COUNT(*) FROM memory"), 4)
            self.assertEqual(
                q("SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL"), 2)
            self.assertEqual(q(
                "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL "
                "AND update_of != ''"), 2)
            self.assertEqual(
                q("SELECT COUNT(*) FROM memory WHERE superseded_at IS NOT NULL"), 2)
            self.assertEqual(q(
                "SELECT COUNT(*) FROM memory WHERE superseded_at IS NOT NULL "
                "AND valid_until = superseded_at AND valid_until != ''"), 2)
            self.assertEqual(q(
                "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL "
                "AND instr(content, '[REDACTED_SECRET]') > 0"), 2)
            self.assertEqual(q(
                "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL "
                "AND (instr(content, 'ghp_') > 0 OR instr(tags, 'ghp_') > 0)"), 0)
            # Review hardening (round 1, finding 2): pin the exact successor
            # (tags, source_ref) pairs so a swapped-tuple regression in the
            # candidate comparison cannot pass on content checks alone.
            expected_pairs = {
                ID_ONE: ("auto-redacted", "fixture:181:one"),
                ID_TWO: ("auto-redacted", "fixture:181:two"),
            }
            successors = conn.execute(
                "SELECT update_of, tags, source_ref FROM memory "
                "WHERE superseded_at IS NULL").fetchall()
            for row in successors:
                self.assertEqual(
                    (row["tags"], row["source_ref"]),
                    expected_pairs[row["update_of"]])
        finally:
            conn.close()
        combined = proc.stdout + proc.stderr
        self.assertNotIn(b"ghp_", combined)
        self.assertNotIn(b"AbCdEfGh", combined)
        self.assertNotIn(b"zYxWvU", combined)

    def test_apply_refusal_rolls_back_without_store_change(self):
        """A sudo -S row (unredactable secret) refuses at scan time: exit 2,
        stable stderr line, store bytes and row counts unchanged."""
        from storelib.schema import init_db, migrate

        store = self.scratch / "refusal.sqlite"
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        init_db(conn)
        migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, "
            "source_ref, source_hash, confidence, signal, valid_from, "
            "ingestion_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ID_ONE, "project:fixture", "fact",
             f"fixture sudo -S echo with {TOKEN_ONE}", "", "fixture:181:one",
             "", 0.9, "test", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        conn.commit()
        conn.close()
        before = _sha256(store)

        proc = self._run(["rescan-secrets", "--apply"], self._env(store))
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"[zmem] rescan-secrets:", proc.stderr)
        self.assertEqual(_sha256(store), before,
                         "a refusal must leave the store byte-identical")
        conn = _connect_verified(store)
        try:
            total = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
            live = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual((total, live), (1, 1))

    def test_dry_run_negative_control_clean_row_is_not_a_candidate(self):
        """Review hardening (round 1, finding 3): a clean live row must NOT
        be flagged — the two-row fixture alone cannot distinguish per-field
        candidate comparison from flag-everything, because both rows are
        candidates. Build a 3-row store (two credential rows + one clean)
        and pin rows_scanned=3, rows_needing_review=2, clean id absent."""
        from storelib.schema import init_db, migrate

        store = self.scratch / "negative-control.sqlite"
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        init_db(conn)
        migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        rows = [
            (ID_ONE, f"fixture credential {TOKEN_ONE} one", "fixture:181:one"),
            (ID_TWO, f"fixture credential {TOKEN_TWO} two", "fixture:181:two"),
            ("00000000-0000-4000-8000-000000000099",
             "fixture clean row with no credential material",
             "fixture:181:clean"),
        ]
        for mid, content, source_ref in rows:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, source_hash, confidence, signal, valid_from, "
                "ingestion_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (mid, "project:fixture", "fact", content, "", source_ref,
                 "", 0.9, "test", "2026-01-01T00:00:00Z",
                 "2026-01-01T00:00:00Z"))
        conn.commit()
        conn.close()

        proc = self._run(["rescan-secrets", "--dry-run"], self._env(store))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(payload["rows_scanned"], 3)
        self.assertEqual(payload["rows_needing_review"], 2)
        self.assertEqual(payload["ids"], [ID_ONE, ID_TWO])
        self.assertNotIn("00000000-0000-4000-8000-000000000099",
                         payload["ids"])

    def test_mode_flags_are_mutually_exclusive_and_required(self):
        store = self.scratch / "store.sqlite"
        shutil.copyfile(SECRETS_FIXTURES / "store.sqlite", store)
        env = self._env(store)

        proc = self._run(["rescan-secrets"], env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"one of the arguments --dry-run --apply is required",
                      proc.stderr)

        proc = self._run(["rescan-secrets", "--dry-run", "--apply"], env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"not allowed with argument --dry-run", proc.stderr)

    def test_new_surfaces_route_through_the_shared_policy_entry_point(self):
        """Phase 4.2 guardrail (structural): the new surfaces must route
        through the single capture-policy entry point and the sanctioned
        ingest path — no local redaction patterns, no private INSERTs (the
        #180 single-policy invariant)."""
        import importlib.util
        import inspect

        import storelib.write as write_mod
        src = inspect.getsource(write_mod.rescan_secrets)
        self.assertIn("apply_capture_policy(", src)
        self.assertIn("update_memory(", src)
        self.assertNotIn("gh[pousr]", src)
        self.assertNotIn("SECRET_CREDENTIAL_PATTERNS", src)

        spec = importlib.util.spec_from_file_location(
            "zmem_import_store_guard", IMPORT_PY)
        import_store = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(import_store)
        hsrc = inspect.getsource(import_store.run_hindsight_import)
        self.assertIn("_validate_sync_row(", hsrc)
        self.assertIn("cmd_ingest_jsonl_strict(", hsrc)
        self.assertNotIn("INSERT INTO memory", hsrc)

    def test_rescan_missing_store_refuses_without_creating(self):
        """A missing store must refuse loudly (exit 2) instead of being
        silently created as an empty store that scans zero rows — the whole
        justification for rescan-secrets' early dispatch."""
        absent = self.scratch / "absent.sqlite"
        for mode in ("--dry-run", "--apply"):
            with self.subTest(mode=mode):
                proc = self._run(["rescan-secrets", mode],
                                 self._env(absent))
                self.assertEqual(proc.returncode, 2)
                self.assertIn(b"[zmem] rescan-secrets:", proc.stderr)
                self.assertFalse(absent.exists(),
                                 "rescan-secrets must not create the store")

    def test_apply_dedup_fold_rolls_back_whole_batch(self):
        """Two live rows whose content redacts IDENTICALLY: the first
        candidate's successor is a live exact-match duplicate for the
        second, so update_memory folds (created_new=False) and the whole
        batch must roll back — exit 2, stderr names the fold, store bytes
        and row counts unchanged (model-absent: the exact-match fallback
        needs no embeddings)."""
        from storelib.schema import init_db, migrate

        store = self.scratch / "fold.sqlite"
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        init_db(conn)
        migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        shared = f"fixture credential {TOKEN_ONE} same"
        for mid in (ID_ONE, ID_TWO):
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, source_hash, confidence, signal, valid_from, "
                "ingestion_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (mid, "project:fixture", "fact", shared, "",
                 f"fixture:181:{mid[-3:]}", "", 0.9, "test",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        conn.commit()
        conn.close()
        before = _sha256(store)

        proc = self._run(["rescan-secrets", "--apply"], self._env(store))
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"folded into an existing row", proc.stderr)
        self.assertEqual(_sha256(store), before,
                         "a dedup-fold refusal must leave bytes unchanged")
        conn = _connect_verified(store)
        try:
            counts = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN superseded_at IS NULL THEN 1 ELSE 0 END) "
                "FROM memory").fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(counts), (2, 2))


class HindsightFeedbackTests(_ScratchCase):
    """Review-round hardening (PR #299 feedback): sidecar-replay regression,
    parser-leg exit codes, and the external-input hardening gates."""

    def test_force_import_into_wal_destination_does_not_replay_stale_sidecar(self):
        """PRR-002 regression: a destination store left with committed
        uncheckpointed WAL frames must not have them replayed onto the
        replaced store (the legacy lane stashes destination sidecars; the
        hindsight lane now does too). Also pins dry-run byte-identity on a
        WAL store (mode=ro open — no checkpoint-on-close mutation)."""
        from storelib.schema import init_db, migrate

        dest = self.scratch / "waldest"
        dest.mkdir()
        store_path = dest / "store.sqlite"
        # Child process: build a real WAL store with a committed marker row
        # and hard-exit WITHOUT closing, so store.sqlite-wal keeps frames.
        child = (
            "import os, sqlite3, sys\n"
            f"sys.path.insert(0, r'{SCRIPTS_DIR}')\n"
            "from storelib.schema import init_db, migrate\n"
            f"conn = sqlite3.connect(r'{store_path}')\n"
            "init_db(conn)\n"
            "migrate(conn)\n"
            "conn.execute('PRAGMA journal_mode=WAL')\n"
            "conn.execute('INSERT INTO memory (id, namespace, type, content,"
            " tags, source_ref, source_hash, confidence, signal, valid_from,"
            " ingestion_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)',\n"
            "  ('eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee', 'project:fixture',"
            " 'fact', 'stale wal marker', '', 'fixture:wal', '', 0.9,"
            " 'test', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'))\n"
            "conn.commit()\n"
            "conn.execute('INSERT INTO memory (id, namespace, type, content,"
            " tags, source_ref, source_hash, confidence, signal, valid_from,"
            " ingestion_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)',\n"
            "  ('ffffffff-ffff-4fff-8fff-ffffffffffff', 'project:fixture',"
            " 'fact', 'uncheckpointed marker', '', 'fixture:wal', '', 0.9,"
            " 'test', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'))\n"
            "conn.commit()\n"
            "os._exit(0)\n"
        )
        proc = subprocess.run([sys.executable, "-c", child], env=self._env(),
                              capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        wal = dest / "store.sqlite-wal"
        self.assertTrue(wal.is_file(), "child must leave a live -wal behind")
        wal_sha = _sha256(wal)
        store_sha = _sha256(store_path)

        # Dry-run (read-only open) must not checkpoint or mutate either file.
        dry = self._run(["rescan-secrets", "--dry-run"],
                        self._env(store_path))
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(_sha256(store_path), store_sha,
                         "dry-run mutated a WAL store's main db file")
        self.assertEqual(_sha256(wal), wal_sha,
                         "dry-run mutated the store's -wal sidecar")

        # --force import: the stale sidecar must be stashed away, not
        # replayed — the destination ends up with exactly the 10 imported
        # rows and no stale marker.
        imp = self._run_import(
            ["--source", "hindsight",
             "--input", str(HINDSIGHT_FIXTURES / "import.jsonl"),
             "--dest-dir", str(dest), "--force"],
            self._env())
        self.assertEqual(imp.returncode, 0, imp.stderr)
        conn = _connect_verified(dest / "store.sqlite")
        try:
            total = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
            marker = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE id = "
                "'ffffffff-ffff-4fff-8fff-ffffffffffff'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(total, 10,
                         "stale WAL replay destroyed/replaced imported rows")
        self.assertEqual(marker, 0, "stale WAL row resurrected")
        self.assertFalse((dest / "store.sqlite-wal").exists(),
                         "stashed sidecar must not survive a successful import")

    def test_importer_parser_combination_failures_exit_2(self):
        """AC4 parser legs (issue #181): missing --input and an --input
        paired with a nonliteral source are argparse-level exit-2
        failures before any destination work."""
        missing = self._run_import(
            ["--source", "hindsight", "--dest-dir", str(self.scratch / "d1")],
            self._env())
        self.assertEqual(missing.returncode, 2)
        self.assertIn(b"--source hindsight requires --input", missing.stderr)

        extra = self._run_import(
            ["--source", str(SECRETS_FIXTURES / "store.sqlite"),
             "--input", str(HINDSIGHT_FIXTURES / "import.jsonl"),
             "--dest-dir", str(self.scratch / "d2")],
            self._env())
        self.assertEqual(extra.returncode, 2)
        self.assertIn(b"--input is only valid with --source hindsight",
                      extra.stderr)
        self.assertFalse((self.scratch / "d1" / "store.sqlite").exists())
        self.assertFalse((self.scratch / "d2" / "store.sqlite").exists())

    def test_parse_rejects_nan_duplicate_keys_and_oversize_input(self):
        """External-input hardening: non-finite JSON constants, duplicate
        object keys, and an over-cap input are content failures (exit 1
        shape: ValueError raised before any destination work)."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "zmem_import_store_hardening", IMPORT_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        cases = {
            "nan": ('{"kind":"world","id":"%s","text":"x",'
                    '"metadata":{"m":NaN}}' % ID_ONE, "non-finite"),
            "infinity": ('{"kind":"world","id":"%s","text":"Infinity",'
                         '"occurred_start":Infinity}' % ID_ONE, "non-finite"),
            "dup-key": ('{"kind":"world","kind":"experience","id":"%s",'
                        '"text":"x"}' % ID_ONE, "duplicate JSON object key"),
            "dup-nested": ('{"kind":"world","id":"%s","text":"x",'
                           '"metadata":{"a":1,"a":2}}' % ID_ONE,
                           "duplicate JSON object key"),
        }
        for name, (text, needle) in cases.items():
            with self.subTest(bad=name):
                src = self.scratch / f"bad-{name}.jsonl"
                src.write_text(text + "\n", encoding="utf-8")
                with self.assertRaises(ValueError) as ctx:
                    mod._parse_hindsight_records(src)
                self.assertIn(needle, str(ctx.exception))

        # The input size cap fires before parsing (module constant, so the
        # in-process test can shrink it).
        big = self.scratch / "big.jsonl"
        big.write_text("x" * 64, encoding="utf-8")
        original = mod.HINDSIGHT_MAX_INPUT_BYTES
        try:
            mod.HINDSIGHT_MAX_INPUT_BYTES = 8
            with self.assertRaises(ValueError) as ctx:
                mod.run_hindsight_import(big, self.scratch / "dest-big")
            self.assertIn("over the", str(ctx.exception))
        finally:
            mod.HINDSIGHT_MAX_INPUT_BYTES = original
        self.assertFalse((self.scratch / "dest-big").exists())


class HindsightImportTests(_ScratchCase):

    def test_hindsight_import_has_expected_types_and_tags(self):
        dest = self.scratch / "dest"
        proc = self._run_import(
            ["--source", "hindsight",
             "--input", str(HINDSIGHT_FIXTURES / "import.jsonl"),
             "--dest-dir", str(dest)],
            self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout.decode("utf-8")
        self.assertIn("source_count=10", out)
        self.assertIn("destination_count=10", out)

        expected_bytes = (HINDSIGHT_FIXTURES / "expected.jsonl").read_bytes()
        expected_digest = hashlib.sha256(expected_bytes).hexdigest()
        self.assertIn(f"output_sha256={expected_digest}", out)
        canonical = dest / "hindsight-import.jsonl"
        self.assertTrue(canonical.is_file(),
                        f"canonical output missing; dest holds "
                        f"{sorted(p.name for p in dest.iterdir())}")
        self.assertEqual(canonical.read_bytes(), expected_bytes)

        # Per-id expectations derived from the committed canonical file.
        per_id = {}
        for line in expected_bytes.decode("utf-8").splitlines():
            row = json.loads(line)
            per_id[row["id"]] = (row["type"], row["tags"])

        conn = _connect_verified(dest / "store.sqlite")
        try:
            count = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
            self.assertEqual(count, 10)
            rows = conn.execute("SELECT id, type, tags FROM memory").fetchall()
        finally:
            conn.close()
        for row in rows:
            self.assertIn(row["id"], per_id, f"unexpected id {row['id']}")
            self.assertEqual((row["type"], row["tags"]), per_id[row["id"]])
        self.assertEqual(len(rows), len(per_id))

    def test_hindsight_import_rejects_bad_input_without_partial_destination(self):
        bad_inputs = {
            "malformed": "{\"kind\": \"world\", not json\n",
            "duplicate-id": (
                "{\"kind\":\"world\",\"id\":\"%s\",\"text\":\"dup one\"}\n"
                "{\"type\":\"world\",\"id\":\"%s\",\"text\":\"dup two\"}\n"
                % (ID_ONE, ID_ONE)),
            "unsupported-kind": (
                "{\"kind\":\"dream\",\"id\":\"%s\",\"text\":\"a dream\"}\n"
                % ID_ONE),
            "invalid-metadata": (
                "{\"kind\":\"world\",\"id\":\"%s\",\"text\":\"meta\",\""
                "metadata\":\"flat\"}\n" % ID_ONE),
        }
        # Fresh destinations: every failure creates no destination store.
        for name, text in bad_inputs.items():
            with self.subTest(bad=name, phase="fresh"):
                src = self.scratch / f"bad-{name}.jsonl"
                src.write_text(text, encoding="utf-8")
                dest = self.scratch / f"dest-{name}"
                proc = self._run_import(
                    ["--source", "hindsight", "--input", str(src),
                     "--dest-dir", str(dest)], self._env())
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn(b"[import] FAILED:", proc.stderr)
                self.assertFalse((dest / "store.sqlite").exists(),
                                 "failed import must not create a destination")

        # Seeded destination: every failure (even with --force) leaves the
        # store and canonical output byte-identical.
        dest = self.scratch / "dest-seeded"
        good = self._run_import(
            ["--source", "hindsight",
             "--input", str(HINDSIGHT_FIXTURES / "import.jsonl"),
             "--dest-dir", str(dest)], self._env())
        self.assertEqual(good.returncode, 0, good.stderr)
        store_sha = _sha256(dest / "store.sqlite")
        output_sha = _sha256(dest / "hindsight-import.jsonl")
        for name, text in bad_inputs.items():
            with self.subTest(bad=name, phase="seeded"):
                src = self.scratch / f"bad2-{name}.jsonl"
                src.write_text(text, encoding="utf-8")
                proc = self._run_import(
                    ["--source", "hindsight", "--input", str(src),
                     "--dest-dir", str(dest), "--force"], self._env())
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(_sha256(dest / "store.sqlite"), store_sha)
                self.assertEqual(
                    _sha256(dest / "hindsight-import.jsonl"), output_sha)

    def test_rejects_invalid_date_string_and_non_uuid_before_destination(self):
        bad_inputs = {
            "invalid-date-string": (
                "{\"kind\":\"world\",\"id\":\"%s\",\"text\":\"d\","
                "\"occurred_start\":\"not-a-date\"}\n" % ID_ONE),
            "invalid-date-number": (
                "{\"kind\":\"world\",\"id\":\"%s\",\"text\":\"d\","
                "\"occurred_start\":1767225600}\n" % ID_ONE),
            "non-uuid-id": (
                "{\"kind\":\"world\",\"id\":\"not-a-uuid-xxxxxxxxxxxxxxxxx\","
                "\"text\":\"d\"}\n"),
        }
        for name, text in bad_inputs.items():
            with self.subTest(bad=name):
                src = self.scratch / f"badx-{name}.jsonl"
                src.write_text(text, encoding="utf-8")
                dest = self.scratch / f"destx-{name}"
                proc = self._run_import(
                    ["--source", "hindsight", "--input", str(src),
                     "--dest-dir", str(dest)], self._env())
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn(b"[import] FAILED:", proc.stderr)
                self.assertFalse((dest / "store.sqlite").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
