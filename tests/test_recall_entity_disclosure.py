"""Recall entity-metadata disclosure: when a BENIGN memory is linked to a
credential-shaped entity, the raw credential-shaped entity name must never
reach the recall render's ``entities`` metadata — neither the JSON rows'
``entities: [{id, kind, name}]`` nor the fenced ``    entities: ...`` line
(trace AC2).

RED at base (entities_for_memories returns e.canonical_name verbatim, so
``recall --json`` emits ``{"name": "password:12345"}`` and the fence renders
``entities: ..., password:12345``), GREEN after the recall-surface masking
fix: the credential-shaped name is masked (the literal ``[REDACTED_SECRET]``
marker) or withheld from the entities metadata, while benign ``label:port``
names (localhost:8080) still render — masking did not over-reach.

Every leg drives the real skills/memory/scripts/store.py as a subprocess.
Each test mints its OWN hermetic scratch store (never the default store,
never shared state) via the house seeding pattern: one real ``add`` with an
``entity:host:...`` tag to mint the schema (that row is the benign memory
recall returns), then direct sqlite3 INSERTs of the legacy pre-v16 shapes
(entity / entity_alias / memory_entity links). Ambient ZMEM_* is stripped
per subprocess; ZMEM_ALLOW_REAL_STORE is never set.

Run: python -X utf8 tests/test_recall_entity_disclosure.py  (no pytest;
house convention — exit nonzero on any failure is the check signal).
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

# Pin a hermetic store BEFORE the #244 guard import: the guard fires on any
# box with a real default store when the import precedes the pin (CI is
# silent). Per-test setUp mints the real scratch store; this pin only keeps
# the guard's resolved path out of the real ~/.zmem area.
_GUARD_SCRATCH = tempfile.mkdtemp(prefix="guard-recdisc-")
os.environ["ZMEM_STORE"] = str(Path(_GUARD_SCRATCH) / "store.sqlite")
os.environ["ZMEM_DATA"] = _GUARD_SCRATCH
os.environ.pop("ZMEM_ALLOW_REAL_STORE", None)
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

REPO = Path(__file__).resolve().parents[1]
STORE_PY = REPO / "skills" / "memory" / "scripts" / "store.py"
MARKER = "[REDACTED_SECRET]"
RAW_CANONICAL = "password:12345"   # credential-shaped canonical name (other)
RAW_ALIAS = "token:98765"          # credential-shaped alias
BENIGN_NAME = "localhost:8080"     # benign label:port control (host)
BENIGN_ALIAS = "server:8080"       # benign alias control
# Distinctive token in the benign memory's content; the recall query and the
# non-vacuous "the row actually rendered" guard both key off it. It must NOT
# contain any credential-shaped value (the raw leak assertions run over the
# whole stdout, and AC2 masks the entities metadata, not the memory content).
BENIGN_CONTENT = (
    "gateway deploy notes: the edge proxy listens on localhost:8080 "
    "behind nginx")
QUERY = "gateway"


class _SeededRecallStore(unittest.TestCase):
    """Shared per-test fixture: a fresh scratch store per test (real `add`
    seed + direct INSERTs of the credential/benign entities) and the store/
    recall drivers + leak assertions built on it."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="zmemac2_")
        scratch = Path(self._td.name)
        self.env = dict(os.environ)
        for var in ("ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_REAL_STORE",
                    "ZMEM_REMOTE_URL", "ZMEM_NAMESPACE", "ZMEM_SESSION",
                    "ZMEM_SESSION_ID", "ZMEM_CONSUME_AGENT_ID",
                    "ZMEM_DELEGATION_AGENT_ID"):
            self.env.pop(var, None)
        self.env["ZMEM_STORE"] = str(scratch / "store.sqlite")
        self.env["ZMEM_DATA"] = str(scratch)
        self.env["ZMEM_MODELS_DIR"] = str(scratch / "models-absent")
        self.env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.store_path = self.env["ZMEM_STORE"]
        self.ns = "user:ac2probe"
        self.suffix = uuid.uuid4().hex[:8]
        # Real `add` first (mints the schema); --signal user keeps conf 0.6
        # above the 0.25 recall floor so the row is actually returned.
        seed = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "add", "--type",
             "fact", "--content", BENIGN_CONTENT,
             "--tags", "entity:host:localhost:8080",
             "--namespace", self.ns, "--signal", "user"],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO), timeout=120)
        self.assertEqual(0, seed.returncode, f"seed add failed: {seed.stderr}")

    def tearDown(self):
        self._td.cleanup()

    # -- seeding helpers (direct INSERTs; one connection per call) ----------

    @property
    def seed_memory_id(self):
        conn = sqlite3.connect(self.store_path)
        try:
            return conn.execute("SELECT id FROM memory").fetchone()[0]
        finally:
            conn.close()

    def insert_entity(self, eid, kind, name, aliases=()):
        conn = sqlite3.connect(self.store_path)
        try:
            now = "2026-11-11T00:00:00Z"
            conn.execute(
                "INSERT INTO entity(id, kind, canonical_name, created_at, "
                "updated_at) VALUES (?,?,?,?,?)", (eid, kind, name, now, now))
            for alias in aliases:
                conn.execute(
                    "INSERT INTO entity_alias(entity_id, alias_norm) "
                    "VALUES (?,?)", (eid, alias))
            conn.commit()
        finally:
            conn.close()

    def link_entity_to_seed_memory(self, eid):
        """Link the benign seed memory row to an entity (the AC2 scenario:
        the memory content is benign; only the entity link carries the
        credential-shaped identity)."""
        conn = sqlite3.connect(self.store_path)
        try:
            conn.execute(
                "INSERT INTO memory_entity(memory_id, entity_id) "
                "VALUES (?,?)", (self.seed_memory_id, eid))
            conn.commit()
        finally:
            conn.close()

    def seed_credential_linked_benign_memory(self):
        """The full AC2 fixture: benign seed memory linked to BOTH the
        credential-shaped entity and a benign label:port entity, so the same
        render must mask one and keep the other."""
        cred = f"e-cred-{self.suffix}"
        benign = f"e-benign-{self.suffix}"
        self.insert_entity(cred, "other", RAW_CANONICAL,
                           aliases=(RAW_ALIAS,))
        self.insert_entity(benign, "host", BENIGN_NAME,
                           aliases=(BENIGN_ALIAS,))
        self.link_entity_to_seed_memory(cred)
        self.link_entity_to_seed_memory(benign)
        return cred, benign

    def run_recall(self, *extra):
        p = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY),
             "recall", "--query", QUERY, "--namespace", self.ns,
             "--limit", "10", "--no-hybrid"] + list(extra),
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO), timeout=180)
        self.assertEqual(0, p.returncode,
                         f"recall exited {p.returncode}: {p.stderr}")
        return p

    def run_store(self, *args, timeout=180):
        """Drive store.py as a subprocess in this test's scratch env."""
        p = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY)] + list(args),
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO), timeout=timeout)
        self.assertEqual(0, p.returncode,
                         f"store.py {' '.join(args)} exited {p.returncode}: "
                         f"{p.stderr}")
        return p

    def assert_no_raw_leak(self, label, stdout, entity_names):
        """Both directions per the house run_surface rule: no raw
        credential-shaped value on stdout / in the parsed entity names."""
        for raw in (RAW_CANONICAL, RAW_ALIAS):
            self.assertNotIn(
                raw, stdout,
                f"{label}: RAW-LEAK credential-shaped value {raw!r} reached "
                f"stdout unmasked; expected {MARKER} (or withholding)")
            leaked = [n for n in entity_names
                      if n and raw in n]
            self.assertEqual(
                [], leaked,
                f"{label}: RAW-LEAK credential-shaped value {raw!r} present "
                f"in rendered entities metadata names {entity_names}")

    # -- AC2 legs -----------------------------------------------------------

