"""Governed dataset artifact layer (issue #134, Workstream E PR 7 of 7).

Three explicit, store-free-at-the-boundary operations over the canonical
SQLite store:

- ``export_dataset`` — deterministic, namespace-scoped dataset directory
  (``manifest.json`` + ``data/*.parquet`` records + ``README.md``). SQLite
  stays authoritative; Parquet is a disposable serialization. Embeddings and
  every other derived column are omitted by construction.
- ``publish_dataset`` — guarded upload of a generated export to a
  ``hf://datasets/owner/repo`` target: whole-row egress scan (TruffleHog when
  available; absence is the ONLY condition ``--allow-unscanned`` may bypass
  and the bypass is recorded in the export manifest), held-back row
  reporting, private-by-default repository creation, and a one-retry
  parent-SHA (CAS) commit. Never touches ``ZMEM_STORE``.
- ``import_dataset`` — revision-pinned, checksum-verified import into an
  ISOLATED ``snapshot.sqlite`` (built via ``.tmp`` + atomic rename); never
  opens the caller's store.

Identity (two-pass, non-circular): pass-1 row checksums are computed over
canonical bytes of rows WITHOUT ``export_snapshot_id``;
``source_snapshot_hash`` is SHA-256 over LF lines ``record_kind + "\\0" +
checksum`` in family order memories, episodes, episode_members, links; then
``export_snapshot_id := source_snapshot_hash`` is stamped onto memory rows
and each memory row's final stored ``row_checksum`` is recomputed over the
completed row. Import re-verifies both layers.

The egress scan serializes each memory row as its 4-field content projection
``{id, namespace, content, ingestion_ts}`` (the portable identity of the
row) plus one auxiliary payload per row carrying tags, source references,
and governance fields; README and manifest bytes are scanned too. A positive
finding always holds its row back (never bypassable); findings on auxiliary
or non-row payloads hold their row or refuse publication, never silently
upload.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from .schema import GLOBAL_NAMESPACE, init_db, migrate
from .schema import _host

DATASET_SCHEMA_VERSION = 1
GENERATOR_REVISION = "zmem-dataset-v1"
REDACTION_POLICY_VERSION = "egress-scan-v1"
DATASET_FORMAT_PARQUET = "parquet"
DATASET_FORMAT_JSONL = "jsonl"

HUB_TARGET_RE = re.compile(r"^hf://datasets/[^/\s]+/[^/\s]+$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")

# Store columns exported per memory row (the full authority projection —
# every portable column in the canonical table; derived columns
# (embedding*, content_norm, consolidated_at) are never exported).
MEMORY_COLUMNS = (
    "id", "namespace", "type", "content", "tags", "source_ref", "source_hash",
    "confidence", "signal", "valid_from", "valid_until", "superseded_at",
    "supersede_reason", "update_of", "merged_from", "applied_count",
    "violated_count", "retrieval_count", "surfaced_count", "ingestion_ts",
    "last_retrieved", "last_surfaced", "taint", "trust_score",
)
# Governance fields the canonical store does not persist; the dataset layer
# serializes them as JSON null and never invents values.
MEMORY_GOVERNANCE_FIELDS = (
    "capture_mode", "redaction_status", "redaction_policy_version",
    "consent_scope", "content_license", "deletion_key", "split_key",
)
# Pinned parquet column order (issue #134 Design §7).
MEMORY_DATASET_COLUMNS = (
    "id", "namespace", "type", "content", "tags", "source_ref", "source_hash",
    "signal", "confidence", "taint", "trust_score", "valid_from",
    "valid_until", "superseded_at", "supersede_reason", "update_of",
    "merged_from", "applied_count", "violated_count", "retrieval_count",
    "surfaced_count", "ingestion_ts", "last_retrieved", "last_surfaced",
) + MEMORY_GOVERNANCE_FIELDS + ("export_snapshot_id", "generator_revision",
                                "row_checksum")
EPISODE_COLUMNS = ("id", "namespace", "started_at", "ended_at",
                   "summary_memory_id", "token_count")
MEMBER_COLUMNS = ("episode_id", "memory_id", "added_at")
LINK_COLUMNS = ("src", "dst", "relation", "score", "created_at")

DATASET_FAMILIES = ("memories", "episodes", "episode_members", "links")


class DatasetError(RuntimeError):
    """Operational dataset failure; the CLI prints ``[zmem] <command>: <msg>``
    and exits 1."""


class DatasetNamespaceUnavailable(DatasetError):
    """The project namespace resolver fell back to ``user:global`` — the
    caller must pass an explicit ``--namespace`` (never silently export a
    global scope)."""


class PublishError(DatasetError):
    """Guarded-publication refusal (target, scanner, overlap, CAS)."""


class ScannerUnavailable(PublishError):
    """TruffleHog binary is not installed — the ONLY condition that
    ``--allow-unscanned`` may bypass (and the bypass is recorded)."""


class ScannerFailed(PublishError):
    """The scanner ran and failed (any exit other than 0 clean / 183
    finding) — never bypassable."""


class ParentConflict(PublishError):
    """The Hub head moved between our read and our commit (CAS)."""


# ---------------------------------------------------------------------------
# Canonical serialization and identity
# ---------------------------------------------------------------------------

def canonical_row_bytes(row: dict) -> bytes:
    """The canonical byte form of a dataset record: UTF-8 compact sorted-key
    JSON with a trailing LF."""
    return (json.dumps(row, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def row_checksum(row: dict) -> str:
    """SHA-256 over canonical bytes of the row minus its ``row_checksum``."""
    payload = {k: v for k, v in row.items() if k != "row_checksum"}
    return hashlib.sha256(canonical_row_bytes(payload)).hexdigest()


def _snapshot_hash_from_checksums(checksum_lines: list[str]) -> str:
    h = hashlib.sha256()
    for line in checksum_lines:
        h.update((line + "\n").encode("utf-8"))
    return h.hexdigest()


def _compute_snapshot_hash(families: dict[str, list[dict]],
                           memories_have_snapshot_id: bool) -> str:
    """Two-pass identity: strip ``export_snapshot_id`` AND
    ``generator_revision`` from memory rows (both are stamped after pass-1)
    so the checksums hashed here are exactly the pass-1 checksums the
    exporter hashed, then hash ``kind\\0checksum`` lines in family order."""
    lines: list[str] = []
    for family in DATASET_FAMILIES:
        for row in families.get(family, []):
            payload = row
            if family == "memories" and memories_have_snapshot_id:
                payload = {k: v for k, v in row.items()
                           if k not in ("export_snapshot_id",
                                        "generator_revision")}
            lines.append(f"{family}\0{row_checksum(payload)}")
    return _snapshot_hash_from_checksums(lines)


# ---------------------------------------------------------------------------
# Record (de)serialization — parquet when pyarrow is present, UTF-8 LF JSONL
# otherwise (the jsonl reader is what byte-deterministic fixtures use)
# ---------------------------------------------------------------------------

_MEMORY_PARQUET_TYPES = {
    "confidence": "double", "trust_score": "double",
    "applied_count": "int64", "violated_count": "int64",
    "retrieval_count": "int64", "surfaced_count": "int64",
    "token_count": "int64", "score": "double",
}
_EPISODE_PARQUET_TYPES = {"token_count": "int64"}
_MEMBER_PARQUET_TYPES: dict[str, str] = {}
_LINK_PARQUET_TYPES = {"score": "double"}
_FAMILY_COLUMNS = {
    "memories": MEMORY_DATASET_COLUMNS,
    # Every record family carries its stored row_checksum (issue D1: import
    # verifies EVERY manifest checksum, which requires a checksum per
    # record in every container).
    "episodes": EPISODE_COLUMNS + ("row_checksum",),
    "episode_members": MEMBER_COLUMNS + ("row_checksum",),
    "links": LINK_COLUMNS + ("row_checksum",),
}
_FAMILY_TYPES = {
    "memories": _MEMORY_PARQUET_TYPES,
    "episodes": _EPISODE_PARQUET_TYPES,
    "episode_members": _MEMBER_PARQUET_TYPES,
    "links": _LINK_PARQUET_TYPES,
}
_FAMILY_FILES = {
    "memories": "memories-000",
    "episodes": "episodes-000",
    "episode_members": "episode_members-000",
    "links": "links-000",
}
def _import_pyarrow():
    try:
        import pyarrow  # noqa: F401
        import pyarrow as pa
        import pyarrow.parquet as pq
        return pa, pq
    except Exception as exc:  # pragma: no cover - exercised via message test
        raise DatasetError("pyarrow is required for export-dataset") from exc


def _parquet_schema(family: str, pa):
    types = _FAMILY_TYPES[family]
    fields = []
    for col in _FAMILY_COLUMNS[family]:
        if types.get(col) == "double":
            fields.append(pa.field(col, pa.float64()))
        elif types.get(col) == "int64":
            fields.append(pa.field(col, pa.int64()))
        else:
            fields.append(pa.field(col, pa.string()))
    return pa.schema(fields)


def _write_family(export_dir: Path, family: str, rows: list[dict],
                  fmt: str) -> None:
    export_dir = Path(export_dir)
    stem = _FAMILY_FILES[family]
    if fmt == DATASET_FORMAT_PARQUET:
        pa, pq = _import_pyarrow()
        schema = _parquet_schema(family, pa)
        schema = schema.with_metadata({"created_by": GENERATOR_REVISION.encode()})
        columns = {col: [row.get(col) for row in rows]
                   for col in _FAMILY_COLUMNS[family]}
        table = pa.table(columns, schema=schema)
        pq.write_table(table, export_dir / "data" / f"{stem}.parquet",
                       compression="zstd", use_dictionary=False,
                       row_group_size=max(len(rows), 1))
    else:
        with open(export_dir / "data" / f"{stem}.jsonl", "wb") as fh:
            for row in rows:
                fh.write(canonical_row_bytes(row))


def _read_family(export_dir: Path, family: str, fmt: str) -> list[dict]:
    export_dir = Path(export_dir)
    stem = _FAMILY_FILES[family]
    if fmt == DATASET_FORMAT_PARQUET:
        pa, pq = _import_pyarrow()
        table = pq.read_table(export_dir / "data" / f"{stem}.parquet")
        return table.to_pylist()
    path = export_dir / "data" / f"{stem}.jsonl"
    rows: list[dict] = []
    with open(path, "rb") as fh:
        for line in fh.read().split(b"\n"):
            if line.strip():
                rows.append(json.loads(line.decode("utf-8")))
    return rows


def load_manifest(export_dir: str | Path) -> dict:
    path = Path(export_dir) / "manifest.json"
    with open(path, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _resolve_namespace_or_raise(namespace: str | None,
                                all_namespaces: bool) -> str | None:
    if all_namespaces:
        return None
    if namespace is None:
        resolved = _host.resolve_namespace(os.getcwd())
        if resolved == GLOBAL_NAMESPACE:
            raise DatasetNamespaceUnavailable(
                "--namespace is required when the project namespace is "
                "unavailable")
        return resolved
    return namespace


def export_dataset(conn: sqlite3.Connection, *, out_dir: str,
                   namespace: str | None, all_namespaces: bool = False,
                   include_tombstones: bool = False,
                   allow_unscoped: bool = False) -> dict:
    """Write the governed dataset directory for the canonical store."""
    if all_namespaces and not allow_unscoped:
        raise DatasetError(
            "--yes is required with --all-namespaces")
    namespace = _resolve_namespace_or_raise(namespace, all_namespaces)

    params: tuple = ()
    where = ""
    if namespace is not None:
        where = " WHERE namespace = ?"
        params = (namespace,)
    if where:
        if not include_tombstones:
            where += " AND superseded_at IS NULL"
    elif not include_tombstones:
        where = " WHERE superseded_at IS NULL"
    rows = conn.execute(
        "SELECT " + ", ".join(MEMORY_COLUMNS) + " FROM memory"
        + where + " ORDER BY ingestion_ts, id",
        params,
    ).fetchall()
    memory_rows = []
    for r in rows:
        row = {col: r[col] for col in MEMORY_COLUMNS}
        for field in MEMORY_GOVERNANCE_FIELDS:
            row[field] = None  # never invented
        memory_rows.append(row)

    ep_params: tuple = ()
    ep_where = ""
    if namespace is not None:
        ep_where = " WHERE namespace = ?"
        ep_params = (namespace,)
    episode_rows = [
        {col: r[col] for col in EPISODE_COLUMNS}
        for r in conn.execute(
            "SELECT " + ", ".join(EPISODE_COLUMNS) + " FROM episode"
            + ep_where + " ORDER BY namespace, started_at, id", ep_params)
    ]
    episode_ids = {e["id"] for e in episode_rows}
    # Endpoint integrity for episode summaries: a summary reference that
    # points outside this view's memory set is blanked to the schema's
    # no-summary value so no view ships a dangling summary.
    for e in episode_rows:
        if e["summary_memory_id"] and e["summary_memory_id"] not in {
                m["id"] for m in memory_rows}:
            e["summary_memory_id"] = ""
    member_rows = []
    link_rows = []
    # Endpoint integrity (issue #134: "Endpoint rows are retained in the
    # audit file and omitted from the live file when their parent is
    # filtered"): a membership is exported only when its memory endpoint is
    # part of THIS view's memory set — the audit view (include_tombstones)
    # retains memberships onto tombstoned rows; the live view omits them
    # rather than shipping a dangling membership.
    memory_ids = {m["id"] for m in memory_rows}
    if episode_ids:
        marks = ",".join("?" * len(episode_ids))
        member_rows = [
            {col: r[col] for col in MEMBER_COLUMNS}
            for r in conn.execute(
                "SELECT " + ", ".join(MEMBER_COLUMNS)
                + f" FROM episode_memory WHERE episode_id IN ({marks})"
                + " ORDER BY episode_id, memory_id", tuple(episode_ids))
            if r["memory_id"] in memory_ids
        ]
    if memory_ids:
        marks = ",".join("?" * len(memory_ids))
        link_rows = [
            {"src": r["src_id"], "dst": r["dst_id"],
             "relation": r["relation"], "score": r["score"],
             "created_at": r["created_at"]}
            for r in conn.execute(
                "SELECT src_id, dst_id, relation, score, created_at"
                + f" FROM memory_link WHERE src_id IN ({marks})"
                + f" AND dst_id IN ({marks}) ORDER BY src_id, dst_id, relation",
                tuple(memory_ids) + tuple(memory_ids))
        ]

    families: dict[str, list[dict]] = {
        "memories": memory_rows,
        "episodes": episode_rows,
        "episode_members": member_rows,
        "links": link_rows,
    }
    source_snapshot_hash = _compute_snapshot_hash(
        families, memories_have_snapshot_id=False)
    namespaces = sorted({row["namespace"] for row in memory_rows})
    for row in memory_rows:
        row["export_snapshot_id"] = source_snapshot_hash
        row["generator_revision"] = GENERATOR_REVISION
        row["row_checksum"] = row_checksum(row)
    for family in ("episodes", "episode_members", "links"):
        for row in families[family]:
            row["row_checksum"] = row_checksum(row)

    manifest = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "source_snapshot_hash": source_snapshot_hash,
        "namespaces": namespaces,
        "redaction_policy_version": REDACTION_POLICY_VERSION,
        "row_counts": {family: len(rows)
                       for family, rows in families.items()},
        "include_tombstones": bool(include_tombstones),
        "generator_revision": GENERATOR_REVISION,
        "export_snapshot_id": source_snapshot_hash,
        "format": DATASET_FORMAT_PARQUET,
        "governance": {
            "policy": "SQLite authoritative; Parquet disposable; "
                      "publication is explicit-only",
            "egress_scan": "required-at-publish",
        },
    }

    out = Path(out_dir)
    if out.exists():
        marker = out / "manifest.json"
        if not marker.is_file():
            raise DatasetError(
                f"refusing to overwrite {out_dir}: it exists and is not a "
                "dataset directory (no manifest.json)")
    staging = Path(str(out) + f".staging-{os.getpid()}")
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "data").mkdir(parents=True)
    try:
        for family, family_rows in families.items():
            _write_family(staging, family, family_rows,
                          DATASET_FORMAT_PARQUET)
        with open(staging / "manifest.json", "wb") as fh:
            fh.write(canonical_row_bytes(manifest))
        (staging / "README.md").write_text(_readme_text(manifest),
                                           encoding="utf-8", newline="\n")
        if out.exists():
            shutil.rmtree(out)
        os.replace(staging, out)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return manifest


def _readme_text(manifest: dict) -> str:
    return (
        "# zmem knowledge dataset\n\n"
        "This directory is a disposable serialization of a zmem memory "
        "store.\n"
        "SQLite remains the source of truth; nothing here is authoritative."
        "\n\n"
        "- `manifest.json` — identity: `source_snapshot_hash` / "
        "`export_snapshot_id`, namespaces, row counts, governance policy.\n"
        f"- `data/*.parquet` — record files (format "
        f"`{manifest.get('format')}`); rebuildable derived indices are "
        "deliberately absent.\n"
        "- Tombstones are audit-only by default; an export with "
        "`include_tombstones: true` carries them for lineage.\n"
        "- Publication is explicit-only (`store.py publish-dataset`): the "
        "egress scan holds back flagged rows and writes `held_back.json`; "
        "Hub targets are created private by default.\n"
        "- Import is revision-pinned: `store.py import-dataset SOURCE "
        "--revision <source_snapshot_hash> --dest DIR` builds an isolated "
        "snapshot; it never touches the caller's store.\n"
    )


def cmd_export_dataset(conn: sqlite3.Connection, *, out_dir: str,
                       namespace: str | None, all_namespaces: bool = False,
                       include_tombstones: bool = False,
                       allow_unscoped: bool = False) -> int:
    try:
        manifest = export_dataset(
            conn, out_dir=out_dir, namespace=namespace,
            all_namespaces=all_namespaces,
            include_tombstones=include_tombstones,
            allow_unscoped=allow_unscoped)
    except DatasetNamespaceUnavailable as exc:
        print(f"[zmem] export-dataset: {exc}", file=sys.stderr)
        return 2
    except DatasetError as exc:
        print(f"[zmem] export-dataset: {exc}", file=sys.stderr)
        return 1
    counts = ", ".join(f"{family}={count}"
                       for family, count in manifest["row_counts"].items())
    print(f"[zmem] dataset exported: {out_dir} ({counts}) "
          f"snapshot={manifest['export_snapshot_id']}")
    return 0


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

class SecretScanner:
    """Real egress scanner: TruffleHog filesystem scan over a private
    staging dir. ``--fail`` is MANDATORY in the invocation (F-001): plain
    ``trufflehog`` exits 0 even when it finds secrets, which would make the
    scan a silent no-op. With ``--fail``: exit 0 = clean, exit 183 =
    findings, anything else = ``ScannerFailed``; a missing binary is
    ``ScannerUnavailable`` (the only ``--allow-unscanned`` bypass)."""

    def scan(self, serialized_rows: list[bytes]) -> list[dict]:
        binary = shutil.which("trufflehog")
        if binary is None:
            raise ScannerUnavailable(
                "trufflehog is not installed; install it or pass "
                "--allow-unscanned (recorded in the dataset manifest)")
        staging = tempfile.mkdtemp(prefix="zmem-egress-")
        try:
            paths = []
            for i, blob in enumerate(serialized_rows):
                p = Path(staging) / f"payload-{i:06d}.bin"
                p.write_bytes(blob)
                paths.append(p)
            try:
                proc = subprocess.run(
                    [binary, "filesystem", "--json", "--fail", staging],
                    capture_output=True, text=True, timeout=600, check=False)
            except subprocess.TimeoutExpired as exc:
                raise ScannerFailed(
                    f"trufflehog timed out after {exc.timeout}s") from exc
            if proc.returncode == 0:
                return []
            if proc.returncode != 183:
                raise ScannerFailed(
                    f"trufflehog exited {proc.returncode}")
            findings: list[dict] = []
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                source = str(rec.get("Source", {}).get("Data", ""))
                match = re.search(r"payload-(\d{6})\.bin$", source)
                if match is None:
                    continue
                findings.append({"row_index": int(match.group(1)),
                                 "detector": rec.get("DetectorName"),
                                 "raw": rec.get("Raw", "")})
            return findings
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _egress_projection(row: dict) -> bytes:
    """The portable content identity of a memory row — the exact bytes a
    held-back entry's ``row_checksum`` binds."""
    return canonical_row_bytes({"id": row["id"],
                                "namespace": row["namespace"],
                                "content": row["content"],
                                "ingestion_ts": row["ingestion_ts"]})


