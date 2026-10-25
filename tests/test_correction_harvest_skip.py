"""Correction-harvest paste gate — acceptance spec.

Defect under test: the corrections capture lane treats a pasted HARVEST
REPORT — "see more lessons below" followed by a fenced ```json block of
candidate memory rows (namespace/type/content/signal objects) — as an
ordinary user correction message, mining the machine's own output back
into the store as if the operator had said it.

POST-FIX CONTRACT pinned here:

- The NEW predicate ``corrections.is_harvest_paste(message)`` is the
  contract name: for a message consisting of a lead-in line ("see more
  lessons below") plus a fenced ```json block holding a JSON ARRAY of >=6
  objects each carrying namespace/type/content/signal keys, it returns
  True (the message is machine harvest output, not a correction).
- The genuine-correction lane is untouched:
  ``should_include_message("No, use cp backup instead")`` stays True.
- A genuine short correction message still passes
  ``should_include_message`` and ``is_harvest_paste`` is False for it.

AMENDMENTS (behavioral wiring legs — the predicate alone could ship
unwired while every check stays green):

- HOOK END-TO-END: the REAL ``hooks/zmem-capture-correction.sh`` driven
  via bash with a JSON event on stdin. Its stdin/env contract (read from
  the script): the event field is ``prompt``; the queue location follows
  ZMEM_STORE/ZMEM_DATA; the namespace comes from ZMEM_NAMESPACE; the
  plugin root from ZMEM_ROOT. The pasted harvest report must land NO
  queue item; a genuine short correction must land one.
- HERMES LANE: in-process ``storelib.cli._hermes_capture_correction``
  (same corrections/correction_queue modules) — the paste returns False
  with no queue item, the genuine correction returns True with one.

ROUND-2A AMENDMENTS:

- FROZEN -k SELECTOR NAMES: the behavioral legs are named so the frozen
  selectors run them — the harvest legs contain 'harvest_not_captured'
  (test_harvest_not_captured_via_hook / ..._via_hermes) and the control
  legs contain 'genuine_control' (test_genuine_control_via_hook /
  ..._via_hermes). The wave-1 predicate tests
  (test_harvest_not_captured_paste / test_genuine_control_correction)
  also match those substrings, so each selector runs the whole family.
- CLEANUP ORDER (finding 9): every test class pins its own throwaway env
  (setUpClass) and restores only to the MODULE pin — never straight to
  ambient — with a single tearDownModule restoring ambient after the
  last class. The hermes legs therefore run under the SAME stripped env
  as the hook legs: an ambient ZMEM_HERMES_CORRECTIONS can never leak
  back in mid-run.

NOTE on the paste's row content: the candidate rows are deliberately
lesson-shaped ("use token budgets not hard row caps ...") so TODAY'S
``detect_patterns`` matches them — the paste is genuinely captured at the
pre-fix tree, which is the bug; with filler-only content the wiring legs
would vacuously pass.

SAFETY: even though corrections.py never opens the store, the environment
is pinned to a throwaway temp ZMEM_DATA/ZMEM_STORE BEFORE the import per
the repo convention (storelib freezes the store path at first import; an
unpinned import anywhere in the import graph could auto-migrate the
shared operator store). Ambient ZMEM_* knobs are stripped, including a
dev box's ZMEM_REMOTE_URL.

Runs standalone: python -X utf8 tests/test_correction_harvest_skip.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
CAPTURE_HOOK = REPO_ROOT / "hooks" / "zmem-capture-correction.sh"

# Mirror the _STRIP_ENV pattern of tests/test_subagent_tasktext.py:45-67,
# plus the capture-lane knobs (remote-mode routing, capture mode/feedback,
# and the hermes corrections kill switch) this spec must control.
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_INJECT_FLOOR_LEX", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
    "ZMEM_SESSION", "ZMEM_REMOTE_URL",
    "ZMEM_CAPTURE_MODE", "ZMEM_CAPTURE_FEEDBACK", "ZMEM_HERMES_CORRECTIONS",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
)


def _build_harvest_paste() -> str:
    """A pasted harvest report: lead-in + fenced json array of 6 rows.

    The row CONTENT is lesson-shaped on purpose ("use token budgets not
    hard row caps ..."): today's detect_patterns matches `use ... not`,
    so the paste is captured at the pre-fix tree — the defect — and the
    wiring legs below fail for the right reason until the gate lands."""
    rows = [
        {"namespace": "project:github.com/zaxbysauce/zmem",
         "type": "lesson", "signal": "test",
         "content": ("candidate lesson row %d: use token budgets not "
                     "hard row caps when trimming recall output filler"
                     % i)}
        for i in range(6)
    ]
    return ("see more lessons below\n```json\n" + json.dumps(rows)
            + "\n```")


_HARVEST_PASTE = _build_harvest_paste()
_GENUINE_CORRECTION = "No, the build flag goes after the test path"
# The wiring legs' genuine correction (matches the `^no,` strong pattern).
_GENUINE_MSG = ("No — use cp backup/restore for uncommitted work, "
                "never git checkout --")

# The wiring legs' namespaces (queue files are namespace-scoped).
_NS_HOOK = "project:github.com/zaxbysauce/harvest-hook-spec"
_NS_HERMES = "project:github.com/zaxbysauce/hermes-spec"


def _pin_env(prefix: str) -> tuple:
    """Strip ambient ZMEM_* (including ZMEM_HERMES_CORRECTIONS — the
    hermes lane's kill switch — and the capture knobs), then pin a
    throwaway store. Returns (tmp, saved-ambient-snapshot)."""
    saved = {k: os.environ.get(k) for k in _STRIP_ENV}
    for key in _STRIP_ENV:
        os.environ.pop(key, None)
    tmp = tempfile.mkdtemp(prefix=prefix)
    os.environ["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    os.environ["ZMEM_DATA"] = tmp
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    os.environ["ZMEM_SESSION"] = "harvest-skip-spec-session"
    return tmp, saved


def _restore_env(saved: dict) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# MODULE IMPORT PIN: storelib freezes the store path at first import, so
# the ambient env is stripped and a module-level throwaway store pinned
# BEFORE corrections / correction_queue / storelib.cli are imported. This
# pin stays in force for the whole run; only tearDownModule (after every
# class) ever restores the ambient env — no class cleanup may do it
# (round-2a finding 9: the wave-1 class tearDown restored ambient env
# mid-run, and the hermes legs then resolved their queue dir against
# ~/.zmem, leaking a queue file onto the operator's box).
_MODULE_TMP, _MODULE_SAVED = _pin_env("zmem-harvest-import-")
sys.path.insert(0, str(SCRIPTS))
import corrections as _CORRECTIONS  # noqa: E402  (env pinned above)
import correction_queue as _CQ  # noqa: E402
import storelib.cli as _ZCLI  # noqa: E402


def tearDownModule():
    """The ONE restore-to-ambient point, after every class has run."""
    _restore_env(_MODULE_SAVED)
    shutil.rmtree(_MODULE_TMP, ignore_errors=True)


class _PinnedEnvTestCase(unittest.TestCase):
    """Per-class env pin (round-2a finding 9).

    Every class re-pins a fresh throwaway store before its first test and
    restores AFTER its last test — but only back to the MODULE pin, never
    straight to ambient, so a later class can never observe ambient env.
    The queue/markers the hermes lane resolves at CALL time follow this
    pinned env, and the hard guarantees below fail the class loudly
    rather than let an ambient knob (e.g. ZMEM_HERMES_CORRECTIONS) leak
    back in."""

    _tmp: str = ""
    _saved: dict = {}

    @classmethod
    def setUpClass(cls):
        cls._tmp, cls._saved = _pin_env("zmem-harvest-%s-" % cls.__name__)
        assert "ZMEM_HERMES_CORRECTIONS" not in os.environ, (
            "ambient hermes kill switch leaked past the class env pin")
        assert os.environ.get("ZMEM_DATA") == cls._tmp, (
            "class env pin did not take effect")

    @classmethod
    def tearDownClass(cls):
        _restore_env(cls._saved)  # back to the MODULE pin, not ambient
        shutil.rmtree(cls._tmp, ignore_errors=True)


def _hook_env(tmp: str, sid: str, namespace: str) -> dict:
    """Child env for the capture hook: ambient ZMEM_* stripped, throwaway
    store/data pinned, the hook's own contract knobs set (ZMEM_NAMESPACE
    scopes the queue; ZMEM_ROOT resolves the scripts dir)."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_SESSION": sid,
        "ZMEM_NAMESPACE": namespace,
        "ZMEM_ROOT": str(REPO_ROOT),
        "PYTHONUTF8": "1",
    })
    return env


