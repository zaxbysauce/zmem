"""AC4 (#512) frozen acceptance check - scope-aware revision resolution.

CONTRACT (new public surface in ``storelib.source``; the symbol is imported
INSIDE the test so this file COLLECTS at base and fails at runtime with the
not-yet-existing-symbol error)::

    resolve_revision(conn, *, revision_of: str, scope: str) -> dict

- ``conn``: a writable sqlite3 connection to a real-schema store.
- ``revision_of``: the id of the ONE earlier memory row this approved revision
  authorizes superseding. The revision linkage is EXPLICIT - the pairing of a
  revision row with its target is this argument; a revision NEVER sweeps other
  rows by text similarity.
- ``scope``: the revision's applicability scope label. A row's scope is its
  ``memory.namespace`` value; scope ``S`` is NARROWER than scope ``R`` iff
  ``S != R`` and (``R == "global"`` or ``S.startswith(R + ":")``).
- Authorization: the call is REFUSED (no mutation) when (a) the named target
  does not exist or is already superseded, or (b) the named target's own scope
  is strictly NARROWER than the revision's scope - a broader-scope revision
  never supersedes a narrower scoped exception; superseding the exception
  requires an explicit revision scoped to it.
- Effect: tombstones (sets ``memory.superseded_at``) EXACTLY the authorized
  target, touches no other row, and commits before returning.
- Result dict (outcome vocabulary is part of the contract)::

      {"outcome": "retained_exception" | "superseded" | "refused_unauthorized",
       "superseded_ids": [ids tombstoned by this call],      # [] on refusal
       "retained_ids": [live narrower-scope exceptions verified retained],
       "refused_ids": [ids this call refused to supersede]}  # [] otherwise

  ``retained_exception`` iff the supersede applied AND ``retained_ids`` is
  non-empty; ``superseded`` iff it applied with no narrower-scope exceptions;
  ``refused_unauthorized`` iff refused.

Scenario pinned: G = broad "global"-scoped observation, E = narrower
"project:billing"-scoped exception, F = same-scope textual-overlap decoy,
G2 = the approved revision row. A revision of G retains E and supersedes only
G; a revision that does not authorize superseding E leaves E live even though
E overlaps the revised subject; nobody is swept by text similarity.
"""
from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "memory" / "scripts"
STORE = SCRIPTS / "store.py"
sys.path.insert(0, str(SCRIPTS))
NS = "project:revision-512"


def _env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "ZMEM_STORE": str(root / "store.sqlite"),
        "ZMEM_DATA": str(root),
        "ZMEM_MODELS_DIR": str(root / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_EMBED_PROFILE": "fake",
        "ZMEM_NAMESPACE": NS,
    })
    for key in ("ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE"):
        env.pop(key, None)
    return env


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE), *args], cwd=ROOT,
        env=_env(root), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)


def _add(root: Path, content: str) -> str:
    result = _run(root, "add", "--namespace", NS, "--type", "fact",
                  "--content", content, "--signal", "test",
                  "--confidence", "0.9", "--json")
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return str(json.loads(result.stdout)["id"])


def _set_scope(db_path: Path, memory_id: str, namespace: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE memory SET namespace=? WHERE id=?",
                     (namespace, memory_id))
        conn.commit()
    finally:
        conn.close()


def _superseded_at(db_path: Path, memory_id: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT superseded_at FROM memory WHERE id=?",
                           (memory_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise AssertionError(f"memory {memory_id} missing from {db_path}")
    return row[0]


def test_scope_vs_revision() -> None:
    from storelib.source import resolve_revision  # NEW surface (#512)

    with tempfile.TemporaryDirectory(prefix="zmem-revision-512-") as temp:
        root = Path(temp)
        db = root / "store.sqlite"
        init = _run(root, "init")
        assert init.returncode == 0, init.stderr
        g = _add(root, "deployments go to the shared staging cluster")
        e = _add(root, "the billing deploy goes to the dedicated cluster")
        f = _add(root,
                 "deployments go to the shared staging cluster unless an "
                 "operator overrides the target")
        # Distinct wording: the write path's content_norm dedup collapses
        # near-identical bodies onto one row, and G2 must be its OWN row.
        g2 = _add(root,
                  "revised general note: deploys land on the shared staging "
                  "cluster per the november audit")
        _set_scope(db, g, "global")
        _set_scope(db, f, "global")
        _set_scope(db, g2, "global")
        _set_scope(db, e, "project:billing")

        # Refusal half: a global-scope revision does not authorize superseding
        # the narrower scoped exception, even though E overlaps the subject.
        conn = sqlite3.connect(db)
        refused = resolve_revision(conn, revision_of=e, scope="global")
        conn.close()
        assert refused["outcome"] == "refused_unauthorized", refused
        assert refused["superseded_ids"] == [], refused
        assert refused["refused_ids"] == [e], refused
        assert _superseded_at(db, e) is None
        assert _superseded_at(db, g) is None

        # Approved revision of G: retains the applicable exception E and
        # supersedes ONLY the authorized target G (durably committed).
        conn = sqlite3.connect(db)
        applied = resolve_revision(conn, revision_of=g, scope="global")
        conn.close()
        assert applied["outcome"] == "retained_exception", applied
        assert applied["superseded_ids"] == [g], applied
        assert applied["retained_ids"] == [e], applied
        assert _superseded_at(db, g) is not None
        assert _superseded_at(db, e) is None
        # The revision row, the same-scope textual-overlap decoy, and the
        # exception all stay live: no text-similarity sweep.
        assert _superseded_at(db, g2) is None
        assert _superseded_at(db, f) is None

        # Explicit linkage: re-targeting the already-superseded row refuses
        # and still sweeps nobody.
        conn = sqlite3.connect(db)
        again = resolve_revision(conn, revision_of=g, scope="global")
        conn.close()
        assert again["outcome"] == "refused_unauthorized", again
        assert _superseded_at(db, e) is None
        assert _superseded_at(db, f) is None
        assert _superseded_at(db, g2) is None


if __name__ == "__main__":
    test_scope_vs_revision()
    print("test_scope_vs_revision passed")