def _egress_aux_bytes(row: dict) -> bytes:
    """Auxiliary per-row payload: tags, source references, and governance
    fields are scanned too (issue contract), but they do not define the
    row's held-back checksum identity."""
    aux = {"id": row["id"], "namespace": row["namespace"]}
    for col in MEMORY_COLUMNS:
        if col in ("id", "namespace", "content", "ingestion_ts"):
            continue
        aux[col] = row.get(col)
    for field in MEMORY_GOVERNANCE_FIELDS:
        aux[field] = row.get(field)
    return canonical_row_bytes(aux)


class HubDatasetClient:
    """Real huggingface_hub-backed client (created lazily only when no
    client is injected). ``huggingface_hub`` is an optional runtime
    dependency: importing it is deferred so publish never imports it on the
    fake-client path."""

    def __init__(self) -> None:
        try:
            from huggingface_hub import HfApi
        except Exception as exc:
            raise PublishError(
                "huggingface_hub is required to publish "
                "(pip install huggingface_hub)") from exc
        self._api = HfApi()

    def _repo_id(self, target: str) -> str:
        # huggingface_hub expects "owner/repo" when repo_type="dataset";
        # do NOT prepend the "datasets/" family here.
        return target[len("hf://datasets/"):]

    @staticmethod
    def _is_missing_repo_error(exc: Exception) -> bool:
        """True only for 'repository does not exist' shapes; anything else
        (network, auth, quota) must fail closed rather than degrade (F-004:
        a skipped CAS/consent gate is worse than a refused publish)."""
        name = type(exc).__name__
        if "NotFound" in name or "Missing" in name:
            return True
        text = str(exc).lower()
        return "not found" in text or "does not exist" in text \
            or "404" in text

    def head(self, target: str) -> str:
        try:
            info = self._api.repo_info(self._repo_id(target),
                                       repo_type="dataset", revision="main")
            return info.sha or ""
        except Exception as exc:
            if self._is_missing_repo_error(exc):
                return ""  # missing repository → empty parent (first commit)
            raise PublishError(
                f"could not read Hub head for {target}: {exc}") from exc

    def is_private(self, target: str) -> bool:
        """Visibility of an EXISTING repo. Raises for missing repos is fine
        (callers treat any exception as unknown → refuse)."""
        try:
            info = self._api.repo_info(self._repo_id(target),
                                       repo_type="dataset")
        except Exception as exc:
            raise PublishError(
                f"could not read Hub visibility for {target}: {exc}") from exc
        return bool(getattr(info, "private", False))

    def manifest(self, target: str) -> dict:
        try:
            import huggingface_hub
            path = huggingface_hub.hf_hub_download(
                self._repo_id(target), "manifest.json", repo_type="dataset")
            with open(path, "rb") as fh:
                return json.loads(fh.read().decode("utf-8"))
        except Exception as exc:
            if self._is_missing_repo_error(exc) or "manifest" in str(exc).lower():
                return {}  # repo (or manifest in it) genuinely absent
            raise PublishError(
                f"could not read Hub manifest for {target}: {exc}") from exc

    def create_private(self, target: str) -> None:
        try:
            self._api.create_repo(self._repo_id(target), repo_type="dataset",
                                  private=True, exist_ok=True)
        except Exception as exc:
            raise PublishError(f"could not create private repo: {exc}") from exc

    def commit(self, target: str, parent: str, tree: dict) -> str:
        import huggingface_hub
        operations = [
            huggingface_hub.CommitOperationAdd(path_in_repo=rel,
                                               path_or_fileobj=blob)
            for rel, blob in sorted(tree.items())
        ]
        try:
            result = self._api.create_commit(
                self._repo_id(target), operations=operations,
                repo_type="dataset", commit_message="zmem dataset publish",
                parent_commit=parent or None)
        except Exception as exc:
            text = str(exc).lower()
            if parent and ("did not match" in text or "conflict" in text
                           or "409" in text):
                raise ParentConflict(str(exc)) from exc
            raise PublishError(f"hub commit failed: {exc}") from exc
        return getattr(result, "commit_id", "") or ""


