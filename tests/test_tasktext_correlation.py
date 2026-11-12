"""Issue #428 acceptance checks (RED baseline): the agent-id correlation
key for the task-text bridge, end to end.

The interface frozen here (store CLI + hook body + ledger reservation +
remote honesty gate):

- ``--delegation-agent-id <id>`` on ``recall``/``recent`` (park path, next
  to ``--delegation-prompt``) -> the parked entry carries agent_id == id.
- ``--consume-agent-id <id>`` on ``recall``/``recent``/``prefetch`` (next
  to ``--consume-tasktext``) -> ``consume_task_text(..., agent_id=id)``.
- Hook body (hooks/lib/zmem-recall-body.py): a pretool Agent/Task event
  whose ``tool_input.agent_id`` is a valid id forwards
  ``--delegation-agent-id <id>``; a subagent event whose top-level
  ``agent_id`` is a valid id forwards ``--consume-agent-id <id>`` next to
  ``--consume-tasktext``. Valid id = string, non-empty once stripped,
  <=128 chars, no C0/DEL control characters; anything else is treated as
  absent (flag NOT forwarded).
- Ledger reservation rule: a consume WITHOUT a matching id (id absent, or
  id not present in the stash) must never take OR age-drop an entry tagged
  with a different non-empty agent_id; the FIFO/aged-head heuristic
  operates over UNTAGGED entries only (an id that matches nothing falls
  back to the untagged entries, never to another agent's tagged one).
  With no tagged entries anywhere the behavior is byte-identical to today
  — including when an id IS supplied but the stash is all-untagged (the
  Claude production shape: the child always carries an agent_id, the
  parks never do).
- Remote honesty gate (storelib/remote_gate.py): with ZMEM_REMOTE_URL set,
  ``--delegation-agent-id``/``--consume-agent-id`` must be REFUSED with a
  stderr naming the flag and "not supported in remote mode" (recall/recent
  via _READ_DEFAULTS, prefetch via the _route_prefetch flag tuple) — the
  remote lane must never silently drop the id. A prefetch WITHOUT an id
  is not refused by that gate (PRESERVING control).

Envelope keys used (verified against the code, not invented): the
for-injection envelope exposes ``results`` (row dicts carrying ``id`` /
``namespace`` / ``content``, recall.py:959-962) and ``rendered`` (the
delivered fenced text) — build_injection_envelope (recall.py:289-349)
carries NO bridged-query key and inject.py keeps ``bridged_query``
internal (inject.py:1119-1127), so DELIVERY is asserted through the
rows the bridged prompt retrieves: the named test seeds rows whose rare
tokens (zebraquartz / mangoflint) appear ONLY in the corresponding parked
prompt, and asserts the envelope delivers the CHILD-1 row and not the
CHILD-2 row. Token retrievability in this model-less environment (FTS
lane, ZMEM_MODEL_AUTODOWNLOAD=0) was probe-verified: a plain
``recall --query <token words>`` returns exactly its row, and the bridged
consume envelope carries ONLY the matching row (count 1, other token
absent from rendered) — a self-check inside the named test re-proves
retrievability at run time so an envelope miss cannot hide a broken FTS.

RED at authoring time BY DESIGN: the flags and the forwarding do not
exist yet, so the wiring-dependent legs fail with clean argparse /
assertion messages; the legs that only pin already-existing behavior
(legacy parity, invalid-id non-forwarding, the remote gate-active
control) pass. Canonical argv form
``python -X utf8 tests/test_tasktext_correlation.py <Class>.<method>``.

Runs standalone: python -X utf8 tests/test_tasktext_correlation.py
"""

from __future__ import annotations

# Scratch env pinned at the very top, BEFORE _store_guard and the first
# storelib import: the guard reads the environment at import time (it
# refuses when the would-be resolution lands on the operator's real
# ~/.zmem store — which EXISTS on the authoring box, so the
# tests/test_tasktext_consume_guards.py :56-65 pin block must run before
# that import here), and the store path freezes at the first storelib
# import. CLI legs never rely on this frozen resolution — every
# subprocess re-pins ZMEM_STORE to its own per-test scratch and every
# ledger seed passes data_dir explicitly.
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"

_MODULE_SCRATCH = tempfile.mkdtemp(prefix="zmem-428-correlation-")
# Issue #389 AC9: capture the pre-pin ambient BEFORE the module-level
# mutations below so each carries an explicit unittest.addModuleCleanup
# restore (registered after the unittest import; standalone runs execute
# doModuleCleanups, one-process runs never depend on it).
_MODULE_ENV_PRIOR = {key: os.environ.get(key) for key in (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
    "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP")}
