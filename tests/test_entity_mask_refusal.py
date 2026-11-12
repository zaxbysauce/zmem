"""Registry-failure refusal coverage (check C8): when the shared redaction
registry breaks, every entity-card surface must REFUSE — never compose, and
never emit, raw credential-shaped identity values.

Three in-process legs plus one subprocess leg, all pinning POST-FIX
behavior (RED at the current tree: the card build path composes raw bytes
today, so a broken registry changes nothing and the raw card is stored and
rendered):

  a. Card build/CLI refusal (in-process, cmd_entity_card): with the
     shared-registry seam poisoned to raise for the fixture name, the card
     build path must refuse (_DisplayRedactionUnavailable-shaped, or the
     ValueError the CLI maps to exit 2) and must leave NO stored card
     carrying the raw name.
  b. Organize planner parity: the SAME poison for exactly ONE
     credential-shaped entity — the shared planner must classify it
     ``skipped`` (dry-run and apply agree), refresh_entity_cards keeps the
     five-key report contract, ONE bounded stderr warning names the entity,
     and the sweep CONTINUES (the benign entity still gets its card;
     refresh_entity_cards is the entity-card stage of organize(), so its
     normal return with the rest of the plan applied is the smallest
     continuation proof).
  c. Read-path refusal: with the poison active, get/recall never emit the
     raw card bytes — they refuse (the refusal exception is caught and
     shape-checked) or render masked.
  d. Subprocess CLI-boundary twin of (a): a scratch COPY of the scripts
     tree whose redaction.py is a wrapper around the real registry with
     redact_secret_like_text poisoned for exactly the fixture value (the
     _shared_registry_masks runtime-failure seam). The real
     ``store.py entity-card refresh --json`` CLI must not print or store
     the raw name. Test-side monkeypatching only — no production env knob.

The fixture name is REGISTRY-ONLY credential-shaped: a bare 42-char
base64-alphabet run with no head-token separators, so
entity._is_credential_shaped consults the shared registry for it (verified:
head-rule names like ``password:12345`` short-circuit True BEFORE the
registry call and can never exercise the seam). Every leg seeds its OWN
fresh scratch store (real `add` to mint the schema, then direct INSERTs);
env is pinned before the module-level storelib import (storelib.schema
resolves STORE_PATH at import time) and connections are closed before
temp-dir cleanup (the Windows teardown rule).

Run: python -X utf8 tests/test_entity_mask_refusal.py  (no pytest; house
convention — exit nonzero on any failure is the check signal).
"""

import contextlib
import importlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

# Pin a hermetic store BEFORE the #244 guard import AND before the
# module-level storelib import below (storelib.schema resolves STORE_PATH at
# import time). Per-test setUp mints the real scratch stores; this pin only
# keeps the guard's resolved path out of the real ~/.zmem area.
_GUARD_SCRATCH = tempfile.mkdtemp(prefix="guard-maskref-")
os.environ["ZMEM_STORE"] = str(Path(_GUARD_SCRATCH) / "store.sqlite")
os.environ["ZMEM_DATA"] = _GUARD_SCRATCH
os.environ["ZMEM_MODELS_DIR"] = str(Path(_GUARD_SCRATCH) / "models-absent")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ.pop("ZMEM_ALLOW_REAL_STORE", None)
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

REPO = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
MARKER = "[REDACTED_SECRET]"
# Registry-only credential shape: 42 chars of the base64 alphabet, no
# separators — the head-token rule never fires, so _is_credential_shaped
# falls through to _shared_registry_masks (the lazy shared-registry seam
# that raises _DisplayRedactionUnavailable on import/runtime failure).
REGISTRY_ONLY_NAME = "aB3dE5fG7hJ9kL1mN3oP5qR7sT9uV1wX3yZ5aB7cD9"
BENIGN_NAME = "localhost:8080"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
# NOTE: importlib.import_module, NOT `from storelib import ...` —
# storelib/__init__ rebinds the `organize` attribute to the organize()
# FUNCTION, so attribute access on the package does not yield the module.
entity_mod = importlib.import_module("storelib.entity")
organize_mod = importlib.import_module("storelib.organize")
recall_mod = importlib.import_module("storelib.recall")