def _parse_target(target: str) -> str:
    if not HUB_TARGET_RE.match(target or ""):
        raise PublishError("target must use hf://datasets/")
    return target


def _validate_manifest_shape(manifest: dict, err: type) -> None:
    if not isinstance(manifest.get("row_counts"), dict) or             not isinstance(manifest.get("namespaces"), list) or             not manifest.get("source_snapshot_hash"):
        raise err("malformed dataset manifest (missing row_counts / "
                  "namespaces / source_snapshot_hash)")


def _read_export_records(export_dir: Path, manifest: dict) -> dict[str, list]:
    _validate_manifest_shape(manifest, PublishError)
    fmt = manifest.get("format", DATASET_FORMAT_PARQUET)
    if fmt not in (DATASET_FORMAT_PARQUET, DATASET_FORMAT_JSONL):
        raise PublishError(f"unsupported dataset format: {fmt}")
    families = {family: _read_family(export_dir, family, fmt)
                for family in DATASET_FAMILIES}
    required = {"memories": ("id", "namespace"),
                "episodes": ("id", "namespace"),
                "episode_members": ("episode_id", "memory_id"),
                "links": ("src", "dst")}
    for family, rows in families.items():
        for row in rows:
            for key in required[family]:
                if key not in row:
                    raise PublishError(
                        f"malformed {family} record: missing {key!r} "
                        f"(row checksum {row.get('row_checksum')!r})")
            if row_checksum(row) != row.get("row_checksum"):
                raise PublishError("checksum mismatch")
    recomputed = _compute_snapshot_hash(
        families, memories_have_snapshot_id=True)
    if recomputed != manifest.get("source_snapshot_hash"):
        raise PublishError("checksum mismatch")
    return families


