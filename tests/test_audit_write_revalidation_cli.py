"""Public-command legs for the amended #507 acceptance checks (C5/C6):
the same second-connection interleaves, driven through the CLI command
surface (`store.py update ...` and `store.py consolidate ...`) instead of
the library functions — the issue's "through the public commands" wording.
RED at base 5a5560ba, GREEN after the fix.

Legs: the update identity interleave (concurrent type/source_ref preserved
in the successor), the round-2 default-intent capture-policy leg (tags a
second connection commits are redacted exactly as the public add path
redacts them, with the capture-policy warning emitted exactly once), and
the consolidate interleave (a neighbour invalidated after selection keeps
its invalidation).

Companion to tests/test_audit_write_revalidation.py: the shared fixtures
(env pin, guard, throwaway stores, one-shot seams) are imported from that
module's _InterleaveCaseBase, and ITS module-level wrappers call
run_cli_update_leg()/run_cli_consolidate_leg() here so the published pytest
node ids still run every leg. This file is also collected by pytest on its
own and runs standalone (CI): python -X utf8
tests/test_audit_write_revalidation_cli.py.
"""

from __future__ import annotations
import importlib
import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
# Make the sibling original module importable under every execution shape
# (pytest already puts this directory on sys.path; a direct run gets it from
# the script's own directory, but be explicit anyway).
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import test_audit_write_revalidation as base  # noqa: E402  (pins env + guard)
import _store_guard  # noqa: E402,F401  (#244 wiring census: the literal
#                      per-file import; the base module above already pins
#                      the env and the guard)

# Round 2, critic leg 6: a tag the DEFAULT capture intent (auto — no
# --capture-mode flag, ZMEM_CAPTURE_MODE unset) treats specially. The 32-hex
# run matches redaction.SECRET_GENERIC_PATTERNS, so the auto policy redacts
# the value span and merges in the auto-redacted marker instead of storing
# the tag verbatim; the scenario-setup step below proves that through the
# public add path and takes the stored form as the expected value.
SECRETISH_TAG = "deploy-0123456789abcdef0123456789abcdef"


