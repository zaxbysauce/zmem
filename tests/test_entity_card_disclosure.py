"""Entity-card storage disclosure: a credential-shaped canonical name or
alias must never be STORED raw inside a refreshed entity-card's memory row
(trace AC1).

RED at base (build_entity_card_text renders ``Entity: password:12345`` and
``Aliases: token:98765`` verbatim into the stored card row, memory table
type='entity_card' / source_ref='card:<entity_id>'), GREEN after the
card-construction masking fix: the stored card content carries masked
display values (the literal ``[REDACTED_SECRET]`` marker) wherever a
credential-shaped identity value would appear, while benign ``label:port``
names (localhost:8080, server:8080) stay unmasked — masking is scoped to the
exact-token head vocabulary (password/passwd/pwd/secret/token/api-key/
api_key/apikey/private-key/private_key/credential/auth), never a blanket
rewrite of every entity name.

Every leg drives the real skills/memory/scripts/store.py as a subprocess.
Each test mints its OWN hermetic scratch store (never the default store,
never shared state) via the house seeding pattern: one real ``add`` with an
``entity:host:...`` tag to mint the schema, then direct sqlite3 INSERTs of
the legacy pre-v16 shapes (entity / entity_alias / memory / memory_entity).
Ambient ZMEM_* is stripped per subprocess; ZMEM_ALLOW_REAL_STORE is never
set.

Run: python -X utf8 tests/test_entity_card_disclosure.py  (no pytest; house
convention — exit nonzero on any failure is the check signal).
"""

import json
import os
import re
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
_GUARD_SCRATCH = tempfile.mkdtemp(prefix="guard-carddisc-")
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
# Mixed-case spellings: the #457 display predicate casefolds
# (entity._display_fold), so these are credential-shaped too (verified:
# entity._is_credential_shaped("Password:12345") is True) — but canonical
# names PRESERVE case while the card's content_norm lowercases everything,
# which is exactly the layer-1 raw-replacement leak shape.
MIXED_CANONICAL = "Password:12345"
MIXED_ALIAS = "Token:98765"
# Unicode fold-variant identities (plan-critic round 5; the pinned families
# live at tests/test_entity_display_redaction_hardening.py:290-304): the
# #457 predicate NFKC-folds and strips Cf (entity._display_fold), so these
# are all credential-shaped (verified predicate-True) and fold to the plain
# ASCII spellings — a raw-value replacement keyed on the exact spelling
# leaks in BOTH directions.
FW_COLON_NAME = "password\uff1a12345"      # fullwidth colon separator
ZWJ_NAME = "pass\u200dword:12345"          # zero-width joiner glue
FW_TOKEN = "token\uff1a98765"              # fullwidth colon separator
ZWJ_TOKEN = "to\u200dken:98765"            # zero-width joiner glue
SOFT_HYPHEN_NAME = "pass\u00adword:12345"  # soft hyphen glue
FW_LETTERS_NAME = ("\uff30\uff41\uff53\uff53\uff57\uff4f\uff52\uff44"
                   ":12345")               # fullwidth letters
# Whitespace-boundary shapes (plan-critic rounds 6+7; every predicate, fold
# and layer-2 result below VERIFIED by import against the live #457
# machinery):
#   'pass word:12345'  predicate-FALSE — bare whitespace is NOT a separator
#                      and the fold keeps the space, so it is a LEGAL
#                      near-miss of the password identity;
#   'password db=12345' predicate-TRUE (round 7's head-first shape — the
#                      assignment-key soft-split yields the 'password' head
#                      piece); the whitespace-CONTAINING identity;
#   '(password  db=12345)' predicate-FALSE as a value and layer-2
#                      (_mask_entity_display) leaves it UNCHANGED (verified:
#                      returned verbatim) — its tokens are the paren-glued
#                      '(password' and 'db=12345)', neither a standalone
#                      head, so ONLY the fold map can mask the identity span
#                      inside the parens (fold of the span == fold of the
#                      identity);
#   'db port=5432'     predicate-FALSE — the benign assignment control;
#   'password\t:12345' / 'password\n:12345' predicate-TRUE as VALUES (the
#                      separator class absorbs surrounding whitespace) but
#                      fold to 'password :12345' — NOT fold-equal to
#                      'password:12345' (the boundary space survives the
#                      map). The card renders both space-joined, and layer 2
#                      masks the standalone 'password' head token
#                      INDEPENDENTLY (verified: _mask_entity_display
#                      ('password') == '[REDACTED_SECRET]') — so the pinned
#                      expectation is '[REDACTED_SECRET] :12345': the masked
#                      head with the ' :12345' tail surviving, never the
#                      whole span replaced as one piece.
WS_LEGAL_NEAR_MISS = "pass word:12345"
WS_HEADFIRST_IDENTITY = "password db=12345"       # predicate-TRUE, head-first
WS_WRAPPED_DOUBLE_SPACE = "(password  db=12345)"  # layer-2 UNCHANGED (verified)
WS_WRAPPED_COLLAPSED = "(password db=12345)"      # its fold (run-collapsed)
WS_BENIGN_ASSIGNMENT = "db port=5432"             # predicate-FALSE control
WS_TAB_SPLIT = "password\t:12345"                 # folds to 'password :12345'
WS_NEWLINE_SPLIT = "password\n:12345"             # folds to 'password :12345'


