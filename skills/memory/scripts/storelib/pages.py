"""Filesystem-backed curated pages (issue #138).

Pages are derived, untrusted artifacts.  They are intentionally never written
to ``memory``: a committed version contains the complete text and grounding so
reads can stay SQLite-free and historical views stay immutable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


_PAGE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_VERSION_ID = re.compile(r"^v[0-9]{6}$")
_SECTION = re.compile(r"<!-- section:([A-Za-z0-9._-]+) -->\n?(.*?)<!-- end-section:\1 -->", re.S)
_FENCE_MARKERS = ("<<<ZMEM_UNTRUSTED_FENCE>>>", "<<<END_ZMEM_UNTRUSTED_FENCE>>>")
_MAX_ADAPTER_CONTENT = 400
_MAX_MARKDOWN = 4000
_MAX_ARTIFACT_BYTES = 262144
_MAX_ADAPTER_OPERATIONS = 20
_MAX_OPERATION_CITATIONS = 20


class PageError(ValueError):
    """A controlled page refusal; callers must leave the old commit intact."""


def _json_bytes(value: object) -> bytes:
    try:
        return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PageError("invalid page artifact") from exc


def _bounded_json(value: object) -> bytes:
    """Serialize an artifact exactly as readers will see it, before publishing."""
    raw = _json_bytes(value)
    if len(raw) > _MAX_ARTIFACT_BYTES:
        raise PageError("page artifact exceeds bound")
    return raw


def _validate_id(page_id: str) -> str:
    if (not isinstance(page_id, str) or page_id in ("", ".", "..")
            or not _PAGE_ID.fullmatch(page_id)):
        raise PageError("invalid page id")
    return page_id


def _root(data_dir: str, *, create: bool = False) -> Path:
    if not isinstance(data_dir, str) or not data_dir:
        raise PageError("invalid data directory")
    root = Path(data_dir).expanduser()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    try:
        resolved = root.resolve(strict=False)
    except OSError as exc:
        raise PageError("unsafe data directory") from exc
    if root.exists() and (_is_reparse(root) or not root.is_dir()):
        raise PageError("unsafe data directory")
    return resolved


def _page_dir(data_dir: str, page_id: str, *, create: bool = False) -> Path:
    page_id = _validate_id(page_id)
    base = _root(data_dir, create=create)
    pages = base / "pages"
    if create:
        pages.mkdir(parents=True, exist_ok=True)
    if pages.exists() and (_is_reparse(pages) or not pages.is_dir()):
        raise PageError("unsafe page directory")
    candidate = pages / page_id
    if candidate.exists() and (_is_reparse(candidate) or not candidate.is_dir()):
        raise PageError("unsafe page directory")
    try:
        resolved = candidate.resolve(strict=False)
        if os.path.commonpath((str(base), str(resolved))) != str(base):
            raise PageError("unsafe page path")
    except (OSError, ValueError) as exc:
        if isinstance(exc, PageError):
            raise
        raise PageError("unsafe page path") from exc
    return candidate


def _regular(path: Path, *, required: bool = True) -> None:
    # ``Path.exists`` follows links and reports False for a dangling symlink.
    # Readers and publishers must reject that link rather than treating it as
    # an absent artifact and later replacing or reading through it.
    if not os.path.lexists(str(path)):
        if required:
            raise PageError("page artifact not found")
        return
    if _is_reparse(path) or not path.is_file():
        raise PageError("unsafe page artifact")


def _is_reparse(path: Path) -> bool:
    """Reject symlinks and Windows junction/reparse points at every boundary."""
    try:
        attrs = path.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        attrs = 0
    return path.is_symlink() or bool(attrs & getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _contained(base: Path, path: Path) -> None:
    try:
        base_resolved = base.resolve(strict=False)
        cursor = path
        while cursor != base.parent:
            if cursor.exists() and _is_reparse(cursor):
                raise PageError("unsafe page path")
            if cursor == base:
                break
            cursor = cursor.parent
        if os.path.commonpath((str(base_resolved), str(path.resolve(strict=False)))) != str(base_resolved):
            raise PageError("unsafe page path")
    except (OSError, ValueError) as exc:
        if isinstance(exc, PageError):
            raise
        raise PageError("unsafe page path") from exc


def _load_json(path: Path) -> dict:
    _regular(path)
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_ARTIFACT_BYTES:
            raise PageError("page artifact exceeds bound")
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PageError("invalid page artifact") from exc
    if not isinstance(data, dict):
        raise PageError("invalid page artifact")
    return data


def _read_committed(directory: Path, version_id: str | None = None) -> tuple[dict, dict, bytes]:
    _contained(directory.parent.parent, directory)
    definition = _load_json(directory / "definition.json")
    current = _load_json(directory / "current.json")
    if set(definition) != {"namespace", "query", "tags", "generator_revision", "creation_policy"} or not isinstance(definition["namespace"], str) or not isinstance(definition["query"], str) or not isinstance(definition["tags"], list):
        raise PageError("invalid page definition")
    if set(current) != {"version_id", "freshness_watermark", "source_ids", "evidence_ids", "retracted_source_ids", "page_checksum"}:
        raise PageError("invalid page pointer")
    if (not all(isinstance(current[k], list) and all(isinstance(x, str) for x in current[k])
                for k in ("source_ids", "evidence_ids", "retracted_source_ids"))
            or not isinstance(current["freshness_watermark"], str)
            or not isinstance(current["page_checksum"], str)):
        raise PageError("invalid page pointer")
    selected = version_id or current.get("version_id")
    if not isinstance(selected, str) or not _VERSION_ID.fullmatch(selected):
        raise PageError("invalid page version")
    version_path = directory / "versions" / (selected + ".json")
    _contained(directory, version_path)
    version = _load_json(version_path)
    content = version.get("content")
    if not isinstance(content, str):
        raise PageError("invalid page version")
    raw = content.encode("utf-8")
    checksum = hashlib.sha256(raw).hexdigest()
    required_version = {"version_id", "freshness_watermark", "source_ids", "evidence_ids", "retracted_source_ids", "page_checksum", "content"}
    if (not required_version.issubset(version) or set(version) - (required_version | {"bullet_sources"})
            or version.get("version_id") != selected or version.get("page_checksum") != checksum):
        raise PageError("page checksum mismatch")
    for key in ("freshness_watermark", "source_ids", "evidence_ids", "retracted_source_ids", "page_checksum"):
        if version.get(key) != current.get(key) and version_id is None:
            raise PageError("page pointer metadata mismatch")
    # A historical version is self-contained.  The pointer validates only its
    # selected current version, so old history keeps working after retractions.
    if version_id is None:
        if current.get("version_id") != selected or current.get("page_checksum") != checksum:
            raise PageError("page commit is invalid")
    return definition, version, raw


def page_read(*, data_dir: str, page_id: str, version_id: str | None = None) -> dict:
    """Read one immutable committed version without opening SQLite."""
    directory = _page_dir(data_dir, page_id)
    definition, version, raw = _read_committed(directory, version_id)
    result = dict(version)
    result["content"] = raw.decode("utf-8")
    result["namespace"] = definition.get("namespace")
    return result


def page_list(*, data_dir: str, namespace: str | None = None) -> list[dict]:
    """List valid committed metadata only; corrupt entries are withheld."""
    base = _root(data_dir)
    pages = base / "pages"
    if not pages.exists():
        return []
    if _is_reparse(pages) or not pages.is_dir():
        raise PageError("unsafe pages directory")
    results: list[dict] = []
    for entry in sorted(pages.iterdir(), key=lambda p: p.name):
        try:
            if _is_reparse(entry) or not entry.is_dir() or not _PAGE_ID.fullmatch(entry.name):
                continue
            definition, version, _raw = _read_committed(entry)
            if namespace is not None and definition.get("namespace") != namespace:
                continue
            item = {k: version.get(k) for k in ("version_id", "freshness_watermark",
                    "source_ids", "evidence_ids", "retracted_source_ids", "page_checksum")}
            item["id"] = entry.name
            item["namespace"] = definition.get("namespace")
            item["query"] = definition.get("query")
            item["tags"] = definition.get("tags")
            results.append(item)
        except PageError:
            continue
    return results


def page_for_injection(*, data_dir: str, page_id: str) -> dict:
    """Return the deliberately closed derived candidate adapter."""
    directory = _page_dir(data_dir, page_id)
    definition, version, raw = _read_committed(directory)
    vid = version["version_id"]
    return {
        "id": "page:%s:%s" % (page_id, vid), "namespace": definition.get("namespace", ""),
        "type": "page", "content": raw.decode("utf-8"),
        "source_ref": "page:%s:%s" % (page_id, vid),
        "source_ids": list(version.get("source_ids") or []),
        "evidence_ids": list(version.get("evidence_ids") or []),
        "version_id": vid, "freshness_watermark": version.get("freshness_watermark", ""),
        "page_checksum": version.get("page_checksum", ""),
    }


def _source_rows(conn: sqlite3.Connection, *, query: str, namespace: str,
                 tags: tuple[str, ...], as_of: str | None,
                 data_dir: str | None = None, now: str | None = None) -> list[dict]:
    """Read the bounded canonical snapshot with evidence as a hard floor."""
    from storelib.beliefs import belief_head_rows
    from storelib.evidence import evidence_ids_for_memories
    heads = [head for head in belief_head_rows(
        conn, query=query, namespace=namespace, limit=50, as_of=as_of
    ) if head.get("head_state") == "active"]
    clauses = ["namespace=?", "superseded_at IS NULL", "(source_ref IS NULL OR source_ref NOT LIKE 'page:%')"]
    params: list[object] = [namespace]
    temporal_at = as_of or now
    if temporal_at:
        clauses.append("ingestion_ts<=?")
        params.append(temporal_at)
        clauses.append("(valid_from IS NULL OR valid_from='' OR valid_from<=?)")
        params.append(temporal_at)
        clauses.append("(valid_until IS NULL OR valid_until='' OR valid_until>?)")
        params.append(temporal_at)
    for tag in tags:
        # Tags are caller input.  Escape SQL LIKE metacharacters so a literal
        # ``_`` or ``%`` tag cannot ground a page in an unrelated source.
        literal_tag = tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(',' || replace(tags, ' ', '') || ',') LIKE ? ESCAPE '\\'")
        params.append("%," + literal_tag + ",%")
    sql = ("SELECT id,namespace,type,content,tags,source_ref,confidence,signal,taint,"
           "trust_score,ingestion_ts,valid_from,valid_until FROM memory WHERE " + " AND ".join(clauses))
    live = [dict(r) for r in conn.execute(sql, params).fetchall()]
    all_source_ids: set[str] = set()
    for h in heads:
        all_source_ids.update(x for x in h.get("source_ids", []) if isinstance(x, str))
    all_source_ids.update(str(r["id"]) for r in live)
    evidence = evidence_ids_for_memories(conn, sorted(all_source_ids)) if all_source_ids else {}
    # ``memory_evidence`` may contain a dangling endpoint in a damaged store.
    # Associations count only when their evidence row still exists.
    live_evidence: dict[str, list[str]] = {sid: [] for sid in all_source_ids}
    if all_source_ids:
        for offset in range(0, len(all_source_ids), 400):
            chunk = sorted(all_source_ids)[offset:offset + 400]
            ph = ",".join("?" for _ in chunk)
            for memory_id, evidence_id in conn.execute(
                "SELECT me.memory_id, me.evidence_id FROM memory_evidence me "
                "JOIN evidence e ON e.id=me.evidence_id WHERE me.memory_id IN (" + ph + ")",
                chunk,
            ).fetchall():
                live_evidence[str(memory_id)].append(str(evidence_id))
    if any(sorted(evidence.get(sid, [])) != sorted(live_evidence.get(sid, []))
           for sid in all_source_ids):
        raise PageError("page source has dangling evidence")
    evidence = live_evidence
    # No partially-grounded canonical source is allowed to influence a page.
    from storelib.inject import selective_inject_filter
    pages_root = (_root(data_dir, create=False) / "pages") if data_dir else None
    def page_path_ref(value: object) -> bool:
        if not isinstance(value, str) or not value.startswith("file:") or pages_root is None:
            return False
        try:
            return os.path.commonpath((str(pages_root), str(Path(value[5:]).resolve(strict=False)))) == str(pages_root)
        except (OSError, ValueError):
            return True
    if any(not evidence.get(r["id"]) for r in live):
        raise PageError("page source is missing evidence")
    live = [r for r in live if not page_path_ref(r.get("source_ref"))]
    for row in live:
        row_id = str(row["id"])
        row["evidence_ids"] = list(evidence.get(row_id, []))
        # Keep the canonical joined associations private to this publication
        # snapshot.  A row's source and evidence lists alone cannot express
        # which evidence belongs to which represented source.
        row["_source_evidence_pairs"] = [
            (row_id, evidence_id) for evidence_id in row["evidence_ids"]
        ]
    live, _status, _stats = selective_inject_filter(live, with_stats=True)
    valid_head_rows: list[dict] = []
    for head in heads:
        # Ordinary belief recall may surface a contested head with its state
        # flag.  Curated pages are a durable derived projection, so only an
        # active head may ground one.
        if head.get("head_state") != "active":
            continue
        ids = [x for x in head.get("source_ids", []) if isinstance(x, str)]
        if ids and not all(evidence.get(x) for x in ids):
            raise PageError("page belief source is missing evidence")
        if ids and not any(str(x).startswith("page:") for x in ids):
            item = dict(head)
            placeholders = ",".join("?" for _ in (ids + [item["id"]]))
            head_evidence = conn.execute(
                "SELECT bhe.source_id,bhe.evidence_id FROM belief_head_evidence bhe "
                "JOIN evidence e ON e.id=bhe.evidence_id WHERE bhe.head_id=? AND bhe.source_id IN (" + placeholders + ")",
                [item["head_id"]] + ids + [item["id"]],
            ).fetchall()
            item["evidence_ids"] = sorted({eid for sid in ids for eid in evidence.get(sid, [])}
                                            | {str(row[1]) for row in head_evidence})
            # Member associations stay exact: never infer a member/evidence
            # edge from the virtual head's union.  The virtual head itself may
            # represent its already-validated evidence union.
            member_pairs = {
                (source_id, evidence_id)
                for source_id in ids for evidence_id in evidence.get(source_id, [])
            }
            member_pairs.update((str(source_id), str(evidence_id))
                                for source_id, evidence_id in head_evidence)
            item["_source_evidence_pairs"] = sorted(
                member_pairs | {(str(item["id"]), evidence_id)
                                for evidence_id in item["evidence_ids"]})
            selected, _status, _stats = selective_inject_filter([item], with_stats=True)
            valid_head_rows.extend(selected)
    rows = valid_head_rows + live
    from storelib.schema import _parse_iso_to_epoch
    rows.sort(key=lambda r: (-float(r.get("trust_score", 0) or 0),
                             -float(r.get("confidence", 0) or 0),
                             -float(_parse_iso_to_epoch(str(r.get("ingestion_ts", ""))) or 0),
                             str(r.get("id", ""))))
    return rows


def _sections(text: str) -> dict[str, tuple[int, int, str]]:
    if text.count("<!-- section:") != text.count("<!-- end-section:"):
        raise PageError("malformed page section markers")
    found: dict[str, tuple[int, int, str]] = {}
    for match in _SECTION.finditer(text):
        ident = match.group(1)
        if ident in found:
            raise PageError("duplicate page section")
        body_start, body_end = match.start(2), match.end(2)
        body = text[body_start:body_end]
        if not body.strip():
            raise PageError("empty page section")
        found[ident] = (body_start, body_end, body)
    if not found or len(found) != text.count("<!-- section:"):
        raise PageError("missing page sections")
    return found


def _safe_text(value: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PageError("page text exceeds bound")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PageError("invalid page text") from exc
    if len(encoded) > maximum:
        raise PageError("page text exceeds bound")
    _require_no_fence_markers(value)
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _require_no_fence_markers(content: str) -> None:
    """Refuse reserved host-control literals without rewriting page bytes."""
    if any(marker in content for marker in _FENCE_MARKERS):
        raise PageError("page text contains fence marker")


def _utf8_cap(value: object, maximum: int) -> str:
    raw = str(value or "").encode("utf-8")[:maximum]
    # Trim a partial multibyte codepoint without changing the declared bound.
    return raw.decode("utf-8", "ignore")


def _bullet_record(source_ids: list[str], evidence_ids: list[str]) -> dict[str, list[str]]:
    """Canonical provenance derived from selected rows or operation citations."""
    return {"source_ids": sorted(set(source_ids)), "evidence_ids": sorted(set(evidence_ids))}


def _bullet(row: dict, evidence: list[str]) -> str:
    source_ids = sorted(set((row.get("source_ids") or []) + [str(row["id"])]))
    evidence_ids = sorted(set(evidence or row.get("evidence_ids") or []))
    digest = hashlib.sha256(_json_bytes({"source_ids": source_ids, "evidence_ids": evidence_ids})).hexdigest()[:16]
    return ("<!-- bullet:%s -->\n- %s\n  source_ids: %s\n  evidence_ids: %s\n"
            "<!-- end-bullet:%s -->" % (digest, str(row.get("content", "")).strip(),
                ", ".join(source_ids), ", ".join(evidence_ids), digest))


def _render_refresh(rows: list[dict]) -> tuple[str, list[str], list[str], dict[str, dict[str, list[str]]]]:
    source_ids: set[str] = set()
    evidence_ids: set[str] = set()
    bullets: dict[str, dict[str, list[str]]] = {}
    rendered: list[str] = []
    for row in rows:
        ids = sorted(set((row.get("source_ids") or []) + [str(row["id"])]))
        eids = sorted(set(row.get("evidence_ids") or []))
        if not ids or not eids:
            continue
        digest = hashlib.sha256(_json_bytes({"source_ids": ids, "evidence_ids": eids})).hexdigest()[:16]
        # A belief head and its single underlying tagged source can describe
        # the identical grounding. Retain one deterministic bullet so explicit
        # IDs remain globally unique without inventing a second derivation.
        if digest in bullets:
            continue
        source_ids.update(ids)
        evidence_ids.update(eids)
        rendered.append(_bullet(row, eids))
        bullets[digest] = _bullet_record(ids, eids)
    return "\n\n".join(rendered) + "\n", sorted(source_ids), sorted(evidence_ids), bullets


def _trusted_bullet_sources(value: object) -> dict[str, dict[str, list[str]]]:
    """Validate provenance written by a previous page publication.

    Page text is derived from untrusted source and adapter input.  It is never
    parsed to reconstruct grounding: only this separately-written manifest is
    carried across an adapter delta.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PageError("invalid page provenance")
    result: dict[str, dict[str, list[str]]] = {}
    for ident, record in value.items():
        if not isinstance(ident, str) or not _PAGE_ID.fullmatch(ident):
            raise PageError("invalid page provenance")
        if not isinstance(record, dict) or set(record) != {"source_ids", "evidence_ids"}:
            raise PageError("invalid page provenance")
        sources = record.get("source_ids")
        evidence = record.get("evidence_ids")
        if (not isinstance(sources, list) or not isinstance(evidence, list)
                or not sources or not evidence
                or any(not isinstance(item, str) or not item for item in sources + evidence)
                or sources != sorted(set(sources)) or evidence != sorted(set(evidence))):
            raise PageError("invalid page provenance")
        result[ident] = _bullet_record(sources, evidence)
    return result