@contextlib.contextmanager
def _registry_poison(name):
    """Test-side monkeypatch of the shared-registry seam: masking `name`
    raises the entity module's _DisplayRedactionUnavailable (the documented
    _shared_registry_masks failure contract); every other value delegates to
    the real registry, so exactly ONE credential-shaped entity fails while
    benign names still classify normally. No production env knob involved —
    the patch lives only for the duration of the with-block."""
    real = entity_mod._shared_registry_masks

    def poison(value):
        if value == name:
            raise entity_mod._DisplayRedactionUnavailable(
                "shared registry broken by test (C8)")
        return real(value)

    entity_mod._shared_registry_masks = poison
    try:
        yield
    finally:
        entity_mod._shared_registry_masks = real


def _assert_refusal_shape(test, label, refusal):
    """The refusal must be _DisplayRedactionUnavailable-shaped, or the
    ValueError the CLI boundary maps to its documented exit 2."""
    test.assertTrue(
        type(refusal).__name__ == "_DisplayRedactionUnavailable"
        or isinstance(refusal, ValueError),
        f"{label}: refusal has an unexpected shape: "
        f"{type(refusal).__name__}: {refusal}")


class _RefusalStore(unittest.TestCase):
    """Fresh scratch store per test: one real `add` (mints the schema), then
    direct INSERTs of a registry-only credential entity and a benign entity
    (five mentions each). The tag-minted seed entity is deleted so the
    organize plan is EXACTLY the two fixture entities; the credential eid
    sorts FIRST so a surviving benign eid proves the sweep continued."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="zmemc8_")
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
        self.ns = "user:refusal"
        self.suffix = uuid.uuid4().hex[:8]
        seed = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "add", "--type",
             "fact", "--content",
             "seed mentions localhost:8080 so the extractor mints the schema",
             "--tags", "entity:host:localhost:8080",
             "--namespace", self.ns],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO), timeout=120)
        self.assertEqual(0, seed.returncode,
                         f"seed add failed: {seed.stderr}")
        self.eid_cred = f"e-a-reg-{self.suffix}"     # sorts BEFORE benign
        self.eid_benign = f"e-b-benign-{self.suffix}"
        conn = sqlite3.connect(self.store_path)
        try:
            now = "2026-11-11T00:00:00Z"
            for eid, kind, name in ((self.eid_cred, "other",
                                     REGISTRY_ONLY_NAME),
                                    (self.eid_benign, "host", BENIGN_NAME)):
                conn.execute(
                    "INSERT INTO entity(id, kind, canonical_name, "
                    "created_at, updated_at) VALUES (?,?,?,?,?)",
                    (eid, kind, name, now, now))
                for i in range(5):
                    mid = f"m-{eid}-{i}"
                    conn.execute(
                        "INSERT INTO memory(id, namespace, type, content, "
                        "tags, source_ref, signal, confidence, valid_from, "
                        "ingestion_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (mid, self.ns, "fact",
                         f"rotation reminder {i} for the account", "",
                         "probe", "user", 0.6, now,
                         f"2026-11-11T00:00:0{i}Z"))
                    conn.execute(
                        "INSERT INTO memory_entity(memory_id, entity_id) "
                        "VALUES (?,?)", (mid, eid))
            # Drop every NON-fixture entity (the seed tag's auto-minted row)
            # so the organize plan is exactly the two fixture entities.
            for sql in (
                    "DELETE FROM entity_alias WHERE entity_id NOT IN (?,?)",
                    "DELETE FROM memory_entity WHERE entity_id NOT IN (?,?)",
                    "DELETE FROM entity WHERE id NOT IN (?,?)"):
                conn.execute(sql, (self.eid_cred, self.eid_benign))
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self._td.cleanup()

    def open_conn(self):
        conn = sqlite3.connect(self.store_path)
        conn.row_factory = sqlite3.Row
        return conn

    def stored_card_contents(self):
        conn = self.open_conn()
        try:
            return [r["content"] for r in conn.execute(
                "SELECT content FROM memory WHERE type='entity_card'"
            ).fetchall()]
        finally:
            conn.close()


# --- leg (a): card build/CLI refusal, in-process ---------------------------

class CardBuildRefusalTest(_RefusalStore):

    def test_card_build_refuses_when_registry_breaks(self):
        refusal = None
        result = None
        out = io.StringIO()
        with _registry_poison(REGISTRY_ONLY_NAME):
            conn = self.open_conn()
            try:
                with contextlib.redirect_stdout(out):
                    result = entity_mod.cmd_entity_card(
                        conn, action="refresh", entity=self.eid_cred)
            except Exception as exc:  # noqa: BLE001 - the refusal itself
                refusal = exc
            finally:
                conn.close()
        # The no-leak contract FIRST (the discriminating direction): no
        # composed/stored card content may carry the raw name.
        leaked = [c for c in self.stored_card_contents()
                  if REGISTRY_ONLY_NAME in c]
        self.assertEqual(
            [], leaked,
            "card-build refusal leg: with the shared registry broken the "
            "card build path COMPOSED AND STORED the raw credential name "
            f"({REGISTRY_ONLY_NAME!r}); it must refuse instead")
        # And the refusal must actually have happened (base: the build
        # succeeds and returns a result dict).
        self.assertIsNotNone(
            refusal,
            "card-build refusal leg: no refusal raised with the registry "
            f"broken (result={result!r}); the build must refuse rather than "
            "compose raw bytes")
        if result is not None:
            self.assertNotIn(
                REGISTRY_ONLY_NAME, json.dumps(result, default=str),
                "card-build refusal leg: the command RESULT carries the raw "
                "credential name")
        self.assertNotIn(
            REGISTRY_ONLY_NAME, out.getvalue(),
            "card-build refusal leg: the command's stdout progress carries "
            "the raw credential name")
        if refusal is not None:
            _assert_refusal_shape(self, "card-build refusal", refusal)


# --- leg (d): the CLI boundary, subprocess (scratch copy + poison registry) -

_POISON_REDACTION_SRC = '''"""Test shim (C8 subprocess leg) — written by
tests/test_entity_mask_refusal.py into a scratch COPY of the scripts tree;
never touches production files. Re-exports the REAL shared registry from
_real_redaction.py (the original redaction.py, moved aside) with
redact_secret_like_text poisoned to raise for exactly one fixture value —
the runtime-failure seam of storelib.entity._shared_registry_masks.
"""
import importlib.util as _ilu
from pathlib import Path as _Path

_spec = _ilu.spec_from_file_location(
    "_real_redaction",
    str(_Path(__file__).with_name("_real_redaction.py")))
_real = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_real)
for _name in dir(_real):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_real, _name)

POISON_VALUE = "aB3dE5fG7hJ9kL1mN3oP5qR7sT9uV1wX3yZ5aB7cD9"


def redact_secret_like_text(text, *args, **kwargs):
    if text == POISON_VALUE:
        raise RuntimeError("shared registry broken by test (C8 subprocess)")
    return _real.redact_secret_like_text(text, *args, **kwargs)
'''


class CardCliRefusalSubprocessTest(_RefusalStore):

    def test_cli_refuses_without_printing_or_storing_raw(self):
        # Scratch COPY of the scripts tree with the poisoned redaction.py:
        # the subprocess resolves `import redaction` to the shim because the
        # script's own directory leads sys.path (a PYTHONPATH shadow cannot
        # win over store.py's own directory, so the copy is required).
        scripts_copy = Path(self._td.name) / "scripts-poisoned"
        shutil.copytree(SCRIPTS_DIR, scripts_copy,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.move(str(scripts_copy / "redaction.py"),
                    str(scripts_copy / "_real_redaction.py"))
        (scripts_copy / "redaction.py").write_text(
            _POISON_REDACTION_SRC, encoding="utf-8")
        p = subprocess.run(
            [sys.executable, "-X", "utf8", str(scripts_copy / "store.py"),
             "entity-card", "refresh", "--json", self.eid_cred],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO), timeout=180)
        # The documented CLI-boundary refusal: exit 2 (the ValueError-shaped
        # refusal the CLI maps through _read_cli_or_report_value_error), NOT
        # a silent success.
        self.assertEqual(
            2, p.returncode,
            f"card-CLI refusal leg (subprocess): expected the documented "
            f"exit-2 refusal with the registry broken; got exit "
            f"{p.returncode} (stderr head: {p.stderr[:300]!r})")
        # A bounded registry-refusal diagnostic on stderr (bounded substring
        # check — the exact text is not pinned).
        low = p.stderr.lower()
        self.assertTrue(
            any(tok in low for tok in
                ("registry", "redaction", "unavailable")),
            f"card-CLI refusal leg (subprocess): stderr carries no bounded "
            f"registry-refusal diagnostic (stderr head: {p.stderr[:300]!r})")
        self.assertLess(
            len(p.stderr), 4000,
            "card-CLI refusal leg (subprocess): the stderr diagnostic is "
            f"unbounded ({len(p.stderr)} bytes)")
        # NO raw identity on stdout OR stderr.
        for stream_label, stream in (("stdout", p.stdout),
                                     ("stderr", p.stderr)):
            self.assertNotIn(
                REGISTRY_ONLY_NAME, stream,
                f"card-CLI refusal leg (subprocess): the raw credential name "
                f"reached {stream_label} with the registry broken; it must "
                f"refuse without echoing it")
        # Nothing stored AT ALL: the refusal must precede any write — no
        # entity_card row may exist in the scratch store afterward.
        conn = self.open_conn()
        try:
            stored = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE type='entity_card'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(
            0, stored,
            "card-CLI refusal leg (subprocess): the CLI stored an "
            f"entity_card row with the registry broken ({stored} row(s)); "
            "the refusal must precede the write")


# --- leg (b): organize planner parity — skip, continue, one warning ---------

class OrganizePlannerSkipTest(_RefusalStore):

    def test_registry_failure_skips_entity_and_sweep_continues(self):
        conn = self.open_conn()
        err_direct = io.StringIO()   # (1) direct _plan_entity_card_pass call
        err_dry = io.StringIO()      # (2) entity_card_counts_readonly
        err_apply = io.StringIO()    # (3) refresh_entity_cards
        err_organize = io.StringIO()  # (4) the full organize() card stage
        consolidate_calls = []
        try:
            out = io.StringIO()
            with _registry_poison(REGISTRY_ONLY_NAME), \
                    contextlib.redirect_stdout(out):
                # (1) DIRECT planner evaluation: the registry-failed entity
                # must classify as skipped.
                with contextlib.redirect_stderr(err_direct):
                    plan = organize_mod._plan_entity_card_pass(
                        conn, min_mentions=5, top_n=8)
                actions = {eid: action
                           for eid, action, _c, _t, _l in plan}
                # (2) DRY-RUN twin: same skip, read-only counts.
                with contextlib.redirect_stderr(err_dry):
                    dry_counts = organize_mod.entity_card_counts_readonly(
                        conn, min_mentions=5, top_n=8)
                # (3) APPLY sweep: five-key report, benign entity carded.
                with contextlib.redirect_stderr(err_apply):
                    counts = organize_mod.refresh_entity_cards(
                        conn, now="2026-11-11T01:00:00Z")
                # (4) ORGANIZE continuation: the REAL pipeline entry (dry
                # run — read-only, force to bypass the cadence gate), with
                # a spy on a LATER stage. The entity-card sweep is stage
                # 4b; consolidate() is stage 6, reached only if every
                # stage in between survived the skipped card. The spy
                # delegates to the real consolidate so the run proceeds
                # normally and merely records the call.
                real_consolidate = organize_mod.consolidate

                def _spy_consolidate(*a, **k):
                    consolidate_calls.append((a, k))
                    return real_consolidate(*a, **k)

                organize_mod.consolidate = _spy_consolidate
                try:
                    with contextlib.redirect_stderr(err_organize):
                        org_report = organize_mod.organize(
                            conn, force=True, dry_run=True)
                finally:
                    organize_mod.consolidate = real_consolidate
        finally:
            conn.close()
        self.assertEqual(
            "skipped", actions.get(self.eid_cred),
            f"planner did not classify the registry-failed entity as "
            f"skipped (actions={actions})")
        self.assertEqual(
            1, dry_counts.get("skipped"),
            f"dry-run counts did not record the skip (dry={dry_counts})")
        self.assertEqual(
            set(organize_mod.ENTITY_CARD_COUNT_KEYS), set(counts),
            f"the five-key report contract broke (counts={counts})")
        self.assertEqual(
            1, counts.get("skipped"),
            f"apply counts did not record the skip (counts={counts})")
        self.assertGreaterEqual(
            counts.get("created", 0) + counts.get("refreshed", 0)
            + counts.get("unchanged", 0), 1,
            f"the benign entity did not survive the sweep (counts={counts})")
        conn = self.open_conn()
        try:
            benign_live = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE type='entity_card' "
                "AND source_ref=?",
                (f"card:{self.eid_benign}",)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(
            1, benign_live,
            "the succeeding (benign) entity's card was not refreshed — the "
            "sweep did not continue past the registry failure")
        # ONE bounded warning naming the failing entity in EACH planner
        # evaluation's OWN capture — one per evaluation, no process-global
        # dedup across the four evaluations above.
        for label, err in (("direct", err_direct), ("dry-run", err_dry),
                           ("apply", err_apply),
                           ("organize", err_organize)):
            named = err.getvalue().count(self.eid_cred)
            self.assertEqual(
                1, named,
                f"planner evaluation ({label}): expected exactly ONE "
                f"bounded stderr warning naming the failed entity, got "
                f"{named} (stderr={err.getvalue()!r})")
        # Stage continuation through the REAL organize() pipeline: the card
        # stage (4b, dry-run counts) records the skip, and a LATER stage
        # (6: consolidate) still executes despite it.
        self.assertEqual(
            1, (org_report.get("entity_cards") or {}).get("skipped"),
            f"organize() dry-run did not record the skipped card "
            f"(entity_cards={org_report.get('entity_cards')})")
        self.assertTrue(
            consolidate_calls,
            "organize() continuation: the consolidation stage (a LATER "
            "stage after the entity-card sweep) never executed — the "
            "skipped card aborted the pipeline")


# --- leg (c): read-path refusal — never emit raw ----------------------------

class ReadPathRefusalTest(_RefusalStore):

    def _plant_raw_card(self):
        """A RAW pre-fix card for the registry-only entity, linked as a
        selected card (mirrors file 1's planted-card fixture)."""
        card_id = f"m-rawcard-{self.suffix}"
        now = "2026-11-11T00:00:00Z"
        card_content = (
            f"Entity: {REGISTRY_ONLY_NAME}\n"
            "Kind: other\n"
            "Mentions:\n"
            f"- score=0.900000 last_seen={now} id=m-legacy-1 source_ref="
            "probe text=meridian rotation checklist for the quarter\n"
            "Last seen: 2026-11-11T00:00:04Z\n"
            "Sources:\n"
            "- m-legacy-1\n")
        conn = self.open_conn()
        try:
            conn.execute(
                "INSERT INTO memory(id, namespace, type, content, "
                "content_norm, tags, source_ref, signal, confidence, "
                "valid_from, ingestion_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, self.ns, "entity_card", card_content,
                 re.sub(r"\s+", " ", card_content.strip().lower()), "",
                 f"card:{self.eid_cred}", "user", 0.9, now, now))
            conn.execute(
                "INSERT INTO memory_entity(memory_id, entity_id, role) "
                "VALUES (?,?,?)", (card_id, self.eid_cred, "mentions"))
            conn.commit()
        finally:
            conn.close()
        return card_id

    def test_read_paths_refuse_rather_than_emit_raw(self):
        card_id = self._plant_raw_card()
        conn = self.open_conn()
        try:
            with _registry_poison(REGISTRY_ONLY_NAME):
                # get: prints the full row JSON (content + content_norm +
                # entities) — must refuse (or mask) with the registry broken.
                out_get = io.StringIO()
                get_refusal = None
                try:
                    with contextlib.redirect_stdout(out_get):
                        recall_mod.get_memory(conn, card_id)
                except Exception as exc:  # noqa: BLE001 - the refusal itself
                    get_refusal = exc
                # recall: the presented rows carry the card content (and the
                # card's entity link rides the entities metadata).
                out_recall = io.StringIO()
                recall_refusal = None
                try:
                    with contextlib.redirect_stdout(out_recall):
                        recall_mod.recall_memory(
                            conn, query="meridian", namespace=self.ns,
                            as_json=True, no_bump=True, hybrid=False,
                            limit=10)
                except Exception as exc:  # noqa: BLE001 - the refusal itself
                    recall_refusal = exc
        finally:
            conn.close()
        for label, out, refusal in (
                ("get", out_get, get_refusal),
                ("recall", out_recall, recall_refusal)):
            # Either a raised, refusal-shaped exception OR a PRESENT MASKED
            # result. A silent-empty success is NOT acceptable: if the call
            # returned without refusing, the output must be non-empty AND
            # carry the masked marker (raw-absent is asserted either way).
            if refusal is None:
                self.assertTrue(
                    out.getvalue().strip(),
                    f"read-refusal ({label}): neither refused nor rendered "
                    "— silent-empty success is not an acceptable outcome")
                self.assertIn(
                    MARKER, out.getvalue(),
                    f"read-refusal ({label}): returned without refusing but "
                    f"the output carries no masked marker ({MARKER}) — "
                    "neither a refusal nor a present masked result")
            self.assertNotIn(
                REGISTRY_ONLY_NAME, out.getvalue(),
                f"read-refusal ({label}): emitted the raw credential name "
                f"with the registry broken; the {label} path must refuse "
                "(or mask) — never return raw content bytes")
            if refusal is not None:
                _assert_refusal_shape(self, f"read-refusal ({label})",
                                      refusal)


if __name__ == "__main__":
    unittest.main()
