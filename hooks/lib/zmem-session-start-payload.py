#!/usr/bin/env python
"""SessionStart Tier-0/Tier-2 payload builder (issue #58; PR #190).

The entire payload block used to be a `python -c` inline string inside
zmem-session-start.sh. PR #190's review round pushed that string past the
Windows CreateProcess ~32K command-line limit and the hook silently
degraded to `{}` (the `|| echo '{}'` fallback swallowed the spawn failure)
— so the block now lives here as a real file with the IDENTICAL argv
contract (sys.argv[1..14], IndexError-tolerant ladder) and identical
stdout contract (a bare JSON envelope the bash wrapper wraps in the
<<<ZMEM_JSON>>> sentinel and neutralizes). Do not inline it again: any
`python -c` payload over ~32K chars silently dies on Windows.

Args (all optional beyond 1, IndexError-tolerant):
  1 core.md path          2 AGENTS.md path       3 store.py path
  4 data dir (native)     5 project dir          6 data dir (bash-resolved)
  7 namespace             8 ctx budget           9 host
 10 settings dir         11 nudge marker        12 session id
 13 drift JSON           14 session source (issue #118)
"""

import json
import os
import re
import subprocess
import sys

import json, os, re, sys, subprocess

core = sys.argv[1]
agents = sys.argv[2]
store_py = sys.argv[3]
home_win = sys.argv[4]
project = sys.argv[5]
data_dir = sys.argv[6]
ns = sys.argv[7]
try:
    budget = int(sys.argv[8])
except (IndexError, ValueError):
    budget = 25000
try:
    host = sys.argv[9]
except IndexError:
    host = ""
try:
    settings_dir = sys.argv[10]
except IndexError:
    settings_dir = ""
try:
    nudge_marker = sys.argv[11]
except IndexError:
    nudge_marker = ""
try:
    session_id = sys.argv[12]
except IndexError:
    session_id = ""
try:
    drift_json = sys.argv[13]
except IndexError:
    drift_json = ""
# Issue #118: SessionStart source (argv 14, optional — the IndexError
# tolerance of the argv ladder means an older wrapper/python skew degrades
# to the cold-start lane instead of crashing the hook).
try:
    source = sys.argv[14]
except IndexError:
    source = ""
if not isinstance(source, str):
    source = ""

# Issue #107 (Workstream A PR 2): the operator-facing drift notice. The bash
# layer already ran drift.py log-once (marker-guarded, log-only, exit 0) and
# handed its JSON here. Parse defensively: any absent, unparseable, or
# non-drifted payload degrades to today exact behavior. The message rides the
# systemMessage channel (operator-visible), NEVER additionalContext.
_drift_msg = ""
if drift_json:
    try:
        _dj = json.loads(drift_json)
        if (isinstance(_dj, dict) and _dj.get("status") == "drifted"
                and isinstance(_dj.get("system_message"), str)
                and _dj["system_message"]):
            _drift_msg = _dj["system_message"]
    except ValueError:
        _drift_msg = ""

# issue #110 (P0-5): ZMEM_INJECT=0 is the passive-injection kill switch.
# Tier 0 (core.md / AGENTS.md), Tier 2 recall, the store-path nudge, the
# promotion nudge, the correction-count nudge and the native-memory nudge
# are ALL passive context: log the disabled decision and emit the empty
# envelope, skipping every part. The maintenance the bash layer dispatched
# above (session-cadence) and the core.md seeding are capture-side and keep
# running. Only the literal 0 disables (ZMEM_QUERY_CONTEXT convention).
# data_dir (argv 6) is the bash-resolved, tilde-expanded data dir — the
# same directory the Tier-2 decision line below writes to.
if os.environ.get("ZMEM_INJECT", "1").strip() == "0":
    try:
        if data_dir and os.path.isdir(data_dir):
            _safe_sid = re.sub(
                r"[^A-Za-z0-9._-]", "_",
                (session_id or ""))[:128] or "unknown"
            # Issue #129: decision lines go to the dedicated, rotated
            # decisions log; the moment field lands at line end (additive).
            _dl = os.path.join(data_dir, "zmem-decisions.log")
            try:
                if store_py and os.path.isfile(store_py):
                    _sp = sys.path[:]
                    try:
                        # Review PRR-005: the rotation package imports from
                        # the scripts dir (storelib parent), not from the
                        # storelib dir itself.
                        sys.path.insert(0, os.path.dirname(store_py))
                        from storelib.log_rotate import rotate_on_append as _rota
                        _rota(_dl)
                    finally:
                        sys.path[:] = _sp
            except Exception:
                pass  # fail-open: append proceeds, growth never loss
            with open(_dl, "a", encoding="utf-8") as _lf:
                _lf.write(
                    "[%d] zmem-hook status=silent reason=disabled ids=[] all=[] sid=%s moment=session_start\n" % (
                        int(__import__("time").time()), _safe_sid))
    except Exception:
        pass  # fail-open: the audit log never blocks session start
    # Issue #107: keep the operator notice alive on the kill-switch path —
    # the drift evaluation already ran in the bash layer above.
    if _drift_msg:
        print(json.dumps({"systemMessage": _drift_msg}))
    else:
        print("{}")
    sys.exit(0)

