"""Issue #504 audit, AC2 companion legs (S2-S4): a restore that fails must
preserve the destination's sidecar state and its own failure contract.
RED at base, GREEN after the fix.

These legs complete ``test_recovery_preserves_later_write`` in
tests/test_audit_restore_identity.py (whose wrapper imports and runs
``run_legs()`` below), and they also run standalone:

- S2 -- a write that lived only in the destination ``-wal`` (a crashed
  writer): the main file plus a committed ``-wal`` are planted with no
  ``-shm`` (scenario-setup proof: a normal SQLite open on a private copy
  sees the row, a main-file-only ``mode=ro&immutable=1`` open does not).
  The same failing restore must RETURN a non-zero int with
  ``restore FAILED`` and a ``rolled back`` line naming the ``prerestore*``
  safety copy, and a ``set aside`` line naming a location that holds a
  byte-identical copy of the planted ``-wal`` (a ``restore-forensic-*``
  directory under the backup directory, which also keeps the pre-restore
  main file under its unchanged name).  The rollback target is exactly
  what SQLite read from the destination before the restore -- main file
  plus the planted ``-wal``'s committed frame -- so the ``-wal``-only row
  IS live afterwards, no ``-wal`` byte-identical to the planted log stays
  beside the store, the safety copy opens alone (no sidecars) and contains
  the row, the forensic main file read ``immutable`` holds exactly the
  main-file rows, and that main file plus the planted ``-wal`` still
  yields the row.  (Amended 2026-11-23 to the decided rollback policy; the
  earlier checkpoint-only target was CHECK_WRONG -- the acceptance
  criterion never stated it.)
- S3 -- failures BEFORE the swap (garbage ``-wal``, ``-shm`` and
  ``-journal`` sidecars with distinct contents planted beside the live
  store; one-shot faults on the first / second / third destination-sidecar
  file operation, on the ``os.replace`` that would install a file on the
  live store path, and a put-back double fault arming the second AND the
  third sidecar operation): each leg FIRST proves the fault was actually
  reached (enough sidecar file operations were observed), then requires a
  non-zero int plus ``restore FAILED``, the main file and every sidecar
  (``-wal``, ``-shm``, ``-journal``) byte-identical to before the call at
  their original paths, live rows intact, a later public ``add`` landing,
  and no NEW staged copy or stash left beside the store.  The put-back
  double fault (F5) is pinned exactly instead: one ``is set aside at``
  line names a ``<store name>.restore-stash-*`` directory BESIDE the store
  (never under the backup directory) holding exactly one file,
  ``<store name>-wal``, byte-identical to the planted ``-wal``; the
  planted ``-wal`` is gone from its original path; ``-shm``/``-journal``
  stay byte-identical; no staged copy remains; the store-named directory
  entries differ from before the restore in exactly two ways (the ``-wal``
  gone, one stash directory added); and the output carries a
  byte-copies/forensic line but NOT ``rolled back`` (the rollback itself
  failed).  The two-sidecar leg (F6) records the operation ORDER on the
  destination sidecar paths and pins it: set-aside of ``-wal``, set-aside
  of ``-shm``, FAILED set-aside of ``-journal``, FAILED put-back of
  ``-shm``, then the put-back of ``-wal`` (put-backs run in the REVERSE
  order of the set-asides and a failed put-back does not stop the
  remaining ones); the ``-wal`` is back byte-identical, the ``-shm`` sits
  alone byte-identical in the stash named by exactly one ``is set aside
  at`` line, the ``-journal`` is untouched byte-identical, no staged copy
  remains, the main file is byte-identical, the rows are intact, and the
  output carries ``restore FAILED`` plus a byte-copies/forensic line but
  NOT ``rolled back``.
- S4 -- no safety copy: with the destination missing the failed restore
  must leave nothing behind; with a 0-byte destination it must leave the
  0-byte file exactly as it was; both print ``pre-restore backup skipped``.
  In S1, S2, S4a and S4b the no-sidecars/no-stage/no-stash condition is
  evaluated immediately after the restore returns, BEFORE any check opens
  the store.
- S5 -- a quiesced destination with no sidecars (success leg): a restore
  through the public CLI must exit 0, create no ``restore-forensic-*``
  directory, print no ``removed stale`` line, and leave no ``-wal``,
  ``-shm`` or ``-journal`` beside the store when it returns, with both
  control rows intact -- a restore onto a store without sidecars must not
  manufacture sidecars and then treat them as its own.
- S6 -- a failed restore (fault right after the swap) onto a quiesced
  NON-EMPTY destination with no sidecars: a controlled non-zero result
  with stderr carrying ``[zmem] restore FAILED: <cause>`` and then
  ``[zmem] restore: rolled back: live store re-installed from the
  pre-restore backup <path>`` naming the one existing ``prerestore*``
  copy; NO ``set aside`` line anywhere; no ``restore-forensic-*``
  directory; the live rows exactly as before; no sidecar, staged copy or
  stash beside the store (judged immediately after the restore returns,
  before anything opens the store); and a later public ``add`` landing.

Hermetic: every subprocess and the in-process legs pin a fresh throwaway
store (ZMEM_STORE/ZMEM_DATA/ZMEM_BACKUP_DIR under a system-temp scratch
dir, ZMEM_MODELS_DIR nonexistent, autodownload off, UTF-8 IO); ambient
zmem/plugin env is stripped from children; the operator store is never
opened.  Run: C:/Python311/python.exe -X utf8 <this file>.
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import io
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PY = sys.executable

# Module-level scratch + env pin BEFORE the _store_guard import (issue #244)
# and BEFORE any storelib import: the guard judges the ambient env at ITS
# import, and storelib freezes env-derived paths at import.
_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-504-recovery-"))


@atexit.register
def _cleanup_scratch():
    shutil.rmtree(_SCRATCH, ignore_errors=True)


os.environ.update({
    "ZMEM_STORE": str(_SCRATCH / "guard-pin.sqlite"),
    "ZMEM_DATA": str(_SCRATCH),
    "ZMEM_BACKUP_DIR": str(_SCRATCH / "backups"),
    "ZMEM_MODELS_DIR": str(_SCRATCH / "no-models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
})

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

_SAVED_MODULES = {}
for _name in [k for k in list(sys.modules)
              if k == "storelib" or k.startswith("storelib.") or k == "schema_meta"]:
    _SAVED_MODULES[_name] = sys.modules.pop(_name)
_PATH_INSERTED = str(SCRIPTS_DIR) not in sys.path
if _PATH_INSERTED:
    sys.path.insert(0, str(SCRIPTS_DIR))
_PINNED_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_BACKUP_DIR", "ZMEM_MODELS_DIR",
                "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING")
_SAVED_ENV = {k: os.environ.get(k) for k in _PINNED_KEYS}

import storelib  # noqa: E402
from storelib.backup import cmd_restore as _cmd_restore  # noqa: E402

_STRIP_ENV = ("ZMEM_STORE ZMEM_DATA ZMEM_HOME ZMEM_BACKUP_DIR ZMEM_NAMESPACE ZMEM_HOST "
              "ZMEM_PROJECT_DIR CLAUDE_PROJECT_DIR ZCODE_PROJECT_DIR CLAUDE_PLUGIN_DATA "
              "ZCODE_PLUGIN_DATA ZMEM_SESSION ZMEM_SESSION_ID CLAUDE_SESSION_ID "
              "ZCODE_SESSION_ID ZMEM_INJECT ZMEM_INJECT_TOKEN_BUDGET ZMEM_MODEL_AUTODOWNLOAD "
              "ZMEM_MODELS_DIR ZMEM_EMBED_PROFILE ZMEM_QUERY_CONTEXT ZMEM_TRANSCRIPT ZMEM_TIER0 "
              "ZMEM_SKILLS_DIRS ZMEM_PYTHON ZMEM_CROSS_PROJECT ZMEM_REMOTE_URL "
              "ZMEM_CROSS_ENCODER ZMEM_ALLOW_NEWER_SCHEMA ZMEM_ALLOW_REAL_STORE").split()

_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _apply_env(mapping):
    for key, value in mapping.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def tearDownModule():  # noqa: N802 - unittest hook
    """Restore the ambient env / module cache (pytest co-run citizenship)."""
    for key in [k for k in list(sys.modules)
                if k == "storelib" or k.startswith("storelib.") or k == "schema_meta"]:
        del sys.modules[key]
    sys.modules.update(_SAVED_MODULES)
    if _PATH_INSERTED:
        try:
            sys.path.remove(str(SCRIPTS_DIR))
        except ValueError:  # pragma: no cover - defensive
            pass
    _apply_env(_SAVED_ENV)
    shutil.rmtree(_SCRATCH, ignore_errors=True)


def _child_env(tmp: Path, store: Path, backup_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({"ZMEM_STORE": str(store), "ZMEM_DATA": str(tmp), "ZMEM_BACKUP_DIR": str(backup_dir),
                "ZMEM_MODELS_DIR": str(tmp / "no-models"), "ZMEM_MODEL_AUTODOWNLOAD": "0",
                "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    return env


def _digest_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


class _RestoreRecoveryCompanionBase(unittest.TestCase):
    """Fresh throwaway store per leg; public-CLI driver helpers."""

    # Every assertion message starts with the acceptance marker; suppress
    # unittest's standard "x != y :" prefix so failure texts are
    # marker-first.
    longMessage = False

    def setUp(self):
        self.mk = "AC4-504"
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-504-rec-"))
        self.store = self.tmp / "store.sqlite"
        self.backup_dir = self.tmp / "backups"
        self.ns = "project:audit504"
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _fresh_leg(self):
        """Give the next leg its own throwaway store dir: every leg runs in
        the SAME TestCase method (one subTest per leg), so setUp alone
        cannot isolate them."""
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-504-rec-"))
        self.store = self.tmp / "store.sqlite"
        self.backup_dir = self.tmp / "backups"
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _cli(self, *args):
        return subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY)] + list(args), capture_output=True,
            text=True, env=_child_env(self.tmp, self.store, self.backup_dir),
            cwd=str(REPO_ROOT), timeout=180)

    def _add(self, content, *, setup=False, what="control row"):
        prefix = f"{self.mk}: " + ("scenario setup: " if setup else "")
        r = self._cli("add", "--namespace", self.ns, "--type", "fact", "--content", content,
                      "--signal", "test", "--json")
        self.assertEqual(r.returncode, 0, prefix + f"{what} add {content!r} failed: "
                         f"{(r.stderr or r.stdout).strip()[:300]}")

    def _live_rows(self):
        """Live rows as any SQLite client sees the store; unreadable -> reason."""
        try:
            with contextlib.closing(sqlite3.connect(str(self.store))) as conn:
                return [r[0] for r in conn.execute(
                    "SELECT content FROM memory WHERE superseded_at IS NULL")]
        except sqlite3.Error as e:
            return [f"<store unreadable: {e}>"]

    def _rows_at(self, path):
        """Live rows a normal SQLite open reads from `path` (sidecars beside
        it apply); unreadable -> reason."""
        try:
            with contextlib.closing(sqlite3.connect(str(path))) as conn:
                return [r[0] for r in conn.execute(
                    "SELECT content FROM memory WHERE superseded_at IS NULL")]
        except sqlite3.Error as e:
            return [f"<store unreadable: {e}>"]

    def _immutable_rows(self, path):
        """Live rows a sidecar-free immutable read of `path`'s MAIN FILE
        alone yields (the -wal/-journal beside it are ignored)."""
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            return [r[0] for r in conn.execute(
                "SELECT content FROM memory WHERE superseded_at IS NULL")]

    def _take_backup(self):
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        r = self._cli("backup")
        self.assertEqual(r.returncode, 0, f"{self.mk}: scenario setup: control backup "
                         f"failed: {(r.stderr or r.stdout).strip()[:300]}")
        snaps = sorted(self.backup_dir.glob("store-*.sqlite"))
        self.assertEqual(len(snaps), 1, f"{self.mk}: scenario setup: expected exactly one "
                         f"fresh snapshot, found {[s.name for s in snaps]}")
        return snaps[0]

    @contextlib.contextmanager
    def _pinned_env(self):
        """Point the in-process storelib at THIS leg's throwaway store.

        Also re-arms the import ecosystem when a previous pytest node's
        suite-level tearDownModule popped ``storelib`` from sys.modules and
        dropped the scripts dir from sys.path: without this, later nodes'
        deferred imports inside cmd_restore would fail (or bind a stale
        instance) and masquerade as product refusals.  Every storelib
        instance alive in this process is refreshed under the pin, so
        deferred imports never see a previous leg's frozen paths."""

        def _refresh_alive_instances():
            alive = sys.modules.get("storelib")
            if alive is not None and alive is not storelib:
                alive._refresh_env_state()
            storelib._refresh_env_state()

        saved = {k: os.environ.get(k) for k in _PINNED_KEYS}
        try:
            os.environ.update({"ZMEM_STORE": str(self.store), "ZMEM_DATA": str(self.tmp),
                               "ZMEM_BACKUP_DIR": str(self.backup_dir),
                               "ZMEM_MODELS_DIR": str(self.tmp / "no-models"),
                               "ZMEM_MODEL_AUTODOWNLOAD": "0",
                               "PYTHONIOENCODING": "utf-8"})
            if str(SCRIPTS_DIR) not in sys.path:
                sys.path.insert(0, str(SCRIPTS_DIR))
            if "storelib" not in sys.modules:
                import storelib as _rearmed  # noqa: F401,E402  (fresh, under this pin)
            _refresh_alive_instances()
            yield
        finally:
            _apply_env(saved)
            _refresh_alive_instances()

    def _run_cmd_restore(self, snap_path, patch, unpatch):
        """Run cmd_restore in-process with the `patch`ed os file-operation
        entry points for the duration of the call; captures stdout/stderr
        separately and records (never swallows) an escaping exception."""
        out, err = io.StringIO(), io.StringIO()
        rc, raised = None, None
        with self._pinned_env():
            patch()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = _cmd_restore(from_path=str(snap_path), force=True)
            except Exception as e:  # an escaping exception is itself a failure
                raised, rc = e, None
            finally:
                unpatch()
        return {"rc": rc, "raised": raised, "out": out.getvalue(), "err": err.getvalue()}

    def _assert_no_sidecars_or_stashes(self, label, *, main_expected="present"):
        """Evaluated immediately after the restore returns, BEFORE any check
        opens the store (no sqlite open, no later add): a restore that was
        rolled back (or failed onto a missing/empty destination) leaves NO
        sidecar, staged copy or stash beside the store.  main_expected:
        "present" (not judged here), "absent", or "empty" (0 bytes)."""
        store_name = self.store.name
        names = set(os.listdir(self.store.parent))
        leftovers = sorted(
            name for name in names
            if name in (store_name + "-wal", store_name + "-shm",
                        store_name + "-journal")
            or name.startswith(store_name + ".restore-stage-")
            or name.startswith(store_name + ".restore-stash-"))
        self.assertEqual(leftovers, [], f"{self.mk}: [{label}] immediately after the "
                         f"restore returned, sidecars, staged copies or stashes are "
                         f"left beside the store: {leftovers!r} -- none may remain")
        if main_expected == "absent":
            size_note = (f" ({self.store.stat().st_size} bytes)"
                         if self.store.exists() else "")
            self.assertFalse(self.store.exists(), f"{self.mk}: [{label}] after the "
                            f"failed restore the destination main file must be absent; "
                            f"it exists{size_note}")
        elif main_expected == "empty":
            self.assertTrue(self.store.exists(), f"{self.mk}: [{label}] the destination "
                            f"main file must still exist after the failed restore")
            if self.store.exists():
                self.assertEqual(self.store.stat().st_size, 0, f"{self.mk}: [{label}] "
                                f"after the failed restore the empty destination must "
                                f"still be 0 bytes; it is now "
                                f"{self.store.stat().st_size} bytes")

    def _assert_controlled_failure(self, result, label):
        """The common S2/S3/S4 failure contract: no escape, an int return,
        a non-zero code, and 'restore FAILED' on stderr."""
        self.assertIsNone(result["raised"], f"{self.mk}: [{label}] cmd_restore let the "
                         f"injected failure escape as {result['raised']!r} instead of "
                         f"returning a controlled non-zero exit code -- a failed restore "
                         f"must be reported, not raised")
        self.assertIsInstance(result["rc"], int, f"{self.mk}: [{label}] cmd_restore "
                         f"returned {result['rc']!r} instead of an int exit code")
        self.assertNotEqual(result["rc"], 0, f"{self.mk}: [{label}] the failed restore "
                            f"returned {result['rc']!r} instead of a non-zero exit code")
        self.assertIn("restore FAILED", result["err"], f"{self.mk}: [{label}] the failed "
                        f"restore did not print 'restore FAILED' on stderr; stderr tail "
                        f"{result['err'][-300:]!r}")

    def _require_rollback_line(self, result, label):
        """The failure output must contain a 'rolled back' line naming an
        existing pre-restore safety copy; returns that copy's path."""
        text = result["out"] + "\n" + result["err"]
        lines = [line for line in text.splitlines() if "rolled back" in line]
        self.assertTrue(lines, f"{self.mk}: [{label}] the failed restore printed no "
                        f"'rolled back' line naming the pre-restore safety copy; output "
                        f"tail {text[-400:]!r}")
        prerestore = (sorted(self.backup_dir.glob("prerestore*"))
                      if self.backup_dir.is_dir() else [])
        self.assertTrue(prerestore, f"{self.mk}: [{label}] the failed restore created no "
                        f"pre-restore safety copy (no prerestore* file in the backup "
                        f"directory) that an automatic rollback could name")
        named = [p for p in prerestore
                 if any(p.name in line or str(p) in line for line in lines)]
        self.assertTrue(named, f"{self.mk}: [{label}] no 'rolled back' line names the "
                        f"pre-restore safety copy; rollback lines {lines!r}, safety copies "
                        f"on disk {[p.name for p in prerestore]!r}")
        return named[0]

    # -- shared fault wrappers -------------------------------------------

    def _after_swap_fault(self, snap_path):
        """The existing after-swap fault: a one-shot wrapper on os.replace
        performs the real replacement of the live store and then raises
        (a RuntimeError, so the product's `except OSError` arms cannot mask
        it), failing the step right after the staged install.  Returns the
        result dict; asserts the fault fired."""
        state = {"fired": False}
        live_abs = os.path.abspath(str(self.store))
        real_replace = os.replace

        class _PostStagingFault(RuntimeError):
            pass

        def _replace_then_fail(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            if not state["fired"] and os.path.abspath(os.fspath(dst)) == live_abs:
                state["fired"] = True
                raise _PostStagingFault("AC4-504 injected failure: the staged "
                                        "replacement of the live store completed; "
                                        "the step after it failed")
            return None

        result = self._run_cmd_restore(
            snap_path, lambda: setattr(os, "replace", _replace_then_fail),
            lambda: setattr(os, "replace", real_replace))
        self.assertTrue(state["fired"], f"{self.mk}: the injected post-replacement fault "
                        f"never fired, so the 'restore fails after staging' scenario was "
                        f"not exercised; restore reported rc={result['rc']!r} "
                        f"raised={result['raised']!r} stderr tail "
                        f"{result['err'][-200:]!r}")
        return result

    def _sidecar_fault(self, snap_path, fire_on):
        """One-shot wrappers on os.replace/os.rename/os.remove/os.unlink
        (what pathlib and shutil call underneath): the destination-sidecar
        file operations (a <store>-wal/-shm/-journal path as source or
        destination) are counted and TRACED -- each recorded as
        "<store name><suffix>:set-aside" when the sidecar path is the
        source (moved away or deleted) and "<store name><suffix>:put-back"
        when it is the destination (moved back) -- and operation number N
        raises OSError before doing anything for every N in `fire_on` (an
        int, or an iterable of ints -- F5 arms the second AND third
        operation; F6 arms the third and the fourth)."""
        fire_numbers = {fire_on} if isinstance(fire_on, int) else set(fire_on)
        sidecar_abs = {os.path.abspath(str(self.store) + s): s
                       for s in _SIDECAR_SUFFIXES}
        state = {"sidecar_ops": 0, "trace": []}
        real = {"replace": os.replace, "rename": os.rename,
                "remove": os.remove, "unlink": os.unlink}

        def _abspath(p):
            try:
                return os.path.abspath(os.fspath(p))
            except (TypeError, ValueError):  # pragma: no cover - defensive
                return ""

        def _gate(src, dst=None):
            src_abs, dst_abs = _abspath(src), (_abspath(dst) if dst is not None else "")
            if src_abs in sidecar_abs or dst_abs in sidecar_abs:
                state["sidecar_ops"] += 1
                if src_abs in sidecar_abs:
                    state["trace"].append(self.store.name + sidecar_abs[src_abs]
                                          + ":set-aside")
                else:
                    state["trace"].append(self.store.name + sidecar_abs[dst_abs]
                                          + ":put-back")
                if state["sidecar_ops"] in fire_numbers:
                    raise OSError(errno.EIO, "AC4-504 injected sidecar fault #%d"
                                  % state["sidecar_ops"])

        def _replace(src, dst, *args, **kwargs):
            _gate(src, dst)
            return real["replace"](src, dst, *args, **kwargs)

        def _rename(src, dst, *args, **kwargs):
            _gate(src, dst)
            return real["rename"](src, dst, *args, **kwargs)

        def _remove(path, *args, **kwargs):
            _gate(path)
            return real["remove"](path, *args, **kwargs)

        def _unlink(path, *args, **kwargs):
            _gate(path)
            return real["unlink"](path, *args, **kwargs)

        def _patch():
            os.replace, os.rename = _replace, _rename
            os.remove, os.unlink = _remove, _unlink

        def _unpatch():
            os.replace, os.rename = real["replace"], real["rename"]
            os.remove, os.unlink = real["remove"], real["unlink"]

        result = self._run_cmd_restore(snap_path, _patch, _unpatch)
        result["sidecar_ops"] = state["sidecar_ops"]
        result["trace"] = list(state["trace"])
        return result

    def _install_fault(self, snap_path):
        """A wrapper on os.replace raises BEFORE doing anything when the
        destination of the replacement is the live store path (the install
        step itself never happens)."""
        state = {"install_faults": 0}
        live_abs = os.path.abspath(str(self.store))
        real_replace = os.replace

        def _replace(src, dst, *args, **kwargs):
            if os.path.abspath(os.fspath(dst)) == live_abs:
                state["install_faults"] += 1
                raise OSError(errno.EIO, "AC4-504 injected install fault: the "
                              "replacement of the live store was refused before "
                              "doing anything")
            return real_replace(src, dst, *args, **kwargs)

        result = self._run_cmd_restore(
            snap_path, lambda: setattr(os, "replace", _replace),
            lambda: setattr(os, "replace", real_replace))
        result["install_faults"] = state["install_faults"]
        return result

    # -- S2: the crashed-writer -wal --------------------------------------

    def _plant_crashed_writer_wal(self, content):
        """Leave beside the live store exactly what a crashed writer
        leaves: a main file WITHOUT the row and a committed `-wal` WITH it
        (no `-shm`).  With a holder and a writer connection open, insert
        one valid row with plain SQL, commit, copy BOTH the live main file
        and the `-wal` bytes into a snapshot directory, close (the close
        checkpoints and deletes the `-wal`), then put the snapshot back.
        Returns the planted `-wal` bytes."""
        wal_id = "walonly-" + uuid.uuid4().hex
        store_str = str(self.store)
        snap_dir = self.tmp / ("walonly-snap-" + uuid.uuid4().hex[:8])
        snap_dir.mkdir(parents=True)
        holder = sqlite3.connect(store_str)
        writer = sqlite3.connect(store_str)
        try:
            writer.execute("INSERT INTO memory (id, namespace, type, content, "
                           "ingestion_ts, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                           (wal_id, self.ns, "fact", content,
                            "2026-11-23T00:00:00Z", 0.9))
            writer.commit()
            shutil.copyfile(store_str, snap_dir / "main.sqlite")
            wal_bytes = Path(store_str + "-wal").read_bytes()
        finally:
            writer.close()
            holder.close()
        self.assertTrue(wal_bytes, f"{self.mk}: scenario setup: no -wal frames were "
                        f"captured for the crashed-writer row, the S2 leg would be "
                        f"vacuous")
        shutil.copyfile(snap_dir / "main.sqlite", store_str)
        Path(store_str + "-wal").write_bytes(wal_bytes)
        for suffix in ("-shm", "-journal"):
            sidecar = Path(store_str + suffix)
            if sidecar.exists():
                sidecar.unlink()
        # Scenario-setup proof on private copies: a NORMAL open of
        # (main + -wal) sees the row; a main-file-only immutable open does not.
        pair_dir = self.tmp / ("walonly-proof-pair-" + uuid.uuid4().hex[:8])
        pair_dir.mkdir(parents=True)
        shutil.copyfile(snap_dir / "main.sqlite", pair_dir / "p.sqlite")
        shutil.copyfile(snap_dir / "main.sqlite", pair_dir / "solo.sqlite")
        Path(str(pair_dir / "p.sqlite") + "-wal").write_bytes(wal_bytes)
        with contextlib.closing(sqlite3.connect(str(pair_dir / "p.sqlite"))) as conn:
            normal_rows = [r[0] for r in conn.execute(
                "SELECT content FROM memory WHERE superseded_at IS NULL")]
        self.assertIn(content, normal_rows, f"{self.mk}: scenario setup: the planted "
                        f"-wal must hold the crashed-writer row so a normal SQLite open "
                        f"sees it; rows {normal_rows!r}")
        solo_uri = (pair_dir / "solo.sqlite").resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(solo_uri, uri=True)) as conn:
            solo_rows = [r[0] for r in conn.execute(
                "SELECT content FROM memory WHERE superseded_at IS NULL")]
        self.assertNotIn(content, solo_rows, f"{self.mk}: scenario setup: the main file "
                        f"alone must NOT hold the crashed-writer row (only its -wal "
                        f"does); rows {solo_rows!r}")
        return wal_bytes

    def _find_wal_copy(self, lines, planted_wal):
        """The first existing file named by one of `lines` (absolute tokens,
        or tokens resolved against the backup dir / store dir / cwd; a named
        directory contributes its files one level deep) whose bytes equal
        `planted_wal`; None when no line names such a location."""
        wanted = _digest_bytes(planted_wal)
        roots = (self.backup_dir, self.store.parent, Path.cwd())

        def _candidates(line):
            found = []
            for token in re.split(r"[\s'\"\[\]()<>]+", line):
                token = token.strip(".,;:`")
                if not token:
                    continue
                stems = [Path(token)]
                for root in roots:
                    stems.append(root / token.lstrip("/\\"))
                for stem in stems:
                    if stem.is_file():
                        found.append(stem)
                    elif stem.is_dir():
                        try:
                            found.extend(p for p in stem.iterdir() if p.is_file())
                            for sub in stem.iterdir():
                                if sub.is_dir():
                                    found.extend(p for p in sub.iterdir() if p.is_file())
                        except OSError:
                            pass
            return found

        for line in lines:
            for candidate in _candidates(line):
                try:
                    if _digest_bytes(candidate.read_bytes()) == wanted:
                        return candidate
                except OSError:
                    continue
        return None

    def _require_set_aside_copy(self, result, planted_wal, label):
        """The output must contain a 'set aside' line naming a location that
        holds a byte-identical copy of the planted -wal; returns that
        copy's path."""
        text = result["out"] + "\n" + result["err"]
        lines = [line for line in text.splitlines() if "set aside" in line]
        self.assertTrue(lines, f"{self.mk}: [{label}] a non-empty -wal sat beside the "
                        f"destination, but the restore output has no 'set aside' line "
                        f"naming where its bytes were preserved; output tail "
                        f"{text[-400:]!r}")
        found = self._find_wal_copy(lines, planted_wal)
        self.assertIsNotNone(found, f"{self.mk}: [{label}] the 'set aside' line does not "
                        f"name any file or directory holding a byte-identical copy of "
                        f"the destination -wal; lines {lines!r}")
        return found

    def _assert_f5_putback_pin(self, result, planted_wal, before_shm,
                               before_journal, before_names, label):
        """The F5 put-back double fault, evaluated immediately after the
        restore returns and before any check opens the store: the first
        sidecar operation succeeded (the -wal was stashed beside the
        store), the second failed, and the put-back failed too.  Pins the
        stash directory, its exact contents, the untouched siblings, the
        exactly-two directory delta, and the output shape (one 'is set
        aside at' line, a byte-copies/forensic line, no 'rolled back')."""
        text = result["out"] + "\n" + result["err"]
        store_name = self.store.name
        stash_prefix = store_name + ".restore-stash-"
        aside_lines = [line for line in text.splitlines()
                       if "is set aside at" in line]
        self.assertEqual(len(aside_lines), 1, f"{self.mk}: [{label}] the output must "
                         f"contain EXACTLY ONE 'is set aside at' line; found "
                         f"{len(aside_lines)}: {aside_lines!r}")
        stashes = sorted(p for p in self.store.parent.glob(stash_prefix + "*")
                         if p.is_dir())
        named = [p for p in stashes
                 if p.name in aside_lines[0] or str(p) in aside_lines[0]]
        self.assertEqual(len(named), 1, f"{self.mk}: [{label}] the 'is set aside at' "
                         f"line must name a directory beside the store named "
                         f"'{stash_prefix}*'; line {aside_lines[0]!r}, stash "
                         f"directories on disk {[p.name for p in stashes]!r}")
        stash = named[0]
        self.assertEqual(stash.parent.resolve(), self.store.parent.resolve(),
                         f"{self.mk}: [{label}] the stash directory must sit BESIDE the "
                         f"store (its parent must be the store's directory), never "
                         f"under the backup directory; found {stash}")
        entries = sorted(p.name for p in stash.iterdir())
        self.assertEqual(entries, [store_name + "-wal"], f"{self.mk}: [{label}] the "
                         f"stash directory must contain exactly one file, "
                         f"{store_name + '-wal'}, and nothing else; found {entries!r}")
        stashed_wal = stash / (store_name + "-wal")
        self.assertEqual(_digest_bytes(stashed_wal.read_bytes()),
                         _digest_bytes(planted_wal), f"{self.mk}: [{label}] the stashed "
                         f"{store_name}-wal must be byte-identical to the planted -wal")
        self.assertFalse(Path(str(self.store) + "-wal").exists(), f"{self.mk}: "
                         f"[{label}] after the failed put-back the planted -wal must "
                         f"NOT sit at its original path beside the store (its only "
                         f"remaining home is the stash directory)")
        for sidecar_path, sidecar_before, suffix in (
                (Path(str(self.store) + "-shm"), before_shm, "-shm"),
                (Path(str(self.store) + "-journal"), before_journal, "-journal")):
            self.assertTrue(sidecar_path.exists(), f"{self.mk}: [{label}] the "
                            f"destination's {suffix} disappeared across the double "
                            f"fault; it must stay at its original path")
            if sidecar_path.exists():
                self.assertEqual(_digest_bytes(sidecar_path.read_bytes()),
                                 sidecar_before, f"{self.mk}: [{label}] the double "
                                 f"fault rewrote the destination's {suffix}; it must "
                                 f"stay byte-identical to before the call at its "
                                 f"original path")
        stages = sorted(name for name in os.listdir(self.store.parent)
                        if name.startswith(store_name + ".restore-stage-"))
        self.assertEqual(stages, [], f"{self.mk}: [{label}] the double fault left "
                         f"staged copies beside the store: {stages!r}")
        before_store = {name for name in before_names if name.startswith(store_name)}
        after_store = {name for name in os.listdir(self.store.parent)
                       if name.startswith(store_name)}
        removed = sorted(before_store - after_store)
        added = sorted(after_store - before_store)
        self.assertEqual((removed, added), ([store_name + "-wal"], [stash.name]),
                         f"{self.mk}: [{label}] across the double fault the store's "
                         f"directory may change in exactly two ways: {store_name}-wal "
                         f"is gone and one {stash_prefix}* directory appears; instead "
                         f"removed {removed!r}, added {added!r}")
        forensic_lines = [line for line in text.splitlines()
                          if "byte copies" in line and "forensic" in line]
        self.assertTrue(forensic_lines, f"{self.mk}: [{label}] the output must state "
                        f"that byte copies of the files from before the restore are "
                        f"kept in the forensic directory (a line matching 'byte "
                        f"copies' and 'forensic'); output tail {text[-400:]!r}")
        self.assertNotIn("rolled back", text, f"{self.mk}: [{label}] the rollback "
                         f"itself failed, so the output must NOT contain the words "
                         f"'rolled back'; output tail {text[-400:]!r}")

    def _assert_f6_pin(self, result, planted_wal, planted_shm, planted_journal,
                       label):
        """The F6 two-sidecar pin, evaluated immediately after the restore
        returns and before any check opens the store: the set-asides of
        -wal and -shm succeeded, the set-aside of -journal FAILED, the
        put-back of -shm FAILED, and the put-back of -wal was still
        attempted and SUCCEEDED.  Put-backs must run in the REVERSE order
        of the set-asides, and a failed put-back must not stop the
        remaining ones."""
        text = result["out"] + "\n" + result["err"]
        store_name = self.store.name
        stash_prefix = store_name + ".restore-stash-"
        expected_trace = [
            store_name + "-wal:set-aside",
            store_name + "-shm:set-aside",
            store_name + "-journal:set-aside",
            store_name + "-shm:put-back",
            store_name + "-wal:put-back",
        ]
        self.assertEqual(result["trace"], expected_trace, f"{self.mk}: [{label}] the "
                         f"destination-sidecar file operations must run in exactly "
                         f"this order -- set-aside -wal, set-aside -shm, failed "
                         f"set-aside of -journal, failed put-back of -shm, then the "
                         f"put-back of -wal (put-backs in REVERSE order of the "
                         f"set-asides, and a failed put-back does not stop the "
                         f"remaining ones); recorded {result['trace']!r}")
        wal_path = Path(str(self.store) + "-wal")
        shm_path = Path(str(self.store) + "-shm")
        journal_path = Path(str(self.store) + "-journal")
        self.assertTrue(wal_path.exists(), f"{self.mk}: [{label}] the -wal must be "
                        f"back at its original path beside the store after its "
                        f"put-back succeeded")
        if wal_path.exists():
            self.assertEqual(_digest_bytes(wal_path.read_bytes()),
                             _digest_bytes(planted_wal), f"{self.mk}: [{label}] the "
                             f"put-back -wal must be byte-identical to the planted "
                             f"log at its original path")
        aside_lines = [line for line in text.splitlines()
                       if "is set aside at" in line]
        self.assertEqual(len(aside_lines), 1, f"{self.mk}: [{label}] the output must "
                         f"contain EXACTLY ONE 'is set aside at' line; found "
                         f"{len(aside_lines)}: {aside_lines!r}")
        stashes = sorted(p for p in self.store.parent.glob(stash_prefix + "*")
                         if p.is_dir())
        named = [p for p in stashes
                 if p.name in aside_lines[0] or str(p) in aside_lines[0]]
        self.assertEqual(len(named), 1, f"{self.mk}: [{label}] the 'is set aside at' "
                         f"line must name the stash directory beside the store named "
                         f"'{stash_prefix}*'; line {aside_lines[0]!r}, stash "
                         f"directories on disk {[p.name for p in stashes]!r}")
        stash = named[0]
        self.assertEqual(stash.parent.resolve(), self.store.parent.resolve(),
                         f"{self.mk}: [{label}] the stash directory must sit BESIDE "
                         f"the store (its parent must be the store's directory), "
                         f"never under the backup directory; found {stash}")
        entries = sorted(p.name for p in stash.iterdir())
        self.assertEqual(entries, [store_name + "-shm"], f"{self.mk}: [{label}] the "
                         f"stash directory must contain exactly one file, "
                         f"{store_name + '-shm'} (its failed put-back), and nothing "
                         f"else; found {entries!r}")
        stashed_shm = stash / (store_name + "-shm")
        self.assertEqual(_digest_bytes(stashed_shm.read_bytes()),
                         _digest_bytes(planted_shm), f"{self.mk}: [{label}] the "
                         f"stashed {store_name}-shm must be byte-identical to the "
                         f"planted -shm")
        self.assertFalse(shm_path.exists(), f"{self.mk}: [{label}] the -shm whose "
                        f"put-back failed must NOT sit at its original path beside "
                        f"the store (its only remaining home is the stash directory)")
        self.assertTrue(journal_path.exists(), f"{self.mk}: [{label}] the -journal "
                        f"(whose set-aside failed) must be untouched at its "
                        f"original path")
        if journal_path.exists():
            self.assertEqual(_digest_bytes(journal_path.read_bytes()),
                             _digest_bytes(planted_journal), f"{self.mk}: [{label}] "
                             f"the -journal must be byte-identical to the planted "
                             f"bytes at its original path")
        stages = sorted(name for name in os.listdir(self.store.parent)
                        if name.startswith(store_name + ".restore-stage-"))
        self.assertEqual(stages, [], f"{self.mk}: [{label}] the double fault left "
                         f"staged copies beside the store: {stages!r}")
        forensic_lines = [line for line in text.splitlines()
                          if "byte copies" in line and "forensic" in line]
        self.assertTrue(forensic_lines, f"{self.mk}: [{label}] the output must state "
                        f"that byte copies of the files from before the restore are "
                        f"kept in the forensic directory (a line matching 'byte "
                        f"copies' and 'forensic'); output tail {text[-400:]!r}")
        self.assertNotIn("rolled back", text, f"{self.mk}: [{label}] the rollback "
                         f"did not complete (the -shm put-back failed), so the output "
                         f"must NOT contain the words 'rolled back'; output tail "
                         f"{text[-400:]!r}")

    def _leg_s2(self):
        self._fresh_leg()
        older = "ac4 older row present in the backup"
        newer = "ac4 newer row written after the backup"
        third = "ac4 third row written after the failed restore"
        walonly = "ac4 crashed-writer row that lived only in the destination -wal"
        self._add(older, setup=True)
        snap = self._take_backup()
        self._add(newer, setup=True)
        # The main-file rows must be verified BEFORE planting: after the
        # plant, any normal SQLite open replays the -wal (and would
        # checkpoint it away), which is exactly the state under test.
        rows = self._live_rows()
        self.assertEqual(sorted(rows), sorted([older, newer]), f"{self.mk}: scenario "
                         f"setup: before the failed restore the main file on its own "
                         f"must hold exactly the older and newer rows; live rows "
                         f"{rows!r}")
        planted_wal = self._plant_crashed_writer_wal(walonly)
        result = self._after_swap_fault(snap)
        self._assert_controlled_failure(result, "S2")
        self._assert_no_sidecars_or_stashes("S2")
        safety_copy = self._require_rollback_line(result, "S2")
        set_aside = self._require_set_aside_copy(result, planted_wal, "S2")
        # The location the 'set aside' line names is the forensic directory:
        # restore-forensic-* under the backup directory, keeping the on-disk
        # files from before the restore byte-for-byte under unchanged names.
        forensic_dir = set_aside.parent
        self.assertTrue(forensic_dir.name.startswith("restore-forensic-"), f"{self.mk}: "
                        f"[S2] the on-disk files from before the restore must be kept "
                        f"byte-for-byte in a directory whose name starts with "
                        f"'restore-forensic-' (found {forensic_dir.name!r})")
        try:
            forensic_dir.resolve().relative_to(self.backup_dir.resolve())
        except ValueError:
            self.fail(f"{self.mk}: [S2] the forensic directory must live under the "
                      f"backup directory {self.backup_dir}; found {forensic_dir}")
        # The rollback target is exactly what SQLite read from the
        # destination before the restore: main file plus the planted
        # -wal's committed frame, so the -wal-only row IS live.
        rows = self._live_rows()
        self.assertEqual(sorted(rows), sorted([older, newer, walonly]), f"{self.mk}: "
                         f"[S2] the failed restore did not put the live store back to "
                         f"exactly what SQLite read from the destination before the "
                         f"restore (main file plus the planted -wal's committed "
                         f"frames); live rows now {rows!r}")
        live_wal = Path(str(self.store) + "-wal")
        wal_clash = (live_wal.exists()
                     and _digest_bytes(live_wal.read_bytes())
                     == _digest_bytes(planted_wal))
        self.assertFalse(wal_clash, f"{self.mk}: [S2] after the rollback a -wal "
                        f"byte-identical to the planted log still sits beside the live "
                        f"store; the rollback target already carries its committed "
                        f"frames, so the planted log must be gone from beside the "
                        f"store")
        # The safety copy is the store as SQLite read it: opened alone, with
        # no sidecars, it contains the -wal-only row.
        alone_dir = self.tmp / ("s2-alone-" + uuid.uuid4().hex[:8])
        alone_dir.mkdir(parents=True)
        alone = alone_dir / "safety-copy-alone.sqlite"
        shutil.copyfile(safety_copy, alone)
        alone_rows = self._rows_at(alone)
        self.assertIn(walonly, alone_rows, f"{self.mk}: [S2] the pre-restore safety "
                        f"copy must be the store as SQLite read it: a scratch copy "
                        f"opened alone, with no sidecars, must contain the -wal-only "
                        f"row; rows {alone_rows!r}")
        # The forensic main file is the destination main file as it was on
        # disk before the restore: immutable it reads exactly the main-file
        # rows, and replaying the planted -wal onto a scratch copy of it
        # still yields the -wal-only row.
        forensic_main = forensic_dir / self.store.name
        self.assertTrue(forensic_main.is_file(), f"{self.mk}: [S2] the forensic "
                        f"directory must keep the pre-restore main file under its "
                        f"unchanged name {self.store.name!r} beside its sidecar "
                        f"copies; {forensic_main} is missing")
        self.assertEqual(sorted(self._immutable_rows(forensic_main)),
                         sorted([older, newer]), f"{self.mk}: [S2] the forensic main "
                         f"file must read exactly the older and newer rows under an "
                         f"immutable open; rows "
                         f"{self._immutable_rows(forensic_main)!r}")
        replay_dir = self.tmp / ("s2-replay-" + uuid.uuid4().hex[:8])
        replay_dir.mkdir(parents=True)
        replay = replay_dir / "replay.sqlite"
        shutil.copyfile(forensic_main, replay)
        Path(str(replay) + "-wal").write_bytes(planted_wal)
        replay_rows = self._rows_at(replay)
        self.assertIn(walonly, replay_rows, f"{self.mk}: [S2] the forensic main file "
                        f"plus the planted -wal must still yield the -wal-only row "
                        f"(the planted bytes are preserved byte-for-byte); rows "
                        f"{replay_rows!r}")
        self._add(third, what="write after the failed restore")
        rows = self._live_rows()
        self.assertEqual(sorted(rows), sorted([older, newer, walonly, third]),
                         f"{self.mk}: [S2] recovery lost or gained rows across the "
                         f"failed restore and the later write; live rows now {rows!r}")

    # -- S3: failures before the swap --------------------------------------

    def _leg_s3(self, fault):
        self._fresh_leg()
        row_one = "ac4 s3 control row one"
        row_two = "ac4 s3 control row two"
        later = "ac4 s3 later write after the pre-swap failure"
        self._add(row_one, setup=True)
        self._add(row_two, setup=True)
        snap = self._take_backup()
        nonce = uuid.uuid4().hex
        wal_path = Path(str(self.store) + "-wal")
        shm_path = Path(str(self.store) + "-shm")
        journal_path = Path(str(self.store) + "-journal")
        garbage_wal = ("ac4 garbage wal bytes " + nonce).encode("utf-8")
        garbage_shm = ("ac4 garbage shm bytes " + nonce).encode("utf-8")
        garbage_journal = ("ac4 garbage journal bytes " + nonce).encode("utf-8")
        wal_path.write_bytes(garbage_wal)
        shm_path.write_bytes(garbage_shm)
        journal_path.write_bytes(garbage_journal)
        before_main = self._digest_file(self.store)
        before_wal = _digest_bytes(garbage_wal)
        before_shm = _digest_bytes(garbage_shm)
        before_journal = _digest_bytes(garbage_journal)
        before_names = set(os.listdir(self.store.parent))
        label = "S3/" + fault
        if fault == "F3":
            result = self._install_fault(snap)
        else:
            fire = {"F1": 1, "F2": 2, "F4": 3, "F5": (2, 3), "F6": (3, 4)}[fault]
            result = self._sidecar_fault(snap, fire)
        # Non-vacuous evidence FIRST, before anything is concluded from the
        # exit code: the injected fault must actually have been reached.
        wal_note = ("" if wal_path.exists()
                    else " (the garbage -wal planted beside the destination is GONE: "
                         "the restore's own reads destroyed it)")
        if fault == "F3":
            self.assertGreaterEqual(result["install_faults"], 1, f"{self.mk}: [{label}] "
                            f"the injected install fault was never reached: no file "
                            f"operation tried to install a file on the live store path "
                            f"(observed {result['install_faults']} install attempt(s)), "
                            f"so the restore never staged a replacement for the fault "
                            f"to strike")
            armed = 1
        else:
            fire_numbers = {fire} if isinstance(fire, int) else set(fire)
            armed = max(fire_numbers)
            ops = result["sidecar_ops"]
            reached = (f"performed only {ops} file operation(s) on the destination "
                       f"sidecar paths" if ops
                       else "performed no file operation on the destination sidecar "
                            f"paths")
            self.assertGreaterEqual(ops, armed, f"{self.mk}: [{label}] the injected "
                            f"fault (armed for destination-sidecar file operation "
                            f"#{armed}) was never reached: the restore {reached}, so "
                            f"it either never takes charge of them or destroyed them "
                            f"before the step the fault was aimed at{wal_note}")
        self._assert_controlled_failure(result, label)
        self.assertEqual(self._digest_file(self.store), before_main, f"{self.mk}: "
                         f"[{label}] the pre-swap failure changed the destination main "
                         f"file's bytes")
        if fault == "F5":
            # The put-back double fault, pinned exactly: the first sidecar
            # operation succeeded (the -wal sits in its stash), the second
            # failed, and the put-back failed too -- so the -wal is stashed
            # beside the store and the output must say so without claiming
            # a rollback.
            self._assert_f5_putback_pin(result, garbage_wal, before_shm,
                                        before_journal, before_names, label)
        elif fault == "F6":
            # The two-sidecar pin: set-aside of -wal and -shm succeeded, the
            # set-aside of -journal failed, the put-back of -shm failed, and
            # the put-back of -wal was still attempted and succeeded --
            # put-backs in REVERSE order, a failed put-back not stopping
            # the rest.
            self._assert_f6_pin(result, garbage_wal, garbage_shm,
                                garbage_journal, label)
        else:
            for sidecar_path, sidecar_before, suffix in (
                    (wal_path, before_wal, "-wal"),
                    (shm_path, before_shm, "-shm"),
                    (journal_path, before_journal, "-journal")):
                self.assertTrue(sidecar_path.exists(), f"{self.mk}: [{label}] the "
                                f"pre-swap failure destroyed the destination's {suffix} "
                                f"sidecar -- it must be preserved (set aside), never "
                                f"deleted")
                if sidecar_path.exists():
                    self.assertEqual(_digest_bytes(sidecar_path.read_bytes()),
                                     sidecar_before, f"{self.mk}: [{label}] the pre-swap "
                                     f"failure rewrote the destination's {suffix} "
                                     f"sidecar; it must stay byte-identical to before "
                                     f"the call at its original path")
        rows = self._live_rows()
        self.assertEqual(sorted(rows), sorted([row_one, row_two]), f"{self.mk}: [{label}] "
                         f"the pre-swap failure changed the live row set; live rows now "
                         f"{rows!r}")
        self._add(later, what="write after the pre-swap failure")
        rows = self._live_rows()
        self.assertEqual(sorted(rows), sorted([row_one, row_two, later]), f"{self.mk}: "
                         f"[{label}] a write after the pre-swap failure did not land "
                         f"beside the preserved rows; live rows now {rows!r}")
        if fault not in ("F5", "F6"):
            new_entries = sorted(set(os.listdir(self.store.parent)) - before_names)
            leftovers = [name for name in new_entries
                         if name.startswith(self.store.name)]
            self.assertEqual(leftovers, [], f"{self.mk}: [{label}] the failed restore "
                             f"left staged copies or stashes beside the store (new "
                             f"entries starting with the store file's name): "
                             f"{leftovers!r}")

    def _digest_file(self, path):
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    # -- S5: a quiesced destination (success leg) ---------------------------

    def _leg_s5_quiesced(self):
        """A restore onto a store with NO sidecars must not manufacture
        sidecars and then treat them as its own: exit 0, no forensic
        directory, no 'removed stale' line, no sidecar beside the store
        when the command returns, rows intact."""
        self._fresh_leg()
        row_one = "ac4 s5 control row one"
        row_two = "ac4 s5 control row two"
        self._add(row_one, setup=True)
        self._add(row_two, setup=True)
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(self.store) + suffix).exists(),
                             f"{self.mk}: scenario setup: the quiesced-destination leg "
                             f"needs no {suffix} beside the store before the restore "
                             f"(the public CLI's clean closes leave none)")
        snap = self._take_backup()
        r = self._cli("restore", "--from", str(snap), "--force")
        self.assertEqual(r.returncode, 0, f"{self.mk}: [S5] a restore onto a quiesced "
                         f"store with no sidecars must succeed (exit 0); got exit "
                         f"{r.returncode}; stderr {r.stderr.strip()[:300]!r}")
        text = r.stdout + "\n" + r.stderr
        forensic = sorted(d.name for d in self.backup_dir.iterdir()
                          if d.is_dir()
                          and d.name.startswith("restore-forensic-")) \
            if self.backup_dir.is_dir() else []
        self.assertEqual(forensic, [], f"{self.mk}: [S5] a restore onto a store without "
                         f"sidecars must not create a restore-forensic-* directory "
                         f"under the backup directory; found {forensic!r}")
        self.assertNotIn("removed stale", text, f"{self.mk}: [S5] a restore onto a "
                         f"store without sidecars must not print 'removed stale' "
                         f"(nothing was taken away); output tail {text[-300:]!r}")
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(self.store) + suffix).exists(), f"{self.mk}: "
                             f"[S5] when the command returns, {suffix} must not exist "
                             f"beside the store -- a restore onto a store without "
                             f"sidecars must not manufacture sidecars and then treat "
                             f"them as its own")
        rows = self._live_rows()
        self.assertEqual(sorted(rows), sorted([row_one, row_two]), f"{self.mk}: [S5] "
                         f"the successful restore changed the live row set; live rows "
                         f"now {rows!r}")

    # -- S6: a failed restore onto a quiesced non-empty destination ---------

    _ROLLED_BACK_PREFIX = ("[zmem] restore: rolled back: live store re-installed "
                           "from the pre-restore backup ")

    def _leg_s6_quiesced_fault(self):
        """A post-swap failure on a quiesced NON-EMPTY destination with no
        sidecars: the ruled rollback-only shape -- the exact stderr line, no
        set-aside and no forensic artifacts, exactly one prerestore copy,
        the store put back byte-for-row, nothing left beside it."""
        self._fresh_leg()
        row_one = "ac4 s6 control row one"
        row_two = "ac4 s6 control row two"
        later = "ac4 s6 later write after the failed restore"
        self._add(row_one, setup=True)
        self._add(row_two, setup=True)
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(self.store) + suffix).exists(),
                             f"{self.mk}: scenario setup: the S6 leg needs no {suffix} "
                             f"beside the quiesced destination before the restore (the "
                             f"public CLI's clean closes leave none)")
        snap = self._take_backup()
        result = self._after_swap_fault(snap)
        self._assert_controlled_failure(result, "S6")
        self._assert_no_sidecars_or_stashes("S6")
        err = result["err"]
        text = result["out"] + "\n" + err
        rolled = [line for line in err.splitlines()
                  if line.startswith(self._ROLLED_BACK_PREFIX)]
        self.assertTrue(rolled, f"{self.mk}: [S6] stderr must carry the ruled "
                        f"rollback line starting {self._ROLLED_BACK_PREFIX!r} "
                        f"after 'restore FAILED'; stderr tail {err[-400:]!r}")
        failed_at = err.find("restore FAILED")
        rolled_at = err.find(rolled[0])
        self.assertTrue(failed_at != -1 and failed_at < rolled_at, f"{self.mk}: "
                        f"[S6] the 'restore FAILED' line must precede the rolled-back "
                        f"line on stderr; stderr tail {err[-400:]!r}")
        named = rolled[0][len(self._ROLLED_BACK_PREFIX):].strip().strip("'\"")
        self.assertTrue(named, f"{self.mk}: [S6] the rolled-back line must name the "
                        f"pre-restore backup path; line {rolled[0]!r}")
        safety_copy = Path(named)
        self.assertTrue(safety_copy.is_file()
                        and safety_copy.name.startswith("prerestore"),
                        f"{self.mk}: [S6] the rolled-back line must name an existing "
                        f"prerestore* copy in the backup directory; named "
                        f"{safety_copy!r}")
        prerestore = (sorted(self.backup_dir.glob("prerestore*"))
                      if self.backup_dir.is_dir() else [])
        self.assertEqual(prerestore, [safety_copy], f"{self.mk}: [S6] exactly one "
                         f"pre-restore safety copy must exist; found "
                         f"{[p.name for p in prerestore]!r}")
        self.assertNotIn("set aside", text, f"{self.mk}: [S6] no sidecar was present, "
                         f"so the output must contain no 'set aside' line anywhere; "
                         f"output tail {text[-400:]!r}")
        forensic = sorted(d.name for d in self.backup_dir.iterdir()
                          if d.is_dir()
                          and d.name.startswith("restore-forensic-")) \
            if self.backup_dir.is_dir() else []
        self.assertEqual(forensic, [], f"{self.mk}: [S6] no sidecar was present, so "
                         f"no restore-forensic-* directory may be created under the "
                         f"backup directory; found {forensic!r}")
        rows = self._live_rows()
        self.assertEqual(sorted(rows), sorted([row_one, row_two]), f"{self.mk}: [S6] "
                         f"the failed restore must leave the live rows exactly as "
                         f"they were; live rows now {rows!r}")
        self._add(later, what="write after the failed restore")
        rows = self._live_rows()
        self.assertEqual(sorted(rows), sorted([row_one, row_two, later]), f"{self.mk}: "
                         f"[S6] a write after the failed restore did not land beside "
                         f"the rolled-back rows; live rows now {rows!r}")

    # -- S4: no safety copy --------------------------------------------------

    def _leg_s4_missing(self):
        self._fresh_leg()
        seed = "ac4 s4 seed row in the donor snapshot"
        self._add(seed, setup=True)
        snap = self._take_backup()
        for suffix in ("", "-wal", "-shm", "-journal"):
            victim = Path(str(self.store) + suffix)
            if victim.exists():
                victim.unlink()
        self.assertFalse(self.store.exists(), f"{self.mk}: scenario setup: the S4a leg "
                         f"needs the destination to be missing before the restore")
        result = self._after_swap_fault(snap)
        self._assert_controlled_failure(result, "S4a")
        self._assert_no_sidecars_or_stashes("S4a", main_expected="absent")
        text = result["out"] + "\n" + result["err"]
        self.assertIn("pre-restore backup skipped", text, f"{self.mk}: [S4a] with the "
                        f"destination missing the restore must say the pre-restore backup "
                        f"was skipped; output tail {text[-300:]!r}")
        self.assertFalse(self.store.exists(), f"{self.mk}: [S4a] after a failed restore "
                        f"onto a missing destination (no safety copy exists to roll back "
                        f"to), the partially installed destination must be removed; it "
                        f"now exists "
                        f"({self.store.stat().st_size if self.store.exists() else 0} bytes)")

    def _leg_s4_empty(self):
        self._fresh_leg()
        seed = "ac4 s4 seed row in the donor snapshot"
        self._add(seed, setup=True)
        snap = self._take_backup()
        self.store.write_bytes(b"")
        for suffix in ("-wal", "-shm", "-journal"):
            victim = Path(str(self.store) + suffix)
            if victim.exists():
                victim.unlink()
        self.assertEqual(self.store.stat().st_size, 0, f"{self.mk}: scenario setup: the "
                         f"S4b leg needs a 0-byte destination file before the restore")
        result = self._after_swap_fault(snap)
        self._assert_controlled_failure(result, "S4b")
        self._assert_no_sidecars_or_stashes("S4b", main_expected="empty")
        text = result["out"] + "\n" + result["err"]
        self.assertIn("pre-restore backup skipped", text, f"{self.mk}: [S4b] with an "
                        f"empty destination file the restore must say the pre-restore "
                        f"backup was skipped; output tail {text[-300:]!r}")
        self.assertTrue(self.store.exists(), f"{self.mk}: [S4b] the empty destination "
                        f"file must still exist after the failed restore")
        if self.store.exists():
            self.assertEqual(self.store.stat().st_size, 0, f"{self.mk}: [S4b] after a "
                            f"failed restore onto an empty 0-byte destination (no safety "
                            f"copy exists to roll back to), the destination must be left "
                            f"exactly as it was; it is now "
                            f"{self.store.stat().st_size} bytes")