def _require_authoritative_bullets(content: str,
                                   bullet_sources: dict[str, dict[str, list[str]]]) -> None:
    """Reject structural bullet text that has no source/citation authority."""
    bullet_ids: list[str] = []
    open_id: str | None = None
    for match in re.finditer(r"<!-- (end-)?bullet:([A-Za-z0-9._-]+) -->", content):
        is_end, ident = match.groups()
        if not is_end:
            if open_id is not None:
                raise PageError("malformed page bullet")
            open_id = ident
            bullet_ids.append(ident)
        elif open_id != ident:
            raise PageError("malformed page bullet")
        else:
            open_id = None
    if open_id is not None:
        raise PageError("malformed page bullet")
    if len(bullet_ids) != len(set(bullet_ids)) or set(bullet_ids) != set(bullet_sources):
        raise PageError("untracked page bullet")


def _require_live_bullet_authority(rows: list[dict],
                                   bullet_sources: dict[str, dict[str, list[str]]]) -> None:
    """Revalidate every retained source/evidence record against publication rows."""
    valid_pairs: set[tuple[str, str]] = set()
    for row in rows:
        pairs = row.get("_source_evidence_pairs")
        if not isinstance(pairs, (list, tuple)):
            raise PageError("page provenance changed during publication")
        for pair in pairs:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or not all(isinstance(value, str) and value for value in pair)):
                raise PageError("page provenance changed during publication")
            valid_pairs.add((pair[0], pair[1]))
    valid_sources = {source for source, _evidence_id in valid_pairs}
    valid_evidence = {evidence_id for _source, evidence_id in valid_pairs}
    for record in bullet_sources.values():
        sources = record["source_ids"]
        evidence = record["evidence_ids"]
        if (any(source not in valid_sources for source in sources)
                or any(evidence_id not in valid_evidence for evidence_id in evidence)
                or any(not any((source, evidence_id) in valid_pairs for source in sources)
                       for evidence_id in evidence)
                or any(not any((source, evidence_id) in valid_pairs for evidence_id in evidence)
                       for source in sources)):
            raise PageError("page provenance changed during publication")