parts = []

# Tier 0: core.md (user-level). errors="replace" so one bad byte does not nuke
# the entire payload — a single corrupt file degrades to that file only.
if core and os.path.isfile(core):
    try:
        with open(core, encoding="utf-8", errors="replace") as f:
            parts.append("# Loaded from memory (Tier 0 — core.md, user-level):\n\n" + f.read())
    except OSError:
        pass

# Tier 0: AGENTS.md (project-level)
if agents and os.path.isfile(agents):
    try:
        with open(agents, encoding="utf-8", errors="replace") as f:
            parts.append("# Loaded from memory (Tier 0 — AGENTS.md, project-level):\n\n" + f.read())
    except OSError:
        pass

# Tier 2: bounded recall — cheap admin pull of recent high-confidence live
# memories. Namespace is the canonical key passed in (ns), NOT basename(project).
# The recent floor reads ZMEM_INJECT_FLOOR_RECENT (default 0.5) — the SAME
# env var the shared body reads — so operator tuning applies uniformly
# (final-critic round-3 fix; was a hardcoded "0.5" literal).
if store_py and os.path.isfile(store_py):
    try:
        _rf_raw = os.environ.get("ZMEM_INJECT_FLOOR_RECENT", "")
        try:
            _recent_floor = float(_rf_raw) if _rf_raw else 0.5
        except ValueError:
            _recent_floor = 0.5
        # Issue #117 D-1: consult the delivery ledger so a row delivered at
        # an earlier moment of this session is not delivered again here.
        # Fail-open: import or read error = no exclusion (pre-#117 behavior).
        _ss_exclude_argv = []
        _ss_ledger = None
        _ss_dd_known = ""
        try:
            _sp_ss = sys.path[:]
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(store_py), "storelib"))
                import delivery_ledger as _dl_ss
            finally:
                sys.path[:] = _sp_ss
            _ss_dd = ""
            _ss_store_env = os.environ.get("ZMEM_STORE", "")
            if _ss_store_env:
                _ss_dd = os.path.expanduser(os.path.dirname(_ss_store_env))
            if not _ss_dd:
                _ss_dd = os.path.expanduser(os.environ.get("ZMEM_DATA", ""))
            if not _ss_dd:
                for _pdv in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
                    _pv = os.environ.get(_pdv, "")
                    if _pv:
                        _ss_dd = os.path.expanduser(_pv)
                        break
            if not _ss_dd:
                _ss_dd = os.path.join(os.path.expanduser("~"), ".zmem")
            # Issue #151 review (ssstart-507): bound by the ledger cap,
            # not a smaller fixed slice — delivered ids must never fall off
            # the exclusion list.
            for _did in _dl_ss.delivered_ids(_ss_dd, session_id)[:_dl_ss.cap()]:
                _ss_exclude_argv.extend(["--exclude", _did])
            _ss_ledger = _dl_ss
            _ss_dd_known = _ss_dd
        except Exception:
            pass
        # Issue #118 (D-2 scope 1): SessionStart(source=compact) — the
        # context was just summarized away. Compose a QUERY-AWARE recall
        # from the compact sidecar (the stashed compact_summary plus the
        # pre-compaction ledger snapshot) instead of the cold-start
        # recency pull. Empty stash, empty query, missing session id, or
        # a ledger-module import failure degrade to exactly the cold-start
        # lane below — fail-open, never blocking.
        _moment = "session_start"
        _compact_query = ""
        if source == "compact" and session_id and _ss_ledger is not None:
            # PR #190 review PRR-003: ZMEM_QUERY_CONTEXT=0 is the
            # documented GLOBAL query-context kill switch — the compact
            # lane composes its query from session-derived state exactly
            # like the pretool lane, so it takes the same gate and falls
            # back to the recency lane when silenced.
            if os.environ.get("ZMEM_QUERY_CONTEXT", "1").strip() == "0":
                _compact_query = ""
            else:
                # PR #190 review PRR-004: the compact moment DELIBERATELY
                # re-delivers the pre-compaction working set (the context
                # holding those rows was summarized away) — never exclude
                # its own query targets, even on the degraded path where
                # PreCompact snapshot succeeded but its ledger clear did
                # not run.
                _ss_exclude_argv = []
                try:
                    # PR #190 review PRR-002/011: READ the stash without
                    # consuming it — the discard happens only after the
                    # recall pull completed (see _ss_pull_ok below), so an
                    # exhausted retry loop leaves the summary + snapshot
                    # in place for the next SessionStart(compact).
                    _c_summary, _c_entries = _ss_ledger.read_compact_context(
                        _ss_dd_known, session_id)
                    _q_parts = []
                    if isinstance(_c_summary, str) and _c_summary.strip():
                        _q_parts.append(_c_summary[:800])
                    for _ce in (_c_entries or [])[-8:]:
                        if isinstance(_ce, dict) and isinstance(_ce.get("text"), str):
                            _t = _ce["text"].strip()
                            if _t:
                                _q_parts.append(_t)
                    _compact_query = " ".join(_q_parts).strip()
                except Exception:
                    _compact_query = ""
            if len(_compact_query) < 5:
                # Too short to be a meaningful query — cold-start lane.
                _compact_query = ""
            else:
                _moment = "session_start_compact"
        # The detached session-cadence worker (dispatched above, same store)
        # can hold the database while this read fires: on slow runners a
        # first recent attempt fails transiently (SQLITE_BUSY -> non-zero
        # exit), silently skipping the whole Tier-2 block below — inject AND
        # its bg-log decision line (seen on CI windows runners). Retry
        # briefly; still bounded and still fail-open on final failure.
        out = ""
        _ss_pull_ok = False
        for _recent_attempt in range(3):
            try:
                # Issue #118: the compact branch runs the QUERY-AWARE recall
                # lane (same argv shape the shared body uses for prompts);
                # the cold-start lane below is byte-identical to pre-#118.
                # PR #190 review PRR-006: the compact lane passes the same
                # confidence floor as cold-start (recent defaults 0.5) —
                # recall without the flag would silently drop to the 0.25
                # internal floor and surface lower-confidence rows.
                if _compact_query:
                    _ss_argv = [sys.executable, store_py, "recall",
                                "--query", _compact_query,
                                "--namespace", ns, "--limit", "5",
                                "--min-confidence", str(_recent_floor),
                                "--include-global", "--global-limit", "3",
                                "--no-bump", "--for-injection", "--json",
                                *_ss_exclude_argv]
                else:
                    _ss_argv = [sys.executable, store_py, "recent",
                                "--namespace", ns, "--limit", "3",
                                "--min-confidence", str(_recent_floor),
                                "--include-global", "--global-limit", "2",
                                "--no-bump", "--for-injection", "--json",
                                *_ss_exclude_argv]
                out = subprocess.check_output(
                    _ss_argv,
                    # 30s: a cold store.py spawn (full storelib import,
                    # first-touch AV scanning on CI windows runners) can
                    # exceed a tight timeout.
                    stderr=subprocess.DEVNULL, timeout=30,
                ).decode("utf-8", "replace")
                _ss_pull_ok = True
                break
            except subprocess.TimeoutExpired:
                # A 30s HANG is pathological (wedged process) — do not
                # triple the stall by retrying; degrade to empty. This also
                # bounds the whole block at ~30s so no caller subprocess
                # timeout can be blown by the retry loop.
                out = ""
                break
            except Exception:
                # Fast failure (the observed CI mode: SQLITE_BUSY exit while
                # the detached cadence worker held the store) — brief
                # backoff, then retry.
                out = ""
                if _recent_attempt < 2:
                    __import__("time").sleep(1.5)
        # PR #190 review PRR-002/011: the stash is discarded only after the
        # pull COMPLETED (success or a gate-emptied result — the summary was
        # used for the attempt). An exhausted retry loop leaves it in place
        # for the next SessionStart(compact) to retry the same query.
        if _compact_query and _ss_pull_ok and _ss_ledger is not None \
                and session_id and _ss_dd_known:
            try:
                _ss_ledger.discard_compact_context(_ss_dd_known, session_id)
            except Exception:
                pass
        rows = json.loads(out) if out.strip() else []
        # Issue #114 (P2-3): the recent pull runs the injection lane
        # (--for-injection), so the selective gate and the token budget were
        # applied INSIDE the store subprocess and rows is already the rendered
        # set. Read the envelope extras BEFORE the unwrap discards them:
        # reason (the #87 closed set, or injected), candidate_ids (the
        # PRE-gATE id list, the miss-rate join pre-image for all=), and the
        # content-sum token numbers for the decision line.
        _env_reason = None
        _env_all = None
        _tok_used = None
        _tok_budget = None
        _env_exc = None
        if isinstance(rows, dict):
            _er = rows.get("reason")
            if isinstance(_er, str) and _er:
                _env_reason = _er
            _ec = rows.get("candidate_ids")
            if isinstance(_ec, list):
                _env_all = [str(_x) for _x in _ec if isinstance(_x, str)]
            _tu = rows.get("tokens_used")
            _tb = rows.get("tokens_budget")
            if isinstance(_tu, int):
                _tok_used = _tu
            if isinstance(_tb, int):
                _tok_budget = _tb
            _ee_ss = rows.get("excluded")
            if isinstance(_ee_ss, int) and not isinstance(_ee_ss, bool):
                _env_exc = _ee_ss
        # v13 (issue #65, 10.8): unwrap the read envelope via the SHARED
        # shim (C38) — same helper the hooks body / Hermes / MCP use; the
        # inline dict/list fallback covers a failed import (fail-open).
        try:
            import inject as _inj_mod
            rows = _inj_mod.envelope_results(rows)
        except Exception:
            if isinstance(rows, dict):
                rows = rows.get("results", [])
            if not isinstance(rows, list):
                rows = []
        # PR #190 review (PRR-001 residual): a zero-row compact recall —
        # the #113 relevance gate dropped everything — must not leave the
        # post-compaction moment silent, which would be strictly worse
        # than the pre-#118 recency pull. Fall back to the recency lane
        # once; the moment label reverts so the fence header and the
        # ledger record match the rows actually rendered. The envelope
        # fields above still describe the compact attempt (why the
        # fallback fired); ids= come from the fallback pull.
        if _compact_query and not rows:
            try:
                out = subprocess.check_output(
                    [sys.executable, store_py, "recent",
                     "--namespace", ns, "--limit", "3",
                     "--min-confidence", str(_recent_floor),
                     "--include-global", "--global-limit", "2",
                     "--no-bump", "--for-injection", "--json"],
                    stderr=subprocess.DEVNULL, timeout=30,
                ).decode("utf-8", "replace")
                rows = json.loads(out) if out.strip() else []
                # The fallback output is a raw read envelope — unwrap it
                # exactly like the main pull above, or the downstream
                # decision-line/fence code iterates the dict's KEYS.
                if isinstance(rows, dict):
                    rows = rows.get("results", [])
                if not isinstance(rows, list):
                    rows = []
                if rows:
                    _moment = "session_start"
            except Exception:
                pass  # fail-open: stay silent rather than crash
        # Issue #114: gate on the PULL having produced an envelope, not on
        # rows surviving — the store-side budget can legitimately wipe the
        # set (reason=budget-drop) and the decision line must still land;
        # the fence render below still happens only when rows exist.
        _pull_ran = bool(out and out.strip())
        if _pull_ran:
            # Issue #58, 3.5: wrap Tier 2 in the same non-executable
            # fence + provenance render that zmem-recall uses. The gate
            # and budget helpers no longer run here: they moved store-side
            # with --for-injection (issue #114), which is why the rendered
            # set arrives already filtered and counted.
            try:
                _sp = sys.path[:]
                try:
                    sys.path.insert(0, os.path.dirname(store_py))
                    sys.path.insert(0, os.path.join(os.path.dirname(store_py), "storelib"))
                    from storelib import _format_fenced_recall
                finally:
                    # Review PRR-005 hygiene: restore the path like every
                    # sibling helper, so later imports here cannot free-ride
                    # on this leak.
                    sys.path[:] = _sp
                # PRR-014 fix: record the injected|silent decision in the
                # SAME bg log the other hook surfaces use (recall /
                # precompact / subagent-recall via the shared body).
                try:
                    # PRR-101 review: mirror the shared body _data_dir()
                    # chain and normalization exactly (ZMEM_STORE >
                    # ZMEM_DATA > CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA >
                    # ~/.zmem; expanduser on EVERY branch — the outer bash
                    # re-exports ZMEM_DATA verbatim, so a tilde-valued
                    # ZMEM_DATA/ZMEM_STORE arrives here unexpanded and this
                    # block must expand it too, or the isdir gate silently
                    # drops the decision line).
                    _log_dir = ""
                    _ss_store = os.environ.get("ZMEM_STORE", "")
                    if _ss_store:
                        _log_dir = os.path.expanduser(os.path.dirname(_ss_store))
                    if not _log_dir:
                        _log_dir = os.path.expanduser(
                            os.environ.get("ZMEM_DATA", ""))
                    if not _log_dir:
                        for _pd_var in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
                            _pd_val = os.environ.get(_pd_var, "")
                            if _pd_val:
                                _log_dir = os.path.expanduser(_pd_val)
                                break
                    if not _log_dir:
                        _log_dir = os.path.join(os.path.expanduser("~"), ".zmem")
                    _log_path = os.path.join(_log_dir, "zmem-decisions.log")
                    if os.path.isdir(_log_dir):
                        # Issue #129: rotate, never truncate — the decisions
                        # log is the audit evidence substrate.
                        try:
                            _sp = sys.path[:]
                            try:
                                # Review PRR-005: insert the package parent
                                # explicitly — this site previously relied on
                                # the fence import above leaking it.
                                sys.path.insert(0, os.path.dirname(store_py))
                                from storelib.log_rotate import rotate_on_append as _rota
                                _rota(_log_path)
                            finally:
                                sys.path[:] = _sp
                        except Exception:
                            pass
                        with open(_log_path, "a", encoding="utf-8") as _lf:
                            _tok = ""
                            if _tok_used is not None:
                                _tok = " tokens=%s/%s" % (_tok_used, _tok_budget if _tok_budget is not None else "-")
                            # Issue #94: always carry the sanitized session
                            # id at line end (same rule as the shared body
                            # _log_inject_decision and the ring paths) so a
                            # mined failure can be bound to the injection
                            # decisions of this session. "unknown" when the
                            # host supplied no session id. Issue #114: the
                            # line now carries reason= from the envelope and
                            # all= is the PRE-gATE candidate set (was the
                            # post-gate rows — aligned to the shared body
                            # convention the miss-rate join matches against).
                            _safe_sid = re.sub(
                                r"[^A-Za-z0-9._-]", "_",
                                (session_id or ""))[:128] or "unknown"
                            _exc_ss = ""
                            if _env_exc:
                                _exc_ss = " exc=%d" % _env_exc
                            _lf.write(
                                "[%d] zmem-hook status=%s reason=%s ids=%s all=%s%s%s sid=%s moment=%s\n" % (
                                    int(__import__("time").time()),
                                    "injected" if rows else "silent",
                                    (_env_reason or ("injected" if rows else "empty-pool")),
                                    [r.get("id") for r in rows],
                                    (_env_all if _env_all is not None else [r.get("id") for r in rows]),
                                    _tok,
                                    _exc_ss,
                                    _safe_sid,
                                    _moment,
                                )
                            )
                except Exception:
                    pass  # fail-open: audit log never blocks session start
                if rows:
                    if _moment == "session_start_compact":
                        _ss_header = (
                            f"Post-compaction memories (Tier 2 — namespace {ns}). "
                            f"Query-aware recall rebuilt from the compaction summary "
                            f"and the pre-compaction deliveries of this session. "
                            f"Consider if they apply; ignore if not."
                        )
                    else:
                        _ss_header = (
                            f"Recent memories (Tier 2 — namespace {ns}). "
                            f"High-confidence admin pull. Consider if relevant; ignore if not."
                        )
                    block = _format_fenced_recall(
                        rows,
                        header=_ss_header,
                    )
                    parts.append(block)
                    # Issue #117 D-1: record the delivered rows so the
                    # next moment of this session (user_prompt, pretool)
                    # suppresses them. Fail-open like everything here.
                    # Issue #151 review (ssstart-698): record only rows whose
                    # bullet is in the rendered block, and skip recording
                    # entirely when the projected payload already exceeds the
                    # context budget (the bash-side final truncation would cut
                    # them before the model sees them; documented residual for
                    # the tail-cut case).
                    if _ss_ledger is not None and session_id:
                        try:
                            _ss_budget = int(os.environ.get("ZMEM_CTX_BUDGET", "25000") or 25000)
                            _ss_proj = sum(len(x) for x in parts) + len(block) + 512
                            _ss_rows = rows
                            if _ss_budget > 0 and _ss_proj > _ss_budget:
                                _ss_rows = _dl_ss.rows_present_in(rows, block[:max(0, _ss_budget - (sum(len(x) for x in parts)))])
                            if _ss_rows:
                                _ss_ledger.record(_ss_dd_known, session_id, _ss_rows, _moment)
                        except Exception:
                            pass
            except Exception:
                # PRR-006 fix: the storelib import failed, so the fence
                # renderer is unavailable. OMIT Tier 2 entirely rather than
                # emit untrusted retrieved text unfenced/un-gated — a missing
                # Tier 2 block is a degraded session, not a safety hole.
                pass
    except Exception:
        pass  # fail-open: recall errors never block session start

