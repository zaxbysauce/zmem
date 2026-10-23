"""Broker CLI — the launcher's remote-mode hook entry (issue #287).

Reads one hook payload JSON document on stdin, serves the moment through
``fleet.broker.handle_event``, and prints the envelope in the SAME
sentinel-wrapped form the bash hooks print (``<<<ZMEM_JSON>>>…<<<END>>>``,
zmem-recall.sh:145) so the launcher's existing ``extractPayload`` /
envelope machinery parses it unchanged (the neutralization already
happened inside the broker).  Exit code is ALWAYS 0 — the fail-open
contract belongs to the broker; diagnostics go to stderr only.

Both stdio streams are pinned to UTF-8 at the single process entry (the
storelib/cli.py pattern): the launcher writes the payload as UTF-8 bytes
and a bare ``sys.stdin.read()`` would decode them with the process locale
codec — cp1252 on a default Windows install — discarding the whole
payload on a cp1252-undefined byte and mojibake-ing prompts otherwise,
while an ``ensure_ascii=False`` envelope containing non-Latin-1 text
would raise UnicodeEncodeError mid-write and drop the entire
additionalContext.

Usage (the launcher spawns this with the resolved python):
    python skills/memory/scripts/fleet/broker_cli.py <EventName> < payload.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from fleet import broker  # noqa: E402  (after the sys.path bootstrap)


def main(argv: list[str]) -> int:
    event = argv[1] if len(argv) > 1 else ""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception as exc:  # noqa: BLE001 - malformed stdin fails open
        print(f"broker_cli: unreadable payload: {exc}", file=sys.stderr)
        payload = {}
    try:
        envelope = broker.handle_event(event, payload)
    except Exception as exc:  # noqa: BLE001 - the hook contract is fail-open
        print(f"broker_cli: broker failure: {exc}", file=sys.stderr)
        envelope = {"additionalContext": ""}
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # noqa: BLE001 - best effort
        pass
    body = json.dumps(envelope, ensure_ascii=False)
    # The sentinel wrap the launcher's extractPayload requires; the broker
    # already neutralized fence markers inside the payload text.
    sys.stdout.write("<<<ZMEM_JSON>>>" + body + "<<<END>>>\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