def _snapshot_bytes(rows: list[dict]) -> bytes:
    """Canonical source snapshot used for revalidation and watermarking."""
    return _json_bytes([{
        "id": r.get("id"), "source_ids": sorted(r.get("source_ids") or []),
        "evidence_ids": sorted(r.get("evidence_ids") or []), "content": r.get("content"),
        "source_evidence_pairs": sorted([list(pair) for pair in r.get("_source_evidence_pairs", [])]),
        "ingestion_ts": r.get("ingestion_ts"), "trust_score": r.get("trust_score"),
        "confidence": r.get("confidence"), "signal": r.get("signal"), "taint": r.get("taint"),
    } for r in rows])


def _apply_operations(text: str, operations: Any,
                      citation_map: dict[str, tuple[list[str], list[str]]],
                      bullet_sources: dict[str, dict[str, list[str]]]) -> tuple[str, dict[str, dict[str, list[str]]]]:
    if (not isinstance(operations, list) or not operations
            or len(operations) > _MAX_ADAPTER_OPERATIONS):
        raise PageError("empty page adapter result")
    sections = _sections(text)
    result = text
    authoritative = {
        ident: _bullet_record(record.get("source_ids", []), record.get("evidence_ids", []))
        for ident, record in bullet_sources.items()
    }
    # Every operation can target only an existing marked section. Recompute
    # offsets after each replacement, preserving untouched byte slices exactly.
    for operation in operations:
        if not isinstance(operation, dict):
            raise PageError("invalid page adapter operation")
        op = operation.get("op")
        section_id = operation.get("section_id")
        citations = operation.get("citations")
        if (not isinstance(op, str) or not isinstance(section_id, str)
                or op not in {"replace_section", "append_bullet", "retract_bullet"}
                or section_id not in sections):
            raise PageError("invalid page adapter target")
        expected = ({"op", "section_id", "markdown", "citations"}
                    if op in {"replace_section", "append_bullet"}
                    else {"op", "section_id", "bullet_id", "citations"})
        if set(operation) != expected:
            raise PageError("invalid page adapter operation")
        if (not isinstance(citations, list) or not citations
                or len(citations) > _MAX_OPERATION_CITATIONS
                or any(not isinstance(c, str) or c not in citation_map for c in citations)
                or len(set(citations)) != len(citations)):
            raise PageError("unresolved page citation")
        sections = _sections(result)
        start, end, body = sections[section_id]
        if op == "replace_section":
            markdown = _safe_text(operation.get("markdown"), _MAX_MARKDOWN)
            digest = hashlib.sha256(_json_bytes({"markdown": markdown, "citations": sorted(citations)})).hexdigest()[:16]
            source_ids = sorted({sid for citation in citations for sid in citation_map[citation][0]})
            evidence_ids = sorted({eid for citation in citations for eid in citation_map[citation][1]})
            for old_id in re.findall(r"<!-- bullet:([A-Za-z0-9._-]+) -->", body):
                authoritative.pop(old_id, None)
            grounded = "<!-- bullet:%s -->\n%s\n  source_ids: %s\n  evidence_ids: %s\n<!-- end-bullet:%s -->\n" % (digest, markdown, ", ".join(source_ids), ", ".join(evidence_ids), digest)
            result = result[:start] + grounded + result[end:]
            authoritative[digest] = _bullet_record(source_ids, evidence_ids)
        elif op == "append_bullet":
            markdown = _safe_text(operation.get("markdown"), _MAX_MARKDOWN)
            suffix = "" if body.endswith("\n") else "\n"
            digest = hashlib.sha256(_json_bytes({"markdown": markdown, "citations": sorted(citations)})).hexdigest()[:16]
            source_ids = sorted({sid for citation in citations for sid in citation_map[citation][0]})
            evidence_ids = sorted({eid for citation in citations for eid in citation_map[citation][1]})
            grounded = "<!-- bullet:%s -->\n%s\n  source_ids: %s\n  evidence_ids: %s\n<!-- end-bullet:%s -->\n" % (digest, markdown, ", ".join(source_ids), ", ".join(evidence_ids), digest)
            result = result[:end] + suffix + grounded + result[end:]
            authoritative[digest] = _bullet_record(source_ids, evidence_ids)
        else:
            bullet_id = operation.get("bullet_id")
            if not isinstance(bullet_id, str):
                raise PageError("invalid page bullet")
            pattern = re.compile(r"<!-- bullet:" + re.escape(bullet_id) + r" -->.*?<!-- end-bullet:" + re.escape(bullet_id) + r" -->\n?", re.S)
            updated, count = pattern.subn("", body, count=1)
            if count != 1:
                raise PageError("unknown page bullet")
            result = result[:start] + updated + result[end:]
            authoritative.pop(bullet_id, None)
    _sections(result)
    _require_authoritative_bullets(result, authoritative)
    return result, authoritative