def _queue_items_in(tmp: str) -> list:
    """Every correction-queue item across all per-namespace queue files
    under <tmp>/queue (the queue dir the store path resolves to)."""
    items: list = []
    qdir = Path(tmp) / "queue"
    if not qdir.is_dir():
        return items
    for path in sorted(qdir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, list):
            items.extend(x for x in data if isinstance(x, dict))
    return items


class CorrectionHarvestSkipTest(_PinnedEnvTestCase):

    def test_harvest_not_captured_paste(self):
        # The pinned contract name: the paste IS recognized as harvest
        # output, and the genuine lane below still flows.
        self.assertIs(_CORRECTIONS.is_harvest_paste(_HARVEST_PASTE), True,
                      "a fenced json harvest block must be classified as "
                      "a harvest paste; message head=%r"
                      % _HARVEST_PASTE[:80])
        self.assertIs(
            _CORRECTIONS.should_include_message("No, use cp backup instead"),
            True,
            "the genuine-correction lane must keep flowing")

    def test_genuine_control_correction(self):
        self.assertIs(
            _CORRECTIONS.should_include_message(_GENUINE_CORRECTION), True,
            "a genuine short correction must still be captured")
        self.assertIs(_CORRECTIONS.is_harvest_paste(_GENUINE_CORRECTION),
                      False,
                      "a genuine correction must NOT look like a harvest "
                      "paste")