class _RestoreRecoveryCompanionCase(_RestoreRecoveryCompanionBase):
    """AC2 (#504), companion legs S2-S4."""

    def test_recovery_sidecar_legs(self):
        """RED at base, GREEN after the fix: the crashed-writer -wal is set
        aside (never destroyed, still recoverable), pre-swap faults (on the
        first, second and third destination-sidecar file operation, on the
        live-store install itself, a pinned put-back double fault, and a
        two-sidecar leg pinning reverse-order put-backs that survive a
        failed one) leave the destination exactly as it was or stash the
        sidecar beside the store with the output saying so, a failed
        restore onto a missing or empty destination leaves no partial
        install behind, a restore onto a quiesced store with no sidecars
        succeeds without manufacturing any, and a failed restore onto a
        quiesced non-empty destination rolls back with the ruled stderr
        line and no set-aside or forensic artifacts."""
        self.mk = "AC4-504"
        with self.subTest(leg="S2: a write that lived only in the destination -wal is "
                              "set aside, not lost"):
            self._leg_s2()
        with self.subTest(leg="S3/F1: fault on the first destination-sidecar file "
                              "operation"):
            self._leg_s3("F1")
        with self.subTest(leg="S3/F2: fault on the second destination-sidecar file "
                              "operation"):
            self._leg_s3("F2")
        with self.subTest(leg="S3/F3: fault on the install of the live store file "
                              "itself"):
            self._leg_s3("F3")
        with self.subTest(leg="S3/F4: fault on the third destination-sidecar file "
                              "operation"):
            self._leg_s3("F4")
        with self.subTest(leg="S3/F5: put-back double fault on the second and third "
                              "destination-sidecar file operations"):
            self._leg_s3("F5")
        with self.subTest(leg="S3/F6: two sidecars set aside, reverse-order put-backs, "
                              "a failed put-back that does not stop the rest"):
            self._leg_s3("F6")
        with self.subTest(leg="S4a: failed restore onto a missing destination leaves "
                              "nothing behind"):
            self._leg_s4_missing()
        with self.subTest(leg="S4b: failed restore onto an empty destination file "
                              "leaves it empty"):
            self._leg_s4_empty()
        with self.subTest(leg="S5: a quiesced destination with no sidecars restores "
                              "cleanly"):
            self._leg_s5_quiesced()
        with self.subTest(leg="S6: a failed restore onto a quiesced non-empty "
                              "destination rolls back with no set-aside or forensic "
                              "artifacts"):
            self._leg_s6_quiesced_fault()


def _run_case(case_cls, method):
    """Run one TestCase method with full setUp/tearDown and surface failures
    as a plain AssertionError for the module-level pytest entry point."""
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(
        unittest.TestSuite([case_cls(method)]))
    if result.wasSuccessful():
        return
    raise AssertionError(f"{case_cls.__name__}.{method} did not pass:\n{stream.getvalue()}")


def test_recovery_sidecar_legs():
    """AC2 (#504) companion pytest entry point (S2-S4).
    RED at base, GREEN after the fix."""
    _run_case(_RestoreRecoveryCompanionCase, "test_recovery_sidecar_legs")


def run_legs():
    """Entry point tests/test_audit_restore_identity.py::
    test_recovery_preserves_later_write uses to run every companion leg."""
    test_recovery_sidecar_legs()


if __name__ == "__main__":
    unittest.main(verbosity=2)
