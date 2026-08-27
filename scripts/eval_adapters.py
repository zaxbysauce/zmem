"""Public-corpus adapters for the zmem eval gold format (issue #64, 9.3).

Converts published eval sets into the gold JSONL shape that
scripts/eval_runner.py and `store.py tune-weights` consume:

    python scripts/eval_adapters.py --adapter longmemeval --input <path> --out <path>
    python scripts/eval_adapters.py --adapter locomo     --input <path> --out <path>

SKIP-IF-MISSING contract: when --input does not exist (or is unreadable) the
adapter prints "skipped: ..." to stdout and exits 0. CI never downloads
corpora or Hugging Face datasets; the adapters only run when an operator
points them at an on-disk corpus. No copyrighted eval text is committed —
tests/fixtures/adapters/ ships 3-row SYNTHETIC toy files that prove each
converter's mapping without reproducing any corpus content.

Output items carry bucket "adapter" and assert must_include_text (ids are
minted when the converted corpus is loaded into a store, so text assertions
are the only honest expectation). Structurally, a converted file validates
through storelib.eval_gold.load_gold like any hand-written gold set.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Adapter namespaces: converted items are namespaced so loading a converted
# corpus alongside a hand-built store cannot collide with project rows.
_LONGMEMEVAL_NS = "project:eval-corpus-longmemeval"
_LOCOMO_NS = "project:eval-corpus-locomo"


def _gold_item(item_id: str, query: str, answer: str, namespace: str) -> dict:
    """One adapter gold item: assert the expected ANSWER text surfaces for the
    QUESTION (bucket 'adapter'; no ids — they are minted at import time)."""
    return {
        "id": item_id,
        "bucket": "adapter",
        "query": query,
        "namespace": namespace,
        "must_include_text": answer[:200],
    }


def convert_longmemeval(path: str, max_items: int | None) -> list[dict]:
    """LongMemEval-shaped JSONL: one JSON object per line with a question and
    its expected answer. Recognized fields (any alias is accepted):
    question_id|id, question|query, answer|expected_answer."""
    items = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            question = obj.get("question") or obj.get("query") or ""
            answer = obj.get("answer") or obj.get("expected_answer") or ""
            if not question or not answer:
                raise ValueError(
                    f"{path}:{lineno}: longmemeval row needs "
                    "'question'+'answer' (or query/expected_answer)")
            item_id = str(obj.get("question_id") or obj.get("id")
                          or f"longmemeval-{lineno}")
            items.append(_gold_item(f"longmemeval-{item_id}", question,
                                    answer, _LONGMEMEVAL_NS))
            if max_items and len(items) >= max_items:
                break
    return items


def convert_locomo(path: str, max_items: int | None) -> list[dict]:
    """LoCoMo-shaped JSON: a list of conversations, each with a `conversation`
    list of turns carrying `speaker`/`text` (field aliases: role/content).
    Every other turn becomes a gold item asserting its own text surfaces for
    a query built from the turn (a structural round-trip, not a semantic
    benchmark pass)."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("conversations", [])
    if not isinstance(data, list):
        raise ValueError(f"{path}: locomo corpus must be a list of conversations")
    items = []
    for conv_i, conv in enumerate(data, start=1):
        turns = conv.get("conversation") or conv.get("turns") or []
        for turn_i, turn in enumerate(turns, start=1):
            speaker = turn.get("speaker") or turn.get("role") or "speaker"
            text = turn.get("text") or turn.get("content") or ""
            if not text:
                continue
            items.append(_gold_item(
                f"locomo-c{conv_i}-t{turn_i}",
                f"{speaker}: {text[:160]}",
                text,
                _LOCOMO_NS,
            ))
            if max_items and len(items) >= max_items:
                return items
    return items


ADAPTERS = {
    "longmemeval": convert_longmemeval,
    "locomo": convert_locomo,
}


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="eval_adapters.py",
        description="Convert a public eval corpus into zmem gold JSONL "
                    "(skips cleanly when the corpus is not on disk)")
    ap.add_argument("--adapter", required=True, choices=sorted(ADAPTERS),
                    help="which corpus format to convert")
    ap.add_argument("--input", required=True,
                    help="path to the on-disk corpus file (never downloaded; "
                         "a missing path prints 'skipped' and exits 0)")
    ap.add_argument("--out", required=True,
                    help="path of the converted gold JSONL to write")
    ap.add_argument("--max-items", type=int, default=None,
                    help="optional cap on converted items")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.is_file():
        print(f"skipped: {args.adapter} corpus not found at {args.input}")
        return 0

    try:
        items = ADAPTERS[args.adapter](str(src), args.max_items)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[eval] adapter {args.adapter}: cannot convert {args.input}: "
              f"{exc}", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"[eval] adapter {args.adapter}: wrote {len(items)} gold item(s) "
          f"to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