class CaptureHookWiringTest(_PinnedEnvTestCase):
    """HOOK END-TO-END (amendment 4a): the REAL
    hooks/zmem-capture-correction.sh driven via bash, exactly as the
    UserPromptSubmit host would — JSON event on stdin (the prompt field is
    ``prompt``), throwaway store/data env, ZMEM_NAMESPACE scoping the
    queue, ZMEM_ROOT resolving the scripts dir. Each test gets a fresh
    data dir so the queue starts empty; the class-level pin keeps the
    PROCESS env stripped for the whole class (round-2a finding 9)."""

    def setUp(self):
        # Per-test throwaway data dir (shadows the class pin dir for the
        # SUBPROCESS only — the class pin governs the in-process env).
        self._tmp = tempfile.mkdtemp(prefix="zmem-harvest-hook-")
        self._sid = "harvest-hook-sid"

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run_hook(self, prompt: str) -> subprocess.CompletedProcess:
        bash = shutil.which("bash")
        if not bash:
            raise unittest.SkipTest("no bash on PATH")
        event = json.dumps({"prompt": prompt,
                            "session_id": self._sid,
                            "cwd": self._tmp})
        return subprocess.run(
            [bash, str(CAPTURE_HOOK)], input=event,
            capture_output=True, text=True,
            env=_hook_env(self._tmp, self._sid, _NS_HOOK), timeout=180,
        )

    def test_harvest_not_captured_via_hook(self):
        # FROZEN -k SELECTOR NAME: must contain 'harvest_not_captured'.
        self.assertEqual(
            _queue_items_in(self._tmp), [],
            "baseline: a fresh data dir holds no queue items")
        proc = self._run_hook(_HARVEST_PASTE)
        self.assertEqual(proc.returncode, 0,
                         f"hook must exit 0; stderr={proc.stderr[-400:]!r}")
        items = _queue_items_in(self._tmp)
        self.assertEqual(
            items, [],
            "a pasted HARVEST REPORT is machine output, not an operator "
            "correction — the capture hook must NOT mine it back into the "
            "correction queue")

    def test_genuine_control_via_hook(self):
        # FROZEN -k SELECTOR NAME: must contain 'genuine_control'.
        proc = self._run_hook(_GENUINE_MSG)
        self.assertEqual(proc.returncode, 0,
                         f"hook must exit 0; stderr={proc.stderr[-400:]!r}")
        items = _queue_items_in(self._tmp)
        self.assertTrue(
            any("cp backup/restore" in str(it.get("message", ""))
                for it in items),
            "a genuine short correction must land in the correction "
            f"queue; got {len(items)} item(s)")