class RecallEntityDisclosureTest(_SeededRecallStore):
    """One test method per recall leg; each seeds a fresh scratch store."""

    def test_recall_json_masks_credential_entity_name(self):
        self.seed_credential_linked_benign_memory()
        p = self.run_recall("--json")
        parsed = json.loads(p.stdout)
        rows = parsed["results"] if isinstance(parsed, dict) else parsed
        # Non-vacuous: the benign memory row rendered at all.
        self.assertTrue(rows, "recall returned no rows for the benign memory")
        names = [e.get("name") for row in rows
                 for e in (row.get("entities") or [])]
        # Control: the benign entity name still renders (masking did not
        # over-reach — entities metadata is present and unmasked for it).
        self.assertIn(BENIGN_NAME, names,
                      "benign entity name vanished from recall entities "
                      f"metadata (names={names}); masking over-reached")
        # Leak: raw credential value absent from stdout and entity names.
        self.assert_no_raw_leak("recall-json", p.stdout, names)
        # Expected shape: masked marker rendered, or the credential entity
        # withheld from the entities metadata entirely.
        self.assertTrue(
            MARKER in p.stdout or RAW_CANONICAL not in names,
            f"recall-json: neither masked ({MARKER} absent) nor withheld "
            f"from entities names {names}")

    def test_recall_fenced_masks_credential_entity_name(self):
        self.seed_credential_linked_benign_memory()
        p = self.run_recall()  # no --json: the fenced text render
        # Non-vacuous: the benign memory row rendered at all.
        self.assertIn("edge proxy", p.stdout,
                      "fenced recall did not render the benign memory row")
        ent_lines = [ln for ln in p.stdout.splitlines()
                     if ln.strip().startswith("entities:")]
        self.assertTrue(
            ent_lines,
            "fenced recall render carries no 'entities:' line under the row")
        line = ent_lines[0]
        # Control: the benign entity name still renders on the entities line.
        self.assertIn(BENIGN_NAME, line,
                      f"benign entity name missing from the fenced entities "
                      f"line ({line!r}); masking over-reached")
        # Leak: raw credential value absent from the whole render.
        self.assert_no_raw_leak("recall-fenced", p.stdout, [line.strip()])
        # Expected shape: masked marker on the entities line, or the
        # credential entity withheld from it.
        self.assertTrue(
            MARKER in line or RAW_CANONICAL not in line,
            f"recall-fenced: entities line neither masked nor withheld "
            f"({line!r})")

    def test_recall_control_benign_entity_name_still_renders(self):
        """Control fixture WITHOUT any credential-shaped entity: the benign
        name must render in both the JSON entities metadata and the fenced
        entities line, with no masked marker anywhere."""
        benign = f"e-benign-{self.suffix}"
        self.insert_entity(benign, "host", BENIGN_NAME,
                           aliases=(BENIGN_ALIAS,))
        self.link_entity_to_seed_memory(benign)

        p = self.run_recall("--json")
        parsed = json.loads(p.stdout)
        rows = parsed["results"] if isinstance(parsed, dict) else parsed
        self.assertTrue(rows, "recall returned no rows for the benign memory")
        names = [e.get("name") for row in rows
                 for e in (row.get("entities") or [])]
        self.assertIn(BENIGN_NAME, names,
                      f"benign entity name absent from recall entities "
                      f"metadata (names={names})")

        p2 = self.run_recall()
        ent_lines = [ln for ln in p2.stdout.splitlines()
                     if ln.strip().startswith("entities:")]
        self.assertTrue(ent_lines,
                        "fenced recall render carries no 'entities:' line")
        self.assertIn(BENIGN_NAME, ent_lines[0],
                      "benign entity name absent from the fenced entities "
                      "line")
        self.assertNotIn(
            MARKER, p2.stdout,
            "masked marker present with no credential-shaped entity in the "
            "store; masking over-reached")

    def test_get_json_masks_credential_entity_name(self):
        """get leg: the seed memory's entity links ride the get surface as
        ``entities: [{id, kind, name}]`` (get_memory ->
        entities_for_memory). The CLI has no --json flag — get ALWAYS prints
        JSON (house tests/test_entity.py::test_get_json_carries_entities
        drives it bare) — so the amendment's `get --id <id> --json` spelling
        is satisfied by the default output."""
        self.seed_credential_linked_benign_memory()
        p = self.run_store("get", "--id", self.seed_memory_id)
        d = json.loads(p.stdout)
        names = [e.get("name") for e in (d.get("entities") or [])]
        # Non-vacuous + control: the benign linked entity's name renders.
        self.assertIn(BENIGN_NAME, names,
                      f"benign entity name absent from get entities "
                      f"metadata (names={names}); masking over-reached")
        # Leak: raw credential value absent from stdout and entity names.
        self.assert_no_raw_leak("get-json", p.stdout, names)
        # Expected shape: masked marker rendered, or the credential entity
        # withheld from the entities metadata entirely.
        self.assertTrue(
            MARKER in p.stdout or RAW_CANONICAL not in names,
            f"get-json: neither masked ({MARKER} absent) nor withheld from "
            f"entities names {names}")

    def test_explain_injection_json_masks_credential_entity_name(self):
        """explain leg. Verification against the current code (amendment 2b
        condition): the PLAIN ``recall --explain --json`` envelope carries
        row CONTENT only — entities_for_memories runs solely under the
        for_injection replay (storelib/recall.py, explain_recall's
        ``if for_injection:`` block ~line 4677) — so with a benign memory
        body the plain explain output never carries entity names. The
        explain lane's entity-name surface is therefore the
        ``--for-injection`` leg, driven here."""
        self.seed_credential_linked_benign_memory()
        p = self.run_store("recall", "--query", QUERY,
                           "--namespace", self.ns,
                           "--explain", "--json", "--for-injection")
        parsed = json.loads(p.stdout)
        rows = parsed["results"] if isinstance(parsed, dict) else parsed
        self.assertTrue(rows, "explain returned no rows for the benign memory")
        names = [e.get("name") for row in rows
                 for e in (row.get("entities") or [])]
        # Non-vacuous + control: the benign linked entity's name renders.
        self.assertIn(BENIGN_NAME, names,
                      f"benign entity name absent from explain entities "
                      f"metadata (names={names}); masking over-reached")
        # Leak: raw credential value absent from stdout and entity names.
        self.assert_no_raw_leak("explain-injection-json", p.stdout, names)
        # Expected shape: masked marker rendered, or the credential entity
        # withheld from the entities metadata entirely.
        self.assertTrue(
            MARKER in p.stdout or RAW_CANONICAL not in names,
            f"explain-injection-json: neither masked ({MARKER} absent) nor "
            f"withheld from entities names {names}")