class EntityCardDisclosureTest(unittest.TestCase):
    """One test method per card leg; each seeds a fresh scratch store."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="zmemcard_")
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
        self.ns = "user:cardprobe"
        self.suffix = uuid.uuid4().hex[:8]
        # Real `add` first: mints the schema (and the extractor's entity
        # tables) exactly as the house seeding pattern requires.
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

    def tearDown(self):
        self._td.cleanup()

    # -- seeding helpers (direct INSERTs; one connection per call) ----------

    def insert_entity_with_mentions(self, eid, kind, name, aliases, count=5):
        """Insert an entity row (+ aliases) and `count` live, linked, benign
        mention memory rows (the default ZMEM_CARD_MIN_MENTIONS threshold is
        5). Mention CONTENT stays benign so the only credential-shaped values
        that can reach the card are the Entity:/Aliases: header lines."""
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
            for i in range(count):
                mid = f"m-{self.suffix}-{eid}-{i}"
                conn.execute(
                    "INSERT INTO memory(id, namespace, type, content, tags, "
                    "source_ref, signal, confidence, valid_from, ingestion_ts)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (mid, self.ns, "fact",
                     f"rotation reminder {i} for the service account",
                     "", "probe", "user", 0.6, now,
                     f"2026-11-11T00:00:0{i}Z"))
                conn.execute(
                    "INSERT INTO memory_entity(memory_id, entity_id) "
                    "VALUES (?,?)", (mid, eid))
            conn.commit()
        finally:
            conn.close()

    def refresh_card(self, eid):
        """`store.py entity-card refresh --json <eid>`; asserts it succeeded."""
        p = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY),
             "entity-card", "refresh", "--json", eid],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO), timeout=180)
        self.assertEqual(0, p.returncode,
                         f"entity-card refresh exited {p.returncode}: "
                         f"{p.stderr}")
        return p.stdout

    def stored_card_content(self, eid):
        """The stored card's memory row (type='entity_card',
        source_ref='card:<eid>')). Guards the vacuous pass: exactly one row,
        non-empty content."""
        conn = sqlite3.connect(self.store_path)
        try:
            rows = conn.execute(
                "SELECT content FROM memory WHERE type='entity_card' "
                "AND source_ref=?", (f"card:{eid}",)).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            1, len(rows),
            f"expected exactly one stored entity_card row for card:{eid}, "
            f"found {len(rows)} (card refresh did not store the card)")
        content = rows[0][0]
        self.assertTrue(
            content and content.strip(),
            "stored entity_card row has empty content")
        return content

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

    def insert_entity_mentions(self, eid, kind, name, aliases, mentions):
        """Insert an entity row (+ aliases) and one live, linked mention
        memory row per (content, source_ref) pair in `mentions`. Unlike
        insert_entity_with_mentions the mention TEXT is caller-supplied, so a
        leg can embed credential-shaped values in the mention rows
        themselves (identity carried by mention content / source_ref, not
        the entity header)."""
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
            for i, (text, ref) in enumerate(mentions):
                mid = f"m-{self.suffix}-{eid}-x{i}"
                conn.execute(
                    "INSERT INTO memory(id, namespace, type, content, tags, "
                    "source_ref, signal, confidence, valid_from, ingestion_ts)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (mid, self.ns, "fact", text, "", ref, "user", 0.6, now,
                     f"2026-11-11T00:00:0{i % 10}Z"))
                conn.execute(
                    "INSERT INTO memory_entity(memory_id, entity_id) "
                    "VALUES (?,?)", (mid, eid))
            conn.commit()
        finally:
            conn.close()

    # Distinctive benign token inside the planted raw card's mention line:
    # the recall query and the "the row actually rendered" guard key off it.
    PLANTED_CARD_TOKEN = "meridian"

    def plant_raw_card(self, eid, card_id, content):
        """Plant a RAW pre-fix entity_card row directly (legacy store /
        hand-edit shape; no refresh involved). Mirrors refresh_entity_card's
        storage shape: memory row type='entity_card', source_ref='card:<eid>',
        confidence above every recall floor, plus the DIRECT card link
        (memory_entity with the default 'mentions' role — the link
        _ensure_direct_card_link maintains, storelib/entity.py
        ENTITY_ROLE_DEFAULT) so recall's card-suppression pass resolves it as
        a selected card. content_norm is populated the way the write path
        would (schema_meta.normalize_content: lowercase, strip, collapse
        whitespace runs to one space — the FULL folded card text, raw values
        included) so a read surface that renders content_norm is pinned too."""
        conn = sqlite3.connect(self.store_path)
        try:
            now = "2026-11-11T00:00:00Z"
            content_norm = re.sub(r"\s+", " ", content.strip().lower())
            conn.execute(
                "INSERT INTO memory(id, namespace, type, content, "
                "content_norm, tags, source_ref, signal, confidence, "
                "valid_from, ingestion_ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, self.ns, "entity_card", content, content_norm, "",
                 f"card:{eid}", "user", 0.9, now, now))
            conn.execute(
                "INSERT INTO memory_entity(memory_id, entity_id, role) "
                "VALUES (?,?,?)", (card_id, eid, "mentions"))
            conn.commit()
        finally:
            conn.close()

    # -- AC1 legs -----------------------------------------------------------

    def test_card_masks_credential_canonical_name_and_alias(self):
        """Credential-shaped canonical name + alias must not be stored raw in
        the card's memory row; the masked marker must be present (both
        directions — raw-absent alone would pass on an empty card)."""
        eid = f"e-cred-{self.suffix}"
        self.insert_entity_with_mentions(
            eid, "other", RAW_CANONICAL, aliases=(RAW_ALIAS,), count=5)
        self.refresh_card(eid)
        content = self.stored_card_content(eid)
        # Non-vacuous guard: this is the real card for THIS entity.
        self.assertIn("Kind: other", content,
                      "stored card does not look like the seeded entity's "
                      "card (Kind line absent)")
        for raw in (RAW_CANONICAL, RAW_ALIAS):
            self.assertNotIn(
                raw, content,
                f"RAW-LEAK: credential-shaped value {raw!r} stored verbatim "
                f"in the entity_card memory row; expected {MARKER}")
        self.assertIn(
            MARKER, content,
            f"masked form {MARKER} absent from the stored card content "
            f"(name neither masked nor withheld)")

    def test_benign_entity_card_keeps_raw_name_unmasked(self):
        """Control: a benign label:port entity's card keeps its raw
        canonical name and alias — masking must not over-reach."""
        eid = f"e-benign-{self.suffix}"
        self.insert_entity_with_mentions(
            eid, "host", BENIGN_NAME, aliases=(BENIGN_ALIAS,), count=5)
        self.refresh_card(eid)
        content = self.stored_card_content(eid)
        self.assertIn(f"Entity: {BENIGN_NAME}", content,
                      "benign canonical name missing from its own card")
        self.assertIn(BENIGN_ALIAS, content,
                      "benign alias missing from the card's Aliases line")
        self.assertNotIn(
            MARKER, content,
            f"masking over-reached: {MARKER} present in a benign "
            f"label:port entity's card")

    def test_card_masks_credential_values_embedded_in_mention_rows(self):
        """Mention-identity leg: credential-shaped values embedded in the
        MENTION rows' content and source_ref must not survive into the stored
        card either (the card renders mention text= and source_ref= lines
        verbatim today)."""
        eid = f"e-cred-embed-{self.suffix}"
        self.insert_entity_mentions(
            eid, "other", RAW_CANONICAL, (RAW_ALIAS,),
            mentions=[
                (f"rotation for {RAW_CANONICAL} using {RAW_ALIAS} scheduled",
                 "probe"),
                (f"first reminder for {RAW_CANONICAL}", "probe"),
                (f"second reminder for {RAW_ALIAS}", "probe"),
                ("third benign rotation reminder", "probe"),
                (f"fourth reminder for {RAW_CANONICAL}",
                 f"probe:{RAW_CANONICAL}"),
            ])
        self.refresh_card(eid)
        content = self.stored_card_content(eid)
        # Non-vacuous guard: the card really rendered for THIS entity.
        self.assertIn("Kind: other", content,
                      "stored card does not look like the seeded entity's "
                      "card (Kind line absent)")
        for raw in (RAW_CANONICAL, RAW_ALIAS):
            self.assertNotIn(
                raw, content,
                f"RAW-LEAK (mention identity): credential-shaped value "
                f"{raw!r} from a mention row's content/source_ref survived "
                f"into the stored entity_card memory row; expected {MARKER}")
        self.assertIn(
            MARKER, content,
            f"masked form {MARKER} absent from the stored card content "
            f"(mention-identity values neither masked nor withheld)")

    def test_card_masks_punctuation_wrapped_identity_in_mention_rows(self):
        """Punctuation-wrapped identity leg: credential-shaped values wrapped
        in (), [], and / inside mention text (and a wrapped source_ref) must
        not survive into the stored card. The raw SUBSTRING is asserted
        absent, so an occurrence glued inside punctuation counts as a leak
        exactly like a bare one."""
        eid = f"e-cred-punct-{self.suffix}"
        self.insert_entity_mentions(
            eid, "other", RAW_CANONICAL, (RAW_ALIAS,),
            mentions=[
                (f"rotate ({RAW_CANONICAL}) before the weekend", "probe"),
                (f"second reminder uses [{RAW_ALIAS}] in brackets", "probe"),
                (f"third path form is /{RAW_CANONICAL} on the host", "probe"),
                ("fourth benign rotation reminder", "probe"),
                ("fifth reminder for the service account",
                 f"probe:({RAW_CANONICAL})"),
            ])
        self.refresh_card(eid)
        content = self.stored_card_content(eid)
        # Non-vacuous guard: the card really rendered for THIS entity.
        self.assertIn("Kind: other", content,
                      "stored card does not look like the seeded entity's "
                      "card (Kind line absent)")
        for raw in (RAW_CANONICAL, RAW_ALIAS):
            self.assertNotIn(
                raw, content,
                f"RAW-LEAK (punctuation-wrapped): credential-shaped substring "
                f"{raw!r} survived into the stored entity_card memory row "
                f"even inside (), [], or / wrapping; expected {MARKER}")
        self.assertIn(
            MARKER, content,
            f"masked form {MARKER} absent from the stored card content "
            f"(punctuation-wrapped values neither masked nor withheld)")

    def test_card_masks_mixed_case_identity_and_lowercased_variants(self):
        """Mixed-case identity leg (plan-critic round 4): canonical names
        PRESERVE case (``Password:12345``) while the card's content_norm
        lowercases EVERYTHING — so a layer-1 raw-value replacement keyed on
        the exact stored spelling leaks: the lowercased punctuation-wrapped
        mention text (``(password:12345)``) and the case-folded content_norm
        both survive under a mixed-case header. The stored card must carry
        NO occurrence of the identity in ANY casing — asserted
        case-insensitively over BOTH content and the write path's own
        content_norm — and the masked marker must appear. RED at base."""
        eid = f"e-cred-mixed-{self.suffix}"
        self.insert_entity_mentions(
            eid, "other", MIXED_CANONICAL, (MIXED_ALIAS,),
            mentions=[
                ("rotation notes (password:12345) and [token:98765] plus "
                 f"raw {MIXED_CANONICAL} inline", "probe"),
                ("second reminder for PASSWORD:12345 on the host", "probe"),
                (f"third uses [{MIXED_ALIAS}] in brackets", "probe"),
                ("fourth benign rotation reminder", "probe"),
                ("fifth reminder for the service account",
                 "probe:(password:12345)"),
            ])
        self.refresh_card(eid)
        conn = sqlite3.connect(self.store_path)
        try:
            rows = conn.execute(
                "SELECT content, content_norm FROM memory WHERE "
                "type='entity_card' AND source_ref=?",
                (f"card:{eid}",)).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            1, len(rows),
            f"expected exactly one stored entity_card row for card:{eid}, "
            f"found {len(rows)} (card refresh did not store the card)")
        content = rows[0][0]
        content_norm = rows[0][1] or ""
        self.assertTrue(
            content and content.strip(),
            "stored entity_card row has empty content")
        # Non-vacuous guard: the card really rendered for THIS entity.
        self.assertIn("Kind: other", content,
                      "stored card does not look like the seeded entity's "
                      "card (Kind line absent)")
        # Case-insensitive absence over BOTH fields: no casing of the
        # identity may survive — not the mixed-case header, not the
        # lowercased punctuation-wrapped mention text, not the case-folded
        # content_norm.
        for field_name, field in (("content", content),
                                  ("content_norm", content_norm)):
            low = field.lower()
            for raw in (RAW_CANONICAL, RAW_ALIAS):
                self.assertNotIn(
                    raw, low,
                    f"RAW-LEAK (mixed-case): lowercased identity {raw!r} "
                    f"found in the stored card's {field_name} — the identity "
                    f"survived in SOME casing (mixed-case header, mention "
                    f"text, or the case-folded content_norm); expected "
                    f"{MARKER}")
        marker_low = MARKER.lower()
        self.assertIn(
            marker_low, content.lower(),
            f"masked form {MARKER} (any casing) absent from the stored card "
            f"content (mixed-case identities neither masked nor withheld)")
        self.assertIn(
            marker_low, content_norm.lower(),
            f"masked form {MARKER} (any casing) absent from the stored "
            f"card's content_norm")

    def test_card_masks_unicode_fold_variants_in_both_directions(self):
        """Unicode fold-variant leg (plan-critic round 5): fullwidth/Cf
        variants of the identities are predicate-True and fold to the ASCII
        forms (verified: entity._display_fold maps every variant below to
        'password:12345' / 'token:98765'), yet a raw-value replacement keyed
        on the exact spelling leaks BOTH directions — FORWARD (variant
        header, plain ASCII punctuation-wrapped mention) and INVERSE (ASCII
        header, variant-bearing mention). The stored card must carry NO
        fold-equivalent occurrence: the plain ASCII spelling AND every
        variant spelling, asserted absent case-insensitively over BOTH
        content and the write path's own content_norm, with the masked
        marker present in both. RED at base."""
        variants = (RAW_CANONICAL, RAW_ALIAS, FW_COLON_NAME, ZWJ_NAME,
                    FW_TOKEN, ZWJ_TOKEN, SOFT_HYPHEN_NAME, FW_LETTERS_NAME)
        # (a) FORWARD: fold variants in the entity header, plain ASCII
        # punctuation-wrapped forms in the mention text.
        eid_fw = f"e-cred-fw-{self.suffix}"
        self.insert_entity_mentions(
            eid_fw, "other", FW_COLON_NAME, (ZWJ_NAME,),
            mentions=[
                ("rotation notes (password:12345) plus [password:12345] "
                 "inline", "probe"),
                ("second benign rotation reminder", "probe"),
                ("third benign rotation reminder", "probe"),
                ("fourth benign rotation reminder", "probe"),
                ("fifth benign rotation reminder", "probe"),
            ])
        self.refresh_card(eid_fw)
        # (b) INVERSE: plain ASCII header, fold variants inside the wraps.
        # The variant identities are ALIASES of THIS entity (canonical stays
        # the plain ASCII spelling) so every asserted variant belongs to the
        # entity's layer-1 identity set (canonical + aliases).
        eid_inv = f"e-cred-inv-{self.suffix}"
        self.insert_entity_mentions(
            eid_inv, "other", RAW_ALIAS,
            (SOFT_HYPHEN_NAME, FW_LETTERS_NAME),
            mentions=[
                (f"rotation notes ({FW_TOKEN}) and [{ZWJ_TOKEN}] plus "
                 f"[{SOFT_HYPHEN_NAME}] and ({FW_LETTERS_NAME}) inline",
                 "probe"),
                ("second benign rotation reminder", "probe"),
                ("third benign rotation reminder", "probe"),
                ("fourth benign rotation reminder", "probe"),
                ("fifth benign rotation reminder", "probe"),
            ])
        self.refresh_card(eid_inv)

        marker_low = MARKER.lower()
        for label, eid in (("forward", eid_fw), ("inverse", eid_inv)):
            conn = sqlite3.connect(self.store_path)
            try:
                rows = conn.execute(
                    "SELECT content, content_norm FROM memory WHERE "
                    "type='entity_card' AND source_ref=?",
                    (f"card:{eid}",)).fetchall()
            finally:
                conn.close()
            self.assertEqual(
                1, len(rows),
                f"({label}) expected exactly one stored entity_card row "
                f"for card:{eid}, found {len(rows)} (card refresh did not "
                f"store the card)")
            content = rows[0][0]
            content_norm = rows[0][1] or ""
            # Non-vacuous guard: the card really rendered for THIS entity.
            self.assertIn("Kind: other", content,
                          f"({label}) stored card does not look like the "
                          f"seeded entity's card (Kind line absent)")
            for field_name, field in (("content", content),
                                      ("content_norm", content_norm)):
                low = field.lower()
                for variant in variants:
                    self.assertNotIn(
                        variant.lower(), low,
                        f"RAW-LEAK (unicode fold, {label}): fold-variant "
                        f"identity {variant!r} found in the stored card's "
                        f"{field_name} — the identity survived in a "
                        f"fullwidth/Cf spelling or its plain ASCII fold; "
                        f"expected {MARKER}")
                self.assertIn(
                    marker_low, low,
                    f"({label}) masked form {MARKER} (any casing) absent "
                    f"from the stored card's {field_name}")

    def test_card_fold_map_preserves_whitespace_boundaries(self):
        """Whitespace-boundary leg (plan-critic rounds 6+7): the fold map
        collapses whitespace RUNS but must never EAT a boundary space — no
        manufactured matches across whitespace — while fold-equal spans
        inside wrapping punctuation DO mask. Verified by import (see the
        WS_* constants): the legal near-miss 'pass word:12345' is
        predicate-FALSE with the space surviving its fold; the head-first
        identity 'password db=12345' is predicate-TRUE and its wrapped
        double-space mention '(password  db=12345)' is predicate-FALSE and
        UNCHANGED by layer 2 (verified: _mask_entity_display returns it
        verbatim), so ONLY the fold map can mask the identity span inside
        the parens; the tab/newline-split forms fold to 'password :12345'
        (NOT fold-equal to 'password:12345') and layer 2 masks the
        standalone head token INDEPENDENTLY — the pinned expectation is
        '[REDACTED_SECRET] :12345' (masked head, surviving tail, never the
        whole span as one piece). The mention renderer collapses whitespace
        runs to one space, so the tab/newline occurrences render
        space-joined. RED at base: the identity-masking parts fail (nothing
        masks today)."""
        # (a)+(c): one credential entity whose mentions carry the legal
        # near-miss and the tab/newline-split boundary forms.
        eid_ws = f"e-cred-ws-{self.suffix}"
        self.insert_entity_mentions(
            eid_ws, "other", RAW_CANONICAL, (),
            mentions=[
                (f"legal near-miss {WS_LEGAL_NEAR_MISS} control inline",
                 "probe"),
                (f"tab split {WS_TAB_SPLIT} inline", "probe"),
                (f"newline split {WS_NEWLINE_SPLIT} inline", "probe"),
                ("fourth benign rotation reminder", "probe"),
                ("fifth benign rotation reminder", "probe"),
            ])
        self.refresh_card(eid_ws)
        # (b): the head-first whitespace-CONTAINING identity; the wrapped
        # double-space mention '(password  db=12345)' is layer-2-UNCHANGED
        # (verified), so its masking discriminates the FOLD MAP: the span
        # inside the parens folds onto the identity and must mask.
        eid_db = f"e-cred-wsdb-{self.suffix}"
        self.insert_entity_mentions(
            eid_db, "other", WS_HEADFIRST_IDENTITY, (),
            mentions=[
                (f"wrapped double-space form {WS_WRAPPED_DOUBLE_SPACE} "
                 f"inline", "probe"),
                (f"benign assignment {WS_BENIGN_ASSIGNMENT} stays",
                 "probe"),
                ("third benign rotation reminder", "probe"),
                ("fourth benign rotation reminder", "probe"),
                ("fifth benign rotation reminder", "probe"),
            ])
        self.refresh_card(eid_db)

        marker_low = MARKER.lower()
        # Tab/newline-split occurrences render space-joined; layer 2 masks
        # the standalone 'password' head independently, so the expected form
        # is the masked head with the ' :12345' tail SURVIVING (an unmasked
        # render lacks the marker; a whole-span replacement lacks the tail —
        # either defect fails this expectation).
        masked_boundary = MARKER + " :12345"
        masked_boundary_low = MARKER.lower() + " :12345"
        for label, eid, absent_forms, verbatim_forms, masked_forms in (
                ("a+c", eid_ws, (RAW_CANONICAL,),
                 (WS_LEGAL_NEAR_MISS,),
                 (("content", masked_boundary),
                  ("content_norm", masked_boundary_low))),
                ("b", eid_db,
                 (WS_HEADFIRST_IDENTITY, WS_WRAPPED_DOUBLE_SPACE,
                  WS_WRAPPED_COLLAPSED),
                 (WS_BENIGN_ASSIGNMENT,), ())):
            conn = sqlite3.connect(self.store_path)
            try:
                rows = conn.execute(
                    "SELECT content, content_norm FROM memory WHERE "
                    "type='entity_card' AND source_ref=?",
                    (f"card:{eid}",)).fetchall()
            finally:
                conn.close()
            self.assertEqual(
                1, len(rows),
                f"({label}) expected exactly one stored entity_card row "
                f"for card:{eid}, found {len(rows)} (card refresh did not "
                f"store the card)")
            content = rows[0][0]
            content_norm = rows[0][1] or ""
            # Non-vacuous guard: the card really rendered for THIS entity.
            self.assertIn("Kind: other", content,
                          f"({label}) stored card does not look like the "
                          f"seeded entity's card (Kind line absent)")
            for field_name, field in (("content", content),
                                      ("content_norm", content_norm)):
                low = field.lower()
                for absent in absent_forms:
                    self.assertNotIn(
                        absent.lower(), low,
                        f"RAW-LEAK (whitespace, {label}): identity "
                        f"{absent!r} found in the stored card's "
                        f"{field_name}; expected {MARKER}")
                self.assertIn(
                    marker_low, low,
                    f"({label}) masked form {MARKER} (any casing) absent "
                    f"from the stored card's {field_name}")
            for verbatim in verbatim_forms:
                self.assertIn(
                    verbatim, content,
                    f"({label}) manufactured-match overreach: legal "
                    f"boundary text {verbatim!r} did not survive verbatim "
                    f"in the stored card (whitespace is a boundary, not a "
                    f"separator — the fold map must not collapse it away)")
            for field_name, expected in masked_forms:
                field = content if field_name == "content" else content_norm
                self.assertIn(
                    expected, field,
                    f"({label}) boundary-mask shape wrong in {field_name}: "
                    f"expected {expected!r} (masked head token with the "
                    f"':12345' tail surviving — NOT the whole span "
                    f"replaced as one piece, and not left unmasked)")

    def test_card_mention_text_benign_names_stay_verbatim(self):
        """Benign mention control: mention content naming localhost:8080 /
        server:8080 stays verbatim in the stored card — masking must not
        over-reach into benign mention text."""
        eid = f"e-benign-mention-{self.suffix}"
        self.insert_entity_mentions(
            eid, "host", BENIGN_NAME, (BENIGN_ALIAS,),
            mentions=[
                (f"check the gateway at {BENIGN_NAME} then {BENIGN_ALIAS} "
                 f"(reminder {i})", "probe")
                for i in range(5)
            ])
        self.refresh_card(eid)
        content = self.stored_card_content(eid)
        for benign in (BENIGN_NAME, BENIGN_ALIAS):
            self.assertIn(
                benign, content,
                f"benign mention text {benign!r} vanished from the stored "
                f"card; masking over-reached into mention text")
        self.assertNotIn(
            MARKER, content,
            f"masking over-reached: {MARKER} present in a benign "
            f"label:port entity's card")

    def test_planted_raw_card_masks_at_every_read_surface(self):
        """Existing-raw-card render leg: a RAW pre-fix card row planted
        directly in the store (with the write path's content_norm) must not
        leak its raw credential values through ANY read surface that renders
        cards. Probe-verified leaks at the current tree: recall --json, get
        (content AND content_norm fields), list (plain and --json), and
        explain --json (plain and --for-injection) — recent does NOT render
        card rows, so it is not pinned. RED at base: no read-time card guard
        exists yet."""
        eid = f"e-cred-rawcard-{self.suffix}"
        card_id = f"m-rawcard-{self.suffix}"
        card_content = (
            f"Entity: {RAW_CANONICAL}\n"
            "Kind: other\n"
            f"Aliases: {RAW_ALIAS}\n"
            "Mentions:\n"
            f"- score=0.900000 last_seen=2026-11-11T00:00:04Z id=m-legacy-1 "
            "source_ref=probe text=meridian rotation checklist for the "
            "quarter\n"
            "Last seen: 2026-11-11T00:00:04Z\n"
            "Sources:\n"
            "- m-legacy-1\n")
        self.insert_entity_mentions(
            eid, "other", RAW_CANONICAL, (RAW_ALIAS,), mentions=[])
        self.plant_raw_card(eid, card_id, card_content)

        # recall --json: the planted card must surface present-masked (the
        # row itself renders; its raw identity values do not).
        p = self.run_store("recall", "--query", self.PLANTED_CARD_TOKEN,
                           "--namespace", self.ns, "--json", "--limit", "10",
                           "--no-hybrid")
        parsed = json.loads(p.stdout)
        rows = parsed["results"] if isinstance(parsed, dict) else parsed
        self.assertTrue(
            any(r.get("id") == card_id for r in rows),
            "planted entity_card row did not surface in recall results — "
            "the present-masked contract requires the row to render")
        for raw in (RAW_CANONICAL, RAW_ALIAS):
            self.assertNotIn(
                raw, p.stdout,
                f"recall-existing-card: RAW-LEAK credential-shaped value "
                f"{raw!r} from the planted raw card reached stdout; expected "
                f"{MARKER}")
        self.assertIn(
            MARKER, p.stdout,
            "recall-existing-card: the planted card neither masked "
            f"({MARKER} absent) nor withheld its raw identity values")

        # get: the CLI has no --json flag (get ALWAYS prints JSON — the
        # house test_entity.py get leg drives it bare), so the amendment's
        # `get --id <id> --json` spelling is satisfied by the default output.
        # BOTH the content and the content_norm fields carry the raw card
        # text today; each must be masked/withheld.
        g = self.run_store("get", "--id", card_id)
        gd = json.loads(g.stdout)
        self.assertIn(
            self.PLANTED_CARD_TOKEN, (gd.get("content") or ""),
            "get did not render the planted card row (vacuous-pass guard)")
        for field in ("content", "content_norm"):
            for raw in (RAW_CANONICAL, RAW_ALIAS):
                self.assertNotIn(
                    raw, (gd.get(field) or ""),
                    f"get-existing-card: RAW-LEAK credential-shaped value "
                    f"{raw!r} from the planted raw card reached the get "
                    f"{field} field; expected {MARKER}")

        # list (plain and --json): the operator listing renders card content
        # (after its #443 withhold pass, which does not catch this row).
        for label, args in (
                ("list-plain", ("list", "--namespace", self.ns)),
                ("list-json", ("list", "--namespace", self.ns, "--json"))):
            lp = self.run_store(*args)
            self.assertIn(
                card_id, lp.stdout,
                f"{label}: planted card row did not render (vacuous-pass "
                f"guard)")
            for raw in (RAW_CANONICAL, RAW_ALIAS):
                self.assertNotIn(
                    raw, lp.stdout,
                    f"{label}: RAW-LEAK credential-shaped value {raw!r} "
                    f"from the planted raw card reached stdout; expected "
                    f"{MARKER}")

        # explain --json (plain and the --for-injection replay): both carry
        # the presented rows' content (the for-injection leg also attaches
        # the entities metadata).
        for label, extra in (
                ("explain-json", ()),
                ("explain-injection-json", ("--for-injection",))):
            ep = self.run_store("recall", "--query", self.PLANTED_CARD_TOKEN,
                                "--namespace", self.ns, "--explain", "--json",
                                *extra)
            self.assertIn(
                card_id, ep.stdout,
                f"{label}: planted card row did not render (vacuous-pass "
                f"guard)")
            for raw in (RAW_CANONICAL, RAW_ALIAS):
                self.assertNotIn(
                    raw, ep.stdout,
                    f"{label}: RAW-LEAK credential-shaped value {raw!r} "
                    f"from the planted raw card reached stdout; expected "
                    f"{MARKER}")

    def test_dead_entity_card_masks_at_every_read_surface(self):
        """Dead-entity card render leg: a RAW card whose SOURCE ENTITY has
        been deleted (entity + entity_alias rows gone; the live card row and
        its realistic content_norm stay behind). With no entity row left,
        the entities metadata JOIN yields nothing for the card — the masked
        render must recover the identities from the card's OWN Entity:/
        Aliases: header (and its punctuation-wrapped mention text), never
        from a live entity lookup. RED at base: every surface renders the
        raw card bytes."""
        eid = f"e-dead-{self.suffix}"
        card_id = f"m-deadcard-{self.suffix}"
        # Mixed-case header (plan-critic round 4): the identities preserve
        # case in the card's own header while the lowercased
        # punctuation-wrapped mention text and the case-folded content_norm
        # carry the other casings — the read-time guard must catch them all.
        card_content = (
            f"Entity: {MIXED_CANONICAL}\n"
            "Kind: other\n"
            f"Aliases: {MIXED_ALIAS}\n"
            "Mentions:\n"
            f"- score=0.900000 last_seen=2026-11-11T00:00:04Z id=m-legacy-1 "
            f"source_ref=probe text=meridian rotation notes ({RAW_CANONICAL}) "
            f"and [{RAW_ALIAS}] plus {FW_COLON_NAME} fullwidth for the "
            f"quarter\n"
            "Last seen: 2026-11-11T00:00:04Z\n"
            "Sources:\n"
            "- m-legacy-1\n")
        self.insert_entity_mentions(
            eid, "other", MIXED_CANONICAL, (MIXED_ALIAS,), mentions=[])
        self.plant_raw_card(eid, card_id, card_content)
        # Kill the source entity: entity + alias rows deleted, live card
        # left behind (the memory_entity row dangles, which the entities
        # JOIN drops — no entity metadata can exist for this card).
        conn = sqlite3.connect(self.store_path)
        try:
            conn.execute("DELETE FROM entity_alias WHERE entity_id=?", (eid,))
            conn.execute("DELETE FROM entity WHERE id=?", (eid,))
            conn.commit()
        finally:
            conn.close()

        # recall --json: the dead-entity card must still surface present-
        # masked (its identities masked from the card's own text).
        p = self.run_store("recall", "--query", self.PLANTED_CARD_TOKEN,
                           "--namespace", self.ns, "--json", "--limit", "10",
                           "--no-hybrid")
        parsed = json.loads(p.stdout)
        rows = parsed["results"] if isinstance(parsed, dict) else parsed
        self.assertTrue(
            any(r.get("id") == card_id for r in rows),
            "dead-entity card row did not surface in recall results — the "
            "present-masked contract requires the row to render")
        for raw in (RAW_CANONICAL, RAW_ALIAS):
            self.assertNotIn(
                raw, p.stdout,
                f"dead-entity recall: RAW-LEAK credential-shaped value "
                f"{raw!r} from the dead-entity card reached stdout; expected "
                f"{MARKER} (masked from the card's own header — the entity "
                f"row is gone)")
        self.assertIn(
            MARKER, p.stdout,
            "dead-entity recall: the card neither masked nor withheld its "
            f"raw identity values ({MARKER} absent)")

        # get: BOTH the content and content_norm fields carry the raw card
        # text (content_norm is the folded full card, raw values included).
        g = self.run_store("get", "--id", card_id)
        gd = json.loads(g.stdout)
        self.assertIn(
            self.PLANTED_CARD_TOKEN, (gd.get("content") or ""),
            "get did not render the dead-entity card row (vacuous-pass "
            "guard)")
        for field in ("content", "content_norm"):
            for raw in (RAW_CANONICAL, RAW_ALIAS):
                self.assertNotIn(
                    raw, (gd.get(field) or ""),
                    f"dead-entity get: RAW-LEAK credential-shaped value "
                    f"{raw!r} reached the get {field} field; expected "
                    f"{MARKER}")

        # list --json (both list variants leaked in the wave-2 probe; the
        # json variant is pinned here — the row must render masked, with the
        # identities recovered from the card's own text).
        lp = self.run_store("list", "--namespace", self.ns, "--json")
        self.assertIn(
            card_id, lp.stdout,
            "dead-entity list: card row did not render (vacuous-pass guard)")
        for raw in (RAW_CANONICAL, RAW_ALIAS):
            self.assertNotIn(
                raw, lp.stdout,
                f"dead-entity list: RAW-LEAK credential-shaped value "
                f"{raw!r} from the dead-entity card reached stdout; "
                f"expected {MARKER}")
        self.assertIn(
            MARKER, lp.stdout,
            f"dead-entity list: the card neither masked nor withheld its "
            f"raw identity values ({MARKER} absent)")

        # explain --json (the plain explain envelope carries the presented
        # rows' content).
        ep = self.run_store("recall", "--query", self.PLANTED_CARD_TOKEN,
                            "--namespace", self.ns, "--explain", "--json")
        self.assertIn(
            card_id, ep.stdout,
            "dead-entity explain: card row did not render (vacuous-pass "
            "guard)")
        for raw in (RAW_CANONICAL, RAW_ALIAS):
            self.assertNotIn(
                raw, ep.stdout,
                f"dead-entity explain: RAW-LEAK credential-shaped value "
                f"{raw!r} from the dead-entity card reached stdout; "
                f"expected {MARKER}")
        self.assertIn(
            MARKER, ep.stdout,
            f"dead-entity explain: the card neither masked nor withheld its "
            f"raw identity values ({MARKER} absent)")


    # A uuid-shaped id matching no row: drives the explain --target
    # not_in_db verdict (whose neighbors rank against the QUERY tokens — a
    # missed UUID carries no semantic content; recall.py explain_recall).
    MISSING_TARGET = "00000000-0000-0000-0000-00000000dead"

    def test_explain_target_neighbors_mask_card_identity(self):
        """Explain --target neighbor exit (implementation-review blocker,
        fixed by commit 12410ffc): the not_in_db verdict's neighbors carry
        80-char content slices, and an entity_card neighbor's slice passes
        the read-time identity guard BEFORE the cut — the planted raw card
        must surface as a neighbor with its slice MASKED (marker inside the
        slice, raw identities absent) in BOTH the JSON verdict detail and
        the human blameline. GREEN at the current tree (the fix is in); the
        leg pins the just-fixed exit. Note the query token must be a
        STANDALONE whitespace token of the card — the neighbor scorer
        tokenizes whitespace-split over content_norm, where the card's
        'text=meridian' glues but 'rotation' stands alone (probe-verified:
        overlap 0.038, masked slice, not withheld)."""
        eid = f"e-cred-tgt-{self.suffix}"
        card_id = f"m-tgtcard-{self.suffix}"
        card_content = (
            f"Entity: {RAW_CANONICAL}\n"
            "Kind: other\n"
            f"Aliases: {RAW_ALIAS}\n"
            "Mentions:\n"
            f"- score=0.900000 last_seen=2026-11-11T00:00:04Z id=m-legacy-1 "
            f"source_ref=probe text=meridian rotation notes ({RAW_CANONICAL}) "
            f"and [{RAW_ALIAS}] for the quarter\n"
            "Last seen: 2026-11-11T00:00:04Z\n"
            "Sources:\n"
            "- m-legacy-1\n")
        self.insert_entity_mentions(
            eid, "other", RAW_CANONICAL, (RAW_ALIAS,), mentions=[])
        self.plant_raw_card(eid, card_id, card_content)

        # JSON leg: no raw identity anywhere on stdout, marker present, and
        # the not_in_db verdict's neighbors carry the card with a MASKED
        # content slice (marker inside the slice itself).
        pj = self.run_store("recall", "--query", "rotation",
                            "--namespace", self.ns, "--explain", "--target",
                            self.MISSING_TARGET, "--json")
        for raw in (RAW_CANONICAL, RAW_ALIAS):
            self.assertNotIn(
                raw, pj.stdout,
                f"explain-target-json: RAW-LEAK credential-shaped value "
                f"{raw!r} reached stdout through the not_in_db neighbor "
                f"slice; expected {MARKER}")
        self.assertIn(
            MARKER, pj.stdout,
            "explain-target-json: masked form absent from the explain "
            "output")
        d = json.loads(pj.stdout)
        neighbors = []
        for v in (d.get("explain") or {}).get("verdicts", []):
            if v.get("reason") == "not_in_db":
                neighbors.extend(
                    (v.get("detail") or {}).get("neighbors") or [])
        card_neighbors = [n for n in neighbors if n.get("id") == card_id]
        self.assertTrue(
            card_neighbors,
            "the planted card did not surface as a not_in_db neighbor for "
            f"the query token (neighbor ids={[n.get('id') for n in neighbors]})")
        slice_text = card_neighbors[0].get("content") or ""
        self.assertIn(
            MARKER, slice_text,
            f"the card neighbor's content slice carries no masked marker "
            f"(slice={slice_text!r} — withheld-empty or raw?)")

        # Human leg: the blameline renders the nearest neighbors inline —
        # same no-leak + marker contract, with the card's id proving it
        # surfaced (vacuous-pass guard).
        ph = self.run_store("recall", "--query", "rotation",
                            "--namespace", self.ns, "--explain", "--target",
                            self.MISSING_TARGET)
        for raw in (RAW_CANONICAL, RAW_ALIAS):
            self.assertNotIn(
                raw, ph.stdout,
                f"explain-target-human: RAW-LEAK credential-shaped value "
                f"{raw!r} reached stdout through the not_in_db blameline "
                f"slice; expected {MARKER}")
        self.assertIn(
            MARKER, ph.stdout,
            "explain-target-human: masked form absent from the blameline "
            "output")
        self.assertIn(
            card_id, ph.stdout,
            "explain-target-human: the planted card did not surface in the "
            "blameline (vacuous-pass guard)")


if __name__ == "__main__":
    unittest.main()