def publish_dataset(export_dir: str, target: str, *, yes: bool = False,
                    allow_unscanned: bool = False, client=None) -> dict:
    """Guarded Hub publication of a generated export directory.

    Raises a ``PublishError`` subclass on EVERY failure mode; returns the
    summary dict ONLY on success (keys exactly: target, revision,
    uploaded_rows, held_back, private)."""
    _parse_target(target)
    export_path = Path(export_dir)
    manifest = load_manifest(export_path)
    families = _read_export_records(export_path, manifest)

    # Egress scan payloads: per-row content identity + aux payloads
    # (tags/source refs/governance) + episodes/memberships/links + README
    # + manifest. The client seam may replace the scanner via the module
    # attribute, so resolve SecretScanner at call time. The scan runs
    # BEFORE any Hub client is constructed: every local, deterministic
    # refusal fires before the first network touch.
    scanner = SecretScanner()
    memories = families["memories"]
    payload_rows: list[tuple[str, str | None]] = []
    payloads: list[bytes] = []
    for row in memories:
        payloads.append(_egress_projection(row))
        payload_rows.append(("memories", row["id"]))
        payloads.append(_egress_aux_bytes(row))
        payload_rows.append(("memories", row["id"]))
    for family in ("episodes", "episode_members", "links"):
        for row in families[family]:
            payloads.append(canonical_row_bytes(row))
            payload_rows.append((family, None))
    payloads.append((export_path / "README.md").read_bytes())
    payload_rows.append(("doc", None))
    payloads.append(canonical_row_bytes(manifest))
    payload_rows.append(("doc", None))

    findings: list[dict]
    used_unscanned_override = False
    try:
        findings = scanner.scan(payloads)
    except ScannerUnavailable:
        if not allow_unscanned:
            raise
        # Scanner ABSENCE is the only condition --allow-unscanned may
        # bypass; the bypass is recorded in both manifests below.
        findings = []
        used_unscanned_override = True
    held_ids: set[str] = set()
    unholdable: list[int] = []
    for finding in findings:
        idx = finding.get("row_index")
        if idx is None or idx < 0 or idx >= len(payload_rows):
            unholdable.append(-1 if idx is None else idx)
            continue
        kind, row_id = payload_rows[idx]
        if kind == "memories" and row_id:
            held_ids.add(row_id)
        else:
            unholdable.append(idx)
    if unholdable:
        raise PublishError(
            "egress scan flagged non-row content (payload index "
            f"{unholdable[0]}); refusing to publish")

    if client is None:
        client = HubDatasetClient()
    existing = client.manifest(target) or {}
    overlap = sorted(set(existing.get("namespaces", []))
                     & set(manifest.get("namespaces", [])))
    if overlap and not yes:
        raise PublishError(
            "target dataset already carries namespaces "
            f"{overlap}; pass --yes to confirm")

    held = sorted(
        ({"id": row["id"], "reason": "secret_scan",
          "row_checksum": hashlib.sha256(
              _egress_projection(row)).hexdigest()}
         for row in memories if row["id"] in held_ids),
        key=lambda entry: entry["id"])
    uploaded = [row for row in memories if row["id"] not in held_ids]

    # Self-consistent published identity (issue #134: "Held-back rows are
    # removed before manifest hashing"): when the scan held rows back, the
    # uploaded artifact is RE-HASHED over the surviving rows only — the
    # pass-1 checksums are recomputed without the held rows, a new
    # snapshot id is derived, and every uploaded memory row is re-stamped
    # (export_snapshot_id, generator_revision, final row_checksum) — so
    # import_dataset's verification accepts exactly what was published.
    # A clean publish (nothing held) keeps the export's identity verbatim.
    if held:
        kept_ids = {row["id"] for row in uploaded}
        rebuilt = []
        for row in uploaded:
            row = {k: v for k, v in row.items()
                   if k not in ("export_snapshot_id", "generator_revision",
                                "row_checksum")}
            rebuilt.append(row)
        uploaded_families = dict(families)
        uploaded_families["memories"] = rebuilt
        # Endpoint integrity applies to the upload too: a membership or
        # link whose memory endpoint was held back must not dangle.
        uploaded_families["episode_members"] = [
            m for m in families["episode_members"]
            if m["memory_id"] in kept_ids]
        uploaded_families["links"] = [
            l for l in families["links"]
            if l["src"] in kept_ids and l["dst"] in kept_ids]
        # Episode summaries anchored on a held-back row are blanked to the
        # no-summary value (the episode and its surviving memberships stay;
        # the secret-linking reference does not). Re-stamp each touched
        # episode's row_checksum so the upload remains verifiable.
        for e in uploaded_families["episodes"]:
            if e["summary_memory_id"] and e["summary_memory_id"] not in kept_ids:
                e["summary_memory_id"] = ""
                e["row_checksum"] = row_checksum(e)
        published_snapshot = _compute_snapshot_hash(
            uploaded_families, memories_have_snapshot_id=False)
        for row in rebuilt:
            row["export_snapshot_id"] = published_snapshot
            row["generator_revision"] = GENERATOR_REVISION
            row["row_checksum"] = row_checksum(row)
        uploaded = rebuilt
    else:
        uploaded_families = families
        published_snapshot = manifest["source_snapshot_hash"]

    staging = Path(tempfile.mkdtemp(prefix="zmem-publish-"))
    try:
        data_dir = staging / "data"
        data_dir.mkdir()
        upload_manifest = dict(manifest)
        upload_manifest["row_counts"] = dict(manifest["row_counts"])
        upload_manifest["row_counts"]["memories"] = len(uploaded)
        if held:
            for family in DATASET_FAMILIES:
                upload_manifest["row_counts"][family] = \
                    len(uploaded_families[family])
            # F-005: the published manifest must advertise exactly the
            # namespaces that survive in the uploaded memory rows — a
            # fully-held-back namespace disappears from the listing.
            upload_manifest["namespaces"] = sorted(
                {row["namespace"] for row in uploaded})
        upload_manifest["source_snapshot_hash"] = published_snapshot
        upload_manifest["export_snapshot_id"] = published_snapshot
        if used_unscanned_override:
            upload_manifest["governance"] = dict(
                manifest.get("governance", {}))
            upload_manifest["governance"]["egress_scan"] = {
                "status": "unscanned", "override": "--allow-unscanned"}
        for family in DATASET_FAMILIES:
            rows = uploaded_families[family]
            fmt = manifest.get("format", DATASET_FORMAT_PARQUET)
            if fmt == DATASET_FORMAT_JSONL:
                stem = _FAMILY_FILES[family]
                with open(data_dir / f"{stem}.jsonl", "wb") as fh:
                    for row in rows:
                        fh.write(canonical_row_bytes(row))
            else:
                _write_family(staging, family, rows,
                              DATASET_FORMAT_PARQUET)
        shutil.copy2(export_path / "README.md", staging / "README.md")
        with open(staging / "manifest.json", "wb") as fh:
            fh.write(canonical_row_bytes(upload_manifest))
        tree = {
            "README.md": (staging / "README.md").read_bytes(),
            "manifest.json": (staging / "manifest.json").read_bytes(),
        }
        for path in sorted(data_dir.iterdir()):
            tree[f"data/{path.name}"] = path.read_bytes()
        if held:
            # F-006: held_back.json stays LOCAL-ONLY. Its row_checksums are
            # unsalted fingerprints of held row content — uploading it next
            # to the manifest (which discloses the namespace) would hand a
            # content-confirmation oracle to anyone who can read the repo.
            held_payload = {"held_back": held,
                            "uploaded_rows": len(uploaded)}
            held_bytes = canonical_row_bytes(held_payload)
            with open(export_path / "held_back.json", "wb") as fh:
                fh.write(held_bytes)
        if used_unscanned_override:
            # Also record the override on the LOCAL export manifest so the
            # operator's copy carries the audit trail.
            local_manifest = dict(manifest)
            local_manifest["governance"] = dict(
                manifest.get("governance", {}))
            local_manifest["governance"]["egress_scan"] = {
                "status": "unscanned", "override": "--allow-unscanned"}
            with open(export_path / "manifest.json", "wb") as fh:
                fh.write(canonical_row_bytes(local_manifest))

        # F-003: private-by-default must hold for PRE-EXISTING repos too —
        # huggingface_hub's create_repo(private=True, exist_ok=True) ignores
        # `private` when the repo already exists, so an accidentally-public
        # target would silently receive memory content. Refuse unless the
        # operator explicitly re-confirms with --yes.
        client.create_private(target)
        target_private = client.is_private(target)
        if not target_private and not yes:
            raise PublishError(
                f"target {target} exists and is PUBLIC; refusing to publish "
                "memory content to it (create a private repo or pass --yes "
                "to confirm a public publish)")
        parent = client.head(target)
        # The injected client signals a moved head with an exception NAMED
        # ParentConflict (the issue's seam contract declares that name on
        # the fake); match by name so any conforming double works.
        try:
            revision = client.commit(target, parent, tree)
        except Exception as exc:  # noqa: BLE001 - seam contract by name
            if type(exc).__name__ != "ParentConflict":
                raise
            parent = client.head(target)
            try:
                revision = client.commit(target, parent, tree)
            except Exception as exc2:  # noqa: BLE001
                if type(exc2).__name__ != "ParentConflict":
                    raise
                raise PublishError(
                    "publish failed: head moved twice during commit "
                    "(CAS exhausted)") from exc2
        return {"target": target, "revision": revision,
                "dataset_revision": published_snapshot,
                "uploaded_rows": len(uploaded), "held_back": len(held),
                "private": bool(target_private)}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def cmd_publish_dataset(out_dir: str, target: str, *, yes: bool = False,
                        allow_unscanned: bool = False) -> int:
    try:
        result = publish_dataset(out_dir, target, yes=yes,
                                 allow_unscanned=allow_unscanned)
    except PublishError as exc:
        print(f"[zmem] publish-dataset: {exc}", file=sys.stderr)
        return 1
    except DatasetError as exc:
        print(f"[zmem] publish-dataset: {exc}", file=sys.stderr)
        return 1
    line = (f"[zmem] published {result['uploaded_rows']} row(s) to "
            f"{result['target']} commit {result['revision'] or '(new)'} "
            f"dataset revision {result['dataset_revision'][:12]}...")
    if result["held_back"]:
        line += f" ({result['held_back']} row(s) held back; held_back.json)"
    print(line)
    return 0


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

