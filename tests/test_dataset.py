"""Issue #134 (Workstream E): governed dataset export / publish / import.

CLI behavior runs through the real store.py subprocess with the canonical
isolation env (the tests/test_jsonl_sync.py discipline); publish-seam
behavior calls the library in-process with the store env pinned at MODULE
IMPORT (storelib freezes STORE_PATH at first import — see the repo-test
hazard notes). No test in this file ever touches the operator's ~/.zmem.
"""

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="zmem-test-dataset-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ.pop("ZMEM_DATA", None)
os.environ.pop("ZMEM_BACKUP_DIR", None)
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_TMP, "no-such-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ["PYTHONIOENCODING"] = "utf-8"

for _key in list(sys.modules):
    if _key == "storelib" or _key.startswith("storelib."):
        del sys.modules[_key]

import hashlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_PY = os.path.join(REPO_ROOT, "skills", "memory", "scripts", "store.py")
sys.path.insert(0, os.path.join(REPO_ROOT, "skills", "memory", "scripts"))
sys.path.insert(0, os.path.join(REPO_ROOT, "tests", "support"))

import fake_dataset_client  # noqa: E402
import storelib  # noqa: E402
from storelib import dataset as ds  # noqa: E402
from fake_secret_scanner import FakeSecretScanner  # noqa: E402

TARGET = "hf://datasets/owner/repo"
TS = "2026-09-10T00:00:00Z"
SECRET_ID = "00000000-0000-4000-8000-000000000101"
SECRET_CONTENT = "token ghp_fixture_000000000000000000000000000000000000"
EXPECTED_HELD_BACK = (
    '{"held_back":[{"id":"00000000-0000-4000-8000-000000000101",'
    '"reason":"secret_scan","row_checksum":"9471fb35c39b082bbb9ac6ee30406'
    'd731b4acefabd8b4a6ce9a8fb3ed8e8bfa2"}],"uploaded_rows":0}\n'
).encode("utf-8")
EXPECTED_HELD_BACK_SHA = (
    "e3c0d78bb6ed457a601a2ca4131eb728c2120e38b3d8f34cbf5932155fb81eec")


