"""Reservation scrub + normalized byte-verify (issue #284, review round 2
finding 7).

The reservation sidecar's entry_text is lowercased and whitespace-collapsed
at write time (delivery_ledger.entry_text) and json.dump escapes non-ASCII,
so a purge needle that spans lines or carries accents NEVER appears in the
sidecar bytes verbatim: the pre-fix byte verify returned clean for
multi-line, non-ASCII and accented full-content needles (a scrub regression
could leave entry_text behind silently), and the scrub's own fold could not
meet a raw multi-line needle either. This pin drives the REAL reserve ->
verify -> scrub -> verify cycle in a scratch data dir:

1. reserve() plants sidecars with a multi-line and a non-ASCII row;
2. the normalized verify MUST report residue on both .reserve files
   (fails on the pre-fix verify — this is the mutant guard);
3. scrub_ledgers MUST remove both rows (fails on the pre-fix fold);
4. the post-scrub verify MUST come back clean.

Env discipline per the house pattern. Runs standalone:
python tests/test_t05_reservation_scrub_verify.py
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(REPO_ROOT / "skills" / "memory" / "scripts")

_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_INJECT",
    "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    "ZMEM_NAMESPACE", "ZMEM_RESERVATION_TTL_S",
)


@contextlib.contextmanager
def _pinned_scratch():
    tmp = tempfile.mkdtemp(prefix=f"zmem-t05-scrub-{uuid.uuid4().hex}-")
    saved = {k: os.environ.get(k) for k in _PIN_KEYS}
    for key in _PIN_KEYS:
        os.environ.pop(key, None)
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_reservation_scrub_and_verify_carry_multiline_and_nonascii():
    with _pinned_scratch() as tmp:
        # Re-pin inside the test body: pytest-randomly shuffles module
        # order and a sibling sys.path restore can strand the
        # module-level insert (see test_delivery_ledger_concurrency).
        if SCRIPTS not in sys.path:
            sys.path.insert(0, SCRIPTS)
        from storelib import delivery_ledger as dl
        from storelib import purge

        multiline = "t05 scrub alpha line one\nline two beta secret"
        nonascii = "t05 scrub café résumé gamma"
        sid = f"scrub-verify-{uuid.uuid4().hex}"
        r1 = dl.reserve(tmp, sid,
                        [{"id": "scrub-row-1", "content": multiline}],
                        "user_prompt")
        r2 = dl.reserve(tmp, sid,
                        [{"id": "scrub-row-2", "content": nonascii}],
                        "user_prompt")
        assert r1 and r2, "both reserves must plant sidecars"

        # (2) the normalized verify must SEE both rows' residue — the
        # raw-bytes verify missed BOTH (newline vs collapsed space;
        # json \\uXXXX escapes vs raw UTF-8). Checked per needle: the
        # escaped raw form happens to match the non-ASCII case, so a
        # pooled check could pass while the multi-line needle stays
        # blind (mutation-probed).
        for needle in (multiline, nonascii):
            hits = purge._verify_ledgers_clean(Path(tmp), [needle])
            reserve_hits = {k: v for k, v in hits.items()
                            if k.endswith(".reserve")}
            assert reserve_hits, (
                "the byte-verify must detect multi-line and non-ASCII "
                "residue in reservation sidecars (needle variants: "
                "collapsed whitespace, ascii fold, json \\uXXXX escapes) "
                "— a clean verify here means a scrub regression ships "
                f"silently; needle={needle!r} verify={hits!r}")

        # (3) the scrub must remove both needle-bearing rows even though
        # the raw needles carry a newline / accents the stored text does
        # not: sidecar rows are entry_text-collapsed at write time.
        stats = purge.scrub_ledgers(Path(tmp), drop_ids=set(),
                                    needles=[multiline, nonascii])
        assert stats["entries_dropped"] >= 2, (
            "the reservation scrub must drop both needle-bearing rows "
            f"(entries_dropped counts the emptied sidecars): {stats!r}")

        # (4) post-scrub verify is clean — fail-closed purge can proceed.
        after = purge._verify_ledgers_clean(Path(tmp),
                                            [multiline, nonascii])
        assert after == {}, (
            f"post-scrub verify must be clean; residue: {after!r}")
        assert not list((Path(tmp) / "ops").glob("resv-*.reserve")), (
            "emptied reservation sidecars must be deleted by the scrub")


_TESTS = (
    test_reservation_scrub_and_verify_carry_multiline_and_nonascii,
)

if __name__ == "__main__":
    failures = []
    for test in _TESTS:
        name = test.__name__
        try:
            test()
            print(f"ok    {name}")
        except AssertionError as exc:
            failures.append((name, f"AssertionError: {exc}"))
            print(f"FAIL  {name}: AssertionError: {exc}")
        except Exception as exc:  # a crashed check is a failed check
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    if failures:
        for name, error in failures:
            print(f"collected: {name}: {error}")
        sys.exit(1)
    print(f"{len(_TESTS)} tests completed")
