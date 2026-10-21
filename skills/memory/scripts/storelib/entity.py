"""Entity identity for the memory store (issue #60, schema v10).

Three tables own the data:

    entity(id, kind, canonical_name, created_at, updated_at)
    entity_alias(entity_id, alias_norm)         -- UNIQUE(alias_norm), global
    memory_entity(memory_id, entity_id, role)   -- UNIQUE(memory_id, entity_id)

``memory_entity`` rows are DERIVED data keyed on the memory's
(content, tags, namespace) — the same class of store-local derived state as
``content_norm`` and embeddings. The rule every write surface follows:

    every site that inserts a memory row, or changes its content/tags/
    namespace, re-derives its entity links in the same transaction.

The extractor is DETERMINISTIC (regex + token rules, no LLM, no network, no
model download). There is deliberately no LLM path and no ``ZMEM_ENTITY_LLM``
knob in this PR: the issue prefers no LLM path at all, and an env var that
parses but does nothing would violate the repo's no-unwired-code release rule.

Extraction sources → kinds (deterministic mapping, first-seen kind wins):

A source carrying an explicit kind is AUTHORITATIVE and is never
re-classified. A source carrying NO explicit kind is resolved by the
deterministic ``classify_entity_kind`` (host/service/model/repo/file/
command/error_class/other, in that precedence).

    source                                   kind
    ---------------------------------------------------------------
    ``entity:<kind>:Name`` (typed)           the named kind
    ``entity:Name`` (untyped)                classify_entity_kind(Name)
    ``project:<suffix>`` namespace            project
    ``--tags`` token ``<kind>:<Name>``        the named kind
    ``--tags`` plain token                    classify_entity_kind(token)
    backtick-quoted span in content           tool
    CamelCase identifier (2+ humps) in content classify_entity_kind(name)

``person`` entities are NEVER auto-detected: they can only be created via an
explicit ``entity:person:Name`` tag, and nothing auto-merges entities of any
kind (``entity-merge`` is manual, ``--confirm``-gated, refuses kind
mismatches — the issue's "ZMem will not auto-merge people" rule).

Alias normalization reuses ``storelib.schema._normalize_content`` — the SAME
function that produces ``content_norm``, as the issue mandates. (Python 3's
``str.lower()`` is locale-independent Unicode folding, so aliases fold
identically on every host.)
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import uuid

from schema_meta import (
    CARD_MIN_MENTIONS_DEFAULT,
    CARD_MIN_MENTIONS_ENV,
    CARD_TOP_N_DEFAULT,
    CARD_TOP_N_ENV,
    ENTITY_CARD_WATERMARK_PREFIX,
    ENTITY_KINDS,
    worse_taint,
)
from storelib.ops_tokens import RUNNER_HEADS
from storelib.schema import (
    GLOBAL_NAMESPACE,
    _as_of_temporal_predicate,
    _commit,
    _normalize_content,
    now_iso,
)

# The single link role shipped in v10. The column exists so phase 6 (links)
# can enrich relationships without another migration; every v10 link row
# carries 'mentions' today and entity-list/get render it verbatim.
ENTITY_ROLE_DEFAULT = "mentions"

# Stopwords never become entities, from any source (issue #60, 5.2 negative
# case names `the`, `and`, `use`). Kept to English function words that carry
# no identity; compared against the NORMALIZED alias so case/whitespace
# variants are covered.
ENTITY_STOPWORDS = frozenset({
    "the", "and", "or", "a", "an", "of", "in", "on", "for", "to", "with",
    "is", "are", "was", "were", "be", "been", "not", "no", "nor", "but",
    "use", "uses", "using", "used", "this", "that", "these", "those", "it",
    "its", "as", "by", "at", "from", "if", "then", "than", "so", "do",
    "does", "did", "done", "yes", "we", "you", "they", "he", "she", "i",
    "into", "over", "under", "out", "up", "down", "when", "what", "which",
    "who", "how", "why", "all", "any", "some", "more", "most", "other",
    "new", "old", "own", "same", "very", "just", "also", "can", "will",
    "should", "would", "must", "may", "might", "shall", "has", "have", "had",
    # The extractor's own keyword: a bare `entity` tag token (no :Name) is
    # syntax, not an identity.
    "entity",
})

# CamelCase identifier: a capital-led run of humps, each hump being one
# uppercase letter followed by lowercase letters/digits, with TWO OR MORE
# humps. Single-hump words (`Python`) and acronym runs without lowercase
# (`FTS5`, `OpenAI`) deliberately do NOT match — they are ordinary prose.
_CAMELCASE_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")

# Backtick-quoted span: the issue's identifier quoting convention
# (``use `ripgrep` not grep`` → tool entity `ripgrep`).
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")

# Explicit tag, in content OR tags: `entity:Name` or `entity:<kind>:Name`.
# The lookbehind rejects suffixed identifiers (`myentity:x`) and URLs.
#
# The kind alternation is DERIVED from schema_meta.ENTITY_KINDS rather than
# written out (issue #175 feedback PRR-002/SPEC-4). Hardcoding the vocabulary
# here made this regex drift from the shared tuple the moment the v2 kinds
# shipped: `entity:host:web01` then failed to match the optional kind group, so
# the NAME group consumed the kind word and the match collapsed to
# ('other', 'host') — and because entity_alias.alias_norm is globally UNIQUE,
# every `entity:host:*` tag in the store folded into ONE wrong-kind singleton
# with the real names discarded. One source, one regex.
_ENTITY_TAG_RE = re.compile(
    r"(?<![A-Za-z0-9_:])entity:(?:(" + "|".join(ENTITY_KINDS) + r"):)?"
    r"([A-Za-z0-9_][A-Za-z0-9_.\-]*)",
    re.IGNORECASE,
)

# Path/URL shapes are never entities (issue #60, 5.2 negative: Windows paths
# are not person entities — and not tool/other entities either). A token with
# a separator or a drive prefix is a location, not an identity.
_PATHISH_RE = re.compile(r"[\\/]|^[A-Za-z]:|://|^www\.")

# The tag-string tokenizer, exported for every consumer that must agree on
# what a "tag token" is (issue #381): the peer attribution strip in
# storelib.sync decides tokenhood under THIS grammar — comma, semicolon and
# whitespace separators — because it is the grammar this module's consumers
# use to read the stored tags back. A private second tokenizer there drifted
# (comma-only, case-sensitive) and let `Host:evil` / `x;host:evil` survive
# the strip and mint host entities here. One grammar, every consumer.
TAG_TOKEN_SPLIT_RE = re.compile(r"[,;\s]+")


def split_tag_tokens(tags: str) -> list[str]:
    """Split a tag string into its tokens under the extractor's grammar.

    Empty segments (consecutive separators, leading/trailing separators)
    yield no tokens. Pure helper: no normalization, no filtering — callers
    decide what the tokens mean.
    """
    return [t for t in TAG_TOKEN_SPLIT_RE.split(tags or "") if t]


def _eligible_name(name: str) -> bool:
    """Can `name` (raw, un-normalized) become an entity/alias at all?

    Path-shaped tokens, URLs, stopwords, and too-short/purely-numeric runs
    are refused from EVERY source. Path-shape takes precedence over the
    backtick rule: a backtick-quoted absolute path is a quoted PATH, not a
    tool.
    """
    name = (name or "").strip()
    if not name or _PATHISH_RE.search(name):
        return False
    norm = _normalize_content(name)
    if len(norm) < 2 or norm in ENTITY_STOPWORDS:
        return False
    # Reject runs that normalize to nothing identity-like (digits/punct only).
    return any(c.isalnum() for c in norm)


def extract_entities(
    content: str, tags: str = "", namespace: str = ""
) -> list[tuple[str, str]]:
    """Deterministically extract (kind, name) pairs from a memory's fields.

    Extraction order fixes which kind wins when two sources produce the same
    alias_norm (first-seen kind is kept on upsert): explicit ``entity:`` tags
    (most deliberate) → namespace suffix → remaining tags → backtick spans →
    CamelCase identifiers. Output is deduped by alias_norm in first-seen
    order, so the same call always returns the same list.
    """
    found: list[tuple[str, str]] = []

    def _offer(kind: str, name: str) -> None:
        if kind not in ENTITY_KINDS or not _eligible_name(name):
            return
        found.append((kind, name.strip()))

    blob = f"{tags or ''}\n{content or ''}"

    # 1) Explicit entity: tags (tags field first, then content) — the only
    #    source that can mint a `person` or `preference` entity. Issue #176:
    #    a TYPED tag is authoritative and is never re-classified; an untyped
    #    `entity:Name` carries no kind, so the value is classified.
    for m in _ENTITY_TAG_RE.finditer(blob):
        typed = (m.group(1) or "").lower()
        kind = typed if typed else classify_entity_kind(m.group(2))
        _offer(kind, m.group(2))

    # 2) project:<suffix> namespace.
    ns = namespace or ""
    if ns.lower().startswith("project:"):
        _offer("project", ns.split(":", 1)[1])

    # 3) Remaining tag tokens: `kind:Name` maps to that kind; a plain token
    #    is classified (issue #176 — previously always `other`; an explicit
    #    typed prefix here stays authoritative). (The explicit entity: form
    #    was consumed above.)
    for tok in split_tag_tokens(tags):
        if not tok or tok.lower().startswith("entity:"):
            continue
        if ":" in tok:
            prefix, _, rest = tok.partition(":")
            if prefix.lower() in ENTITY_KINDS and rest:
                _offer(prefix.lower(), rest)
                continue
        _offer(classify_entity_kind(tok), tok)

    # 4) Backtick-quoted spans in content → tool entities.
    for m in _BACKTICK_RE.finditer(content or ""):
        _offer("tool", m.group(1))

    # 5) CamelCase identifiers (2+ humps) in content → classified (issue
    #    #176 — previously always `other`; CamelCase error names such as
    #    ValueError now kind correctly).
    for m in _CAMELCASE_RE.finditer(content or ""):
        _offer(classify_entity_kind(m.group(0)), m.group(0))

    # Dedup by alias_norm, first-seen order (also makes the kind-conflict
    # policy concrete: the first source in the order above wins).
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for kind, name in found:
        a = _normalize_content(name)
        if a in seen:
            continue
        seen.add(a)
        out.append((kind, name))
    return out


# --- Issue #176: deterministic entity-kind classification -------------------
#
# The classifier is a PURE function of the name string: no LLM, no shell, no
# network, no DNS, no filesystem — the same input always yields the same
# member of ENTITY_KINDS. Rules are applied to the STRIPPED, CASEFOLDED value
# in the issue's fixed precedence (host -> service -> model -> repo -> file
# -> command -> error_class -> other); every pattern below is written against
# the folded string.

_CLASSIFIER_MODEL_PREFIXES = (
    "gpt-", "claude-", "llama-", "mistral-", "gemma-", "text-embedding-",
    "embedding-",
)
_CLASSIFIER_SOURCE_SUFFIXES = (
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".rs", ".go",
    ".java", ".rb", ".sh", ".bash", ".ps1", ".sql", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".swift", ".kt", ".php", ".pl", ".lua", ".toml",
    ".yaml", ".yml", ".json", ".xml", ".html", ".css",
)
_CLASSIFIER_DOC_SUFFIXES = (
    ".md", ".txt", ".rst", ".markdown", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".log", ".csv",
)
_CLASSIFIER_BASENAMES = frozenset({
    "readme", "makefile", "license", "changelog", "contributing", "notice",
    "authors",
})

# host: strict IPv4 (four dotted octets, each 0-255), 3+-label DNS names, or
# the one-dot "hyphen in first label + alphabetic final label" shape. The
# digit-leading-final-label rejection reroutes model-shaped names (gpt-5.3,
# claude-3.5sonnet) away from host so the model prefix rule can take them.
_CLASSIFIER_IPV4_RE = re.compile(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$")
_CLASSIFIER_HOST_MULTI_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+){2,}$")
# The one-label form is written as `alnum* (- alnum*)+` so each repetition
# MUST begin with a literal hyphen: the split point is therefore unambiguous
# and the engine backtracks linearly. The previous `[a-z0-9-]*-[a-z0-9-]*`
# form had two overlapping classes with the hyphen in an optional position,
# which made a hyphen-dense NON-MATCHING input (e.g. a long `a-a-a-` token
# arriving through the sync/ingest `tags` field, which is NOT length-capped)
# quadratic: measured 7.6 s at 64 KB, extrapolating to minutes at the 1 MiB
# ingest line cap. The rewrite accepts exactly the same language (pinned by
# ReDosRegressionTest.test_rewrite_is_semantically_identical_on_edge_cases)
# and is ~800x faster on that shape.
_CLASSIFIER_HOST_ONEDOT_RE = re.compile(
    r"^[a-z0-9]*(?:-[a-z0-9]*)+\.[a-z][a-z0-9-]*$"
)
# service: a host-ish label with a 1-5 digit port, or a WHOLE-WORD service
# noun (word-bounded: "databaseexception" must fall through to error_class,
# while the hyphen in "the-database" is a boundary).
_CLASSIFIER_SERVICE_PORT_RE = re.compile(r"^[a-z0-9_.-]+:[0-9]{1,5}$")
_CLASSIFIER_SERVICE_WORD_RE = re.compile(
    r"\b(?:service|server|daemon|database)\b"
)
# repo: the two github.com URL forms plus the generic owner/repo shape.
# Generic labels are DOT-FREE so "src/main.py" cannot match repo (it is a
# file) and "gitlab.com/org/repo" matches none of the three forms.
_CLASSIFIER_REPO_GH_RE = re.compile(r"^github\.com/[a-z0-9_.-]+/[a-z0-9_.-]+$")
_CLASSIFIER_REPO_URL_RE = re.compile(
    r"^https?://github\.com/[a-z0-9_.-]+/[a-z0-9_.-]+/?$"
)
_CLASSIFIER_REPO_OWNER_RE = re.compile(r"^[a-z0-9_-]+/[a-z0-9_-]+$")
# file: a drive prefix (Windows) — a PREFIX match, not fullmatch — or a
# slash-rooted POSIX path.
_CLASSIFIER_FILE_WIN_RE = re.compile(r"^[a-z]:[\\/]")
# error_class: suffix rule on the folded value (case-insensitive by
# construction); "Errors" fails the end anchor and stays other.
_CLASSIFIER_ERROR_RE = re.compile(r"^[a-z_][a-z0-9_]*(?:error|exception)$")


def _classify_ipv4(v: str) -> bool:
    if not _CLASSIFIER_IPV4_RE.fullmatch(v):
        return False
    return all(0 <= int(o) <= 255 for o in v.split("."))


def classify_entity_kind(name: str) -> str:
    """Deterministically classify one entity NAME into an ENTITY_KINDS member.

    Pure string function (regex + membership only — no LLM, shell, network,
    DNS, or filesystem access). The issue #176 precedence, applied to the
    stripped casefolded value:

    1. host — localhost, strict IPv4, 3+-label DNS names (final label not
       digit-leading, no edge-hyphen labels), or one-dot names whose first
       label contains a hyphen and whose final label starts alphabetic.
    2. service — ``label:port`` (1-5 digits) or a whole-word
       service/server/daemon/database noun.
    3. model — literal prefix gpt-/claude-/llama-/mistral-/gemma-/
       text-embedding-/embedding- (the issue's verbatim "beginning with").
    4. repo — github.com/org/repo, its https form, or dot-free owner/repo.
    5. file — drive-rooted Windows path, slash-rooted POSIX path, a string
       containing ``/`` with a recognized source suffix, or a README-like
       doc/data basename.
    6. command — first whitespace token is an ``ops_tokens.RUNNER_HEADS``
       runner verb.
    7. error_class — ``[A-Za-z_][A-Za-z0-9_]*(Error|Exception)``,
       case-insensitively.
    8. otherwise other.
    """
    v = (name or "").strip().casefold()
    if not v:
        return "other"
    # 1) host
    if v == "localhost" or _classify_ipv4(v):
        return "host"
    if _CLASSIFIER_HOST_MULTI_RE.fullmatch(v):
        labels = v.split(".")
        if (all(labels) and not labels[-1][:1].isdigit()
                and not any(l.startswith("-") or l.endswith("-")
                            for l in labels)):
            return "host"
    if _CLASSIFIER_HOST_ONEDOT_RE.fullmatch(v):
        return "host"
    # 2) service
    if _CLASSIFIER_SERVICE_PORT_RE.fullmatch(v) or _CLASSIFIER_SERVICE_WORD_RE.search(v):
        return "service"
    # 3) model
    if v.startswith(_CLASSIFIER_MODEL_PREFIXES):
        return "model"
    # 4) repo
    if (_CLASSIFIER_REPO_GH_RE.fullmatch(v)
            or _CLASSIFIER_REPO_URL_RE.fullmatch(v)
            or _CLASSIFIER_REPO_OWNER_RE.fullmatch(v)):
        return "repo"
    # 5) file
    if _CLASSIFIER_FILE_WIN_RE.match(v) or v.startswith("/"):
        return "file"
    if "/" in v and v.endswith(_CLASSIFIER_SOURCE_SUFFIXES):
        return "file"
    if "/" not in v and (v.endswith(_CLASSIFIER_DOC_SUFFIXES)
                         or v in _CLASSIFIER_BASENAMES):
        return "file"
    # 6) command
    head = v.split()[0] if v.split() else ""
    if head in RUNNER_HEADS:
        return "command"
    # 7) error_class
    if _CLASSIFIER_ERROR_RE.fullmatch(v):
        return "error_class"
    return "other"


def _default_entity_kind_weights() -> dict[str, float]:
    weights = {k: 1.0 for k in ENTITY_KINDS}
    weights["other"] = 0.6
    return weights


# Values of ZMEM_ENTITY_KIND_WEIGHTS already warned about in THIS process, so
# one bad knob produces one stderr line per command instead of one per recall
# tier (recall parses the knob once per tier plus once for --explain).
_WEIGHTS_WARNED: set[str] = set()


def parse_entity_kind_weights() -> dict[str, float]:
    """Parse ZMEM_ENTITY_KIND_WEIGHTS into a per-kind weight map.

    Defaults: 1.0 for every ENTITY_KINDS member except ``other`` = 0.6. A
    missing variable returns the defaults SILENTLY. Any invalid
    configuration — non-JSON, non-object, unknown key, nonnumeric value,
    NaN/infinity, zero, or negative — logs EXACTLY ONE warning line to
    stderr PER PROCESS PER DISTINCT BAD VALUE (recall parses once per tier
    plus once more for --explain, so an unguarded print repeated the
    identical line 2-3 times per command), and returns the COMPLETE
    defaults (no partial overrides).

    This parser NEVER raises: recall reads it on every tier of every
    invocation and malformed operator configuration must degrade, not
    crash. Two escape classes the naive `except ValueError` missed are
    handled explicitly - ``RecursionError`` from ``json.loads`` on deeply
    nested JSON, and ``OverflowError`` from ``float()`` on an integer
    literal too large for a C double - and the whole body is wrapped so no
    future edit can reintroduce an escape into recall.
    """
    raw = os.environ.get("ZMEM_ENTITY_KIND_WEIGHTS")
    if raw is None:
        return _default_entity_kind_weights()

    def _fallback(reason: str) -> dict[str, float]:
        # One warning line per (process, raw value), then complete defaults.
        global _WEIGHTS_WARNED
        if raw not in _WEIGHTS_WARNED:
            _WEIGHTS_WARNED.add(raw)
            print(f"[zmem] entity-kind weights: {reason}; using defaults",
                  file=sys.stderr)
        return _default_entity_kind_weights()

    try:
        try:
            obj = json.loads(raw)
        except RecursionError:
            return _fallback("ZMEM_ENTITY_KIND_WEIGHTS is nested too deeply")
        except ValueError:
            return _fallback("ZMEM_ENTITY_KIND_WEIGHTS is not valid JSON")
        if not isinstance(obj, dict):
            return _fallback("ZMEM_ENTITY_KIND_WEIGHTS is not a JSON object")
        for key, value in obj.items():
            if key not in ENTITY_KINDS:
                return _fallback(f"unknown entity kind {key!r}")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return _fallback(f"weight for {key!r} is not a number")
            number = float(value)  # OverflowError on a huge int literal
            if not math.isfinite(number):
                return _fallback(f"weight for {key!r} is not finite")
            if number <= 0:
                return _fallback(f"weight for {key!r} must be positive")
        weights = _default_entity_kind_weights()
        for key, value in obj.items():
            weights[key] = float(value)
        return weights
    except Exception as exc:  # noqa: BLE001 - the never-raise contract
        return _fallback(f"ZMEM_ENTITY_KIND_WEIGHTS could not be read "
                         f"({type(exc).__name__})")


def _other_link_share(conn: sqlite3.Connection,
                      kind_by_id: dict[str, str]) -> float:
    """Share of memory_entity rows linked to an `other` entity (0.0 when
    the table is empty — an empty denominator is no evidence of anything)."""
    rows = conn.execute(
        "SELECT me.entity_id FROM memory_entity me "
        "JOIN entity e ON e.id = me.entity_id"
    ).fetchall()
    if not rows:
        return 0.0
    other = sum(1 for r in rows if kind_by_id.get(r[0]) == "other")
    return other / len(rows)


def reclassify_entities(
    conn: sqlite3.Connection, *, dry_run: bool = True,
) -> dict:
    """Propose (dry_run=True) or apply (dry_run=False) kind classification
    for every entity row currently kinded `other` (issue #176).

    Non-`other` rows are NEVER considered or modified — an entity's kind is
    authoritative once it is not `other` (the same first-seen-kind-wins
    policy as `_upsert_entity`; manual reconciliation stays `entity-merge`).
    Proposals come from `classify_entity_kind` only, and every changed kind
    is validated against ENTITY_KINDS before any write.

    Returns ``{"dry_run": bool, "scanned": N, "changed": N, "changes":
    [{"id","name","old_kind","new_kind"}...] ordered by entity id,
    "before_other_link_share": float, "after_other_link_share": float}``
    where the shares are computed over ALL memory_entity rows (before: the
    current kinds; after: the kinds projected through the proposal — for
    apply, the post-UPDATE kinds).

    dry_run=True performs NO UPDATE and NEVER commits (read-only). apply
    updates only the changed rows' ``entity.kind`` in ONE transaction and
    commits once; any invalid proposal or SQL error rolls the whole apply
    back. Entity ids, entity_alias rows, and memory_entity rows are never
    touched.
    """
    if dry_run:
        # Read-only: a deferred transaction we always roll back. Nothing
        # writes, so this is belt-and-braces isolation for the caller.
        started = not conn.in_transaction
        if started:
            conn.execute("BEGIN")
        try:
            return _reclassify_scan(conn, apply_changes=False)
        finally:
            if started and conn.in_transaction:
                conn.rollback()
    # apply path: one write transaction, committed once, rolled back whole.
    proposal = _reclassify_scan(conn, apply_changes=False)
    changes = proposal["changes"]
    started = not conn.in_transaction
    try:
        if started:
            conn.execute("BEGIN IMMEDIATE")
        # Re-verify every target is still `other` under the write lock —
        # a concurrent writer between scan and apply must fail closed, not
        # silently re-kind a row someone else already kinded.
        for c in changes:
            row = conn.execute(
                "SELECT kind FROM entity WHERE id=?", (c["id"],)
            ).fetchone()
            if row is None or row[0] != "other":
                raise ValueError(
                    f"reclassify-entities: entity {c['id']} is no longer "
                    "kind 'other' (changed since the proposal)"
                )
        conn.executemany(
            "UPDATE entity SET kind=? WHERE id=?",
            [(c["new_kind"], c["id"]) for c in changes],
        )
        result = _reclassify_scan(conn, apply_changes=True,
                                  proposed=proposal)
        if started:
            conn.commit()
        return result
    except Exception:
        if started and conn.in_transaction:
            conn.rollback()
        raise


def _reclassify_scan(conn: sqlite3.Connection, *, apply_changes: bool,
                     proposed: dict | None = None) -> dict:
    """Shared scan for both reclassify modes (runs inside the caller's
    transaction). With ``proposed`` (the apply path's pre-UPDATE proposal)
    the after-share reflects the proposal, matching what dry-run reported."""
    if apply_changes and proposed is not None:
        # The UPDATE already ran; rebuild the envelope from the proposal so
        # apply reports exactly what dry-run proposed. The before-share is
        # the current post-UPDATE view folded BACKWARDS through the
        # proposal; the after-share is the current view itself.
        changes = proposed["changes"]
        scanned = proposed["scanned"]
        kind_by_id = dict(conn.execute("SELECT id, kind FROM entity").fetchall())
        after = _other_link_share(conn, kind_by_id)
        for c in changes:
            kind_by_id[c["id"]] = "other"
        before = _other_link_share(conn, kind_by_id)
        return {
            "dry_run": False, "scanned": scanned, "changed": len(changes),
            "changes": changes,
            "before_other_link_share": before,
            "after_other_link_share": after,
        }
    rows = conn.execute(
        "SELECT id, canonical_name FROM entity WHERE kind='other' ORDER BY id"
    ).fetchall()
    changes = []
    for r in rows:
        new_kind = classify_entity_kind(r["canonical_name"])
        if new_kind != "other" and new_kind in ENTITY_KINDS:
            changes.append({
                "id": r["id"], "name": r["canonical_name"],
                "old_kind": "other", "new_kind": new_kind,
            })
    kind_by_id = dict(conn.execute("SELECT id, kind FROM entity").fetchall())
    before = _other_link_share(conn, kind_by_id)
    for c in changes:
        kind_by_id[c["id"]] = c["new_kind"]
    after = _other_link_share(conn, kind_by_id)
    return {
        "dry_run": True, "scanned": len(rows), "changed": len(changes),
        "changes": changes,
        "before_other_link_share": before,
        "after_other_link_share": after,
    }


def _upsert_entity(conn: sqlite3.Connection, kind: str, name: str) -> str | None:
    """Return the entity id for `name`, creating entity + primary alias if new.

    Alias-first upsert: an existing alias_norm adopts its entity regardless of
    the incoming kind (FIRST-SEEN KIND WINS — never re-kinded silently; the
    manual reconciliation tool is `entity-merge`). Returns None only for an
    unusable name (callers pre-filter, so in practice never).
    """
    alias = _normalize_content(name)
    if not alias:
        return None
    row = conn.execute(
        "SELECT entity_id FROM entity_alias WHERE alias_norm=?", (alias,)
    ).fetchone()
    if row:
        return row[0]
    eid = str(uuid.uuid4())
    ts = now_iso()
    conn.execute(
        "INSERT INTO entity(id, kind, canonical_name, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (eid, kind, name, ts, ts),
    )
    try:
        conn.execute(
            "INSERT INTO entity_alias(entity_id, alias_norm) VALUES (?,?)",
            (eid, alias),
        )
    except sqlite3.IntegrityError:
        # Concurrent writer minted the alias first: adopt its entity, drop
        # the shell row we just created (fail-closed to the shared identity).
        conn.execute("DELETE FROM entity WHERE id=?", (eid,))
        row = conn.execute(
            "SELECT entity_id FROM entity_alias WHERE alias_norm=?", (alias,)
        ).fetchone()
        return row[0] if row else None
    return eid


def link_memory_entities(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    content: str,
    tags: str,
    namespace: str,
) -> int:
    """Extract + upsert entities for one memory and link them (idempotent).

    Runs INSIDE the caller's transaction (never commits). INSERT OR IGNORE on
    the UNIQUE(memory_id, entity_id) link makes re-runs — including the
    migration backfill re-running after a crash — exact no-ops.
    """
    linked = 0
    for kind, name in extract_entities(content, tags, namespace):
        eid = _upsert_entity(conn, kind, name)
        if eid is None:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO memory_entity(memory_id, entity_id, role) "
            "VALUES (?,?,?)",
            (memory_id, eid, ENTITY_ROLE_DEFAULT),
        )
        linked += max(cur.rowcount or 0, 0)
    return linked


def relink_memory(conn: sqlite3.Connection, memory_id: str) -> int:
    """Re-derive one memory's links from its stored row.

    For every site that changes a memory's content/tags/namespace WITHOUT
    inserting a new row (dedup tag-union merges, consolidate absorb,
    namespace re-keys): drop the stale links, re-run the extractor. Must run
    inside the caller's transaction.
    """
    row = conn.execute(
        "SELECT content, tags, namespace FROM memory WHERE id=?", (memory_id,)
    ).fetchone()
    if row is None:
        return 0
    conn.execute("DELETE FROM memory_entity WHERE memory_id=?", (memory_id,))
    return link_memory_entities(
        conn, memory_id,
        content=row["content"], tags=row["tags"], namespace=row["namespace"],
    )


def backfill_entities(conn: sqlite3.Connection, batch_size: int = 500) -> int:
    """Derive entities for EVERY memory row (v10 migration backfill).

    Includes tombstoned rows: ``--as-of`` recall reaches historical rows via
    the entity lane's temporal predicate, so history must carry its links.
    Rowid-paginated with a commit per batch (the v8 content_norm backfill
    pattern) so a large store never holds one long exclusive lock. The
    version bump happens in the caller AFTER this returns (crash → re-run;
    INSERT OR IGNORE makes the re-run a no-op).
    """
    total = 0
    last_rowid = -1
    while True:
        rows = conn.execute(
            "SELECT rowid, id, content, tags, namespace FROM memory "
            "WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (last_rowid, batch_size),
        ).fetchall()
        if not rows:
            return total
        for r in rows:
            last_rowid = r["rowid"]
            link_memory_entities(
                conn, r["id"],
                content=r["content"], tags=r["tags"], namespace=r["namespace"],
            )
            total += 1
        conn.commit()


def entities_for_memories(
    conn: sqlite3.Connection, memory_ids: list[str]
) -> dict[str, list[dict]]:
    """Batched entity cards keyed by memory id: {mid: [{id, kind, name}]}.

    Ordered by canonical_name so recall JSON and `get --json` are
    deterministic across platforms.
    """
    if not memory_ids:
        return {}
    ph = ",".join("?" * len(memory_ids))
    rows = conn.execute(
        f"SELECT me.memory_id AS mid, e.id AS eid, e.kind AS kind, "
        f"e.canonical_name AS name FROM memory_entity me "
        f"JOIN entity e ON e.id = me.entity_id "
        f"WHERE me.memory_id IN ({ph}) ORDER BY me.memory_id, e.canonical_name",
        list(memory_ids),
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["mid"], []).append(
            {"id": r["eid"], "kind": r["kind"], "name": r["name"]}
        )
    return out


def entities_for_memory(conn: sqlite3.Connection, memory_id: str) -> list[dict]:
    return entities_for_memories(conn, [memory_id]).get(memory_id, [])


def entity_match_ids(
    conn: sqlite3.Connection,
    query: str,
    *,
    ns_list: list[str] | None = None,
    as_of: str | None = None,
    limit: int = 5,
    matched_kinds: dict[str, set[str]] | None = None,
    prefer_cards: bool = True,
) -> tuple[list[str], dict[str, float]]:
    """The entity lane of recall (issue #60, 5.3): match the QUERY against
    stored aliases and return live memory ids ranked for RRF fusion.

    Issue #176: when ``matched_kinds`` is a dict, it is filled with
    ``{memory_id: {kinds of the QUERY-MATCHED entities}}`` — the basis the
    per-kind entity-lane weights are reduced over (only the evidence that
    fired is weighted; the in-place fill keeps the historical 2-tuple
    return shape for every existing caller).

    Matching uses the SAME extractor as the write path (backtick / CamelCase /
    explicit ``entity:`` forms in the query) PLUS plain query tokens matched
    read-only against EXISTING alias_norms — no writes, no new entities. This
    is what makes ``rg`` recall ``ripgrep``-linked memories once the alias
    exists (created at write time or moved by ``entity-merge``).

    Ranking (the issue's spec): number of matched entities DESC, then
    recency (ingestion_ts) DESC. Liveness mirrors the other lanes exactly —
    the shared ``_as_of_temporal_predicate`` (valid_from inclusive,
    valid_until exclusive) with ``superseded_at IS NULL`` dropped under
    ``as_of``; namespace filter is this tier's expanded alias set.

    Returns ``(ranked_ids, relevance_map)`` where relevance_map is
    matched_entities / total_matched_query_entities in [0, 1] — the relevance
    proxy the tier's composite scoring uses for entity-only rows (they have
    neither an FTS rank nor a vec similarity).

    Issue #179: with ``prefer_cards=True`` (the default), a live card of a
    matched entity (type entity_card, source_ref=card:<entity_id>) is
    promoted to the HEAD of the ranked list under the tier's own namespace
    and as_of predicates, entering the entity arm with that entity's
    relevance. A store with no card rows keeps a byte-identical result.

    Unknown alias ⇒ empty list (the other lanes fuse unchanged, no crash).
    The result window is ``max(50, limit*10)``: bounded fetch work, while the
    RRF contribution of ranks beyond ~50 is under 1% of the head
    (1/(60+1) vs 1/(60+51)).
    """
    cand_norms: set[str] = set()
    for _kind, name in extract_entities(query):
        cand_norms.add(_normalize_content(name))
    for tok in re.split(r"[\s,;]+", query or ""):
        if _eligible_name(tok):
            cand_norms.add(_normalize_content(tok))
    if not cand_norms:
        return [], {}

    norms = sorted(cand_norms)  # deterministic IN-list order
    aph = ",".join("?" * len(norms))
    try:
        eids = [
            r[0] for r in conn.execute(
                f"SELECT entity_id FROM entity_alias WHERE alias_norm IN ({aph})",
                norms,
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        if matched_kinds is not None:
            matched_kinds.clear()  # fail-open leaves the fill dict empty
        return [], {}  # pre-v10 store edge (tables absent) — fail open
    if not eids:
        return [], {}

    eph = ",".join("?" * len(eids))
    params: list = list(eids)
    ns_clause = ""
    if ns_list:
        ns_ph = ",".join("?" * len(ns_list))
        ns_clause = f"AND m.namespace IN ({ns_ph})"
        params.extend(ns_list)
    as_of_clause, as_of_params = _as_of_temporal_predicate(as_of, alias="m")
    params.extend(as_of_params)
    live_clause = "" if as_of else "AND m.superseded_at IS NULL"
    params.append(max(50, limit * 10))
    try:
        rows = conn.execute(
            f"SELECT me.memory_id AS mid, COUNT(DISTINCT me.entity_id) AS n, "
            f"MAX(m.ingestion_ts) AS latest, "
            f"GROUP_CONCAT(DISTINCT e.kind) AS matched_kind_list "
            f"FROM memory_entity me JOIN memory m ON m.id = me.memory_id "
            f"JOIN entity e ON e.id = me.entity_id "
            f"WHERE me.entity_id IN ({eph}) {ns_clause} {live_clause} "
            f"{as_of_clause} "
            f"GROUP BY me.memory_id ORDER BY n DESC, latest DESC, mid LIMIT ?",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        if matched_kinds is not None:
            matched_kinds.clear()  # fail-open leaves the fill dict empty
        return [], {}
    if matched_kinds is not None:
        # Issue #176: kinds are [_a-z0-9] tokens, so ',' is a safe split.
        matched_kinds.clear()
        for r in rows:
            matched_kinds[r["mid"]] = {
                k for k in (r["matched_kind_list"] or "").split(",") if k
            }
    total = float(len(eids))
    rel_map = {r["mid"]: min(1.0, r["n"] / total) if total else 0.0 for r in rows}
    ranked = [r["mid"] for r in rows]
    if prefer_cards and eids:
        # Issue #179: promote the matched entities' live cards to the head
        # of the entity arm under this tier's own namespace/as_of
        # predicates, so the card (not its represented sources) leads the
        # arm. The cross-tier suppression of the represented rows happens at
        # recall's merge (recall.py) — per-tier placement is impossible
        # because the card lives in user:global while its sources live in
        # project fences.
        card_ids = _live_card_ids_for_entities(
            conn, eids, ns_list=ns_list, as_of=as_of)
        if card_ids:
            card_set = set(card_ids)
            # The card enters with that entity's relevance: matched-entity
            # count / total matched query entities, the same reduction the
            # natural ranking applies.
            cph = ",".join("?" * len(card_ids))
            eph = ",".join("?" * len(eids))
            try:
                counts = {
                    r[0]: r[1] for r in conn.execute(
                        f"SELECT me.memory_id, COUNT(DISTINCT me.entity_id) "
                        f"FROM memory_entity me WHERE me.memory_id IN ({cph}) "
                        f"AND me.entity_id IN ({eph}) GROUP BY me.memory_id",
                        [*card_ids, *eids],
                    ).fetchall()
                }
            except sqlite3.OperationalError:
                counts = {}
            for cid in card_ids:
                rel_map[cid] = (
                    min(1.0, counts.get(cid, 1) / total) if total else 0.0)
            ranked = card_ids + [m for m in ranked if m not in card_set]
    return ranked, rel_map


def _live_card_ids_for_entities(
    conn: sqlite3.Connection,
    eids: list[str],
    *,
    ns_list: list[str] | None,
    as_of: str | None,
) -> list[str]:
    """Live card ids for the given entity ids under ns/as_of predicates
    (deterministic id ASC order). Fail-open to [] on a pre-v10 edge."""
    refs = [card_source_ref(e) for e in eids]
    rph = ",".join("?" * len(refs))
    params: list = list(refs)
    ns_clause = ""
    if ns_list:
        ns_ph = ",".join("?" * len(ns_list))
        ns_clause = f"AND m.namespace IN ({ns_ph})"
        params.extend(ns_list)
    as_of_clause, as_of_params = _as_of_temporal_predicate(as_of, alias="m")
    params.extend(as_of_params)
    live_clause = "" if as_of else "AND m.superseded_at IS NULL"
    try:
        return [
            r[0] for r in conn.execute(
                f"SELECT m.id FROM memory m "
                f"WHERE m.type='entity_card' AND m.source_ref IN ({rph}) "
                f"{ns_clause} {live_clause} {as_of_clause} "
                f"ORDER BY m.id",
                params,
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        return []  # fail open: no cards, the arm fuses unchanged


def cmd_entity_list(
    conn: sqlite3.Connection, kind: str | None = None, as_json: bool = False
) -> int:
    """`store.py entity-list [--kind K] [--json]` — inspect the entity tables.

    The issue ships this command so doctor-style inspection and humans can
    see what the extractor minted (kind, canonical name, aliases, link
    counts) without raw SQL.
    """
    import json as _json

    where = "WHERE e.kind=?" if kind else ""
    params: list = [kind] if kind else []
    rows = conn.execute(
        f"SELECT e.id AS id, e.kind AS kind, e.canonical_name AS name, "
        f"(SELECT COUNT(*) FROM entity_alias a WHERE a.entity_id = e.id) AS n_aliases, "
        f"(SELECT COUNT(*) FROM memory_entity me WHERE me.entity_id = e.id) AS n_links "
        f"FROM entity e {where} ORDER BY e.kind, e.canonical_name",
        params,
    ).fetchall()
    items = []
    for r in rows:
        aliases = [
            a[0] for a in conn.execute(
                "SELECT alias_norm FROM entity_alias WHERE entity_id=? "
                "ORDER BY alias_norm",
                (r["id"],),
            ).fetchall()
        ]
        items.append({
            "id": r["id"], "kind": r["kind"], "name": r["name"],
            "aliases": aliases, "links": r["n_links"],
        })
    if as_json:
        print(_json.dumps(items, indent=2))
        return 0
    if not items:
        print("[zmem] (no entities)")
        return 0
    for it in items:
        alias_note = f" aliases[{','.join(it['aliases'])}]" if it["aliases"] else ""
        print(f"[{it['id']}] kind={it['kind']} links={it['links']} :: "
              f"{it['name']}{alias_note}")
    return 0


def cmd_entity_merge(
    conn: sqlite3.Connection,
    from_id: str,
    to_id: str,
    confirm: bool = False,
) -> int:
    """`store.py entity-merge --from ID --to ID [--confirm]` (issue #60, 5.6).

    DRY RUN by default: without ``--confirm`` nothing is written — the plan
    (alias/link moves, collisions, the deletion) is printed instead. With
    ``--confirm``: the from-entity's aliases and memory links move to the
    to-entity (INSERT OR IGNORE — a target that already has the alias/link
    keeps it; collisions are counted, never overwritten), the from-entity is
    deleted, and the to-entity's updated_at is touched.

    Refused (exit 2, nothing written): unknown id on either side, from == to,
    or a kind mismatch (merging a `person` into a `tool` would silently
    re-classify history). Manual person→person merges ARE allowed with
    ``--confirm`` — the issue forbids AUTO-merging people, not the operator.
    """
    frm = conn.execute("SELECT * FROM entity WHERE id=?", (from_id,)).fetchone()
    if frm is None:
        print(f"[zmem] entity-merge: no entity with id {from_id}", file=sys.stderr)
        return 2
    to = conn.execute("SELECT * FROM entity WHERE id=?", (to_id,)).fetchone()
    if to is None:
        print(f"[zmem] entity-merge: no entity with id {to_id}", file=sys.stderr)
        return 2
    if from_id == to_id:
        print("[zmem] entity-merge: --from and --to are the same entity "
              "(no-op merge refused)", file=sys.stderr)
        return 2
    if frm["kind"] != to["kind"]:
        print(f"[zmem] entity-merge: refusing to merge kind '{frm['kind']}' "
              f"into kind '{to['kind']}' - an entity's kind never changes "
              "silently", file=sys.stderr)
        return 2

    from_aliases = [r[0] for r in conn.execute(
        "SELECT alias_norm FROM entity_alias WHERE entity_id=? ORDER BY alias_norm",
        (from_id,),
    ).fetchall()]
    to_alias_set = {
        r[0] for r in conn.execute(
            "SELECT alias_norm FROM entity_alias WHERE entity_id=?", (to_id,)
        ).fetchall()
    }
    from_links = [r[0] for r in conn.execute(
        "SELECT memory_id FROM memory_entity WHERE entity_id=?", (from_id,)
    ).fetchall()]
    to_link_set = {
        r[0] for r in conn.execute(
            "SELECT memory_id FROM memory_entity WHERE entity_id=?", (to_id,)
        ).fetchall()
    }
    alias_collisions = [a for a in from_aliases if a in to_alias_set]
    link_collisions = [m for m in from_links if m in to_link_set]

    if not confirm:
        # NOTE: runtime prints stay ASCII-only (unlike docstrings): stdout may
        # be a legacy codepage console (cp1252/cp932), and an unencodable
        # char here would crash the command after the work is done.
        print("[zmem] entity-merge DRY RUN (no writes - pass --confirm to apply)")
        print(f"[zmem]   from: {from_id} kind={frm['kind']} name={frm['canonical_name']!r}")
        print(f"[zmem]   to:   {to_id} kind={to['kind']} name={to['canonical_name']!r}")
        print(f"[zmem]   aliases to move: {len(from_aliases) - len(alias_collisions)} "
              f"({len(alias_collisions)} already on target"
              + (f": {', '.join(alias_collisions)}" if alias_collisions else "") + ")")
        print(f"[zmem]   links to move: {len(from_links) - len(link_collisions)} "
              f"({len(link_collisions)} memories already linked to target)")
        print(f"[zmem]   result: from-entity deleted; target keeps kind "
              f"'{to['kind']}' and gains its aliases/links")
        return 0

    started_tx = False
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_tx = True
        # ORDER MATTERS for aliases: alias_norm is GLOBALLY unique, so an
        # INSERT ... SELECT from this same table would hit the unique index
        # while the source row still exists, and INSERT OR IGNORE would
        # silently DROP the alias (verified in isolation: changes() == 0),
        # after which the DELETE below loses it forever. Delete the source
        # aliases FIRST, then re-insert them on the target from the
        # collected list (collisions with the target are skipped + counted).
        conn.execute("DELETE FROM entity_alias WHERE entity_id=?", (from_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO entity_alias(entity_id, alias_norm) "
            "VALUES (?,?)",
            [(to_id, a) for a in from_aliases],
        )
        # Links are keyed (memory_id, entity_id): a moved link can only
        # collide with an EXISTING target link (skipped by OR IGNORE,
        # counted above), never with the source rows being deleted below,
        # so INSERT ... SELECT then DELETE is safe here.
        conn.execute(
            "INSERT OR IGNORE INTO memory_entity(memory_id, entity_id, role) "
            "SELECT memory_id, ?, role FROM memory_entity WHERE entity_id=?",
            (to_id, from_id),
        )
        conn.execute("DELETE FROM memory_entity WHERE entity_id=?", (from_id,))
        conn.execute("DELETE FROM entity WHERE id=?", (from_id,))
        conn.execute("UPDATE entity SET updated_at=? WHERE id=?", (now_iso(), to_id))
        if started_tx:
            conn.commit()
    except Exception:
        if started_tx and conn.in_transaction:
            conn.rollback()
        raise
    print(f"[zmem] entity-merge: merged {from_id} into {to_id} "
          f"(moved {len(from_aliases) - len(alias_collisions)} alias(es), "
          f"{len(from_links) - len(link_collisions)} link(s); "
          f"{len(alias_collisions)} alias collision(s) kept on target)")
    return 0


def cmd_reclassify_entities(
    conn: sqlite3.Connection, *, apply_mode: bool, as_json: bool = False
) -> int:
    """`store.py reclassify-entities --dry-run|--apply [--json]` (issue #176).

    The CLI dispatch (cli.py) owns the connection policy: dry-run arrives on
    a strict read-only connection acquired BEFORE the shared store lifecycle;
    apply arrives on an rw connection under the writer lease. This function
    only runs the reclassifier and renders the result.

    --json prints the canonical envelope — exactly
    ``{"scanned","changed","before_other_link_share",
    "after_other_link_share","changes"}`` (no ``dry_run`` key) — as LF-only
    bytes through ``sys.stdout.buffer`` so dry-run and apply print IDENTICAL
    bytes on every platform (a Windows text-mode print would emit CRLF and
    break the committed-fixture byte contract). Human output prints one line
    per proposed change followed by ``scanned=N changed=N``.
    """
    result = reclassify_entities(conn, dry_run=not apply_mode)
    envelope = {k: v for k, v in result.items() if k != "dry_run"}
    if as_json:
        payload = json.dumps(envelope, indent=2, sort_keys=True) + "\n"
        sys.stdout.buffer.write(payload.encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    for c in envelope["changes"]:
        print(f"[zmem] reclassify: {c['id']} {c['name']!r} "
              f"other -> {c['new_kind']}")
    print(f"scanned={envelope['scanned']} changed={envelope['changed']}")
    return 0


# --- Issue #179 (Workstream K PR 8): entity cards ---------------------------
#
# One deterministic card per qualifying entity: a live `entity_card` memory
# row in user:global with source_ref=card:<entity_id>, signal=none, and the
# reserved entity_card_sha256=<hex> watermark tag (SHA-256 over the card's
# exact UTF-8 content bytes). Cards are CARD-REFRESH-ONLY: they are born
# through refresh_entity_card (the entity-card CLI and the organize cadence
# sweep) and through nothing else — every interactive add surface refuses
# the type, and every ordinary write refuses the watermark tag.

_CARD_TEXT_WS_RE = re.compile(r"\s+")


def _card_env_int(name: str, default: int) -> int:
    """Read one card env var as an integer >= 1 (absent -> default).

    A present empty string, non-integer, zero, or negative value raises
    ValueError naming the variable and the required lower bound, BEFORE any
    card write starts (the caller resolves configuration first).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int((raw or "").strip())
    except ValueError:
        value = None
    if value is None or value < 1:
        raise ValueError(
            f"{name} must be an integer >= 1 (got {raw!r}); the card sweep "
            "refuses to start a write with an invalid configuration"
        )
    return value


def card_config_from_env() -> tuple[int, int]:
    """(min_mentions, top_n) from ZMEM_CARD_MIN_MENTIONS / ZMEM_CARD_TOP_N.

    Defaults 5 and 8. An absent variable selects its default; an invalid
    value raises before any card write starts.
    """
    return (
        _card_env_int(CARD_MIN_MENTIONS_ENV, CARD_MIN_MENTIONS_DEFAULT),
        _card_env_int(CARD_TOP_N_ENV, CARD_TOP_N_DEFAULT),
    )


def card_source_ref(entity_id: str) -> str:
    """The card row's structural source_ref: ``card:<entity_id>``."""
    return "card:" + entity_id


def card_watermark_tag(content: str) -> str:
    """The reserved card tag: SHA-256 over the card's exact UTF-8 bytes."""
    return ENTITY_CARD_WATERMARK_PREFIX + hashlib.sha256(
        content.encode("utf-8")).hexdigest()


def _tag_tokens(tags: str | None) -> set[str]:
    return {t.strip() for t in (tags or "").split(",") if t.strip()}


def card_matches_content(live: sqlite3.Row, content: str) -> bool:
    """The SHA-256 no-op predicate: True when the live card row carries
    exactly ``content`` bytes AND its reserved watermark tag matches the
    recomputed digest (identical content + watermark -> no SQL write)."""
    return (live["content"] == content
            and card_watermark_tag(content) in _tag_tokens(live["tags"]))


def live_card_row(
    conn: sqlite3.Connection, entity_id: str
) -> sqlite3.Row | None:
    """The live entity_card row for entity_id (source_ref == card:<id>).

    Liveness is the same law every lane uses: ``superseded_at IS NULL``. A
    tombstoned card remains queryable history and never matches here.
    """
    return conn.execute(
        "SELECT id, type, content, tags, ingestion_ts FROM memory "
        "WHERE type='entity_card' AND source_ref=? AND superseded_at IS NULL "
        "ORDER BY ingestion_ts DESC, id DESC LIMIT 1",
        (card_source_ref(entity_id),),
    ).fetchone()


def _card_mention_line(r) -> str:
    """One ``Mentions:`` bullet from a selected source row: score (six
    decimals), last_seen, id, source_ref, and the whitespace-collapsed
    single-line text (CR, LF, and every whitespace run become one ASCII
    space, then stripped)."""
    text = _CARD_TEXT_WS_RE.sub(" ", r["content"] or "").strip()
    return (f"- score={r['conf']:.6f} last_seen={r['ts']} id={r['mid']} "
            f"source_ref={r['ref'] or ''} text={text}")


def build_entity_card_text(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    min_mentions: int,
    top_n: int,
) -> tuple[str, list[str], str] | None:
    """Deterministically render entity_id's card.

    PR #385 review fixed-in-passing (RB-05/RB-06, 2026-10-18): the two knob
    bounds are validated HERE as well — ``min_mentions >= 1`` and
    ``top_n >= 1`` raise ValueError. The config path (card_config_from_env)
    already guarantees both, but a direct library caller bypassing it must
    not reach the selection SQL with a zero/negative bound (a ``top_n`` of 0
    would render a card with zero mentions and zero provenance rows).

    Selects live NON-card rows (superseded_at IS NULL; a row with a
    non-empty valid_until is NOT live) linked to entity_id across ALL
    namespaces, orders them confidence DESC (NULL confidence reads as 0.0),
    ingestion_ts DESC, memory id ASC, returns None when the live count is
    below min_mentions (inclusive), and renders the first top_n rows.

    Rendering (UTF-8, LF-terminated), fixed field order::

        Entity: <canonical_name>
        Kind: <kind>
        Aliases: <alias_norm_1>, <alias_norm_2>
        Mentions:
        - score=<six decimals> last_seen=<ts> id=<id> source_ref=<ref> text=<single-line>
        Last seen: <max ingestion_ts over EVERY live non-card source row>
        Sources:
        - <memory_id>

    Aliases are the lexicographically sorted alias_norm values. Mention
    content replaces CR, LF, and every whitespace run with one ASCII space,
    then strips. Returns ``(text, selected_memory_ids_in_render_order,
    taint)`` where ``taint`` is the WORST-OF taint over the SELECTED source
    rows (PR #385 review F-8/PRR-008: max TAINT_RANK; a NULL/empty row taint
    reads as its schema default trusted_internal, an unknown value degrades
    fail-closed to the worst rank) — the card's aggregated content is only
    as trustworthy as its least-trustworthy contributor, so an
    ``untrusted_web`` mention makes the card itself untrusted and the
    passive untrusted fence withholds the card exactly as it withholds the
    source row it aggregates.
    """
    if min_mentions < 1:
        raise ValueError(
            f"min_mentions must be >= 1 (got {min_mentions!r}); refusing to "
            "build an entity card with an invalid threshold")
    if top_n < 1:
        raise ValueError(
            f"top_n must be >= 1 (got {top_n!r}); refusing to build an "
            "entity card with an invalid provenance cap")
    ent = conn.execute(
        "SELECT kind, canonical_name FROM entity WHERE id=?", (entity_id,)
    ).fetchone()
    if ent is None:
        return None
    rows = conn.execute(
        "SELECT m.id AS mid, m.content AS content, m.source_ref AS ref, "
        "COALESCE(m.confidence, 0.0) AS conf, m.ingestion_ts AS ts, "
        "m.taint AS taint "
        "FROM memory m JOIN memory_entity me ON me.memory_id = m.id "
        "WHERE me.entity_id = ? AND m.type != 'entity_card' "
        "AND m.superseded_at IS NULL "
        "AND (m.valid_until IS NULL OR m.valid_until = '') "
        "ORDER BY conf DESC, m.ingestion_ts DESC, m.id ASC",
        (entity_id,),
    ).fetchall()
    if len(rows) < min_mentions:
        return None
    aliases = [r[0] for r in conn.execute(
        "SELECT alias_norm FROM entity_alias WHERE entity_id=? "
        "ORDER BY alias_norm",
        (entity_id,),
    ).fetchall()]
    selected = rows[:top_n]
    lines = [
        f"Entity: {ent['canonical_name']}",
        f"Kind: {ent['kind']}",
        "Aliases: " + ", ".join(aliases),
        "Mentions:",
    ]
    # F-8: worst-of over the SELECTED rows only — the rendered Mentions/
    # Sources lines are exactly the selected set, so the taint floor covers
    # precisely the content the card aggregates (an unselected row's text is
    # absent from the card). The floor starts at the card's own
    # signal-derived default (signal=none -> untrusted_tool, the writer's
    # _default_taint_for_signal law), so a clean store's card keeps its
    # pre-F-8 taint byte-identically and an untrusted_web mention (rank 2)
    # dominates it. NULL/empty row taint reads trusted_internal (the schema
    # default); worse_taint keeps an unknown value fail-closed at the worst
    # rank.
    taint = "untrusted_tool"
    for r in selected:
        taint = worse_taint(taint, r["taint"] or "trusted_internal")
        lines.append(_card_mention_line(r))
    # [R9] Last seen spans EVERY live non-card source row linked to the
    # entity — all of them, not only the selected top_n.
    lines.append("Last seen: " + max(r["ts"] for r in rows))
    lines.append("Sources:")
    lines.extend(f"- {r['mid']}" for r in selected)
    return "\n".join(lines) + "\n", [r["mid"] for r in selected], taint


# Issue #179: the card row's explicit confidence. A signal=none row inherits
# SIGNAL_CONFIDENCE["none"] (0.2), which sits BELOW the recall CONFIDENCE_FLOOR
# (0.25) — an implicit-confidence card would be invisible to floor-filtered
# recall and the entity arm could never surface it. The summary precedent
# (organize.SUMMARY_CONFIDENCE): high enough to be recallable anywhere, low
# enough to never outrank the grounded rows it aggregates.
ENTITY_CARD_CONFIDENCE = 0.5


def refresh_entity_card(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    content: str,
    taint: str | None = None,
    now: str | None = None,
) -> tuple[str, bool]:
    """Create or replace entity_id's card; returns (card_id, changed).

    Runs INSIDE the caller's writer transaction (starts one only when the
    caller holds none). ``now`` defaults to ``schema.now_iso()`` — the
    writer clock NEVER reads ZMEM_TEST_NOW (that seam is recall-only, the
    #178 lesson); tests pass ``now="2026-09-10T00:00:00Z"``.

    - Identical content bytes AND matching watermark -> ``(existing_id,
      False)`` with NO SQL write (SHA-256 no-op detection). PR #385 review
      fixed-in-passing WD-08: the no-op path first verifies the card's
      DIRECT entity link still exists and repairs it (INSERT OR IGNORE) if
      missing before returning — a no-op must never leave the card
      unlinkable from the entity arm. PR #385 review F-6/PRR-004: the
      PRE-lock read is only that fast path; the authoritative liveness
      decision is a RE-READ inside the BEGIN IMMEDIATE (after the write
      lock is held), so a card committed by a concurrent writer between
      plan and apply is superseded, never duplicated into two live cards.
    - Otherwise: tombstone the live card (superseded_at AND valid_until both
      set — the old row stays queryable history with a non-empty
      valid_until), insert the replacement via add_memory with
      type_=entity_card, source_ref=card:<entity_id>, signal="none",
      update_of=<old_id>, force_append=True, capture_mode="manual" (the
      pinned manual mode keeps the watermark tag from being dropped by
      auto-mode redaction), and link the new row DIRECTLY to entity_id.
    - Any failure raises and rolls back the WHOLE transaction.

    ``taint`` (PR #385 review F-8/PRR-008) is the caller-passed worst-of
    source taint (build_entity_card_text computes it over the selected
    rows); add_memory validates it. The card's aggregated content is only
    as trustworthy as its least-trustworthy contributor.

    (PR #385 review fixed-in-passing RB-08: the former ``source_ids``
    parameter was dead — the provenance set is already rendered into the
    content's fixed ``Sources:`` lines by build_entity_card_text — and has
    been removed; callers that minted it from the same build no longer
    pass it.)
    """
    # Imported here (not at module scope) because write.py imports this
    # module's link helpers — a top-level import would be circular.
    from storelib.write import add_memory, supersede_memory

    ts = now if now is not None else now_iso()
    watermark = card_watermark_tag(content)
    # F-6 fast path ONLY: a pre-lock read may short-circuit the SHA no-op
    # without taking the write lock; every other decision is re-made on the
    # post-lock re-read below.
    live = live_card_row(conn, entity_id)
    if live is not None and card_matches_content(live, content):
        _ensure_direct_card_link(conn, live["id"], entity_id)
        return live["id"], False

    started_tx = False
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_tx = True
        # F-6: the AUTHORITATIVE read. Between the pre-lock read above and
        # this BEGIN IMMEDIATE a concurrent writer (another entity-card CLI
        # under the shared consolidate lock, or a cadence sweep) may have
        # committed its own card for this entity — deciding old_id on the
        # stale pre-lock row would leave that card live alongside the
        # replacement (the executed duplicate-live-card probe).
        live = live_card_row(conn, entity_id)
        if live is not None and card_matches_content(live, content):
            # Byte-identical under the lock: a no-op after all. The empty
            # transaction commits (nothing was written); WD-08 repairs the
            # direct link first.
            _ensure_direct_card_link(conn, live["id"], entity_id)
            if started_tx:
                _commit(conn)
            return live["id"], False
        old_id = live["id"] if live is not None else None
        if old_id is not None:
            supersede_memory(conn, old_id, "entity_card_refresh", at=ts)
        new_id = add_memory(
            conn,
            namespace=GLOBAL_NAMESPACE,
            type_="entity_card",
            content=content,
            tags=watermark,
            source_ref=card_source_ref(entity_id),
            signal="none",
            confidence=ENTITY_CARD_CONFIDENCE,
            capture_mode="manual",
            update_of=old_id,
            force_append=True,
            taint=taint,
            # The watermark is a structural marker like a summary's tags —
            # never evolve it onto neighbor rows (the organize-summary
            # precedent).
            link_attr_propagate=False,
            now=ts,
        )
        # Direct provenance link: the card's ONLY entity link is the entity
        # it summarizes. add_memory's deterministic extractor ran over the
        # card text (which yields nothing) and the reserved watermark tag
        # (whose token shape would otherwise mint a fresh garbage entity on
        # every content change) — drop the extractor's links for this row
        # and any entity they minted that is now linkless, then link the
        # row directly.
        auto_eids = [r[0] for r in conn.execute(
            "SELECT entity_id FROM memory_entity "
            "WHERE memory_id=? AND entity_id<>?",
            (new_id, entity_id),
        ).fetchall()]
        if auto_eids:
            conn.execute(
                "DELETE FROM memory_entity WHERE memory_id=? AND entity_id<>?",
                (new_id, entity_id),
            )
            aph = ",".join("?" * len(auto_eids))
            orphans = [r[0] for r in conn.execute(
                f"SELECT id FROM entity WHERE id IN ({aph}) AND NOT EXISTS "
                "(SELECT 1 FROM memory_entity me WHERE me.entity_id = entity.id)",
                auto_eids,
            ).fetchall()]
            for eid in orphans:
                conn.execute(
                    "DELETE FROM entity_alias WHERE entity_id=?", (eid,))
                conn.execute("DELETE FROM entity WHERE id=?", (eid,))
        conn.execute(
            "INSERT OR IGNORE INTO memory_entity(memory_id, entity_id, role) "
            "VALUES (?,?,?)",
            (new_id, entity_id, ENTITY_ROLE_DEFAULT),
        )
        if started_tx:
            _commit(conn)
        return new_id, True
    except Exception:
        if started_tx and conn.in_transaction:
            conn.rollback()
        raise


def _ensure_direct_card_link(
    conn: sqlite3.Connection, card_id: str, entity_id: str
) -> None:
    """WD-08 (PR #385 review, fixed-in-passing): the SHA no-op path must not
    return a card whose direct entity link has gone missing (a store-level
    repair or a hand-edit could have dropped it — the recall suppression
    resolves cards by the DIRECT link, so an unlinkable card would render
    unsuppressed). Re-insert the link when absent; INSERT OR IGNORE keeps an
    existing link untouched. On the pre-lock fast path no transaction is
    open, so the repair commits its own tiny transaction; inside a caller's
    open transaction the insert joins it."""
    started = not conn.in_transaction
    conn.execute(
        "INSERT OR IGNORE INTO memory_entity(memory_id, entity_id, role) "
        "VALUES (?,?,?)",
        (card_id, entity_id, ENTITY_ROLE_DEFAULT),
    )
    if started and conn.in_transaction:
        conn.commit()


def resolve_entity(conn: sqlite3.Connection, spec: str) -> str:
    """Resolve an entity ID or canonical name to exactly one entity id.

    Exact entity id first; then exact canonical name. Zero matches and
    multiple canonical-name matches raise ValueError (the CLI maps them to
    exit 2 before any write).
    """
    row = conn.execute("SELECT id FROM entity WHERE id=?", (spec,)).fetchone()
    if row is not None:
        return row[0]
    rows = conn.execute(
        "SELECT id FROM entity WHERE canonical_name=? ORDER BY id", (spec,)
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        raise ValueError(
            f"no entity with id or canonical name {spec!r}")
    raise ValueError(
        f"canonical name {spec!r} matches {len(rows)} entities "
        f"({', '.join(r[0] for r in rows)}); pass the entity ID")


def cmd_entity_card(
    conn: sqlite3.Connection, *, action: str, entity: str
) -> dict[str, object]:
    """`store.py entity-card build|refresh ENTITY` (issue #179).

    Both actions resolve configuration (card_config_from_env) and the
    entity (id first, then canonical name) BEFORE any write — an invalid
    configuration or an unresolvable/ambiguous entity raises ValueError and
    the CLI exits 2 with the store bytes unchanged. One ``now_iso()`` clock
    is resolved per command and passed to every write (WD-10, PR #385
    review fixed-in-passing: the below-threshold tombstone and any card
    refresh share a single timestamp instead of straddling two internal
    ``now_iso()`` reads). Returns the result object with the EXACT key
    order ``result, id, entity_id, source_ref, warnings`` (id/source_ref
    are None for ``no_card``).
    """
    min_mentions, top_n = card_config_from_env()
    entity_id = resolve_entity(conn, entity)
    ts = now_iso()
    live = live_card_row(conn, entity_id)
    built = build_entity_card_text(
        conn, entity_id=entity_id, min_mentions=min_mentions, top_n=top_n)
    warnings: list = []
    ref = card_source_ref(entity_id)
    if action == "build":
        if live is not None:
            raise ValueError(
                f"entity {entity_id} already has a live card ({live['id']}); "
                "build refuses a duplicate (use refresh)")
        if built is None:
            return {"result": "no_card", "id": None, "entity_id": entity_id,
                    "source_ref": None, "warnings": warnings}
        content, _source_ids, taint = built
        new_id, _changed = refresh_entity_card(
            conn, entity_id=entity_id, content=content, taint=taint, now=ts)
        return {"result": "created", "id": new_id, "entity_id": entity_id,
                "source_ref": ref, "warnings": warnings}
    if action == "refresh":
        if built is None:
            if live is not None:
                # Below-threshold refresh tombstones the previously live
                # card, then reports no_card.
                from storelib.write import supersede_memory
                supersede_memory(
                    conn, live["id"], "entity_card_below_threshold", at=ts)
            return {"result": "no_card", "id": None, "entity_id": entity_id,
                    "source_ref": None, "warnings": warnings}
        content, _source_ids, taint = built
        new_id, changed = refresh_entity_card(
            conn, entity_id=entity_id, content=content, taint=taint, now=ts)
        if not changed:
            result = "unchanged"
        elif live is not None:
            result = "updated"
        else:
            result = "created"
        return {"result": result, "id": new_id, "entity_id": entity_id,
                "source_ref": ref, "warnings": warnings}
    raise ValueError(
        f"unknown entity-card action {action!r} (expected build or refresh)")
