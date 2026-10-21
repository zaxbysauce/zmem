#!/usr/bin/env python3
"""Ingest a closeout-remote harvest JSON array into the zmem store.

Reads a harvest JSON file (the array format documented in
skills/closeout-remote/SKILL.md's "Output format" section), validates each
row's shape and enum values, and calls `store.py add` via subprocess for
each valid row.

This script does ONLY mechanical shape validation (required keys present,
enum values in range) and implements NO pattern matching of its own — the
capture bar is applied by the store's shared capture policy, which every
`store.py add --capture-mode auto --json` child subprocess runs (issue #180:
value spans are redacted, whole-row refusals are quarantined, and this
adapter reports stored/deduped/quarantined/failed counts). It does NOT dedup
against the store beyond the writer's own dedup-on-write — the judgment call
belongs to the agent running `commands/ingest-harvest.md`, which is expected
to hand this script an already-trimmed, already-recall-checked set of
surviving rows.

Python 3.11, stdlib only, ASCII-only stdout/stderr.

Usage:
  python ingest_harvest.py <harvest.json> [--source-ref REF] [--store PATH]
      [--trust {authoritative,untrusted-peer}]
      [--allow-namespace NS ...] [--attribute-host HOST]

Issue #373: --trust untrusted-peer gives the harvest lane the SAME trust
boundary ingest-jsonl got in issue #282 (storelib.sync.IngestTrustPolicy /
apply_trust_policy): every namespace is gated against --allow-namespace
(refusals counted as namespace_refused, never fatal), row-claimed signal
grounding is demoted to a claimed_signal tag (the store derives taint and
confidence from the demoted signal), row-declared host:/agent: attribution
tags are stripped under the #381-normalized tokenizer with only the caller's
--attribute-host stamped, and provenance is caller-derived (--source-ref or
the adapter's own session:harvest-<stem> default — a row's own source_ref is
a claim and never wins in peer mode). Default 'authoritative' keeps the
pre-#373 behavior byte-identically.

Exit code: 0 if every row in the file was stored, deduped, quarantined, or
(peer mode) namespace-refused cleanly; 1 if any row was rejected (bad
shape/enum) or failed to ingest; 2 for flag-contract violations (peer mode
without --allow-namespace, invalid namespace grammar, --attribute-host
outside peer mode).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REQUIRED_KEYS = ("namespace", "type", "content", "tags", "signal", "why")

# Import the write-path constants from schema_meta (the single source of truth
# shared with store.py, the MCP server, and the local Hermes provider) so this
# validator can never drift from the other surfaces (#37 L7/L8 closed that drift
# class everywhere else; this 4th copy was the last one). The directory layout
# is <repo>/scripts/ingest_harvest.py -> <repo>/skills/memory/scripts/schema_meta.py.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "memory" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
try:
    from schema_meta import ALLOWED_TYPES, ALLOWED_SIGNALS, MAX_CONTENT_CHARS, is_valid_namespace  # noqa: F401
except ImportError:
    # Defensive fallback (should not happen in a normal checkout): keep the
    # historical literals so a broken import never silently disables validation.
    ALLOWED_TYPES = ("fact", "lesson", "convention", "preference")
    ALLOWED_SIGNALS = ("test", "compile", "lint", "reviewer", "user", "none")
    MAX_CONTENT_CHARS = 65536

    def is_valid_namespace(value):  # noqa: F811 - degraded fallback only
        """Grammar floor for the degraded-import case: scoped prefix or the
        canonical global namespace (schema_meta stays the single source)."""
        return isinstance(value, str) and (
            value == "user:global" or (
                ":" in value and value.split(":", 1)[0] in (
                    "project", "user", "fleet", "host", "agent", "domain")
                and value.split(":", 1)[1].strip() != ""))


def _ascii(value: object) -> str:
    """Coerce to str and strip anything outside ASCII.

    Harvest content is arbitrary agent-authored text (em-dashes, curly
    quotes, etc. show up constantly), and a legacy Windows console/pipe can
    have a non-UTF-8 codepage with strict error handling — printing raw
    non-ASCII there raises UnicodeEncodeError mid-batch and kills the run
    before the summary line prints. Every non-ASCII codepoint is replaced
    with '?' rather than raising or silently dropping the row.
    """
    return str(value).encode("ascii", "replace").decode("ascii")


def _print(msg: str, *, err: bool = False) -> None:
    print(_ascii(msg), file=sys.stderr if err else sys.stdout)


def default_store_path() -> Path:
    """Resolve store.py relative to this script's location in the repo
    layout: <repo>/scripts/ingest_harvest.py -> <repo>/skills/memory/scripts/store.py.
    """
    here = Path(__file__).resolve().parent
    return here.parent / "skills" / "memory" / "scripts" / "store.py"


def validate_row(row: object, index: int) -> tuple[dict | None, str | None]:
    """Return (row, None) if valid, or (None, error-message) if not."""
    if not isinstance(row, dict):
        return None, f"row {index}: not a JSON object"

    missing = [k for k in REQUIRED_KEYS if k not in row]
    if missing:
        return None, f"row {index}: missing required key(s): {', '.join(missing)}"

    for k in REQUIRED_KEYS:
        if not isinstance(row[k], str):
            return None, f"row {index}: field '{k}' must be a string"

    if not row["namespace"].strip():
        return None, f"row {index}: namespace is empty"
    if not row["content"].strip():
        return None, f"row {index}: content is empty"
    if len(row["content"]) > MAX_CONTENT_CHARS:
        return None, (
            f"row {index}: content is {len(row['content'])} chars, over the "
            f"{MAX_CONTENT_CHARS} limit"
        )
    if row["type"] not in ALLOWED_TYPES:
        return None, (
            f"row {index}: invalid type '{row['type']}' "
            f"(allowed: {', '.join(ALLOWED_TYPES)})"
        )
    if row["signal"] not in ALLOWED_SIGNALS:
        return None, (
            f"row {index}: invalid signal '{row['signal']}' "
            f"(allowed: {', '.join(ALLOWED_SIGNALS)})"
        )

    return row, None


def ingest_row(store_py: Path, row: dict, source_ref: str) -> tuple[str, str]:
    """Call `store.py add --capture-mode auto --json` for one row.

    Issue #180: the harvest lane runs the SAME capture policy as every other
    importer — auto mode redacts value spans and quarantines whole-row
    refusals (the child's quarantine record lands under the child store's
    data dir). Returns a structured status: the first value is exactly one of
    ``stored``, ``deduped``, ``quarantined``, or ``failed``; the second is the
    exact public message. A row carrying its own ``source_ref`` forwards it
    (the caller-supplied uniform ref is the fallback); a child failure,
    timeout, or malformed JSON is ``failed``.

    Deliberately does NOT pass --confidence: the signal-derived default in
    store.py's add_memory() is intentional, and hand-setting it here would
    let a harvest silently override the honesty check signal is supposed
    to encode. This adapter implements NO pattern matching of its own — the
    policy lives in storelib, reached only through the subprocess boundary.

    Encoding, both directions, because a harvest carries arbitrary non-ASCII:
      - PYTHONIOENCODING=utf-8 in the CHILD's env, so store.py's own success
        print (which echoes the namespace) cannot die with UnicodeEncodeError
        on a legacy Windows codepage AFTER the row was already committed --
        that made a landed row report as FAILED, the worst possible outcome
        (the operator re-runs and the store gets a near-duplicate).
      - encoding/errors on the PARENT's pipe decode, so a child byte sequence
        this console cannot represent is replaced rather than raising here.
    """
    raw_ref = row.get("source_ref")
    # A MISSING or empty ref falls back to the batch reference (the designed
    # provenance default); a NON-STRING ref is a shape error and fails THIS
    # row (failed count, nonzero exit) — silently substituting provenance
    # and reporting the row stored would be a quiet integrity loss.
    if raw_ref is not None and not isinstance(raw_ref, str):
        return ("failed",
                f"row source_ref must be a string, got "
                f"{type(raw_ref).__name__}")
    row_ref = raw_ref if raw_ref else source_ref
    cmd = [
        sys.executable, str(store_py), "add",
        "--namespace", row["namespace"],
        "--type", row["type"],
        "--content", row["content"],
        "--tags", row["tags"],
        "--signal", row["signal"],
        "--source-ref", row_ref,
        "--capture-mode", "auto",
        "--json",
    ]
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=child_env,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "failed", "store.py add timed out after 120s"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return "failed", detail or f"store.py add exited {result.returncode}"
    stdout = (result.stdout or "").strip()
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return "failed", stdout or "store.py add produced no JSON result"
    if not isinstance(envelope, dict):
        return "failed", stdout or "store.py add produced a non-object JSON result"
    status = envelope.get("result")
    if status not in ("stored", "deduped", "quarantined"):
        return "failed", f"store.py add reported unknown result {status!r}"
    if status == "quarantined":
        warnings = envelope.get("warnings") or []
        reason = next((w.get("reason") for w in warnings
                       if isinstance(w, dict) and w.get("reason")), "unknown")
        return "quarantined", f"quarantined by the capture policy ({reason})"
    return status, stdout


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest a closeout-remote harvest JSON array into the zmem store."
    )
    parser.add_argument("harvest_file", help="path to the harvest JSON file")
    parser.add_argument(
        "--source-ref", default=None,
        help="source-ref passed to every store.py add call "
             "(default: session:harvest-<file-stem>)",
    )
    parser.add_argument(
        "--store", default=None,
        help="explicit path to store.py "
             "(default: resolve relative to this script's repo layout, "
             "<repo>/skills/memory/scripts/store.py)",
    )
    parser.add_argument(
        "--trust", default="authoritative",
        choices=["authoritative", "untrusted-peer"],
        help="issue #373: peer mode clamps every trust-bearing field a "
             "harvest row could self-declare (signal grounding, "
             "attribution, provenance) and gates every namespace against "
             "--allow-namespace, exactly like ingest-jsonl's peer mode "
             "(issue #282). Default 'authoritative' keeps the pre-#373 "
             "behavior byte-identically.",
    )
    parser.add_argument(
        "--allow-namespace", dest="allow_namespace", action="append",
        default=[], metavar="NS",
        help="peer mode only: namespaces this harvest may write (repeatable). "
             "Rows outside the list are refused and counted as "
             "namespace_refused in the summary (counted, never fatal).",
    )
    parser.add_argument(
        "--attribute-host", dest="attribute_host", default=None,
        help="peer mode only: stamp host:<value> on every ingested row's "
             "tags (row-carried host:/agent: tags are stripped); with "
             "--source-ref the caller's ref wins, otherwise each row's "
             "source_ref is stamped peer:<value> (an explicit --source-ref "
             "overrides the stamp).",
    )
    args = parser.parse_args()

    harvest_path = Path(args.harvest_file)
    if not harvest_path.is_file():
        _print(f"[ingest-harvest] ERROR: harvest file not found: {harvest_path}", err=True)
        return 1

    try:
        raw = harvest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        _print(f"[ingest-harvest] ERROR: could not read {harvest_path}: {e}", err=True)
        return 1

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as e:
        # RecursionError: a hostile/corrupted harvest file can nest arrays/objects
        # deep enough to blow the JSON decoder's recursion limit; treat that the
        # same as malformed JSON and exit cleanly instead of a traceback.
        _print(f"[ingest-harvest] ERROR: invalid JSON in {harvest_path}: {e}", err=True)
        return 1

    if not isinstance(data, list):
        _print(
            f"[ingest-harvest] ERROR: harvest file must contain a JSON array, "
            f"got {type(data).__name__}",
            err=True,
        )
        return 1

    store_py = Path(args.store) if args.store else default_store_path()
    if not store_py.is_file():
        _print(
            f"[ingest-harvest] ERROR: store.py not found at {store_py}. "
            f"Pass --store PATH to point at it explicitly (e.g. the path the "
            f"SessionStart hook injected into context this session).",
            err=True,
        )
        return 1

    source_ref = args.source_ref or f"session:harvest-{harvest_path.stem}"

    trust_policy = None
    apply_trust_policy = None
    if args.trust == "untrusted-peer":
        # Flag contract mirrors store.py's ingest-jsonl validation
        # (storelib/cli.py, issue #282) so both importers reject the same
        # misuse with the same exit code.
        if not args.allow_namespace:
            parser.error(
                "--trust untrusted-peer requires at least one "
                "--allow-namespace (a peer harvest must be scoped; refusing "
                "everything silently is not an import)")
        for _ns in args.allow_namespace:
            if not is_valid_namespace(_ns):
                parser.error(
                    "--allow-namespace values must be namespaces the store "
                    "itself admits (scoped, e.g. project:foo or host:box), "
                    f"got {_ns!r}")
        # Lazy import: the default (authoritative) run stays stdlib-only at
        # startup; storelib.sync has no import cycle and the scripts dir is
        # already on sys.path (schema_meta import above).
        from storelib.sync import IngestTrustPolicy, apply_trust_policy
        trust_policy = IngestTrustPolicy(
            allowed_namespaces=args.allow_namespace,
            attribute_host=args.attribute_host)
    elif args.attribute_host:
        parser.error(
            "--attribute-host only applies to --trust untrusted-peer "
            "(issue #373)")

    total = len(data)
    stored = 0
    deduped = 0
    quarantined = 0
    failed = 0
    namespace_refused = 0
    trust_clamped = 0

    for i, raw_row in enumerate(data, start=1):
        row, err = validate_row(raw_row, i)
        if err:
            _print(f"[ingest-harvest] REJECTED: {err}", err=True)
            failed += 1
            continue

        if trust_policy is not None:
            # Issue #373: the namespace allow-list rejects (never clamps) —
            # counted, not fatal, exactly like ingest-jsonl's legacy peer
            # lane. The row dies here: nothing of it is forwarded.
            if not trust_policy.namespace_allowed(row["namespace"]):
                namespace_refused += 1
                _print(
                    f"[ingest-harvest] NAMESPACE-REFUSED row {i}: "
                    f"[{row['namespace']}] {row['type']}", err=True)
                continue
            # Provenance is caller-derived (issue #373, mirroring the lanes):
            # apply_trust_policy never writes caller_source_ref into the row
            # (it only skips its own stamp), so the adapter applies the
            # caller ref itself. `source_ref` here is --source-ref or the
            # adapter's own session:harvest-<stem> default — both are
            # locally derived, never row-claimed, so both are trusted input
            # and a row's own source_ref can never win in peer mode.
            row["source_ref"] = source_ref
            if apply_trust_policy(row, trust_policy,
                                  caller_source_ref=source_ref):
                trust_clamped += 1

        status, message = ingest_row(store_py, row, source_ref)
        if status == "stored":
            stored += 1
            # No content preview: the raw row text can carry the very
            # credential the policy redacted (or that dedup kept unstored) —
            # the status line names the row, the store/ledger holds the text.
            _print(f"[ingest-harvest] added row {i}: [{row['namespace']}] {row['type']}")
        elif status == "deduped":
            deduped += 1
            _print(f"[ingest-harvest] deduped row {i}: [{row['namespace']}] {row['type']}")
        elif status == "quarantined":
            quarantined += 1
            _print(f"[ingest-harvest] QUARANTINED row {i}: {message}", err=True)
        else:
            failed += 1
            _print(f"[ingest-harvest] FAILED row {i}: {message}", err=True)

    if trust_policy is not None:
        _print(f"[ingest-harvest] summary: {total} row(s) in file, "
               f"{stored} stored, {deduped} deduped, "
               f"{quarantined} quarantined, {failed} failed/rejected, "
               f"namespace_refused={namespace_refused}, "
               f"trust_clamped={trust_clamped}")
    else:
        _print(f"[ingest-harvest] summary: {total} row(s) in file, "
               f"{stored} stored, {deduped} deduped, "
               f"{quarantined} quarantined, {failed} failed/rejected")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