class AuditWriteRevalidationCliTest(base._InterleaveCaseBase):
    """The #507 interleaves must hold through the CLI dispatch too."""

    def test_update_through_public_command(self):
        """AC6-507 (RED at base, GREEN after the fix): driving update
        through store.py's argparse dispatch (main() in-process), a second
        connection's concurrent type/source_ref commit between the pre-lock
        read and the write is preserved in the successor."""
        conn, other = self._new_store()
        mid = self._seed_one(conn, "cliupd")
        # Plain-word content: the CLI's default capture intent is 'auto',
        # whose redactor rewrites hex-shaped payloads; --capture-mode manual
        # (below) keeps the stored bytes identical to the argument anyway.
        new_content = "cli corrected deployment fact rev one"

        def fire():
            other.execute(
                "UPDATE memory SET type=?, source_ref=? WHERE id=?",
                ("lesson", "session:concurrent-cli", mid))
            other.commit()

        # Interleave point: inside update_memory's _validate_namespace call
        # — after the pre-lock row read, before BEGIN IMMEDIATE — while the
        # CLI's connection holds no write lock (the file's existing
        # technique; see _hook_second_writer).
        undo = self._hook_second_writer(fire)
        try:
            rc, out, err = self._cli(
                ["store.py", "update", "--id", mid, "--content", new_content,
                 "--capture-mode", "manual"])
        finally:
            undo()
        self.assertEqual(rc, 0,
                         "AC6-507 scenario setup: the CLI update must exit 0 "
                         "(rc=%r; stderr tail=%r)"
                         % (rc, err[-300:]))
        live = self._live_successors(conn, mid)
        self.assertEqual(len(live), 1,
                         "AC6-507: exactly one live successor must exist "
                         "after the CLI update (found %r)" % (live,))
        succ = self._row(conn, live[0])
        self.assertEqual(succ["content"], new_content,
                         "AC6-507: the successor of the CLI update lost the "
                         "caller's new content (content=%r)"
                         % (succ["content"],))
        self.assertEqual(succ["type"], "lesson",
                         "AC6-507: the successor of the CLI update carries "
                         "the stale type from the pre-lock read instead of "
                         "the concurrently committed value (type=%r; "
                         "expected 'lesson')" % (succ["type"],))
        self.assertEqual(succ["source_ref"], "session:concurrent-cli",
                         "AC6-507: the successor of the CLI update carries "
                         "the stale source_ref from the pre-lock read "
                         "instead of the concurrently committed value "
                         "(source_ref=%r)" % (succ["source_ref"],))
        old = self._row(conn, mid)
        self.assertEqual((old["type"], old["source_ref"]),
                         ("lesson", "session:concurrent-cli"),
                         "AC6-507: the old row no longer keeps the "
                         "concurrently committed identity after the CLI "
                         "update (type=%r, source_ref=%r)"
                         % (old["type"], old["source_ref"]))

    def test_capture_policy_final_values_through_public_command(self):
        """AC6-507 (RED at base, GREEN after the fix): the CLI update's
        DEFAULT capture intent (auto — no --capture-mode flag) must apply to
        the FINAL field values: tags a second connection commits between the
        pre-lock read and the write are redacted exactly as the public add
        path redacts them, and the capture-policy warning is emitted exactly
        once."""
        with self.subTest(
                "capture policy applies to the final tags under the "
                "default intent"):
            # Scenario setup: prove through the PUBLIC add path (same default
            # capture intent — no --capture-mode flag) that this tag is
            # treated specially, and capture what that path stores for it.
            ref_conn, _ref_other = self._new_store()
            rc, _out, err = self._cli(
                ["store.py", "add", "--namespace", base.NS, "--type", "fact",
                 "--content", "capture reference fact rev one",
                 "--tags", SECRETISH_TAG, "--source-ref", "audit",
                 "--signal", "test"])
            self.assertEqual(rc, 0,
                             "AC6-507 scenario setup: the reference add must "
                             "exit 0 under the default capture intent "
                             "(rc=%r; stderr tail=%r)" % (rc, err[-300:]))
            ref_rows = ref_conn.execute(
                "SELECT tags FROM memory WHERE superseded_at IS NULL"
            ).fetchall()
            self.assertEqual(
                len(ref_rows), 1,
                "AC6-507 scenario setup: the reference add must store "
                "exactly one live row (found %d)" % (len(ref_rows),))
            stored_tag = ref_rows[0]["tags"]
            self.assertNotEqual(
                stored_tag, SECRETISH_TAG,
                "AC6-507 scenario setup: the default capture intent must "
                "treat this tag specially — the public add stored it "
                "VERBATIM (%r); without a rewrite/redaction this leg has no "
                "derived value to demand" % (stored_tag,))

            conn, other = self._new_store()
            mid = self._seed_one(conn, "capupd")

            def fire():
                other.execute("UPDATE memory SET tags=? WHERE id=?",
                              (SECRETISH_TAG, mid))
                other.commit()

            # Interleave point: inside update_memory's _validate_namespace
            # call — after the pre-lock row read, before BEGIN IMMEDIATE —
            # while the CLI's connection holds no write lock (the original
            # file's existing technique; see _hook_second_writer).
            undo = self._hook_second_writer(fire)
            try:
                rc, out, err = self._cli(
                    ["store.py", "update", "--id", mid,
                     "--content",
                     "capture policy corrected fact rev two"])
            finally:
                undo()
            self.assertEqual(rc, 0,
                             "AC6-507 scenario setup: the CLI update must "
                             "exit 0 under the default capture intent "
                             "(rc=%r; stderr tail=%r)"
                             % (rc, err[-300:]))
            live = self._live_successors(conn, mid)
            self.assertEqual(len(live), 1,
                             "AC6-507: exactly one live successor must exist "
                             "after the CLI update (found %r)" % (live,))
            succ = self._row(conn, live[0])
            self.assertEqual(
                succ["tags"], stored_tag,
                "AC6-507: the successor's tags were not produced by the "
                "capture policy over the FINAL (concurrently committed) "
                "tags — expected the %r the public add path stores for that "
                "tag, got %r (neither the stale original tags nor the raw "
                "concurrent tag may be stored)"
                % (stored_tag, succ["tags"]))
            combined = out + err
            warning_lines = (
                combined.count("WARNING (advisory, before write attempt)")
                + combined.count(
                    "NOTICE (automatic capture sanitized; write pending)"))
            self.assertEqual(
                warning_lines, 1,
                "AC6-507: the capture-policy warning for the final tags "
                "must be printed EXACTLY once in the CLI update's combined "
                "output — once proves the FINAL values were scanned, and "
                "not twice proves the stale pre-lock values were not "
                "re-scanned (found %d; combined output tail=%r)"
                % (warning_lines, combined[-400:]))

    def test_consolidate_through_public_command(self):
        """AC5-507 (RED at base, GREEN after the fix): driving consolidate
        through store.py's argparse dispatch (main() in-process, --json), a
        neighbour invalidated between selection and the cluster transaction
        keeps that invalidation and the stale merge is refused."""
        conn, other = self._new_store()
        keeper, neighbor = self._seed_pair(conn)

        # Interleave point: inside _admitted_neighbors — after the admitted
        # list is computed, before the per-cluster BEGIN — while the CLI's
        # consolidating connection holds no write lock (the file's existing
        # technique; see _hook_invalidate_during_admission).
        consolidate_mod = importlib.import_module("storelib.consolidate")
        undo = base._one_shot(
            consolidate_mod, "_admitted_neighbors",
            lambda: self._invalidate(other, neighbor))
        try:
            rc, out, err = self._cli(
                ["store.py", "consolidate", "--force", "--json"])
        finally:
            undo()
        self.assertEqual(rc, 0,
                         "AC5-507 scenario setup: the CLI consolidate must "
                         "exit 0 (rc=%r; stderr tail=%r)"
                         % (rc, err[-300:]))
        try:
            report = json.loads(out)
        except ValueError as exc:
            self.fail("AC5-507 scenario setup: consolidate --json must print "
                      "one parseable report object on stdout (%s; got %r)"
                      % (exc, out[:300]))
        self.assertEqual(report.get("merged"), 0,
                         "AC5-507: the CLI consolidate merged a neighbour "
                         "invalidated after selection — the stale decision "
                         "must be refused, not committed "
                         "(report['merged']=%r)" % (report.get("merged"),))
        nb = self._row(conn, neighbor)
        self.assertEqual(
            (nb["superseded_at"], nb["supersede_reason"]),
            (base.A_NOW, base.INVALIDATE_REASON),
            "AC5-507: the CLI consolidate overwrote a neighbour's concurrent "
            "invalidation: tombstone now (%r, %r), second writer wrote (%r, "
            "%r)" % (nb["superseded_at"], nb["supersede_reason"],
                     base.A_NOW, base.INVALIDATE_REASON))
        self.assertEqual(self._row(conn, keeper)["content"], base.KEEPER_TEXT,
                         "AC5-507: the keeper's content grew with the "
                         "invalidated neighbour's text under the CLI "
                         "consolidate (now %r)"
                         % (self._row(conn, keeper)["content"],))
        self.assertNotIn(neighbor,
                         self._row(conn, keeper)["merged_from"] or "",
                         "AC5-507: the keeper's merged_from lists the "
                         "invalidated neighbour under the CLI consolidate "
                         "(merged_from=%r)"
                         % (self._row(conn, keeper)["merged_from"],))


def _run_cli_case(method_name):
    """Same published-argv runner shape as the original module's _run_case:
    run one TestCase method under a real unittest runner (subTest included)."""
    suite = unittest.TestSuite()
    suite.addTest(AuditWriteRevalidationCliTest(method_name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


def run_cli_update_leg():
    """Invoked by the original module's test_metadata_interleave wrapper:
    BOTH public update legs (identity interleave + default-intent capture
    policy over the final tags) always run."""
    ok = _run_cli_case("test_update_through_public_command")
    ok = _run_cli_case(
        "test_capture_policy_final_values_through_public_command") and ok
    return ok


def run_cli_consolidate_leg():
    """Invoked by the original module's test_invalidated_neighbor wrapper."""
    return _run_cli_case("test_consolidate_through_public_command")


if __name__ == "__main__":
    unittest.main(verbosity=2)