_MEMORY_STORE_COLUMNS = tuple(MEMORY_COLUMNS)


def _hub_cache_dir(source: str, revision: str) -> Path:
    try:
        import huggingface_hub
        local = huggingface_hub.snapshot_download(
            repo_id=source[len("hf://datasets/"):], repo_type="dataset",
            revision=revision, local_files_only=True)
        return Path(local)
    except Exception as exc:
        raise DatasetError(
            f"revision {revision} is not present in the local Hub cache "
            f"({exc})") from exc


def import_dataset(source: str, *, revision: str, dest_dir: str,
                   namespace: str | None = None,
                   min_confidence: float | None = None,
                   min_trust: float | None = None,
                   taint: str | None = None,
                   include_tombstones: bool = False) -> dict:
    """Revision-pinned import into an isolated snapshot; never opens the
    caller's store."""
    if source.startswith("hf://datasets/"):
        _parse_target(source)
        if not REVISION_RE.match(revision or ""):
            raise DatasetError(
                "revision must be the exact 40-character source commit SHA")
        source_path = _hub_cache_dir(source, revision)
    else:
        source_path = Path(source)

    manifest = load_manifest(source_path)
    if manifest.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise DatasetError(
            "unsupported dataset schema_version "
            f"{manifest.get('schema_version')!r} "
            f"(expected {DATASET_SCHEMA_VERSION})")
    # F-002: for LOCAL sources the revision IS the manifest's
    # source_snapshot_hash and equality is the pin. For HUB sources the
    # revision is the repo's 40-char commit SHA — already exact-pinned by
    # reading only that revision from the local cache above — so comparing
    # it against the 64-char hash can never pass; content integrity is
    # enforced by the per-record checksums + snapshot re-derivation below.
    if not source.startswith("hf://datasets/") and             (revision or "") != manifest.get("source_snapshot_hash"):
        raise DatasetError("revision mismatch")
    _validate_manifest_shape(manifest, DatasetError)
    fmt = manifest.get("format", DATASET_FORMAT_PARQUET)
    if fmt not in (DATASET_FORMAT_PARQUET, DATASET_FORMAT_JSONL):
        raise DatasetError(f"unsupported dataset format: {fmt}")
    families = {family: _read_family(source_path, family, fmt)
                for family in DATASET_FAMILIES}
    required = {"memories": ("id", "namespace"),
                "episodes": ("id", "namespace"),
                "episode_members": ("episode_id", "memory_id"),
                "links": ("src", "dst")}
    for family, rows in families.items():
        for row in rows:
            for key in required[family]:
                if key not in row:
                    raise DatasetError(
                        f"malformed {family} record: missing {key!r}")
            if row_checksum(row) != row.get("row_checksum"):
                raise DatasetError("checksum mismatch")
    if _compute_snapshot_hash(families, True) != \
            manifest.get("source_snapshot_hash"):
        raise DatasetError("checksum mismatch")

    memories = families["memories"]
    available = {row["namespace"] for row in memories}
    if namespace is not None and namespace not in available:
        raise DatasetError("namespace absent")

    now = _now_iso()
    kept = []
    for row in memories:
        if namespace is not None and row["namespace"] != namespace:
            continue
        if row.get("superseded_at") and not include_tombstones:
            continue
        if min_confidence is not None and \
                float(row.get("confidence") or 0.0) < min_confidence:
            continue
        if min_trust is not None and \
                float(row.get("trust_score") if row.get("trust_score")
                      is not None else 1.0) < min_trust:
            continue
        if taint is not None and row.get("taint") != taint:
            continue
        valid_from = row.get("valid_from") or ""
        if valid_from and valid_from > now:
            continue  # not yet in force (NEW-01: mirror the canonical
        # _as_of_temporal_predicate semantics, valid_from <= now)
        valid_until = row.get("valid_until") or ""
        if valid_until and not include_tombstones and valid_until <= now:
            continue
        kept.append(row)

    kept_ids = {row["id"] for row in kept}
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    tmp_path = dest / f"snapshot.sqlite.tmp-{os.getpid()}"
    final_path = dest / "snapshot.sqlite"
    if tmp_path.exists():
        tmp_path.unlink()
    conn = sqlite3.connect(tmp_path)
    try:
        # init_db + migrate — the full current schema (including migrated
        # columns like supersede_reason) so the snapshot is a real store.
        # The STORE_PATH-bound _prepare_store is deliberately NOT used:
        # this connection is the isolated snapshot, never the caller's
        # store.
        init_db(conn)
        migrate(conn)
        try:
            for row in kept:
                conn.execute(
                    "INSERT INTO memory (" + ", ".join(_MEMORY_STORE_COLUMNS)
                    + ") VALUES ("
                    + ", ".join("?" * len(_MEMORY_STORE_COLUMNS)) + ")",
                    tuple(row.get(col) for col in _MEMORY_STORE_COLUMNS))
            kept_ids = {row["id"] for row in kept}
            episodes = [e for e in families["episodes"]
                        if namespace is None or e["namespace"] == namespace]
            # Endpoint validation after filtering: a summary reference to a
            # memory the filters excluded becomes the no-summary value rather
            # than a dangling pointer in the snapshot.
            for e in episodes:
                if e["summary_memory_id"] and \
                        e["summary_memory_id"] not in kept_ids:
                    e["summary_memory_id"] = ""
            for e in episodes:
                conn.execute(
                    "INSERT INTO episode (id, namespace, started_at, ended_at, "
                    "summary_memory_id, token_count) VALUES (?, ?, ?, ?, ?, ?)",
                    (e["id"], e["namespace"], e["started_at"], e["ended_at"],
                     e["summary_memory_id"], e["token_count"]))
            for m in families["episode_members"]:
                if m["episode_id"] in {e["id"] for e in episodes} and \
                        m["memory_id"] in kept_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO episode_memory (episode_id, "
                        "memory_id, added_at) VALUES (?, ?, ?)",
                        (m["episode_id"], m["memory_id"], m["added_at"]))
            for l in families["links"]:
                if l["src"] in kept_ids and l["dst"] in kept_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_link (src_id, dst_id, "
                        "relation, score, created_at) VALUES (?, ?, ?, ?, ?)",
                        (l["src"], l["dst"], l["relation"], l["score"],
                         l["created_at"]))
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DatasetError(
                f"crafted dataset rejected (duplicate/inconsistent ids): "
                f"{exc}") from exc
    finally:
        conn.close()
    os.replace(tmp_path, final_path)

    summary = {
        "snapshot": str(final_path),
        "revision": revision,
        "memories": len(kept),
        "episodes": len(episodes),
        "filters": {
            "namespace": namespace,
            "min_confidence": min_confidence,
            "min_trust": min_trust,
            "taint": taint,
            "include_tombstones": bool(include_tombstones),
        },
    }
    return summary


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_import_dataset(source: str, *, revision: str, dest_dir: str,
                       namespace: str | None = None,
                       min_confidence: float | None = None,
                       min_trust: float | None = None,
                       taint: str | None = None,
                       include_tombstones: bool = False) -> int:
    try:
        result = import_dataset(
            source, revision=revision, dest_dir=dest_dir,
            namespace=namespace, min_confidence=min_confidence,
            min_trust=min_trust, taint=taint,
            include_tombstones=include_tombstones)
    except DatasetError as exc:
        print(f"[zmem] import-dataset: {exc}", file=sys.stderr)
        return 1
    filters = result["filters"]
    parts = [f"namespace={filters['namespace']}"]
    if filters["min_confidence"] is not None:
        parts.append(f"min_confidence={filters['min_confidence']}")
    if filters["min_trust"] is not None:
        parts.append(f"min_trust={filters['min_trust']}")
    if filters["taint"] is not None:
        parts.append(f"taint={filters['taint']}")
    if filters["include_tombstones"]:
        parts.append("include_tombstones=True")
    print(f"[zmem] imported {result['memories']} memory row(s) and "
          f"{result['episodes']} episode(s) into isolated snapshot "
          f"{result['snapshot']} (revision {result['revision']}; "
          f"{' '.join(parts)})")
    print(f"[zmem] to recall against this snapshot: set "
          f"ZMEM_STORE={result['snapshot']}")
    return 0
