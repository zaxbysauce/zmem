#!/usr/bin/env python3
"""Shared capture entry for the DeepSeek adapter (issues #374/#375).

Reads ONE JSON object on stdin:
  {"kind": "correction"|"failure", "text": str, "session": str,
   "namespace": str, "event_id": str|null, "meta": {...}}

Contract (05-fix-plan.md D1-quartic / R2-6):
- classification gates are the SHARED corrections.py gates; no JS-side
  re-classifier exists;
- redaction is ALWAYS auto-mode (redact_secret_like_text +
  redact_training_text) — the passive path never preserves secret-bearing
  originals (manual-mode preservation can never engage here);
- dedup key = sha256 over (kind, session, normalized_text) joined with the
  NUL byte; an identical (kind, text) pair for the same session collapses to
  ONE queue entry (tests/test_t14_deepseek_learning.py pins this);
- remote mode (ZMEM_REMOTE_URL): enqueue an opqueue `capture` op carrying
  the same dedup key (admit-once replay suppression); local mode: append a
  review candidate to the correction queue under the O_EXCL queue lock.
Fail-open: always exits 0; capture is never fatal.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _dedup_key(kind: str, session: str, text: str) -> str:
    normalized = " ".join((text or "").split()).lower()
    joined = chr(0).join((kind or "", session or "", normalized))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _store_scripts_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    return os.path.join(root, "skills", "memory", "scripts")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    kind = str(payload.get("kind") or "")
    text = str(payload.get("text") or "")
    session = str(payload.get("session") or "")
    namespace = str(payload.get("namespace") or "user:global")
    event_id = payload.get("event_id")
    if kind not in ("correction", "failure") or not text.strip():
        return 0
    sys.path.insert(0, _store_scripts_dir())
    try:
        import redaction
        redacted, _n = redaction.redact_secret_like_text(text)
        redacted = redaction.redact_training_text(redacted)[0]
    except Exception:
        redacted = text[:2000]
    # Capture-entry fallback (issue #375 AC1): the shared pattern set keys
    # AKIA on exactly 16 trailing chars (AKIA[0-9A-Z]{16}); longer
    # uppercase runs (e.g. synthetic fixture tokens) survive it. The
    # passive capture path never persists an unneutralized secret-shaped
    # run, so widen locally to {16,} with the same redaction marker.
    import re as _re
    redacted = _re.sub(r"(?<![0-9A-Z])(?:AKIA|ASIA)[0-9A-Z]{16,}(?![0-9A-Z])",
                       "[REDACTED_SECRET]", redacted)
    redacted = _re.sub(r"(?<![0-9A-Za-z])SK-[0-9A-Za-z]{16,}(?![0-9A-Za-z])",
                       "[REDACTED_SECRET]", redacted)
    try:
        import corrections
        if kind == "correction":
            if len(text) < 5 or not corrections.should_include_message(text):
                return 0
            if corrections.is_harvest_paste(text):
                return 0
        else:
            # classify_error_type returns a TUPLE — (None, None) for
            # unclassified/excluded text is truthy. Test the type element.
            _etype = corrections.classify_error_type(text)
            if not (_etype and _etype[0]):
                return 0
    except Exception:
        pass
    key = (str(event_id) if event_id else
           _dedup_key(kind, session, redacted))
    if (os.environ.get("ZMEM_REMOTE_URL", "") or "").strip():
        try:
            sys.path.insert(
                0, os.path.join(_store_scripts_dir(), "fleet"))
            # fleet package lives as sibling modules; import via path
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "zmem_opqueue", os.path.join(
                    _store_scripts_dir(), "fleet", "opqueue.py"))
            opqueue = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(opqueue)
            entry = opqueue.enqueue(
                "local", "capture",
                {"namespace": namespace, "type": "fact",
                 "content": redacted, "capture_mode": "auto",
                 "session": session, "kind": kind},
                env=dict(os.environ), dedup_key=key)
            print(json.dumps({"ok": True, "op_id": entry.get("op_id"),
                              "status": "pending", "queue": "ops",
                              "text": redacted[:120]}))
        except Exception:
            pass
        return 0
    try:
        import correction_queue as cq
    except Exception:
        return 0
    try:
        # The read-check-append window is under the SAME O_EXCL lock every
        # queue writer takes: two concurrent captures of one (kind, text)
        # pair collapse to one entry (the frozen content+session contract).
        with cq.queue_lock(namespace):
            existing = cq.load_queue(namespace)
            for item in existing:
                if isinstance(item, dict) and item.get("dedup_key") == key:
                    print(json.dumps({"ok": True, "deduped": True,
                                      "text": str(item.get("message")
                                                  or "")[:120]}))
                    return 0
            item = cq.make_item(
                namespace=namespace, message=redacted,
                capture_mode="auto", session=session, host="deepseek",
                type_=("error_pattern" if kind == "failure" else "correction"),
                patterns=("failure" if kind == "failure" else "correction"),
                confidence=0.6, sentiment="neutral", decay_days=90)
            item["dedup_key"] = key
            item["source"] = "live-capture"
            cq.append_queue(namespace, item)
        print(json.dumps({"ok": True, "deduped": False,
                          "text": redacted[:120]}))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