os.environ["ZMEM_STORE"] = os.path.join(_MODULE_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = _MODULE_SCRATCH
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_MODULE_SCRATCH, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
for _k in ("ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP"):
    os.environ.pop(_k, None)


def _restore_module_env():
    for key, value in _MODULE_ENV_PRIOR.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import time
import unittest
from unittest import mock

sys.path.insert(0, str(SCRIPTS))

unittest.addModuleCleanup(_restore_module_env)

from storelib import delivery_ledger as dl  # noqa: E402

# Ambient markers every store-touching child env must shed (the
# tests/test_subagent_tasktext.py _STRIP_ENV convention, extended with the
# #428 ambient agent/session markers).
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_PROJECT", "CLAUDE_PROJECT_DIR", "ZCODE_PROJECT_DIR",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_INJECT_FLOOR_LEX", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
    "ZMEM_SESSION", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
    "ZMEM_PENDING_SIDECAR", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    "ZMEM_SESSION_SOURCE", "ZMEM_TRANSCRIPT", "ZMEM_CROSS_PROJECT",
    "ZMEM_REMOTE_URL",
    "HOME", "USERPROFILE",
)
_STRIP_PREFIXES = ("ZMEM_SESSION", "ZMEM_AGENT_", "CLAUDE_SESSION",
                   "ZCODE_SESSION")