class DeliveryLedgerFuelMaskTest(_SeededRecallStore):
    """In-process leg (amendment 2c, round-2 rework): the delivery ledger's
    matcher fuel, driven through the REAL pipeline.

    The row handed to storelib.delivery_ledger.entry_text in production is
    built from the store — its entities list is exactly what
    storelib.entity.entities_for_memories(conn, [mid]) returns (see the
    callers at delivery_ledger.py:1026/1298). This leg seeds the same
    fixture as the recall legs (inherited setUp), then IN-PROCESS (env
    pinned BEFORE the storelib import — storelib.schema resolves STORE_PATH
    at import time — storelib* purged from sys.modules first, scripts dir on
    sys.path) opens the store, pulls the row's real entities list and
    content/tags, and drives entry_text over them. entry_text LOWERCASES its
    whole output (and truncates at TEXT_MAX=400), so the masked-marker
    assertion compares the lowercased marker. RED at base:
    entities_for_memories returns raw canonical names today.
    """

    MARKER_LOW = MARKER.lower()

    def test_entry_text_masks_credential_entity_name(self):
        self.seed_credential_linked_benign_memory()
        mid = self.seed_memory_id
        saved_env = dict(os.environ)
        saved_path = list(sys.path)
        scripts_dir = REPO / "skills" / "memory" / "scripts"
        stale = [m for m in list(sys.modules)
                 if m == "storelib" or m.startswith("storelib.")
                 or m == "schema_meta"]
        saved_modules = {m: sys.modules[m] for m in stale}
        try:
            for m in stale:
                del sys.modules[m]
            for var in ("ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA",
                        "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
                        "ZMEM_ALLOW_REAL_STORE", "ZMEM_REMOTE_URL",
                        "ZMEM_NAMESPACE", "ZMEM_SESSION", "ZMEM_SESSION_ID"):
                os.environ.pop(var, None)
            os.environ["ZMEM_STORE"] = self.store_path
            os.environ["ZMEM_DATA"] = str(Path(self._td.name))
            os.environ["ZMEM_MODELS_DIR"] = str(
                Path(self._td.name) / "models-absent")
            os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
            os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            import importlib
            entity_mod = importlib.import_module("storelib.entity")
            delivery_ledger = importlib.import_module("storelib.delivery_ledger")

            # The REAL pipeline row: the memory's own content/tags from the
            # store plus the entities list entities_for_memories builds for
            # it — [{id, kind, name}] with canonical names only. Content is
            # short, so the appended names land well inside entry_text's
            # TEXT_MAX=400 truncation.
            conn = sqlite3.connect(self.store_path)
            try:
                conn.row_factory = sqlite3.Row
                row_db = conn.execute(
                    "SELECT content, tags FROM memory WHERE id=?",
                    (mid,)).fetchone()
                cards = entity_mod.entities_for_memories(conn, [mid])
            finally:
                conn.close()
            self.assertIn(mid, cards,
                          "entities_for_memories returned nothing for the "
                          "seed memory (vacuous-pass guard)")
            fuel = delivery_ledger.entry_text({
                "content": row_db["content"],
                "tags": row_db["tags"],
                "entities": cards[mid],
            })
            # Non-vacuous: the fuel rendered the row content.
            self.assertIn("gateway", fuel,
                          f"entry_text fuel did not render the row content: "
                          f"{fuel!r}")
            # Control: the benign linked entity's name still appears.
            self.assertIn(BENIGN_NAME, fuel,
                          f"benign entity name absent from the ledger fuel "
                          f"({fuel!r}); masking over-reached")
            # Leak: the raw credential-shaped name must not ride the fuel.
            self.assertNotIn(
                RAW_CANONICAL, fuel,
                f"ledger-fuel: RAW-LEAK credential-shaped value "
                f"{RAW_CANONICAL!r} from entities_for_memories rendered into "
                f"the delivery-ledger matcher fuel; expected "
                f"{self.MARKER_LOW} (or withholding)")
            # Expected shape: masked marker in the fuel, or the credential
            # entity's name withheld from it (entry_text lowercases both).
            self.assertTrue(
                self.MARKER_LOW in fuel or RAW_CANONICAL not in fuel,
                f"ledger-fuel: neither masked ({self.MARKER_LOW} absent) "
                f"nor withheld ({fuel!r})")
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
            sys.path[:] = saved_path
            for m in [m for m in list(sys.modules)
                      if m == "storelib" or m.startswith("storelib.")
                      or m == "schema_meta"]:
                del sys.modules[m]
            sys.modules.update(saved_modules)


if __name__ == "__main__":
    unittest.main()