def _acquire_actual_maintenance(conn: sqlite3.Connection) -> tuple[Path, str]:
    from storelib import schema
    row = conn.execute("PRAGMA database_list").fetchone()
    path = str(row[2]) if row and len(row) >= 3 else ""
    if not path or path == ":memory:":
        raise PageError("page refresh requires a file-backed store")
    store = Path(path).resolve(strict=False)
    if store.name in ("", ".", "..") or store.parent == store:
        raise PageError("unsafe page store path")
    host = getattr(schema, "_host", None)
    if host is None:
        raise PageError("host lock support unavailable")
    lock = store.parent / ".zmem-maintenance.lock"
    deadline = time.time() + float(getattr(schema, "MAINTENANCE_WAIT_SECONDS", 5.0))
    while True:
        token = host.acquire_lock(lock, getattr(schema, "MAINTENANCE_LOCK_STALE_SECONDS", 1800.0))
        if token == getattr(host, "_NO_LOCK_TOKEN", "unlocked"):
            raise PageError("could not safely acquire maintenance lock")
        if token is not None:
            return lock, token
        if time.time() >= deadline:
            raise PageError("maintenance lock timed out")
        time.sleep(float(getattr(schema, "MAINTENANCE_POLL_SECONDS", 0.05)))


def _wait_for_writers(store_parent: Path) -> None:
    from storelib import schema
    deadline = time.time() + float(getattr(schema, "MAINTENANCE_WAIT_SECONDS", 5.0))
    leases = store_parent / ".zmem-writers"
    while True:
        live = []
        try:
            for lease in leases.glob("*.lease"):
                if time.time() - lease.stat().st_mtime <= getattr(schema, "WRITER_LEASE_STALE_SECONDS", 300.0):
                    live.append(lease)
        except OSError as exc:
            raise PageError("unable to inspect writer leases") from exc
        if not live:
            return
        if time.time() >= deadline:
            raise PageError("writers did not quiesce")
        time.sleep(float(getattr(schema, "MAINTENANCE_POLL_SECONDS", 0.05)))


