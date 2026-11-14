"""tests.support.nightly_curator — evaluation harness for issue #377 (opt-in model
curator + bounded nightly collation evaluation).

Public surface:
    corpus      — frozen three-lane corpus (+ digests)
    writers     — candidate registry with honest availability
    metrics     — frozen rubric (four distinct disagreement surfaces)
    proposals   — typed proposal boundary + real belief-ops contract
    orchestration — nightly shadow prototype (real cadence/lock discipline)
    cleanup     — policy matrix + checkpoint/export/rollback/forward recovery
    report      — provenance-validated decision report (two digests)
    resources   — allowlist env builder, budgets, memory enforcement, redaction

Everything runs against ISOLATED scratch stores; no canonical store is ever touched.
"""

__version__ = "0.1.0"
