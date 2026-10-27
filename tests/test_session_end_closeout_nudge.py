"""SessionEnd closeout nudge — acceptance spec.

Defect under test: ``hooks/zmem-session-end.sh`` ends a session that wrote
memory rows without ever nudging the operator to run the closeout — the
rows stay unreviewed and the session's knowledge evaporates with no
bounded, one-line reminder.

POST-FIX CONTRACT (public behaviors, driven through the real hook script):

- The session-end surface emits EXACTLY ONE bounded closeout-reminder line
  WHEN the session has >=1 attributed store write and NO closeout
  recorded.
- NO reminder when a closeout marker exists.
- NO reminder when the session recorded no writes.
- The hook never writes the store (store.sqlite absent, or byte-identical
  before/after the hook run).

FROZEN PUBLIC SEAM (documented deliberately): the reminder state consults
two sidecar files under ``<ZMEM_DATA>/ops/`` named

    <sha256(session_id)[:32]>.wrote      (>=1 attributed store write)
    <sha256(session_id)[:32]>.closeout   (closeout already recorded)

This filename contract IS the seam this spec pins; the hook legs write the
markers directly (simulating the write-attribution and closeout-recording
paths) and assert only the hook's observable stdout/stderr and store
bytes. It must not depend on any other internal filename.

AMENDMENTS (producer + lifecycle legs — the marker writers and reapers
are part of the same contract, not just the frozen filenames):

- PRODUCER: a REAL ``store.py add --source-ref "session:<sid>"`` writes
  the ``.wrote`` marker itself (with or without a ``:detail`` suffix on
  the ref; the marker keys on the bare sid). A REFUSED write (rc 2)
  leaves no marker.
- ``store.py closeout-ack --session-id <sid>`` writes the ``.closeout``
  marker WITHOUT creating store.sqlite anywhere under ZMEM_DATA
  (store-independent acknowledgment).
- SWEEP: ``store.py sweep --marker-dir <tmp> --max-age-days N`` reaps
  aged ``.wrote``/``.closeout`` markers past the max age and preserves
  fresh ones.

All stores are throwaway temp stores; ambient zmem env is stripped from
every child process (storelib freezes the store path at first import — an
unpinned child could auto-migrate the shared operator store). Runs
standalone: python -X utf8 tests/test_session_end_closeout_nudge.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
SESSION_END = REPO_ROOT / "hooks" / "zmem-session-end.sh"

NS = "project:nudge-probe"

# Mirror the _STRIP_ENV pattern of tests/test_subagent_tasktext.py:45-67,
# plus the session-end retain-stage knobs the hook consults.
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
    "ZMEM_SESSION", "ZMEM_REMOTE_URL",
    "ZMEM_TRANSCRIPT", "ZMEM_CAPTURE", "ZMEM_AUTO_RETAIN",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
)


def _clean_env(tmp: str, sid: str, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_SESSION": sid,
        "ZMEM_NAMESPACE": NS,
        "ZMEM_ROOT": str(REPO_ROOT),
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _marker(tmp: str, sid: str, suffix: str) -> Path:
    stem = hashlib.sha256(sid.encode("utf8")).hexdigest()[:32]
    return Path(tmp) / "ops" / (stem + suffix)


def _run_hook(env: dict) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if not bash:
        raise unittest.SkipTest("no bash on PATH")
    return subprocess.run(
        [bash, str(SESSION_END)],
        input="{}", capture_output=True, text=True, env=env,
        timeout=180, cwd=env["ZMEM_DATA"],
    )


def _closeout_lines(proc: subprocess.CompletedProcess) -> list:
    combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines()
    return [ln for ln in combined if "closeout" in ln.lower()]


def _store_add(tmp: str, sid: str, *, source_ref: str,
               namespace: str = NS) -> subprocess.CompletedProcess:
    """A REAL `store.py add` attributed to ``sid`` via --source-ref.

    ZMEM_SESSION is removed from the child env so the marker (when the
    producer lands) can only key on the session id PARSED from the
    source-ref, never an ambient session knob."""
    env = _clean_env(tmp, sid)
    env.pop("ZMEM_SESSION", None)
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE_PY), "add",
         "--namespace", namespace, "--type", "fact",
         "--content", "closeout producer probe row",
         "--signal", "test", "--confidence", "0.9",
         "--source-ref", source_ref],
        capture_output=True, text=True, env=env, timeout=180,
    )


class SessionEndCloseoutNudgeTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-nudge-probe-")
        self._sid = "nudge-probe-" + hashlib.sha256(
            self._tmp.encode("utf8")).hexdigest()[:12]

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _attributed_write(self):
        """The write-attribution input: one real store.py add whose
        --source-ref carries the session id."""
        env = _clean_env(self._tmp, self._sid)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "add",
             "--namespace", NS, "--type", "fact",
             "--content", "nudge probe attributed write",
             "--signal", "test", "--confidence", "0.9",
             "--source-ref", "session:%s:test" % self._sid],
            capture_output=True, text=True, env=env, timeout=180,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def _write_marker(self, suffix: str) -> None:
        path = _marker(self._tmp, self._sid, suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1\n", encoding="utf-8")

    def test_wrote_without_closeout_yields_exactly_one_reminder(self):
        self._attributed_write()
        # Frozen seam: simulate the attribution sidecar directly so the
        # assertion rests on the HOOK's decision, not the writer wiring.
        self._write_marker(".wrote")
        proc = _run_hook(_clean_env(self._tmp, self._sid))
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        lines = _closeout_lines(proc)
        self.assertEqual(
            len(lines), 1,
            "exactly one bounded closeout-reminder line expected; got "
            f"{lines!r} (stdout={proc.stdout!r} stderr={proc.stderr!r})")

    def test_closeout_marker_suppresses_reminder(self):
        self._attributed_write()
        self._write_marker(".wrote")
        self._write_marker(".closeout")
        proc = _run_hook(_clean_env(self._tmp, self._sid))
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        self.assertEqual(
            _closeout_lines(proc), [],
            "a recorded closeout must suppress the reminder "
            f"(stdout={proc.stdout!r} stderr={proc.stderr!r})")

    def test_no_writes_no_reminder(self):
        proc = _run_hook(_clean_env(self._tmp, self._sid))
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        self.assertEqual(
            _closeout_lines(proc), [],
            "a session with no recorded writes must not be nudged "
            f"(stdout={proc.stdout!r} stderr={proc.stderr!r})")

    def test_hook_never_writes_the_store(self):
        self._attributed_write()
        self._write_marker(".wrote")
        store = Path(self._tmp) / "store.sqlite"
        before = store.read_bytes() if store.is_file() else None
        proc = _run_hook(_clean_env(self._tmp, self._sid))
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        if before is None:
            self.assertFalse(
                store.exists(),
                "the session-end hook must never create the store")
        else:
            self.assertEqual(
                store.read_bytes(), before,
                "the session-end hook must leave store.sqlite "
                "byte-identical")

    # -- AMENDMENT 2a: the PRODUCER side of the .wrote seam ------------
    def test_producer_session_ref_writes_wrote_marker(self):
        r = _store_add(self._tmp, self._sid,
                       source_ref="session:%s" % self._sid)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        marker = _marker(self._tmp, self._sid, ".wrote")
        self.assertTrue(
            marker.exists(),
            "a real `store.py add --source-ref session:<sid>` (no suffix, "
            "ZMEM_SESSION absent) must write the "
            "<sha256(sid)[:32]>.wrote marker under <ZMEM_DATA>/ops/")

    def test_producer_suffixed_session_ref_same_marker(self):
        # "session:<sid>:detail" attributes to the SAME bare sid — the
        # suffix is detail provenance, not part of the session identity.
        r = _store_add(
            self._tmp, self._sid,
            source_ref="session:%s:detail" % self._sid)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertTrue(
            _marker(self._tmp, self._sid, ".wrote").exists(),
            "a `--source-ref session:<sid>:detail` write must land the "
            "same <sha256(sid)[:32]>.wrote marker as the bare ref")
        naive = hashlib.sha256(
            ("%s:detail" % self._sid).encode("utf8")).hexdigest()[:32]
        self.assertFalse(
            (Path(self._tmp) / "ops" / (naive + ".wrote")).exists(),
            "the marker must key on the bare sid, not the ref string with "
            "its :detail suffix")

    def _seeded_row_id(self) -> str:
        """A real attributed add WITH --json, so the row id is parseable."""
        env = _clean_env(self._tmp, self._sid)
        env.pop("ZMEM_SESSION", None)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "add",
             "--namespace", NS, "--type", "fact",
             "--content", "closeout producer probe row",
             "--signal", "test", "--confidence", "0.9",
             "--source-ref", "session:%s" % self._sid, "--json"],
            capture_output=True, text=True, env=env, timeout=180,
        )
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        parsed = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(parsed.get("result"), "stored", parsed)
        return str(parsed["id"])

    def test_producer_supersede_success_writes_wrote_marker(self):
        # Reviewer round 2 (R2-1): supersede is a named stamping dispatch
        # (plan W4.1 + the 0.124.0 release note) — a successful supersede
        # must attribute the session write exactly like invalidate (the
        # empty session_ref falls back to the ZMEM_SESSION knob).
        seeded = self._seeded_row_id()
        # The seed add itself stamps the marker (the add dispatch at
        # cli.py writes it on success), so the leg must CLEAR it first —
        # otherwise the assert below holds even with the supersede stamp
        # deleted (reviewer round 3's no-op control proved the vacuity).
        _marker(self._tmp, self._sid, ".wrote").unlink(missing_ok=True)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "supersede",
             "--id", seeded, "--reason", "closeout supersede producer probe"],
            capture_output=True, text=True,
            env=_clean_env(self._tmp, self._sid), timeout=180,
        )
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertTrue(
            _marker(self._tmp, self._sid, ".wrote").exists(),
            "a successful `store.py supersede` must write the "
            "<sha256(sid)[:32]>.wrote marker (named stamping dispatch)")

    def test_producer_update_refusal_leaves_no_wrote_marker(self):
        # The success-only stamping law holds for EVERY dispatch: an
        # update that is REFUSED (empty --reason is a stable exit-2
        # refusal in write.py update_memory) must leave no marker behind.
        seeded = self._seeded_row_id()
        _marker(self._tmp, self._sid, ".wrote").unlink(missing_ok=True)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "update",
             "--id", seeded, "--content", "updated closeout probe row",
             "--reason", ""],
            capture_output=True, text=True,
            env=_clean_env(self._tmp, self._sid), timeout=180,
        )
        self.assertEqual(
            r.returncode, 2,
            "the refused update (empty --reason) must exit 2, got "
            f"{r.returncode}: {r.stderr[-400:]!r}")
        self.assertFalse(
            _marker(self._tmp, self._sid, ".wrote").exists(),
            "a REFUSED update must leave no .wrote marker behind")

    def test_refused_write_produces_no_wrote_marker(self):
        r = _store_add(self._tmp, self._sid,
                       source_ref="session:%s" % self._sid,
                       namespace="project:")  # invalid: empty project name
        self.assertEqual(
            r.returncode, 2,
            "the refused write must exit 2 (capture-policy refusal), got "
            f"{r.returncode}: {r.stderr[-400:]!r}")
        self.assertFalse(
            _marker(self._tmp, self._sid, ".wrote").exists(),
            "a REFUSED write must leave no .wrote marker behind")

    # -- AMENDMENT 2b: closeout-ack (store-independent) ----------------
    def test_closeout_ack_writes_marker_without_store(self):
        env = _clean_env(self._tmp, self._sid)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "closeout-ack",
             "--session-id", self._sid],
            capture_output=True, text=True, env=env, timeout=180,
        )
        self.assertEqual(
            r.returncode, 0,
            "`store.py closeout-ack --session-id <sid>` must succeed, got "
            f"{r.returncode}: {r.stderr[-400:]!r}")
        self.assertTrue(
            _marker(self._tmp, self._sid, ".closeout").exists(),
            "closeout-ack must write the <sha256(sid)[:32]>.closeout "
            "marker under <ZMEM_DATA>/ops/")
        strays = [
            str(p) for p in Path(self._tmp).rglob("store.sqlite")]
        self.assertEqual(
            strays, [],
            "closeout-ack must be store-independent — no store.sqlite "
            "anywhere under ZMEM_DATA; found: %r" % strays)

    # -- AMENDMENT 2c: sweep reaps the lifecycle markers ---------------
    def test_sweep_reaps_aged_wrote_and_closeout_markers(self):
        ops = Path(self._tmp) / "ops"
        ops.mkdir(parents=True, exist_ok=True)
        aged_sid = self._sid + "-aged"
        fresh_sid = self._sid + "-fresh"
        aged = [_marker(self._tmp, aged_sid, ".wrote"),
                _marker(self._tmp, aged_sid, ".closeout")]
        fresh = [_marker(self._tmp, fresh_sid, ".wrote"),
                 _marker(self._tmp, fresh_sid, ".closeout")]
        for path in aged + fresh:
            path.write_text("1\n", encoding="utf-8")
        stale = time.time() - 40 * 86400  # 40 days old
        for path in aged:
            os.utime(path, (stale, stale))
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "sweep",
             "--marker-dir", self._tmp, "--max-age-days", "30"],
            capture_output=True, text=True, env=_clean_env(self._tmp,
                                                           self._sid),
            timeout=180,
        )
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        for path in aged:
            self.assertFalse(
                path.exists(),
                f"sweep --max-age-days 30 must reap the 40-day-old "
                f"marker {path.name}")
        for path in fresh:
            self.assertTrue(
                path.exists(),
                f"sweep must preserve the fresh marker {path.name}")


class SessionStartDeliveryTest(unittest.TestCase):
    """PR #407 review blocker 3(a): SessionEnd output has no live model
    turn on most hosts (the launcher comment records Claude Code ignoring
    bare additionalContext there), so the closeout reminder is re-surfaced
    ONCE at the next SessionStart through the payload python — the module
    the launcher translates into model-visible context. Exactly-once via
    the ``.nudged`` marker; silenced by a recorded closeout."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-nudge-start-")
        self._sid = "start-delivery-" + hashlib.sha256(
            self._tmp.encode("utf8")).hexdigest()[:12]
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "zmem_start_payload_probe",
            REPO_ROOT / "hooks" / "lib" / "zmem-session-start-payload.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._payload = mod

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_wrote_without_closeout_delivers_once(self):
        self._write_marker(".wrote")
        text = self._payload._closeout_reminder(self._tmp, self._sid)
        self.assertIn("closeout", text.lower())
        self.assertIn("closeout-ack", text)
        self.assertTrue(
            _marker(self._tmp, self._sid, ".nudged").exists(),
            "the delivery must stamp the .nudged exactly-once marker")
        self.assertEqual(
            self._payload._closeout_reminder(self._tmp, self._sid), "",
            "the reminder must surface exactly once per session id")

    def test_recorded_closeout_silences_delivery(self):
        self._write_marker(".wrote")
        self._write_marker(".closeout")
        self.assertEqual(self._payload._closeout_reminder(self._tmp, self._sid),
                         "")
        self.assertFalse(_marker(self._tmp, self._sid, ".nudged").exists())

    def test_no_writes_no_delivery(self):
        self.assertEqual(self._payload._closeout_reminder(self._tmp, self._sid),
                         "")
        self.assertFalse(_marker(self._tmp, self._sid, ".nudged").exists())

    def test_wiring_callsites_present(self):
        source = (REPO_ROOT / "hooks" / "lib"
                  / "zmem-session-start-payload.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("nudge, closeout_reminder"), 2,
                                "the reminder must ride BOTH emitted "
                                "envelopes (first fast-path + final context)")

    def _write_marker(self, suffix: str) -> None:
        path = _marker(self._tmp, self._sid, suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