# Inject the store.py path so the agent knows how to invoke the memory skill.
if store_py and os.path.isfile(store_py):
    parts.append("# Memory skill: invoke `%s <subcommand>` to recall/add/search memories." % store_py)
    # Check for promotion candidates (non-blocking, one-line suggestion).
    try:
        promote_out = subprocess.check_output(
            [sys.executable, store_py, "promote", "--dry-run"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8", "replace")
        # Extract the count from the first line.
        for line in promote_out.strip().split("\n"):
            if "promotion candidate" in line.lower():
                parts.append(line.strip())
                break
    except Exception:
        pass  # fail-open: promotion check errors never block session start

# Pending live-capture correction candidates (issue #47): surface a COUNT only
# (budget — never the contents) so the agent knows the queue has candidates for
# the closeout skill to review. The queue is a fail-open sidecar; a read error
# must never block session start.
if store_py and os.path.isfile(store_py):
    try:
        sys.path.insert(0, os.path.dirname(store_py))
        import correction_queue as _cq
        # Surface a COUNT of PENDING (non-stale) candidates. Stale items are
        # already past decay and are pruned by closeout `--drop-stale`, so they
        # are not "pending review" - do not nudge the agent about them.
        _pending = sum(1 for _it in _cq.load_queue(ns) if not _it.get("stale"))
        if _pending:
            # PREPEND (not append): the payload is truncated with ctx[:budget],
            # which keeps the FRONT and drops the tail, so an appended note would
            # vanish whenever Tier 0 + recall fill the budget. Leading placement
            # guarantees the agent sees the closeout-work nudge.
            parts.insert(
                0,
                "zmem: %d captured correction candidate(s) pending review — "
                "run the closeout skill to process." % _pending
            )
    except Exception:
        pass  # fail-open: queue read errors never block session start

# Native-memory nudge (CC only, P6): one-time, best-effort, fail-open. Never
# raises - a missing/malformed settings.json just means the nudge stays quiet.
if host == "claude" and nudge_marker:
    try:
        already_shown = os.path.isfile(nudge_marker)
        native_disabled = bool(os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"))
        if not native_disabled and settings_dir:
            for fname in ("settings.json", "settings.local.json"):
                fpath = os.path.join(settings_dir, fname)
                try:
                    with open(fpath, encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict) and data.get("autoMemoryEnabled") is False:
                        native_disabled = True
                        break
                except (OSError, ValueError):
                    continue  # absent or malformed — treat as "not set here"
        if not already_shown and not native_disabled:
            # Attempt the exclusive marker create FIRST, and only append the
            # notice text in the process that actually won the create. This
            # is the order that matters: appending-then-racing-the-create (the
            # old order) let two concurrent sessions both queue the notice
            # before either one touched the marker, so both showed it and only
            # one "won" a create that, by then, was pointless. Deciding who
            # gets to show the notice before building any output closes that
            # race.
            show_notice = False
            try:
                os.makedirs(os.path.dirname(nudge_marker), exist_ok=True)
                # Atomic exclusive create (matches host.py _try_create_lock
                # pattern): two sessions starting concurrently could both see
                # the marker absent under a plain overwrite-open and both
                # show the nudge. O_EXCL makes only one winner.
                fd = os.open(nudge_marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, b"shown\n")
                finally:
                    os.close(fd)
                show_notice = True  # we won the race - we are the one to show it
            except FileExistsError:
                show_notice = False  # another process already showed it — no-op
            except OSError:
                # Marker could not be created for an unrelated reason (e.g. the
                # data dir is unwritable/read-only, or missing and could not be
                # made). Deliberate choice: show the notice anyway rather than
                # silently wedging the user out of it forever. Worst case here
                # is the notice repeats on a later session (annoying, visible,
                # self-correcting) instead of the alternative — a
                # never-writable marker path permanently suppressing a notice
                # about a real double-run risk. Fail-open toward showing.
                show_notice = True
            if show_notice:
                parts.append(
                    "# ZMem notice (one-time): Claude Code native memory still looks "
                    "enabled. ZMem is replacing it as your sole memory system - add "
                    "\"autoMemoryEnabled\": false to ~/.claude/settings.json so the two "
                    "systems do not double-run (a plugin cannot set this for you)."
                )
    except Exception:
        pass  # fail-open: nudge logic never blocks session start

ctx = "\n\n".join(parts) if parts else ""
# Soft budget cap (belt-and-suspenders; the launcher enforces the hard encoded
# budget). Trim raw content here so the payload is roughly bounded before the
# launcher re-measures the JSON-encoded envelope.
# Final-critic round-2 fix: a naive slice can cut mid-Tier-2-block and drop
# the <<<END_ZMEM_UNTRUSTED_FENCE>>> closer, leaving a dangling fence opener
# in the injected context. If a truncation would split a fence, cut at the
# fence closer instead (the block is simply shorter, never unclosed).
if budget > 0 and len(ctx) > budget:
    _closer = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
    _cut = ctx[:budget]
    _last_open = _cut.rfind("<<<ZMEM_UNTRUSTED_FENCE>>>")
    _last_close_in_cut = _cut.rfind(_closer)
    if _last_open > _last_close_in_cut:
        # The cut lands inside a fence: KEEP the partial body (up to the
        # cut) and append the closer so the fence is complete —
        # never an unclosed or orphaned marker (final-critic round-3
        # fix: the previous branch dropped the opener and emitted a
        # dangling closer).
        ctx = _cut.rstrip() + "\n" + _closer + "\n[recall truncated]"
    else:
        ctx = _cut + "\n[recall truncated]"
# Issue #107: systemMessage rides ONLY when the served tree drifted — an
# operator-facing notice that must never enter additionalContext.
_payload = {}
if ctx:
    _payload["additionalContext"] = ctx
if _drift_msg:
    _payload["systemMessage"] = _drift_msg
print(json.dumps(_payload) if _payload else "{}")