def _child_env(tmp: str, **extra: str) -> dict:
    """A sandboxed subprocess env: HOME/USERPROFILE in the scratch, store
    pinned to <tmp>/store.sqlite (so the store's production data dir —
    the PARENT of the resolved store path, inject.py:993-1004 — is tmp
    itself), no ambient session/agent/project/remote markers."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    for key in list(env):
        if key.startswith(_STRIP_PREFIXES):
            env.pop(key, None)
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    env.update({
        "HOME": home,
        "USERPROFILE": home,
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _run_store(env: dict, *argv: str):
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE_PY), *argv],
        capture_output=True, text=True, env=env, timeout=180,
        stdin=subprocess.DEVNULL)


def _stash_texts(data_dir: str, sid: str) -> list:
    path = dl.tasktext_path(data_dir, sid)
    if not path or not os.path.exists(path):
        return []
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = raw.get("entries", []) if isinstance(raw, dict) else []
    return [e.get("text") for e in entries if isinstance(e, dict)]


def _stash_agent_ids(data_dir: str, sid: str) -> dict:
    """text -> agent_id for every stashed entry (aged entries included)."""
    path = dl.tasktext_path(data_dir, sid)
    if not path or not os.path.exists(path):
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries", []) if isinstance(raw, dict) else []
    return {e.get("text"): e.get("agent_id")
            for e in entries if isinstance(e, dict)}


class _CliScratch(unittest.TestCase):
    """Shared fixture: a per-test scratch whose dir IS the production
    tasktext data dir for both the in-process seeds and the child store."""

    def setUp(self):
        self.tmp = str(Path(tempfile.mkdtemp(prefix="zmem-428-cli-")).resolve())
        self.env = _child_env(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_row(self, env: dict, content: str) -> None:
        r = _run_store(env, "add", "--namespace", "project:pin428kt",
                       "--type", "fact", "--content", content,
                       "--signal", "test", "--confidence", "0.9")
        self.assertEqual(r.returncode, 0,
                         "seed add failed: %s" % (r.stderr or "")[-400:])


# The two rare tokens: each appears in exactly one parked prompt and
# exactly one seeded row, so a delivered row identifies which prompt
# bridged (probe-verified: plain recall returns exactly its row, and the
# bridged consume envelope carries only the matching row). SELECTIVITY
# DISCIPLINE: a prompt must share NO token with the OTHER child's row —
# the rows keep "the"/"pin428", the prompts drop them (an earlier
# revision carried "pin428" in both prompts and the bridged CHILD-1
# query legitimately FTS-matched the CHILD-2 row too, so the
# not-delivered assertion could not discriminate).
_TOK1, _TOK2 = "zebraquartz", "mangoflint"
_ROW1 = _TOK1 + " harness the flange pins before the winter swap pin428"
_ROW2 = _TOK2 + " calibrate the burr grinder for the dark roast pin428"
_PROMPT1 = "CHILD-1 " + _TOK1 + " harness flange pins brief"
_PROMPT2 = "CHILD-2 " + _TOK2 + " calibrate burr grinder brief"
# Five lexically-diverse newer fillers after a 1.5s gap — the frozen
# C1/C3 fixture shape (tests/test_subagent_tasktext.py _seed_fixture):
# the task rows are older than the fillers, so only QUERY relevance (not
# recency) can lift a task row into the delivered set.
_FILLERS = (
    "build cache eviction policy scans the ubuntu runner images nightly filler1",
    "editorial review prefers tables over bullet spam for short facts filler2",
    "winter tires for the fleet vans get swapped in november before mountain routes freeze filler3",
    "the greenhouse tomatoes need staking before the august storms arrive filler4",
    "archival paper stock prefers alkaline buffering for long-term storage filler5",
)


class CorrelationKeyTest(_CliScratch):
    """The #428 named scenario and the CLI surface of the correlation key.

    RED at base: ``--consume-agent-id`` / ``--delegation-agent-id`` are
    unknown flags, so these fail on the returncode assertion with the
    argparse stderr in the message — never an import-time crash."""

    def test_aged_tagged_head_still_served(self):
        # THE issue's named test. An aged entry tagged ag-1 at the FIFO
        # head behind a fresh entry tagged ag-2: the ag-1 child (arriving
        # late, past the 300s orphan band) must still receive ITS entry —
        # the exact agent_id match outranks the aging heuristic and FIFO —
        # and the ag-2 entry must stay for its own child. DELIVERY (not
        # just consumption) is proven through the bridged query: the
        # consumed prompt becomes the recall query (inject.py:1119-1127),
        # so the envelope must deliver the CHILD-1 row (zebraquartz) and
        # not the CHILD-2 row (mangoflint).
        sid = "pin-428-aged-tagged"
        env = dict(self.env, ZMEM_INJECT_FLOOR_LEX="0")
        self._seed_row(env, _ROW1)
        self._seed_row(env, _ROW2)
        time.sleep(1.5)
        for filler in _FILLERS:
            self._seed_row(env, filler)
        # Self-check (non-vacuity): both rows are retrievable by their
        # tokens in THIS environment before anything #428-shaped runs —
        # an envelope miss below can then only mean the bridge broke.
        for token, row in ((_TOK1, _ROW1), (_TOK2, _ROW2)):
            r = _run_store(env, "recall", "--namespace", "project:pin428kt",
                           "--query", row, "--json")
            self.assertEqual(r.returncode, 0, "recall self-check failed")
            found = any(token in str(x.get("content", ""))
                        for x in json.loads(r.stdout).get("results", []))
            self.assertTrue(found, "self-check: the %s row must be "
                            "retrievable by its token (FTS lane, no model)"
                            % token)
        dl.park_task_text(self.tmp, sid, _PROMPT1, agent_id="ag-1",
                          now=time.time() - 600)
        dl.park_task_text(self.tmp, sid, _PROMPT2, agent_id="ag-2")
        r = _run_store(
            env, "recent", "--namespace", "project:pin428kt",
            "--limit", "5", "--include-global", "--global-limit", "3",
            "--no-bump", "--for-injection", "--json",
            "--session-id=" + sid, "--moment", "subagent", "--lane", "zcode",
            "--consume-tasktext", "--consume-agent-id", "ag-1")
        self.assertEqual(
            r.returncode, 0,
            "recent must accept --consume-agent-id (the #428 correlation "
            "key); store stderr:\n%s" % (r.stderr or "")[-500:])
        texts = _stash_texts(self.tmp, sid)
        self.assertNotIn(
            _PROMPT1, texts,
            "the ag-1 child's consume must take ITS aged tagged entry — "
            "CHILD-1 still stashed (%r)" % texts)
        self.assertIn(
            _PROMPT2, texts,
            "the ag-2 entry must REMAIN for its own child — CHILD-2 left "
            "the stash (%r)" % texts)
        payload = json.loads(r.stdout)
        self.assertIsInstance(payload, dict, "the envelope must be a JSON "
                              "object (got %r)" % type(payload).__name__)
        contents = [str(x.get("content", ""))
                    for x in payload.get("results", [])]
        rendered = str(payload.get("rendered", ""))
        self.assertIn(
            _TOK1, rendered,
            "DELIVERY: the bridged CHILD-1 prompt must become the recall "
            "query and deliver the %s row (results: %r)" % (_TOK1, contents))
        self.assertTrue(
            any(_TOK1 in c for c in contents),
            "the %s row must appear in envelope results (got %r)"
            % (_TOK1, contents))
        self.assertNotIn(
            _TOK2, rendered,
            "DELIVERY: the CHILD-2 row must NOT be delivered — its prompt "
            "never bridged (results: %r)" % contents)
        self.assertFalse(
            any(_TOK2 in c for c in contents),
            "the %s row must be absent from envelope results (got %r)"
            % (_TOK2, contents))

    def test_park_recent_carries_agent_id(self):
        sid = "pin-428-park-recent"
        prompt = "park me with a correlation id via recent pin428"
        r = _run_store(
            self.env, "recent", "--namespace", "project:pin428kt",
            "--limit", "5", "--include-global", "--global-limit", "3",
            "--no-bump", "--for-injection", "--json",
            "--session-id=" + sid, "--moment", "pretool", "--lane", "zcode",
            "--delegation-prompt", prompt,
            "--delegation-agent-id", "ag-7")
        self.assertEqual(
            r.returncode, 0,
            "recent must accept --delegation-agent-id; store stderr:\n%s"
            % (r.stderr or "")[-500:])
        ids = _stash_agent_ids(self.tmp, sid)
        self.assertEqual(
            ids.get(prompt), "ag-7",
            "the parked entry must carry agent_id 'ag-7' (stash: %r)" % ids)

    def test_park_recall_carries_agent_id(self):
        sid = "pin-428-park-recall"
        prompt = "park me with a correlation id via recall pin428"
        r = _run_store(
            self.env, "recall", "--namespace", "project:pin428kt",
            "--query", "", "--limit", "5", "--include-global",
            "--global-limit", "3", "--no-bump", "--for-injection", "--json",
            "--session-id=" + sid, "--moment", "pretool", "--lane", "zcode",
            "--delegation-prompt", prompt,
            "--delegation-agent-id", "ag-7")
        self.assertEqual(
            r.returncode, 0,
            "recall must accept --delegation-agent-id; store stderr:\n%s"
            % (r.stderr or "")[-500:])
        ids = _stash_agent_ids(self.tmp, sid)
        self.assertEqual(
            ids.get(prompt), "ag-7",
            "the parked entry must carry agent_id 'ag-7' (stash: %r)" % ids)

    def test_consume_recall_agent_id(self):
        # recall's consume lane: --query "" (empty) + --moment subagent +
        # --consume-tasktext + --consume-agent-id (storelib/cli.py:6093).
        sid = "pin-428-consume-recall"
        child1 = "CHILD-1 recall-lane aged tagged text pin428"
        child2 = "CHILD-2 recall-lane fresh tagged text pin428"
        dl.park_task_text(self.tmp, sid, child1, agent_id="ag-1",
                          now=time.time() - 600)
        dl.park_task_text(self.tmp, sid, child2, agent_id="ag-2")
        r = _run_store(
            self.env, "recall", "--namespace", "project:pin428kt",
            "--query", "", "--limit", "5", "--include-global",
            "--global-limit", "3", "--no-bump", "--for-injection", "--json",
            "--session-id=" + sid, "--moment", "subagent", "--lane", "zcode",
            "--consume-tasktext", "--consume-agent-id", "ag-1")
        self.assertEqual(
            r.returncode, 0,
            "recall must accept --consume-agent-id; store stderr:\n%s"
            % (r.stderr or "")[-500:])
        texts = _stash_texts(self.tmp, sid)
        self.assertNotIn(child1, texts,
                         "the recall consume must take the ag-1 entry (%r)"
                         % texts)
        self.assertIn(child2, texts,
                      "the recall consume must spare the ag-2 entry (%r)"
                      % texts)

    def test_consume_prefetch_agent_id(self):
        # prefetch's consume lane (storelib/cli.py:6295-6321): --query is
        # REQUIRED on prefetch, and the consume only fires on an EMPTY
        # query — so the canonical shape is --query "".
        sid = "pin-428-consume-prefetch"
        child1 = "CHILD-1 prefetch-lane aged tagged text pin428"
        child2 = "CHILD-2 prefetch-lane fresh tagged text pin428"
        dl.park_task_text(self.tmp, sid, child1, agent_id="ag-1",
                          now=time.time() - 600)
        dl.park_task_text(self.tmp, sid, child2, agent_id="ag-2")
        r = _run_store(
            self.env, "prefetch", "--query", "",
            "--namespace", "project:pin428kt",
            "--session-id=" + sid, "--moment", "subagent", "--lane", "zcode",
            "--consume-tasktext", "--consume-agent-id", "ag-1")
        self.assertEqual(
            r.returncode, 0,
            "prefetch must accept --consume-agent-id; store stderr:\n%s"
            % (r.stderr or "")[-500:])
        texts = _stash_texts(self.tmp, sid)
        self.assertNotIn(child1, texts,
                         "the prefetch consume must take the ag-1 entry (%r)"
                         % texts)
        self.assertIn(child2, texts,
                      "the prefetch consume must spare the ag-2 entry (%r)"
                      % texts)


class LegacyParityTest(_CliScratch):
    """Legacy parity: with no TAGGED entries anywhere, the #329 aged-head
    skip keeps operating byte-identically over the untagged stash —
    including when an id IS supplied (the Claude production shape after
    the fix: the child always carries an agent_id, the parks stay
    untagged, so the id matches nothing and the legacy heuristic runs).
    GREEN at base for the id-less leg; RED for the id-present leg only
    because the flag does not exist yet."""

    def test_no_ids_aged_head_skip_unchanged(self):
        sid = "pin-428-legacy-parity"
        aged = "legacy aged orphan head text pin428"
        fresh = "legacy fresh delegation text pin428"
        dl.park_task_text(self.tmp, sid, aged, now=time.time() - 600)
        dl.park_task_text(self.tmp, sid, fresh)
        r = _run_store(
            self.env, "recent", "--namespace", "project:pin428kt",
            "--limit", "5", "--include-global", "--global-limit", "3",
            "--no-bump", "--for-injection", "--json",
            "--session-id=" + sid, "--moment", "subagent", "--lane", "zcode",
            "--consume-tasktext")
        self.assertEqual(r.returncode, 0,
                         "the id-less consume shape must keep working; "
                         "stderr:\n%s" % (r.stderr or "")[-500:])
        texts = _stash_texts(self.tmp, sid)
        # The aged head is skipped AND dropped; the FRESH entry is the one
        # consumed (an envelope key for the bridged query does not exist,
        # so 'fresh left the stash' is the delivery evidence).
        self.assertEqual(
            texts, [],
            "legacy parity: id-less [aged, fresh] must end with an EMPTY "
            "stash (aged head skipped and dropped, fresh consumed) — got %r"
            % texts)

    def test_id_present_all_untagged_matches_legacy(self):
        # The production-path parity leg: an id is SUPPLIED but the stash
        # is all-untagged (no tagged entries anywhere) — the consume must
        # behave EXACTLY like the id-less legacy path above: aged head
        # skipped and dropped, fresh entry consumed, empty stash.
        sid = "pin-428-legacy-id-present"
        aged = "untagged aged head with an id present pin428"
        fresh = "untagged fresh delegation with an id present pin428"
        dl.park_task_text(self.tmp, sid, aged, now=time.time() - 600)
        dl.park_task_text(self.tmp, sid, fresh)
        r = _run_store(
            self.env, "recent", "--namespace", "project:pin428kt",
            "--limit", "5", "--include-global", "--global-limit", "3",
            "--no-bump", "--for-injection", "--json",
            "--session-id=" + sid, "--moment", "subagent", "--lane", "zcode",
            "--consume-tasktext", "--consume-agent-id", "ag-anything")
        self.assertEqual(
            r.returncode, 0,
            "recent must accept --consume-agent-id; store stderr:\n%s"
            % (r.stderr or "")[-500:])
        texts = _stash_texts(self.tmp, sid)
        self.assertEqual(
            texts, [],
            "an id over an ALL-UNTAGGED stash must take the legacy path: "
            "aged head skipped and dropped, fresh consumed, empty stash "
            "(got %r)" % texts)


class ReservationRuleTest(unittest.TestCase):
    """Ledger-level reservation rule (#428): an id-less (or mismatched-id)
    consume must NEVER take or age-drop an entry tagged for another
    agent. Deterministic — the orphan band is reached by backdating the
    park (now=time.time()-600, inside the 6h window, outside the 300s
    band). RED at base: today's aged-head skip drops the TAGGED aged head
    (leg 1), the FIFO fallback serves a tagged entry to a mismatched id
    (leg 2), and no untagged fallback exists for a mismatched id (leg 3).
    """

    def setUp(self):
        self.tmp = str(Path(tempfile.mkdtemp(prefix="zmem-428-reserve-")).resolve())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_untagged_consume_spares_tagged_entries(self):
        sid = "pin-428-reserve-untagged"
        tagged = "tagged aged entry reserved for ag-1 pin428"
        untagged = "untagged fresh entry for the id-less child pin428"
        dl.park_task_text(self.tmp, sid, tagged, agent_id="ag-1",
                          now=time.time() - 600)
        dl.park_task_text(self.tmp, sid, untagged)
        served = dl.consume_task_text(self.tmp, sid)  # NO id
        self.assertEqual(
            served, untagged,
            "an id-less consume takes the next UNTAGGED entry, not the "
            "tagged one (got %r)" % served)
        texts = _stash_texts(self.tmp, sid)
        self.assertIn(
            tagged, texts,
            "RESERVATION VIOLATED: an id-less consume must not age-drop an "
            "entry tagged for another agent — the tagged aged entry must "
            "survive for its own child (stash: %r)" % texts)

    def test_mismatched_id_takes_nothing_and_keeps_entry(self):
        sid = "pin-428-reserve-mismatch"
        tagged = "tagged fresh entry reserved for ag-1 pin428"
        dl.park_task_text(self.tmp, sid, tagged, agent_id="ag-1")
        # A consume whose id matches NO stash entry and NO untagged entry
        # exists: it must take NOTHING (empty string, no crash) and leave
        # the tagged entry in place.
        served = dl.consume_task_text(self.tmp, sid, agent_id="ag-other")
        self.assertEqual(
            served, "",
            "RESERVATION VIOLATED: a consume with a non-matching id must "
            "take NOTHING when no untagged entry exists — the FIFO fallback "
            "served another agent's tagged entry (got %r)" % served)
        texts = _stash_texts(self.tmp, sid)
        self.assertIn(
            tagged, texts,
            "the mismatched-id consume must leave the tagged entry stashed "
            "for its own child (stash: %r)" % texts)

    def test_mismatched_id_falls_back_to_untagged(self):
        # The mixed stash: [tagged ag-1 fresh, untagged fresh]. A consume
        # with a NON-matching id must skip the tagged entry and fall back
        # to the UNTAGGED entry (the untagged lane is the shared pool);
        # the tagged entry stays reserved for ag-1.
        sid = "pin-428-reserve-mixed"
        tagged = "tagged fresh entry reserved for ag-1 pin428"
        untagged = "untagged fresh entry any child may take pin428"
        dl.park_task_text(self.tmp, sid, tagged, agent_id="ag-1")
        dl.park_task_text(self.tmp, sid, untagged)
        served = dl.consume_task_text(self.tmp, sid, agent_id="ag-x")
        self.assertEqual(
            served, untagged,
            "a non-matching id must fall back to the UNTAGGED entry, never "
            "another agent's tagged one (got %r)" % served)
        texts = _stash_texts(self.tmp, sid)
        self.assertIn(
            tagged, texts,
            "the tagged entry must stay reserved for ag-1 after the "
            "mismatched-id consume (stash: %r)" % texts)


# --------------------------------------------------------------------------
# Hook-body legs: the REAL adapter (hooks/lib/zmem-recall-body.py) driven
# standalone with the store subprocess CAPTURED (the
# tests/test_tasktext_adapter_guards.py harness pattern — check_output is
# faked so no store child ever runs; the body imports no storelib).
# --------------------------------------------------------------------------

_NS = "project:pin428body"


def _body_module():
    spec = importlib.util.spec_from_file_location("zmem_428_body", BODY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeStdin:
    def __init__(self, event: dict):
        self.buffer = io.BytesIO(json.dumps(event).encode("utf-8"))


class _FakeProc:
    returncode = 1
    stdout = b"{}"


def _drive_body(event: dict, mode: str):
    """Run the adapter's main() with every store subprocess captured.
    Returns (returncode, captured_argv_list)."""
    module = _body_module()
    calls: list = []

    def _fake_check_output(command, **kwargs):
        calls.append([str(part) for part in command])
        return b"{}"

    # argv mirrors the real hook invocations (zmem-subagent-recall.sh:174
    # passes mode/limits/agent_type; argv[7] is discarded by the body).
    argv = ["body", str(STORE_PY), _NS, "25000", mode, "5", "3",
            "general-purpose"]
    sink = io.StringIO()
    with mock.patch.object(module.subprocess, "check_output",
                           _fake_check_output), \
            mock.patch.object(module.subprocess, "run",
                              lambda *a, **k: _FakeProc()), \
            mock.patch.object(sys, "argv", argv), \
            mock.patch.object(sys, "stdin", _FakeStdin(event)), \
            contextlib.redirect_stdout(sink):
        rc = module.main()
    return rc, calls


class HookForwardingTest(unittest.TestCase):
    """The hook body forwards the correlation id on the store argv it
    already builds (park block hooks/lib/zmem-recall-body.py:878-910;
    subagent grant :860-866). The two *_forwarded legs are RED at base
    (no forwarding exists) and double as the non-vacuity controls for
    the invalid-id legs below."""

    def test_pretool_agent_id_forwarded(self):
        prompt = "pretool delegation prompt carrying an agent id pin428"
        rc, calls = _drive_body({
            "hook_event_name": "PreToolUse", "tool_name": "Agent",
            "tool_input": {"prompt": prompt, "agent_id": "ag-7"},
            "session_id": "pin-428-body-pretool",
        }, "pretool")
        self.assertEqual(rc, 0, "the adapter must complete cleanly")
        self.assertTrue(calls, "the adapter must attempt a store call")
        cmd = next((c for c in calls if "--delegation-prompt" in c), None)
        self.assertIsNotNone(
            cmd, "the delegation park must ride the store argv (calls: %r)"
            % calls)
        self.assertIn(
            "--delegation-agent-id", cmd,
            "a pretool Agent event with a valid tool_input.agent_id must "
            "forward --delegation-agent-id (store argv: %r)" % cmd)
        self.assertEqual(cmd[cmd.index("--delegation-agent-id") + 1], "ag-7",
                         "the forwarded id must be the event's agent_id")

    def test_subagent_agent_id_forwarded(self):
        rc, calls = _drive_body({
            "hook_event_name": "SubagentStart",
            "session_id": "pin-428-body-subagent",
            "agent_id": "ag-9", "agent_type": "general-purpose",
        }, "subagent")
        self.assertEqual(rc, 0, "the adapter must complete cleanly")
        self.assertTrue(calls, "the adapter must attempt a store call")
        cmd = next((c for c in calls if "--consume-tasktext" in c), None)
        self.assertIsNotNone(
            cmd, "the subagent lane must carry --consume-tasktext (calls: %r)"
            % calls)
        self.assertIn(
            "--consume-agent-id", cmd,
            "a subagent event with a valid top-level agent_id must forward "
            "--consume-agent-id next to --consume-tasktext (store argv: %r)"
            % cmd)
        self.assertEqual(cmd[cmd.index("--consume-agent-id") + 1], "ag-9",
                         "the forwarded id must be the event's agent_id")

    def _assert_park_without_id_flag(self, agent_id_value, why: str):
        # Invalid ids are treated as ABSENT: the park itself must still
        # ride the argv (--delegation-prompt present, so 'flag absent' is
        # not the park block failing to run) but no id flag may appear.
        prompt = "pretool delegation prompt with an invalid id pin428"
        event = {
            "hook_event_name": "PreToolUse", "tool_name": "Agent",
            "tool_input": {"prompt": prompt, "agent_id": agent_id_value},
            "session_id": "pin-428-body-invalid",
        }
        rc, calls = _drive_body(event, "pretool")
        self.assertEqual(rc, 0, "an invalid agent_id must not fail the adapter")
        cmd = next((c for c in calls if "--delegation-prompt" in c), None)
        self.assertIsNotNone(
            cmd, "the delegation park must still ride the store argv "
            "(calls: %r)" % calls)
        self.assertNotIn(
            "--delegation-agent-id", cmd,
            "an INVALID agent_id (%s) must be treated as absent — no "
            "--delegation-agent-id on the store argv (%r)" % (why, cmd))

    def test_pretool_newline_agent_id_not_forwarded(self):
        self._assert_park_without_id_flag("bad\nid", "contains a newline")

    def test_pretool_oversized_agent_id_not_forwarded(self):
        self._assert_park_without_id_flag("x" * 129, "129 chars > 128 cap")

    def test_pretool_whitespace_agent_id_not_forwarded(self):
        self._assert_park_without_id_flag("   ", "whitespace-only")

    def test_pretool_non_string_agent_id_not_forwarded(self):
        self._assert_park_without_id_flag(42, "non-string")

    def test_legacy_events_without_ids_forward_no_id_flags(self):
        # Legacy hosts supply no ids anywhere: the park and the consume
        # grant keep their today shape, and neither id flag appears.
        rc, calls = _drive_body({
            "hook_event_name": "PreToolUse", "tool_name": "Agent",
            "tool_input": {"prompt": "legacy prompt without an id pin428"},
            "session_id": "pin-428-body-legacy-pretool",
        }, "pretool")
        self.assertEqual(rc, 0, "the adapter must complete cleanly")
        cmd = next((c for c in calls if "--delegation-prompt" in c), None)
        self.assertIsNotNone(cmd, "the legacy park must still ride the argv")
        self.assertNotIn("--delegation-agent-id", cmd,
                         "no id in the event -> no id flag (argv: %r)" % cmd)

        rc, calls = _drive_body({
            "hook_event_name": "SubagentStart",
            "session_id": "pin-428-body-legacy-subagent",
            "agent_type": "general-purpose",
        }, "subagent")
        self.assertEqual(rc, 0, "the adapter must complete cleanly")
        cmd = next((c for c in calls if "--consume-tasktext" in c), None)
        self.assertIsNotNone(
            cmd, "the legacy subagent lane must still carry the consume grant")
        self.assertNotIn("--consume-agent-id", cmd,
                         "no id in the event -> no id flag (argv: %r)" % cmd)


class RemoteModeRefusalTest(unittest.TestCase):
    """Remote honesty gate (#428): with ZMEM_REMOTE_URL set, the new id
    flags must be REFUSED by name ("not supported in remote mode") on
    recall/recent (remote_gate._READ_DEFAULTS) and on the remote prefetch
    route (_route_prefetch's flag tuple) — never silently dropped. Env
    per the remote contract (remote_gate.handle_remote): ZMEM_STORE (and
    a store-pinned ZMEM_DATA) together with ZMEM_REMOTE_URL is a CONFIG
    ERROR, so these legs drop ZMEM_STORE/ZMEM_DATA entirely and pin
    CLAUDE_PLUGIN_DATA to a scratch dir. The gate-active control proves
    the env really routes remote (otherwise the refusals below could pass
    vacuously in local mode); the no-id prefetch control proves the
    refusal is specific to the id flag. RED at base: the flags die at
    argparse BEFORE the gate, so stderr names the flag but lacks the
    remote-mode phrase."""

    REFUSAL_PHRASE = "not supported in remote mode"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-428-remote-")).resolve()
        home = self.tmp / "home"
        home.mkdir(parents=True, exist_ok=True)
        plugdata = self.tmp / "plugdata"
        plugdata.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        for key in list(env):
            if key.startswith(_STRIP_PREFIXES):
                env.pop(key, None)
        env.update({
            "HOME": str(home), "USERPROFILE": str(home),
            "CLAUDE_PLUGIN_DATA": str(plugdata),
            "ZMEM_REMOTE_URL": "http://127.0.0.1:9/mcp",
            "PYTHONUTF8": "1",
        })
        self.env = env

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *argv: str):
        return _run_store(self.env, *argv)

    def test_remote_gate_active_control(self):
        # PRESERVING control (GREEN at base): a KNOWN refused flag
        # (--for-injection on recall, remote_gate._READ_DEFAULTS) draws
        # the exact refusal in this env — proving the gate is armed, so
        # the refusal legs below cannot pass vacuously in local mode.
        r = self._run("recall", "--query", "q", "--for-injection")
        self.assertEqual(r.returncode, 2, "the refused read exits 2")
        self.assertIn(self.REFUSAL_PHRASE, r.stderr,
                      "the gate-active control must draw the remote-mode "
                      "refusal; stderr:\n%s" % (r.stderr or "")[-400:])

    def test_recall_delegation_agent_id_refused(self):
        r = self._run("recall", "--query", "q",
                      "--delegation-agent-id", "ag-1")
        self.assertEqual(r.returncode, 2,
                         "remote recall must REFUSE --delegation-agent-id; "
                         "stderr:\n%s" % (r.stderr or "")[-400:])
        self.assertIn("--delegation-agent-id", r.stderr,
                      "the refusal must NAME the flag; stderr:\n%s"
                      % (r.stderr or "")[-400:])
        self.assertIn(self.REFUSAL_PHRASE, r.stderr,
                      "remote mode must refuse (never silently drop) "
                      "--delegation-agent-id; stderr:\n%s"
                      % (r.stderr or "")[-400:])

    def test_recent_consume_agent_id_refused(self):
        r = self._run("recent", "--consume-agent-id", "ag-1")
        self.assertEqual(r.returncode, 2,
                         "remote recent must REFUSE --consume-agent-id; "
                         "stderr:\n%s" % (r.stderr or "")[-400:])
        self.assertIn("--consume-agent-id", r.stderr,
                      "the refusal must NAME the flag; stderr:\n%s"
                      % (r.stderr or "")[-400:])
        self.assertIn(self.REFUSAL_PHRASE, r.stderr,
                      "remote mode must refuse (never silently drop) "
                      "--consume-agent-id; stderr:\n%s"
                      % (r.stderr or "")[-400:])

    def test_prefetch_consume_agent_id_refused(self):
        # The remote prefetch route forwards consume_tasktext VERBATIM
        # (remote_gate._forward_prefetch) and would silently drop the id —
        # the honesty contract requires a named refusal instead.
        r = self._run("prefetch", "--query", "",
                      "--namespace", "project:pin428r",
                      "--session-id", "s-r428", "--moment", "subagent",
                      "--lane", "zcode",
                      "--consume-tasktext", "--consume-agent-id", "ag-1")
        self.assertEqual(r.returncode, 2,
                         "remote prefetch must REFUSE --consume-agent-id; "
                         "stderr:\n%s" % (r.stderr or "")[-400:])
        self.assertIn("--consume-agent-id", r.stderr,
                      "the refusal must NAME the flag; stderr:\n%s"
                      % (r.stderr or "")[-400:])
        self.assertIn(self.REFUSAL_PHRASE, r.stderr,
                      "remote prefetch must refuse (never silently drop) "
                      "--consume-agent-id; stderr:\n%s"
                      % (r.stderr or "")[-400:])

    def test_prefetch_without_id_not_refused(self):
        # PRESERVING control (GREEN at base): the identical remote
        # prefetch WITHOUT an id is not refused by the honesty gate. On an
        # unreachable hub it degrades downstream instead (observed here:
        # exit 0 with a reason=hub-unreachable JSON envelope and an EMPTY
        # stderr) — so the honest assertion is only that the remote-mode
        # refusal phrase (and the id flag) never appear on stderr.
        r = self._run("prefetch", "--query", "",
                      "--namespace", "project:pin428r",
                      "--session-id", "s-r428b", "--moment", "subagent",
                      "--lane", "zcode", "--consume-tasktext")
        self.assertNotIn(self.REFUSAL_PHRASE, r.stderr,
                         "the no-id prefetch must not draw the "
                         "remote-mode refusal; stderr:\n%s"
                         % (r.stderr or "")[-400:])
        self.assertNotIn("--consume-agent-id", r.stderr,
                         "stderr: %r" % (r.stderr or "")[-200:])
        self.assertNotIn("Traceback", r.stderr,
                         "the no-id remote prefetch must fail open, never "
                         "crash; stderr:\n%s" % (r.stderr or "")[-400:])


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    if _argv:
        _suite = unittest.TestSuite()
        for _name in _argv:
            _suite.addTests(
                _loader.loadTestsFromName(_name, sys.modules[__name__]))
    else:
        _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print("CHECK-PASS" if _result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if _result.wasSuccessful() else 1)