def base_env(store_path: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = store_path
    env.pop("ZMEM_DATA", None)
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_BACKUP_INTERVAL_DAYS", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(_TMP, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_cli(env: dict, *args: str):
    return subprocess.run([sys.executable, STORE_PY, *args], env=env,
                          capture_output=True, text=True, timeout=300)


def sha256_file(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class _StoreCase(unittest.TestCase):
    """One throwaway store + scratch dir per test."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="zmem-dataset-case-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = base_env(self.store)
        r = run_cli(self.env, "init")
        self.assertEqual(r.returncode, 0, r.stderr)

    def seed_row(self, rid: str, ns: str, content: str,
                 confidence: float = 0.5) -> None:
        """Direct, explicit SQL insert so fixture ids and timestamps never
        depend on the CLI's UUID or wall-clock write path (the
        injection-parity generator discipline)."""
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, "
                "ingestion_ts, confidence) VALUES (?, ?, 'fact', ?, ?, ?)",
                (rid, ns, content, TS, confidence))
            conn.commit()
        finally:
            conn.close()

    def export(self, name: str, *extra: str) -> str:
        out = os.path.join(self.tmp, name)
        r = run_cli(self.env, "export-dataset", out, *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return out

    def manifest(self, export_dir: str) -> dict:
        with open(os.path.join(export_dir, "manifest.json"), "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))

    def memory_rows(self, export_dir: str) -> list:
        return ds._read_family(export_dir, "memories",
                               self.manifest(export_dir)["format"])


def _rid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


class _CleanScanner:
    """Patch the scanner seam on the dataset MODULE (publish resolves the
    name from storelib.dataset globals at call time) with a scanner that
    reports a clean scan."""

    def __init__(self):
        self._original = ds.SecretScanner

    def __enter__(self):
        ds.SecretScanner = lambda: FakeSecretScanner()
        return self

    def __exit__(self, *exc):
        ds.SecretScanner = self._original
        return False


class DatasetExportTest(_StoreCase):
    """The issue's 13 named dataset behaviors."""

    def test_namespace_scoped_export_is_deterministic(self):
        self.seed_row(_rid(1), "project:det", "alpha determinism row")
        self.seed_row(_rid(2), "project:det", "beta determinism row")
        out1 = self.export("exp1", "--namespace", "project:det")
        out2 = self.export("exp2", "--namespace", "project:det")
        self.assertEqual(sha256_file(os.path.join(out1, "manifest.json")),
                         sha256_file(os.path.join(out2, "manifest.json")))
        for name in ("memories-000.parquet", "episodes-000.parquet",
                     "episode_members-000.parquet", "links-000.parquet"):
            self.assertEqual(
                sha256_file(os.path.join(out1, "data", name)),
                sha256_file(os.path.join(out2, "data", name)),
                name)
        self.assertEqual(self.manifest(out1)["namespaces"], ["project:det"])

    def test_unscoped_export_requires_yes(self):
        self.seed_row(_rid(1), "project:guard", "guarded row one")
        out = os.path.join(self.tmp, "guarded")
        r = run_cli(self.env, "export-dataset", out, "--all-namespaces")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("--yes is required with --all-namespaces", r.stderr)
        self.assertFalse(os.path.exists(out),
                         "exit 2 must fire before any row query / output")
        r = run_cli(self.env, "export-dataset", out, "--all-namespaces",
                    "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("project:guard", self.manifest(out)["namespaces"])

    def test_manifest_and_row_checksums_verify(self):
        self.seed_row(_rid(1), "project:chk", "checksum row alpha")
        out = self.export("chkexp", "--namespace", "project:chk")
        manifest = self.manifest(out)
        rows = self.memory_rows(out)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(ds.row_checksum(row), row["row_checksum"],
                             row["id"])
            self.assertEqual(row["export_snapshot_id"],
                             manifest["export_snapshot_id"])
            self.assertEqual(row["generator_revision"],
                             manifest["generator_revision"])
        self.assertEqual(
            ds._compute_snapshot_hash(
                {"memories": rows, "episodes": [], "episode_members": [],
                 "links": []}, True),
            manifest["source_snapshot_hash"])

    def test_tombstones_are_audit_only_by_default(self):
        rid = _rid(1)
        self.seed_row(rid, "project:tomb", "to be tombstoned")
        self.seed_row(_rid(2), "project:tomb", "keeper for episode")
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "UPDATE memory SET superseded_at = ?, supersede_reason = ? "
                "WHERE id = ?", ("2026-09-11T00:00:00Z", "test tombstone",
                                 rid))
            conn.execute(
                "INSERT INTO episode (id, namespace, started_at, ended_at,"
                " summary_memory_id, token_count) VALUES"
                " ('00000000-0000-4000-8000-000000000401', 'project:tomb',"
                " '2026-09-10T00:00:00Z', '', '', 0)")
            # Memberships onto BOTH the tombstoned row and the keeper.
            for mid in (rid, _rid(2)):
                conn.execute(
                    "INSERT INTO episode_memory (episode_id, memory_id,"
                    " added_at) VALUES"
                    " ('00000000-0000-4000-8000-000000000401', ?,"
                    " '2026-09-10T00:00:00Z')", (mid,))
            conn.commit()
        finally:
            conn.close()
        live_out = self.export("live", "--namespace", "project:tomb")
        self.assertEqual(sorted(row["id"] for row in self.memory_rows(live_out)),
                         [_rid(2)],
                         "tombstones must be audit-only by default; the "
                         "live keeper row stays")
        live_members = ds._read_family(live_out, "episode_members",
                                       self.manifest(live_out)["format"])
        self.assertEqual(
            sorted(m["memory_id"] for m in live_members), [_rid(2)],
            "live view keeps the keeper's membership and omits the one "
            "whose memory endpoint was filtered (no dangling endpoints)")
        audit_out = self.export("audit", "--namespace", "project:tomb",
                                "--include-tombstones")
        self.assertIn(rid, [row["id"] for row in self.memory_rows(audit_out)])
        audit_members = ds._read_family(audit_out, "episode_members",
                                        self.manifest(audit_out)["format"])
        self.assertEqual(
            sorted(m["memory_id"] for m in audit_members),
            sorted([rid, _rid(2)]),
            "audit view retains endpoint rows behind tombstones")
        self.assertTrue(self.manifest(audit_out)["include_tombstones"])

    def test_egress_scan_holds_back_secret_rows(self):
        fixture = os.path.join(REPO_ROOT, "tests", "fixtures", "dataset",
                               "expected-held-back.json")
        with open(fixture, "rb") as fh:
            self.assertEqual(fh.read(), EXPECTED_HELD_BACK,
                             "committed held-back fixture drifted")
        self.assertEqual(sha256_file(fixture), EXPECTED_HELD_BACK_SHA)

        self.seed_row(SECRET_ID, "project:test", SECRET_CONTENT)
        out = self.export("secret-exp", "--namespace", "project:test")
        clean = os.path.join(self.tmp, "secret-clean")
        shutil.copytree(out, clean)
        with _CleanScanner():
            result = storelib.publish_dataset(
                clean, TARGET, yes=True,
                client=fake_dataset_client.FakeDatasetClient(["old"]))
        self.assertEqual(set(result),
                         {"target", "revision", "dataset_revision",
                          "uploaded_rows", "held_back", "private"})
        self.assertEqual(result["held_back"], 0)

        found = os.path.join(self.tmp, "secret-found")
        shutil.copytree(out, found)
        scanner = FakeSecretScanner(flag_if_contains=(SECRET_CONTENT,))
        original = ds.SecretScanner
        ds.SecretScanner = lambda: scanner
        try:
            result = storelib.publish_dataset(
                found, TARGET, yes=True,
                client=fake_dataset_client.FakeDatasetClient(["old"]))
        finally:
            ds.SecretScanner = original
        self.assertEqual(result["held_back"], 1)
        self.assertEqual(result["uploaded_rows"], 0)
        with open(os.path.join(found, "held_back.json"), "rb") as fh:
            self.assertEqual(fh.read(), EXPECTED_HELD_BACK)

    def test_real_scanner_passes_fail_flag(self):
        """F-001 regression: the real trufflehog invocation MUST carry
        --fail — plain trufflehog exits 0 even with findings, which would
        make the egress scan a silent no-op. Assert the invocation contract
        against the source: --fail sits between --json and the staging dir."""
        source_path = os.path.join(REPO_ROOT, "skills", "memory", "scripts",
                                   "storelib", "dataset.py")
        with open(source_path, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('"--json", "--fail"', source,
                      "trufflehog invocation must pass --fail (F-001)")

    def test_real_scanner_exit183_yields_findings(self):
        """F-001 companion: with --fail, trufflehog exits 183 on findings;
        a stub exercising the real scan() (not the fake seam) must surface
        those findings rather than reporting a clean scan."""
        bindir = os.path.join(self.tmp, "bin")
        os.mkdir(bindir)
        stub = os.path.join(bindir, "trufflehog")
        if os.name == "nt":
            stub += ".bat"
        lines = [
            "#!/bin/sh",
            'echo \'{"Source":{"Data":"payload-000000.bin"},'
            '"DetectorName":"GitHub"}\'',
            "exit 183",
        ]
        with open(stub, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        if os.name != "nt":
            os.chmod(stub, 0o755)
        else:
            # Windows: rewrite as a batch stub (sh shebang is unusable).
            with open(stub, "w", encoding="utf-8", newline="\n") as fh:
                fh.write('@echo off\n'
                         'echo {"Source":{"Data":"payload-000000.bin"},'
                         '"DetectorName":"GitHub"}\n'
                         'exit 183\n')
        env = {**os.environ, "PATH": bindir + os.pathsep + os.environ["PATH"]}
        original = os.environ["PATH"]
        os.environ["PATH"] = env["PATH"]
        try:
            findings = ds.SecretScanner().scan([b"token ghp_probe"])
        finally:
            os.environ["PATH"] = original
        self.assertEqual([f["row_index"] for f in findings], [0])

    def test_public_target_refused_without_yes(self):
        """F-003 regression: create_repo(private=True, exist_ok=True)
        ignores `private` on an EXISTING repo — publish must refuse a
        public target unless --yes, and report visibility honestly."""
        self.seed_row(_rid(206), "project:vis", "visibility row")
        out = self.export("vis-exp", "--namespace", "project:vis")
        refusing = fake_dataset_client.FakeDatasetClient(
            ["old"], existing_private=False)
        with _CleanScanner():
            with self.assertRaises(storelib.PublishError):
                storelib.publish_dataset(out, TARGET, yes=False,
                                         client=refusing)
        self.assertEqual(refusing.commits, 0)
        public_ok = fake_dataset_client.FakeDatasetClient(
            ["old"], existing_private=False)
        with _CleanScanner():
            result = storelib.publish_dataset(out, TARGET, yes=True,
                                              client=public_ok)
        self.assertFalse(result["private"],
                         "a confirmed public publish must report private=False")
        private_ok = fake_dataset_client.FakeDatasetClient(["old"])
        with _CleanScanner():
            result = storelib.publish_dataset(out, TARGET, yes=True,
                                              client=private_ok)
        self.assertTrue(result["private"])

    def test_missing_scanner_refuses_without_allow_unscanned(self):
        self.seed_row(_rid(201), "project:scan", "plain row")
        out = self.export("scan-exp", "--namespace", "project:scan")
        with mock.patch.dict(os.environ, {"PATH": ""}):
            with self.assertRaises(storelib.ScannerUnavailable):
                storelib.publish_dataset(
                    out, TARGET, yes=True,
                    client=fake_dataset_client.FakeDatasetClient(["old"]))
        result = storelib.publish_dataset(
            out, TARGET, yes=True, allow_unscanned=True,
            client=fake_dataset_client.FakeDatasetClient(["old"]))
        self.assertEqual(result["uploaded_rows"], 1)
        self.assertIn("unscanned",
                      self.manifest(out)["governance"]["egress_scan"]
                      ["status"])

    def test_publish_retries_one_parent_conflict(self):
        self.seed_row(_rid(202), "project:cas", "cas row")
        out = self.export("cas-exp", "--namespace", "project:cas")
        stub_path = os.path.join(REPO_ROOT, "tests", "fixtures", "dataset",
                                 "hub-client-stub.json")
        with open(stub_path, "rb") as fh:
            stub = json.loads(fh.read().decode("utf-8"))
        self.assertEqual(stub["parents"], ["old", "conflict", "new"])
        self.assertTrue(stub["publish_private"])
        client = fake_dataset_client.FakeDatasetClient(stub["parents"])
        with _CleanScanner():
            result = storelib.publish_dataset(out, TARGET, yes=True,
                                              client=client)
        self.assertEqual(client.commit_attempts, 2)
        self.assertEqual(client.commits, 1)
        self.assertEqual(client.head_reads, 2)
        self.assertGreaterEqual(client.private_creates, 1)
        self.assertTrue(result["private"])
        self.assertIn("revision", result)

    def test_publish_fails_after_second_parent_conflict(self):
        self.seed_row(_rid(203), "project:cas2", "cas2 row")
        out = self.export("cas2-exp", "--namespace", "project:cas2")
        client = fake_dataset_client.FakeDatasetClient(
            ["old", "conflict", "conflict"])
        with _CleanScanner():
            with self.assertRaises(storelib.PublishError):
                storelib.publish_dataset(out, TARGET, yes=True, client=client)
        self.assertEqual(client.commit_attempts, 2)
        self.assertEqual(client.commits, 0)

    def test_wrong_target_namespace_requires_confirmation(self):
        self.seed_row(_rid(204), "project:overlap", "overlap row")
        out = self.export("overlap-exp", "--namespace", "project:overlap")
        refusing = fake_dataset_client.FakeDatasetClient(
            ["old"], existing_manifest={"namespaces": ["project:overlap"]})
        with self.assertRaises(storelib.PublishError):
            storelib.publish_dataset(out, TARGET, yes=False, client=refusing)
        self.assertEqual(refusing.commits, 0)
        confirming = fake_dataset_client.FakeDatasetClient(
            ["old"], existing_manifest={"namespaces": ["project:overlap"]})
        with _CleanScanner():
            result = storelib.publish_dataset(out, TARGET, yes=True,
                                              client=confirming)
        self.assertIn("revision", result)

    def test_import_requires_exact_revision(self):
        self.seed_row(_rid(1), "project:rev", "revision pinned row")
        out = self.export("rev-exp", "--namespace", "project:rev")
        revision = self.manifest(out)["source_snapshot_hash"]
        dest = os.path.join(self.tmp, "snap-bad")
        r = run_cli(self.env, "import-dataset", out, "--revision", "0" * 40,
                    "--dest", dest)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(
            os.path.join(dest, "snapshot.sqlite")))
        dest2 = os.path.join(self.tmp, "snap-ok")
        r = run_cli(self.env, "import-dataset", out, "--revision", revision,
                    "--dest", dest2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(
            os.path.join(dest2, "snapshot.sqlite")))

    def test_import_filters_before_recall(self):
        self.seed_row(_rid(301), "project:filter",
                      "unique llama husbandry guidance", confidence=0.95)
        self.seed_row(_rid(302), "project:filter",
                      "unique llama feeding schedules", confidence=0.20)
        out = self.export("filter-exp", "--namespace", "project:filter")
        revision = self.manifest(out)["source_snapshot_hash"]
        dest = os.path.join(self.tmp, "snap-filtered")
        r = run_cli(self.env, "import-dataset", out, "--revision", revision,
                    "--dest", dest, "--min-confidence", "0.9")
        self.assertEqual(r.returncode, 0, r.stderr)
        snap = os.path.join(dest, "snapshot.sqlite")
        conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        try:
            kept = [row[0] for row in
                    conn.execute("SELECT id FROM memory ORDER BY id")]
        finally:
            conn.close()
        self.assertEqual(kept, [_rid(301)],
                         "filters must apply before the snapshot exists")
        r = run_cli(base_env(snap), "recall", "--query",
                    "llama husbandry", "--namespace", "project:filter",
                    "--no-bump")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("llama husbandry", r.stdout)

    def test_missing_namespace_refuses_without_fallback(self):
        self.seed_row(_rid(1), "project:absent-ns", "absent namespace row")
        out = self.export("absent-exp", "--namespace", "project:absent-ns")
        revision = self.manifest(out)["source_snapshot_hash"]
        dest = os.path.join(self.tmp, "snap-absent")
        r = run_cli(self.env, "import-dataset", out, "--revision", revision,
                    "--dest", dest, "--namespace", "project:nowhere")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("namespace absent", r.stderr)
        self.assertFalse(os.path.exists(
            os.path.join(dest, "snapshot.sqlite")))

    def test_held_back_upload_is_importable(self):
        """Final-critic regression (trace round 2): when the egress scan
        holds rows back, the published artifact is RE-HASHED over the
        surviving rows, so the exact uploaded tree imports cleanly under
        its own published revision — with the secret row absent and no
        dangling links/memberships behind it."""
        self.seed_row(SECRET_ID, "project:test", SECRET_CONTENT)
        self.seed_row(_rid(210), "project:test", "publishable row one")
        # A link and a membership anchored on BOTH rows: the held-back
        # side must not dangle in the upload.
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "INSERT INTO memory_link (src_id, dst_id, relation, score, "
                "created_at) VALUES (?, ?, 'related', 0.5, ?)",
                (SECRET_ID, _rid(210), TS))
            # An episode whose extractive summary IS the held secret row
            # (final-critic round-3 regression): the upload must not ship
            # a summary reference to the held-back memory.
            conn.execute(
                "INSERT INTO episode (id, namespace, started_at, ended_at,"
                " summary_memory_id, token_count) VALUES"
                " ('00000000-0000-4000-8000-000000000501', 'project:test',"
                " ?, '', ?, 7)", (TS, SECRET_ID))
            conn.commit()
        finally:
            conn.close()
        out = self.export("importable-exp", "--namespace", "project:test")
        client = fake_dataset_client.FakeDatasetClient(["old"])
        scanner = FakeSecretScanner(flag_if_contains=(SECRET_CONTENT,))
        original = ds.SecretScanner
        ds.SecretScanner = lambda: scanner
        try:
            result = storelib.publish_dataset(out, TARGET, yes=True,
                                              client=client)
        finally:
            ds.SecretScanner = original
        self.assertEqual(result["held_back"], 1)
        self.assertEqual(result["uploaded_rows"], 1)

        # Reconstruct the EXACT uploaded tree from the fake client's
        # commit and import it as a consumer would.
        self.assertEqual(len(client.committed_trees), 1)
        _, _, tree = client.committed_trees[0]
        upload_dir = os.path.join(self.tmp, "upload")
        os.mkdir(upload_dir)
        for rel, blob in tree.items():
            path = os.path.join(upload_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(blob)
        with open(os.path.join(upload_dir, "manifest.json"), "rb") as fh:
            published = json.loads(fh.read().decode("utf-8"))
        self.assertNotEqual(published["source_snapshot_hash"],
                            self.manifest(out)["source_snapshot_hash"],
                            "held-back upload must carry a RE-HASHED "
                            "identity over the surviving rows")
        dest = os.path.join(self.tmp, "upload-snap")
        r = run_cli(self.env, "import-dataset", upload_dir,
                    "--revision", published["source_snapshot_hash"],
                    "--dest", dest)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        snap = os.path.join(dest, "snapshot.sqlite")
        conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        try:
            ids = [row[0] for row in
                   conn.execute("SELECT id FROM memory ORDER BY id")]
            links = conn.execute("SELECT COUNT(*) FROM memory_link"
                                 ).fetchone()[0]
            summaries = [row[0] for row in conn.execute(
                "SELECT summary_memory_id FROM episode")]
        finally:
            conn.close()
        self.assertEqual(ids, [_rid(210)], "secret row must be absent")
        self.assertEqual(links, 0, "links behind held rows must not dangle")
        self.assertEqual(summaries, [""],
                         "episode summaries anchored on held rows must "
                         "arrive as the no-summary value, not dangle")
        # F-006: held_back.json stays LOCAL-ONLY (unsalted content
        # fingerprints must not ship beside the manifest that discloses
        # the namespace).
        _, _, uploaded_tree = client.committed_trees[0]
        self.assertNotIn("held_back.json", uploaded_tree)

    def test_hub_import_accepts_commit_sha_revision(self):
        """F-002 regression: an hf:// source's revision is the repo's
        40-char commit SHA, pinned by reading only that revision from the
        local cache — it must NOT be compared against the manifest's
        64-char source_snapshot_hash. The cache lookup is stubbed to the
        local export dir, which is exactly what a warm Hub cache gives."""
        self.seed_row(_rid(220), "project:hub", "hub import row")
        out = self.export("hub-exp", "--namespace", "project:hub")
        sha40 = "a" * 40
        original = ds._hub_cache_dir
        ds._hub_cache_dir = lambda source, revision: Path(out)
        try:
            dest = os.path.join(self.tmp, "hub-snap")
            result = ds.import_dataset(
                TARGET, revision=sha40, dest_dir=dest,
                namespace="project:hub")
        finally:
            ds._hub_cache_dir = original
        self.assertEqual(result["memories"], 1)

    def test_import_excludes_future_valid_from(self):
        """NEW-01 regression: temporal filtering mirrors the canonical
        predicate — a row not yet in force (valid_from in the future,
        valid_until empty) must not import."""
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, "
                "ingestion_ts, valid_from) VALUES (?, ?, 'fact', ?, ?, ?)",
                (_rid(221), "project:temporal", "future row", TS,
                 "2099-01-01T00:00:00Z"))
            conn.commit()
        finally:
            conn.close()
        out = self.export("temporal-exp", "--namespace", "project:temporal")
        revision = self.manifest(out)["source_snapshot_hash"]
        dest = os.path.join(self.tmp, "temporal-snap")
        r = run_cli(self.env, "import-dataset", out, "--revision", revision,
                    "--dest", dest)
        self.assertEqual(r.returncode, 0, r.stderr)
        snap = os.path.join(dest, "snapshot.sqlite")
        conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM memory").fetchone()[0], 0,
                "a future-valid_from row must not import")
        finally:
            conn.close()

    def test_import_committed_jsonl_fixtures(self):
        """NEW-03 + C-014 regression: the committed manifest-v1.json
        fixture set carries checksums from an INDEPENDENT inline oracle
        (build_fixtures.py, no storelib import) — the real importer must
        verify and import it, proving the integrity chain against
        non-self-derived values."""
        fixture_dir = os.path.join(REPO_ROOT, "tests", "fixtures", "dataset")
        work = os.path.join(self.tmp, "fixture-dataset")
        shutil.copytree(fixture_dir, work)
        shutil.copy2(os.path.join(work, "manifest-v1.json"),
                     os.path.join(work, "manifest.json"))
        with open(os.path.join(work, "manifest.json"), "rb") as fh:
            revision = json.loads(fh.read().decode("utf-8"))[
                "source_snapshot_hash"]
        dest = os.path.join(self.tmp, "fixture-snap")
        r = run_cli(self.env, "import-dataset", work, "--revision", revision,
                    "--dest", dest)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        snap = os.path.join(dest, "snapshot.sqlite")
        conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        try:
            names = sorted(row[0] for row in conn.execute(
                "SELECT DISTINCT namespace FROM memory"))
        finally:
            conn.close()
        self.assertEqual(names, ["project:a", "project:b"],
                         "the independent-oracle fixture must import intact")

    def test_export_refuses_non_dataset_dir(self):
        """C-011 regression: export-dataset must never clobber an existing
        directory that is not a dataset (no manifest.json)."""
        victim = os.path.join(self.tmp, "victim")
        os.mkdir(victim)
        with open(os.path.join(victim, "precious.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("do not delete")
        r = run_cli(self.env, "export-dataset", victim,
                    "--namespace", "project:whatever")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("refusing to overwrite", r.stderr)
        self.assertTrue(os.path.exists(os.path.join(victim, "precious.txt")))

    def test_import_rejects_wrong_schema_version(self):
        """C-013 regression: a manifest whose schema_version the importer
        does not support is refused, not silently imported."""
        self.seed_row(_rid(222), "project:schema", "schema row")
        out = self.export("schema-exp", "--namespace", "project:schema")
        revision = self.manifest(out)["source_snapshot_hash"]
        tampered = os.path.join(self.tmp, "tampered")
        shutil.copytree(out, tampered)
        path = os.path.join(tampered, "manifest.json")
        with open(path, "rb") as fh:
            manifest = json.loads(fh.read().decode("utf-8"))
        manifest["schema_version"] = 999
        with open(path, "wb") as fh:
            fh.write(ds.canonical_row_bytes(manifest))
        dest = os.path.join(self.tmp, "schema-snap")
        r = run_cli(self.env, "import-dataset", tampered,
                    "--revision", revision, "--dest", dest)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("schema_version", r.stderr)

    def test_publish_never_opens_ambient_store(self):
        self.seed_row(_rid(205), "project:ambient", "ambient probe row")
        out = self.export("ambient-exp", "--namespace", "project:ambient")
        # ZMEM_STORE points at a path that does not exist: sqlite's connect()
        # would CREATE it, so if publish-dataset were dispatched through the
        # connected section (connect + _prepare_store), the ghost file would
        # exist after the run. The command still fails on its own contract
        # (non-Hub target) — fully offline, no Hub client is ever built.
        ghost = os.path.join(self.tmp, "ghost", "store.sqlite")
        r = run_cli(base_env(ghost), "publish-dataset", out, "./local/path")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("target must use hf://datasets/", r.stderr)
        self.assertFalse(os.path.exists(ghost),
                         "publish-dataset must never open ZMEM_STORE")

    def test_hub_client_maps_conflict_and_notfound(self):
        """C-010 regression: the REAL HubDatasetClient maps a commit
        conflict-shaped exception to ParentConflict (named so the publish
        seam retries) and a not-found repo to an empty head — while other
        failures fail closed as PublishError."""
        client = storelib.HubDatasetClient()

        class StubApi:
            def __init__(self, behavior):
                self.behavior = behavior

            def repo_info(self, repo_id, repo_type=None, revision=None):
                if self.behavior == "missing":
                    raise Exception("Repository not found for id")
                if self.behavior == "network":
                    raise Exception("connection reset by peer")
                raise AssertionError(self.behavior)

            def create_commit(self, repo_id, operations=None,
                              repo_type=None, commit_message=None,
                              parent_commit=None):
                if self.behavior == "conflict":
                    raise Exception(
                        f"parent_commit {parent_commit!r} did not match")
                return type("R", (), {"commit_id": "abc"})()

        original = client._api
        client._api = StubApi("conflict")
        try:
            with self.assertRaises(ds.ParentConflict):
                client.commit(TARGET, "old", {"f": b"x"})
        finally:
            client._api = original
        client._api = StubApi("missing")
        try:
            self.assertEqual(client.head(TARGET), "")
        finally:
            client._api = original
        client._api = StubApi("network")
        try:
            with self.assertRaises(storelib.PublishError):
                client.head(TARGET)
        finally:
            client._api = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
