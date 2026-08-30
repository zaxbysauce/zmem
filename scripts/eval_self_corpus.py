"""Measure zmem recall against the operator's OWN corpus (issue #82).

Builds template probes from live rows in an explicit store, runs each through
the real recall pipeline, and reports whether the source row surfaces at k.
This is the self-diagnostic counterpart to scripts/eval_runner.py (whose gold
set measures the synthetic fixture corpus): it answers "how well does recall
surface MY rows" without shipping operator content anywhere — the report is
the only output, and it is never committed.

Safety contract (mirrors eval_runner.py, strictly):
- ``--store`` is REQUIRED. Without it argparse exits 2 — the script never
  resolves a default store on its own.
- If the resolved --store path IS the host default store (the path
  host.resolve_store_path() would return with no overrides), the run is
  REFUSED with exit 2 and a remediation line: take a `store.py backup`
  snapshot and point --store at the copy.
- Every probe recall is fully passive: no_telemetry=True (zero writes — the
  store stays byte-identical), no_bump=True (passive filter semantics;
  structurally excludes the change-intent unfold), link_hops=0 (no link
  expansion), no_mmr=True (pure composite order, deterministic).
- Probe generation is deterministic and domain-agnostic: rows sampled in id
  order, queries templated from each row's first sentence / distinctive
  tokens / tag-minted entity aliases. No LLM anywhere.

Stdlib-only apart from storelib. The output report MAY embed operator content
(first sentences), so --json-out defaults to OFF and the default out path is
gitignored; never commit a report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
DEFAULT_JSON_OUT_NAME = "self-corpus-results.json"

_HOME_ENV_VARS = ("ZMEM_STORE", "ZMEM_DATA",
                  "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")


def _default_store_path() -> str:
    """The store path the host WOULD resolve with a clean env (the path this
    script must refuse). Computed with the home-override env vars stripped so
    an ambient ZMEM_STORE cannot make the check compare against itself."""
    saved = {k: os.environ.pop(k) for k in _HOME_ENV_VARS if k in os.environ}
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        import host  # noqa: E402  (scripts dir on path)
        return str(Path(host.resolve_store_path()).resolve())
    finally:
        os.environ.update(saved)


def _refuse_home(store: str) -> bool:
    """True when --store resolves to the same file as the host default store."""
    try:
        return Path(store).resolve() == Path(_default_store_path()).resolve()
    except OSError:
        return False


def _bootstrap_env(store: str) -> None:
    """Force the model-absent env BEFORE storelib is imported (storelib
    freezes STORE_PATH at import; see eval_runner.py's identical seam)."""
    os.environ["ZMEM_STORE"] = store
    os.environ["ZMEM_EMBED_PROFILE"] = "fake"
    os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
    os.environ.setdefault("ZMEM_MODELS_DIR", "/nonexistent-zmem-models-dir")
    os.environ.setdefault("PYTHONUTF8", "1")


def _sample_live_rows(conn, limit: int) -> list:
    rows = conn.execute(
        """SELECT id, namespace, content, tags FROM memory
           WHERE superseded_at IS NULL
           ORDER BY id
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return rows


def _first_sentence(content: str) -> str:
    """The row's first sentence, trimmed to a probe-sized query."""
    text = re.sub(r"\s+", " ", (content or "").strip())
    if not text:
        return ""
    cut = re.split(r"(?<=[.!?:])\s", text, maxsplit=1)[0]
    return cut[:120].strip()


def _distinctive_tokens(content: str, k: int = 6) -> str:
    """Longest low-frequency-ish tokens of the row — a keyword probe variant
    distinct from the first-sentence template."""
    words = re.findall(r"[a-z][a-z0-9_-]{3,}", (content or "").lower())
    seen: list[str] = []
    for w in sorted(set(words), key=lambda x: (-len(x), x)):
        if w not in seen:
            seen.append(w)
        if len(seen) >= k:
            break
    return " ".join(seen)


def _alias_probe(tags: str) -> str:
    """First entity alias tag (`tool:rg` -> `rg`), or '' — the entity-lane
    probe variant."""
    for tag in (tags or "").split(","):
        tag = tag.strip()
        if ":" in tag:
            return tag.split(":")[-1].strip()
    return ""


def build_probes(rows: list) -> list[dict]:
    """One deterministic probe per row: first-sentence primary, alternating
    with a distinctive-token or entity-alias variant so both retrieval lanes
    get exercised. Domain-agnostic by construction."""
    probes: list[dict] = []
    for i, row in enumerate(rows):
        query = _first_sentence(row["content"])
        variant = "first-sentence"
        if i % 2 == 1:
            alt = _alias_probe(row["tags"]) or _distinctive_tokens(row["content"])
            if alt:
                query, variant = alt, "alias" if _alias_probe(row["tags"]) \
                    else "tokens"
        if not query:
            continue
        probes.append({
            "source_id": row["id"],
            "namespace": row["namespace"],
            "probe": query,
            "variant": variant,
        })
    return probes


def run_probes(conn, probes: list[dict], k_values: list[int]) -> list[dict]:
    """Recall every probe passively and record per-k source-row hits."""
    import contextlib
    import io
    from storelib.recall import recall_memory

    results: list[dict] = []
    for probe in probes:
        # Fully passive read: zero writes, no link expansion, no MMR, and the
        # no_bump gate structurally excludes the change-intent unfold.
        with contextlib.redirect_stdout(io.StringIO()):
            rows = recall_memory(
                conn,
                query=probe["probe"],
                namespace=probe["namespace"],
                limit=max(k_values),
                no_bump=True,
                no_telemetry=True,
                link_hops=0,
                include_global=False,
                no_mmr=True,
            )
        ranked = [r["id"] for r in rows]
        results.append({
            **probe,
            "hits": {str(k): probe["source_id"] in ranked[:k] for k in k_values},
            "first_rank": (ranked.index(probe["source_id"]) + 1
                           if probe["source_id"] in ranked else 0),
        })
    return results


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="eval_self_corpus.py",
        description="Self-corpus recall probe against an explicit store copy "
                    "(model-absent; the report is JSON on stdout)")
    ap.add_argument("--store", required=True,
                    help="path to the store copy to probe. REQUIRED — never "
                         "the default home store; the script refuses the home "
                         "path with a backup-snapshot remediation.")
    def _k_values(value: str) -> list[int]:
        out = []
        for part in value.split(","):
            n = int(part.strip())
            if n < 1:
                raise argparse.ArgumentTypeError(
                    f"k values must be positive integers, got {part!r}")
            out.append(n)
        if not out:
            raise argparse.ArgumentTypeError("at least one k value required")
        return out
    ap.add_argument("--k", type=_k_values, default=[5, 20],
                    help="comma-separated top-k cut points (default 5,20)")
    def _positive_int(value: str) -> int:
        n = int(value)
        if n < 1:
            raise argparse.ArgumentTypeError(
                f"must be a positive integer, got {value!r}")
        return n
    # PRR-004: SQLite treats a negative LIMIT as unlimited — a typo'd
    # `--limit -1` must be refused, not silently sample the whole store.
    ap.add_argument("--limit", type=_positive_int, default=100,
                    help="max live rows to sample (default 100)")
    ap.add_argument("--json-out", default=None,
                    help="also write the JSON report to this path. The report "
                         f"may embed operator content — the repo default "
                         f"({DEFAULT_JSON_OUT_NAME}) is gitignored; never "
                         "commit a report.")
    args = ap.parse_args()
    # Same rationale as eval_runner: reconfigure the ALREADY-CREATED console
    # streams so a cp1252 Windows console cannot abort the report on
    # non-ASCII probe content.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    if _refuse_home(args.store):
        print(f"[self-corpus] REFUSED: {args.store} resolves to the host "
              f"default store. Take a snapshot first and probe the copy:\n"
              f"  python skills/memory/scripts/store.py backup\n"
              f"  python scripts/eval_self_corpus.py --store <backup snapshot "
              f"path>", file=sys.stderr)
        return 2
    if not Path(args.store).is_file():
        # Refuse BEFORE any connect(): sqlite would happily create an empty
        # database at a typo'd path, and probing an empty store is never what
        # an operator pointing at "a copy of my corpus" wanted.
        print(f"[self-corpus] REFUSED: no store file at {args.store}. "
              f"Point --store at an existing store COPY (see `store.py "
              f"backup`), not a fresh path.", file=sys.stderr)
        return 2

    _bootstrap_env(args.store)
    sys.path.insert(0, str(SCRIPTS_DIR))
    from storelib.schema import connect  # noqa: E402

    conn = None
    try:
        try:
            conn = connect()
        except Exception as exc:
            print(f"[self-corpus] cannot open store {args.store}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        rows = _sample_live_rows(conn, args.limit)
        probes = build_probes(rows)
        if not probes:
            print("[self-corpus] no live rows to probe — nothing to measure",
                  file=sys.stderr)
            return 2
        results = run_probes(conn, probes, sorted(set(args.k)))
    finally:
        if conn is not None:
            conn.close()

    aggregate = {
        str(k): round(sum(1 for r in results if r["hits"][str(k)])
                      / len(results), 4)
        for k in sorted(set(args.k))
    }
    report = {
        "runner": "scripts/eval_self_corpus.py",
        "store": args.store,
        "k": sorted(set(args.k)),
        "rows_sampled": len(rows),
        "probes": len(results),
        "aggregate_hit_rate": aggregate,
        "per_probe": results,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
