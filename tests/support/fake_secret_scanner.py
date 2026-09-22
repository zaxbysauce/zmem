"""Deterministic secret-scanner double for dataset publish tests (issue
#134). The real seam is ``storelib.dataset.SecretScanner.scan(serialized_rows)
-> list[dict]``; this fake returns scripted findings keyed by row index so
publish tests can drive hold-back without TruffleHog.
"""

from __future__ import annotations


class FakeSecretScanner:
    """Returns one finding per scripted index.

    ``findings`` is a list of ints (payload indexes) or dicts (each carrying
    at least ``row_index``). A payload whose bytes contain a needle from
    ``flag_if_contains`` is also reported, so tests can flag "whatever row
    carries this secret" without knowing payload order."""

    def __init__(self, findings=(), flag_if_contains=()) -> None:
        self._explicit: list[int] = []
        for f in findings:
            idx = f.get("row_index") if isinstance(f, dict) else f
            self._explicit.append(int(idx))
        self._needles = [n.encode("utf-8") if isinstance(n, str) else n
                         for n in flag_if_contains]
        self.calls = 0

    def scan(self, serialized_rows) -> list:
        self.calls += 1
        found: list[dict] = []
        for i, blob in enumerate(serialized_rows):
            if i in self._explicit or any(n in blob for n in self._needles):
                found.append({"row_index": i, "reason": "secret_scan",
                              "detector": "fake"})
        return found