def _atomic_write(path: Path, payload: bytes) -> Path:
    staged = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    with open(staged, "xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return staged


def _next_version(directory: Path) -> str:
    versions = directory / "versions"
    max_number = 0
    if versions.exists():
        if _is_reparse(versions) or not versions.is_dir():
            raise PageError("unsafe versions directory")
        for child in versions.iterdir():
            match = re.fullmatch(r"v([0-9]{6})\.json", child.name)
            if match and child.is_file() and not child.is_symlink():
                max_number = max(max_number, int(match.group(1)))
    return "v%06d" % (max_number + 1)


def _publish(directory: Path, definition: dict, current: dict, version: dict, content: bytes,
             *, definition_was_missing: bool) -> None:
    # These are the exact bytes that readers later cap in _load_json.  Check
    # every serialized artifact before creating a staging directory or replacing
    # an existing projection/pointer, so an oversized candidate preserves the
    # entire prior committed tree.
    definition_bytes = _bounded_json(definition)
    current_bytes = _bounded_json(current)
    version_bytes = _bounded_json(version)
    projection = directory / "page.md"
    definition_path = directory / "definition.json"
    # Validate links before taking the rollback snapshot.  ``os.replace`` is
    # safe for a later race because it replaces the directory entry itself;
    # this check prevents the initial read from following an attacker-planted
    # projection or definition link.
    _regular(projection, required=False)
    _regular(definition_path, required=False)
    versions = directory / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    stage = directory / (".staging-" + uuid.uuid4().hex)
    stage.mkdir()
    old_projection = projection.read_bytes() if os.path.lexists(str(projection)) else None
    old_definition = definition_path.read_bytes() if os.path.lexists(str(definition_path)) else None
    installed_version: Path | None = None
    committed = False
    try:
        version_temp = _atomic_write(stage / (version["version_id"] + ".json"), version_bytes)
        page_temp = _atomic_write(stage / "page.md", content)
        current_temp = _atomic_write(stage / "current.json", current_bytes)
        definition_temp = _atomic_write(stage / "definition.json", definition_bytes)
        final_version = versions / (version["version_id"] + ".json")
        if final_version.exists():
            raise PageError("page version already exists")
        # Flat immutable version, projection, then pointer LAST.  A failed
        # projection write has no new pointer and is rolled back below.
        os.replace(version_temp, final_version); installed_version = final_version
        os.replace(page_temp, projection)
        if definition_was_missing:
            os.replace(definition_temp, definition_path)
        os.replace(current_temp, directory / "current.json")
        committed = True
    except Exception as exc:
        rollback_error = None
        try:
            if old_projection is None:
                projection.unlink(missing_ok=True)
            else:
                # Never write through the replaced entry: stage the old bytes
                # locally and atomically replace the entry instead.
                os.replace(_atomic_write(stage / "rollback-page.md", old_projection), projection)
            if definition_was_missing:
                if old_definition is None:
                    definition_path.unlink(missing_ok=True)
                else:
                    os.replace(_atomic_write(stage / "rollback-definition.json", old_definition), definition_path)
            if installed_version is not None:
                installed_version.unlink(missing_ok=True)
        except Exception as rollback_exc:  # retain the valid old pointer either way
            rollback_error = rollback_exc
        if rollback_error is not None:
            raise PageError("page publication rollback failed") from rollback_error
        raise PageError("page publication refused") from exc
    finally:
        try:
            shutil.rmtree(stage)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # The pointer is already durable. Residue is unreferenced and
            # ignored by readers; operator maintenance owns interrupted-stage
            # cleanup.
            # Raising here would falsely claim the old commit survived.
            if not committed:
                raise PageError("page staging cleanup failed") from exc


def page_refresh(conn: sqlite3.Connection, *, data_dir: str, page_id: str, query: str,
                 namespace: str, tags: tuple[str, ...] = (), llm_local: bool = False,
                 adapter=None, as_of: str | None = None, now: str | None = None) -> dict:
    """Publish a new immutable grounded page version, or raise ``PageError``."""
    if conn.in_transaction:
        raise PageError("page refresh refuses an open transaction")
    if llm_local and not callable(adapter):
        raise PageError("llm_local requires a page adapter")
    if not isinstance(query, str) or not isinstance(namespace, str) or not namespace:
        raise PageError("invalid page definition")
    normalized_tags = tuple(sorted({str(tag).strip() for tag in tags if isinstance(tag, str) and tag.strip()}))
    lock, token = _acquire_actual_maintenance(conn)
    try:
        _wait_for_writers(lock.parent)
        directory = _page_dir(data_dir, page_id, create=True)
        directory.mkdir(parents=True, exist_ok=True)
        definition_path = directory / "definition.json"
        definition_was_missing = not os.path.lexists(str(definition_path))
        if definition_was_missing:
            definition = {"namespace": namespace, "query": query, "tags": list(normalized_tags),
                          "generator_revision": "pages-v1", "creation_policy": "explicit"}
        else:
            definition = _load_json(definition_path)
            if (definition.get("namespace") != namespace or definition.get("query") != query
                    or tuple(definition.get("tags") or []) != normalized_tags):
                raise PageError("conflicting page definition")
        projection_path = directory / "page.md"
        old_version: dict | None = None
        if not definition_was_missing:
            _definition, old_version, raw = _read_committed(directory)
            base = raw.decode("utf-8")
        elif os.path.lexists(str(projection_path)):
            _regular(projection_path)
            base = projection_path.read_bytes().decode("utf-8")
        else:
            base = "<!-- section:refresh -->\nInitial page\n<!-- end-section:refresh -->\n"
        base = base.replace("\r\n", "\n").replace("\r", "\n")
        sections = _sections(base)
        effective_now = now
        if effective_now is None:
            from storelib.schema import now_iso
            effective_now = now_iso()
        # Pin the source collection to one managed read snapshot before adapter
        # work.  The later publication snapshot is a separate revalidation
        # boundary; avoid a discarded third full-namespace scan here.
        conn.execute("BEGIN")
        try:
            fresh_rows = _source_rows(conn, query=query, namespace=namespace, tags=normalized_tags, as_of=as_of, data_dir=data_dir, now=effective_now)
        finally:
            if conn.in_transaction:
                conn.rollback()
        fresh_rendered, source_ids, evidence_ids, fresh_bullets = _render_refresh(fresh_rows)
        if not source_ids:
            raise PageError("no eligible page sources")
        if "refresh" not in sections:
            raise PageError("missing refresh section")
        start, end, refresh_body = sections["refresh"]
        content = base[:start] + fresh_rendered + base[end:]
        prior_bullets = _trusted_bullet_sources(
            old_version.get("bullet_sources") if old_version is not None else None)
        # An ordinary refresh rewrites only the refresh section.  Keep the
        # separately-authenticated provenance for every untouched section,
        # then replace authority for the old refresh bullets with the current
        # canonical snapshot's records.
        actual_bullets = dict(prior_bullets)
        for old_id in re.findall(r"<!-- bullet:([A-Za-z0-9._-]+) -->", refresh_body):
            actual_bullets.pop(old_id, None)
        actual_bullets.update(fresh_bullets)
        if llm_local:
            offered = fresh_rows[:20]
            payload = {
                "sections": {name: hashlib.sha256(body.encode("utf-8")).hexdigest()
                             for name, (_s, _e, body) in _sections(base).items()},
                "candidates": [{"id": r.get("id"), "source_ids": list(r.get("source_ids") or [r.get("id")]),
                                "evidence_ids": list(r.get("evidence_ids") or []),
                                "content": _utf8_cap(r.get("content", ""), _MAX_ADAPTER_CONTENT)}
                               for r in offered],
            }
            citation_pairs: dict[str, set[tuple[tuple[str, ...], tuple[str, ...]]]] = {}
            for row in offered:
                row_sources = sorted({str(x) for x in (list(row.get("source_ids") or []) + [row.get("id")]) if isinstance(x, str)})
                row_evidence = sorted({str(x) for x in (row.get("evidence_ids") or []) if isinstance(x, str)})
                for citation in row_sources + row_evidence:
                    citation_pairs.setdefault(citation, set()).add((tuple(row_sources), tuple(row_evidence)))
            # A shared citation deliberately represents every offered row that
            # is grounded by it.  Union those exact row authorities rather than
            # letting insertion order choose one unrelated pair.
            citation_map = {
                citation: (
                    sorted({source_id for sources, _evidence in pairs for source_id in sources}),
                    sorted({evidence_id for _sources, evidence in pairs for evidence_id in evidence}),
                )
                for citation, pairs in citation_pairs.items()
            }
            try:
                output = adapter(payload)
            except Exception as exc:
                raise PageError("page adapter failed") from exc
            if not isinstance(output, dict) or set(output) != {"operations"}:
                raise PageError("invalid page adapter result")
            # Adapter deltas retain every original untouched section slice.
            # The extractive refresh body is the non-adapter publication path.
            content, actual_bullets = _apply_operations(
                base, output.get("operations"), citation_map, prior_bullets)
        # Do not publish adapter output against a superseded/changed snapshot.
        conn.execute("BEGIN")
        try:
            publish_rows = _source_rows(conn, query=query, namespace=namespace,
                                        tags=normalized_tags, as_of=as_of, data_dir=data_dir, now=effective_now)
        finally:
            if conn.in_transaction:
                conn.rollback()
        _fresh_text, publish_sources, publish_evidence, _publish_bullets = _render_refresh(publish_rows)
        if _snapshot_bytes(publish_rows) != _snapshot_bytes(fresh_rows):
            raise PageError("page sources changed during publication")
        _sections(content)
        # Canonical content and retained sections bypass adapter field caps.
        # Scan the composed page once, without normalizing it, before building
        # a version or entering publication staging.
        _require_no_fence_markers(content)
        _require_authoritative_bullets(content, actual_bullets)
        _require_live_bullet_authority(publish_rows, actual_bullets)
        represented_sources = sorted({sid for item in actual_bullets.values() for sid in item["source_ids"]})
        represented_evidence = sorted({eid for item in actual_bullets.values() for eid in item["evidence_ids"]})
        if not represented_sources or not represented_evidence:
            raise PageError("page content has no grounded bullets")
        source_ids, evidence_ids = represented_sources, represented_evidence
        raw = content.encode("utf-8")
        checksum = hashlib.sha256(raw).hexdigest()
        previous_sources: set[str] = set()
        try:
            _d, old, _raw = _read_committed(directory)
            previous_sources = set(old.get("source_ids") or [])
        except PageError:
            pass
        retracted = sorted(previous_sources - set(source_ids))
        watermark_seed = _snapshot_bytes(publish_rows)
        watermark = "%s:%s" % (max(str(r.get("ingestion_ts", "")) for r in fresh_rows),
                                hashlib.sha256(watermark_seed).hexdigest())
        version_id = _next_version(directory)
        current = {"version_id": version_id, "freshness_watermark": watermark,
                   "source_ids": source_ids, "evidence_ids": evidence_ids,
                   "retracted_source_ids": retracted, "page_checksum": checksum}
        version = dict(current); version["content"] = content
        version["bullet_sources"] = actual_bullets
        _publish(directory, definition, current, version, raw,
                 definition_was_missing=definition_was_missing)
        return {"success": True, **current}
    finally:
        from storelib import schema
        try:
            schema._host.release_lock(lock, token)
        except Exception as exc:
            # Do not report a normal completion when the serialization token
            # could not be released. The committed pointer remains valid and
            # readers continue to use it, but maintenance must surface this.
            raise PageError("maintenance lock release failed") from exc


def _page_fts_coverage(content: str, rows: list[dict[str, Any]], query: str) -> tuple[int, float | None]:
    """Measure page eligibility with recall's exact FTS term semantics.

    A page's derived text and the ordinary rows it represents form one
    candidate for this purpose.  The transient in-memory index uses the same
    unicode61 tokenizer, searchable columns, normalized terms, and MATCH
    expression as ``memory_fts``.  It is never attached to or persisted in
    the canonical store.
    """
    from storelib import recall as _recall

    terms = _recall._normalize_query_terms(query) if query else []
    if not terms:
        return 0, None
    transient: sqlite3.Connection | None = None
    try:
        transient = sqlite3.connect(":memory:")
        transient.execute(
            "CREATE VIRTUAL TABLE page_relevance_fts USING fts5("
            "content, tags, tokenize='unicode61')"
        )
        transient.execute(
            "INSERT INTO page_relevance_fts(content,tags) VALUES (?,?)",
            (
                " ".join([str(content or "")] + [
                    str(row.get("content") or "") for row in rows
                ]),
                " ".join(str(row.get("tags") or "") for row in rows),
            ),
        )
        matched = sum(
            1 for term in terms
            if transient.execute(
                "SELECT 1 FROM page_relevance_fts WHERE page_relevance_fts MATCH ?",
                (_recall._fts_expression([term]),),
            ).fetchone() is not None
        )
        return matched, matched / len(terms)
    except sqlite3.Error:
        # Recall's lexical lane fails open to no candidates when FTS is
        # unavailable or rejects a query expression; derived pages follow the
        # same conservative behavior rather than broadening eligibility.
        return 0, 0.0
    finally:
        if transient is not None:
            transient.close()


def _page_candidates_for_selector(conn: sqlite3.Connection, *, data_dir: str,
                                  query: str, namespace: str, moment: str | None = None,
                                  lane: str | None = None,
                                  user_global_floor: float | None = None) -> list[dict]:
    """Internal selector discovery with current source/evidence revalidation."""
    candidates: list[dict] = []
    for item in page_list(data_dir=data_dir, namespace=None):
        page_id = item["id"]
        try:
            candidate = page_for_injection(data_dir=data_dir, page_id=page_id)
            directory = _page_dir(data_dir, page_id)
            _definition, committed, _raw = _read_committed(directory)
            if candidate["namespace"] not in {namespace, "user:global"}:
                continue
            ids = candidate["source_ids"]
            if not ids:
                continue
            ph = ",".join("?" for _ in ids)
            from storelib.schema import now_iso
            current_now = now_iso()
            rows = [dict(row) for row in conn.execute(
                "SELECT id,content,tags,source_ref,confidence,signal,taint,trust_score,namespace,valid_from,valid_until "
                "FROM memory WHERE id IN (" + ph + ") AND superseded_at IS NULL "
                "AND (valid_from='' OR valid_from<=?) AND (valid_until='' OR valid_until>?)",
                ids + [current_now, current_now]).fetchall()]
            # A virtual belief id is supported when its represented ordinary
            # sources remain live; all ordinary IDs must still resolve.
            ordinary = [i for i in ids if not i.startswith("belief:")]
            if len({r["id"] for r in rows} & set(ordinary)) != len(set(ordinary)):
                continue
            if any(r["namespace"] != candidate["namespace"] for r in rows):
                continue
            pages_root = _root(data_dir) / "pages"
            def page_ref(value: object) -> bool:
                if not isinstance(value, str):
                    return False
                if value.startswith("page:"):
                    return True
                if not value.startswith("file:"):
                    return False
                try:
                    return os.path.commonpath((str(pages_root), str(Path(value[5:]).resolve(strict=False)))) == str(pages_root)
                except (OSError, ValueError):
                    return True
            if any(page_ref(r.get("source_ref")) for r in rows):
                continue
            evidence = []
            if ordinary:
                ordinary_ph = ",".join("?" for _ in ordinary)
                evidence = conn.execute(
                    "SELECT me.memory_id,me.evidence_id FROM memory_evidence me "
                    "JOIN evidence e ON e.id=me.evidence_id WHERE me.memory_id IN (" + ordinary_ph + ")",
                    ordinary).fetchall()
            evidence_by_source = {}
            for source_id, evidence_id in evidence:
                evidence_by_source.setdefault(str(source_id), set()).add(str(evidence_id))
            if any(not evidence_by_source.get(source_id) for source_id in ordinary):
                continue
            page_evidence = set(candidate["evidence_ids"])
            if not page_evidence:
                continue
            ep = ",".join("?" for _ in page_evidence)
            live_evidence = {str(r[0]) for r in conn.execute(
                "SELECT id FROM evidence WHERE id IN (" + ep + ")", list(page_evidence)).fetchall()}
            if live_evidence != page_evidence:
                continue
            # Each committed bullet declares the exact source/evidence pair it
            # represents. The old association cannot be swapped for an
            # unrelated live association after publication.
            bullet_sources = committed.get("bullet_sources") or {}
            if not isinstance(bullet_sources, dict):
                continue
            valid_pairs = {(str(source_id), str(evidence_id)) for source_id, evidence_id in evidence}
            belief_ids = [sid for sid in ids if sid.startswith("belief:")]
            if belief_ids:
                head_names = [sid[len("belief:"):] for sid in belief_ids]
                belief_ph = ",".join("?" for _ in head_names)
                active_heads = {str(row[0]) for row in conn.execute(
                    "SELECT id FROM belief_head WHERE id IN (" + belief_ph + ") "
                    "AND head_state='active'",
                    head_names,
                ).fetchall()}
                # A derived page must stop surfacing as soon as one of its
                # represented heads leaves the active state.
                if active_heads != set(head_names):
                    continue
                for head_id, source_id, evidence_id in conn.execute(
                    "SELECT head_id,source_id,evidence_id FROM belief_head_evidence WHERE head_id IN (" + belief_ph + ")",
                    head_names).fetchall():
                    valid_pairs.add(("belief:" + str(head_id), str(evidence_id)))
                    valid_pairs.add((str(source_id), str(evidence_id)))
            exact = True
            for item in bullet_sources.values():
                if not isinstance(item, dict):
                    exact = False; break
                sources = [x for x in item.get("source_ids", []) if isinstance(x, str) and not x.startswith("belief:")]
                evidence_ids = [x for x in item.get("evidence_ids", []) if isinstance(x, str)]
                all_sources = [x for x in item.get("source_ids", []) if isinstance(x, str)]
                if not all_sources or not evidence_ids or any(not any((sid, eid) in valid_pairs for sid in all_sources) for eid in evidence_ids):
                    exact = False; break
            if not exact:
                continue
            matched, coverage = _page_fts_coverage(candidate["content"], rows, query)
            if coverage is not None and not (matched >= 2 or coverage >= 1.0):
                continue
            import schema_meta
            signal_rank = {"none": 0, "agent": 1, "test": 2, "compile": 3, "lint": 4, "reviewer": 5, "user": 6}
            candidate.update({
                "confidence": min(float(r.get("confidence") or 0) for r in rows),
                "signal": min((str(r.get("signal") or "none") for r in rows), key=lambda s: signal_rank.get(s, 0)),
                "taint": max((str(r.get("taint") or "trusted_internal") for r in rows),
                             key=lambda t: getattr(schema_meta, "TAINT_RANK", {}).get(t, 99)),
                "trust_score": min(float(r.get("trust_score") or 0) for r in rows),
                "represented_ids": ordinary,
                # Same lexical eligibility predicate as recall: a one-term
                # query can fully cover, otherwise two terms are required.
                # A page has no independent BM25 rank, so coverage is the
                # measurable lexical lane rather than a fabricated 1.0.
                "_rel_lex": coverage,
            })
            measured = [candidate[k] for k in ("_rel_lex", "_rel_cos", "_rel_ent", "_rel_graph") if candidate.get(k) is not None]
            if candidate["namespace"] == "user:global" and user_global_floor is not None and measured and max(measured) < user_global_floor:
                continue
            candidates.append(candidate)
        except (PageError, sqlite3.Error):
            continue
    return candidates