class HermesLaneWiringTest(_PinnedEnvTestCase):
    """HERMES LANE (amendment 4b): in-process
    ``storelib.cli._hermes_capture_correction(namespace, session_id,
    user_message, data_dir)``. The correction queue resolves its data dir
    from the CURRENT env at call time, so these legs run under this
    class's pin (never ambient — round-2a finding 9) and measure DELTAS
    on the class data dir's namespace-scoped queue file."""

    def _items(self) -> list:
        path = Path(_CQ.queue_path_for(_NS_HERMES))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [x for x in data if isinstance(x, dict)] \
            if isinstance(data, list) else []

    def test_genuine_control_via_hermes(self):
        # FROZEN -k SELECTOR NAME: must contain 'genuine_control'.
        before = self._items()
        queued = _ZCLI._hermes_capture_correction(
            _NS_HERMES, "hermes-spec-genuine", _GENUINE_MSG, self._tmp)
        self.assertIs(
            queued, True,
            "a genuine short correction must be queued on the hermes "
            "lane (return True)")
        after = self._items()
        self.assertEqual(
            len(after), len(before) + 1,
            "exactly one queue item must land for the genuine correction")
        self.assertIn(
            "cp backup/restore", str(after[-1].get("message", "")),
            "the queued item must carry the genuine correction text")

    def test_harvest_not_captured_via_hermes(self):
        # FROZEN -k SELECTOR NAME: must contain 'harvest_not_captured'.
        before = self._items()
        queued = _ZCLI._hermes_capture_correction(
            _NS_HERMES, "hermes-spec-paste", _HARVEST_PASTE, self._tmp)
        self.assertIs(
            queued, False,
            "a pasted harvest report must NOT be queued on the hermes "
            "lane — it is machine output, not an operator correction")
        self.assertEqual(
            self._items(), before,
            "no new queue item may appear for the harvest paste")


class FencedObjectShapeTest(unittest.TestCase):
    """Round-1 implementation-review follow-up: the fenced SINGLE-object
    shape (a closeout-remote harvest block carrying one object) must never
    raise and must classify correctly (an earlier wrap-indexing bug raised
    IndexError on this shape)."""

    HARVEST_OBJ = {"namespace": "p:x", "type": "lesson",
                   "content": "c" * 400, "signal": "test"}
    NON_HARVEST_OBJ = {"name": "x", "value": 42, "extra": "y", "pad": "z" * 400}

    def test_harvest_not_captured_fenced_single_object(self):
        import corrections
        msg = ("see below\n```json\n"
               + json.dumps(self.HARVEST_OBJ) + "\\n```")
        self.assertTrue(corrections.is_harvest_paste(msg))

    def test_genuine_control_fenced_non_harvest_object(self):
        import corrections
        msg = ("look:\n```json\n"
               + json.dumps(self.NON_HARVEST_OBJ) + "\\n```")
        self.assertFalse(corrections.is_harvest_paste(msg))
        self.assertFalse(corrections.is_harvest_paste(
            "short genuine correction text without any fence"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
