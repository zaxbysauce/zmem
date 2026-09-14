#!/usr/bin/env python
"""SessionStart Tier-0/Tier-2 payload builder (issue #58; PR #190; issue #121).

The entire payload block used to be a `python -c` inline string inside
zmem-session-start.sh. PR #190's review round pushed that string past the
Windows CreateProcess ~32K command-line limit and the hook silently
degraded to `{}` (the `|| echo '{}'` fallback swallowed the spawn failure)
— so the block now lives here as a real file with the IDENTICAL argv
contract (sys.argv[1..14], IndexError-tolerant ladder). Do not inline it
again: any `python -c` payload over ~32K chars silently dies on Windows.

Issue #121 (timeout budget + Tier 0 fast path): this module now owns the
``<<<ZMEM_JSON>>>…<<<END>>>`` sentinel emission itself (moved from the bash
wrapper) and emits TWO complete sentinels on the normal path —

  1. a Tier 0-only envelope (core.md / AGENTS.md / local nudges — no store
     subprocess), FLUSHED before the first store subprocess so the launcher
     can hand Tier 0 to the host even if the store stalls, and
  2. the final envelope (Tier 0 + bounded Tier 2 + promote note).

The launcher (zmem-launch.js) extracts the LAST COMPLETE sentinel, so a
normal close delivers envelope 2 and a watchdog kill delivers envelope 1.
The store pull is ONE `store.py recent` attempt capped by
ZMEM_STORE_RECALL_TIMEOUT_S (finite positive float seconds, default 8.0;
non-finite, non-positive, and values above 8.0 use 8.0 — one warning).

Args (all optional beyond 1, IndexError-tolerant):
  1 core.md path          2 AGENTS.md path       3 store.py path
  4 data dir (native)     5 project dir          6 data dir (bash-resolved)
  7 namespace             8 ctx budget           9 host
 10 settings dir         11 nudge marker        12 session id
 13 drift JSON           14 session source (issue #118)
 15 validated host lane (issue #153, optional)

Decision lines carry the optional issue #153 suffix ``lane``, ``ver``, and
``t_ms`` after ``moment`` when the host lane and release manifest validate;
otherwise the complete legacy line is retained.
"""

import json
import math
import os
import re
import subprocess
import sys
import time

_SENTINEL_START = "<<<ZMEM_JSON>>>"
_SENTINEL_END = "<<<END>>>"

# Marker neutralization (moved from zmem-session-start.sh with the sentinel
# emission, issue #121): a memory whose OWN content contains a sentinel or
# fence marker would move the launcher's extraction boundary into the middle
# of the JSON. Both replacements are safe inside a serialized JSON string.
_NEUTRALIZE = (
    ("<<<ZMEM_JSON>>>", "<<<ZMEM_JSON_NEUTRALIZED>>>"),
    ("<<<END>>>", "<<<END_NEUTRALIZED>>>"),
    ("<<<ZMEM_UNTRUSTED_FENCE>>>", "<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>"),
    ("<<<END_ZMEM_UNTRUSTED_FENCE>>>", "<<<END_ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>"),
)

_ATTR_LANES = ("claude", "codex", "zcode", "hermes-provider", "hermes-compat")
_ATTR_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _validated_attribution_lane(host):
    """Return a closed-set host lane, or ``None`` for legacy output."""
    if not isinstance(host, str):
        return None
    value = host.strip()
    return value if value in _ATTR_LANES else None


def _release_version():
    """Read the served tree semver; malformed manifests retain legacy lines."""
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        candidates = [os.path.join(root, "release-manifest.json")]
        home = os.environ.get("ZMEM_HOME", "").strip()
        if home:
            candidates.append(os.path.join(os.path.expanduser(home),
                                           "release-manifest.json"))
        for path in candidates:
            try:
                with open(path, encoding="utf-8") as fh:
                    value = json.load(fh).get("version")
                if isinstance(value, str) and _ATTR_VERSION_RE.fullmatch(value):
                    return value
            except (OSError, ValueError, TypeError):
                continue
    except Exception:
        pass
    return None


def _rounded_elapsed_ms(started):
    """Return a nonnegative rounded duration for one store attempt."""
    try:
        return max(0, int(round((time.perf_counter() - started) * 1000)))
    except Exception:
        return 0


def _format_attribution(lane, version, t_ms):
    """Serialize validated issue #153 attribution fields, or nothing."""
    if ((lane is None or lane in _ATTR_LANES)
            and isinstance(version, str)
            and _ATTR_VERSION_RE.fullmatch(version)
            and isinstance(t_ms, int) and not isinstance(t_ms, bool)
            and t_ms >= 0):
        lane_field = " lane=%s" % lane if lane is not None else ""
        return "%s ver=%s t_ms=%d" % (lane_field, version, t_ms)
    return ""


def _emit(payload):
    """Neutralize + sentinel-wrap + print ONE envelope, flushed immediately.

    Flushing matters: envelope 1 must reach the launcher's pipe BEFORE the
    first store subprocess starts, or the Tier 0 fast path does not exist.
    """
    text = json.dumps(payload) if payload else "{}"
    for marker, replacement in _NEUTRALIZE:
        text = text.replace(marker, replacement)
    sys.stdout.write(_SENTINEL_START + text + _SENTINEL_END + "\n")
    sys.stdout.flush()


def _budget_default_s(key, fallback_s):
    """F-010: hooks/timeout-budget.json is the canonical table; the
    runtime default comes from it (fail-open to the hardcoded fallback
    when the file is absent/malformed). Keep in sync with the sibling
    reader in the other hook file — the parity test pins both."""
    try:
        import json
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            os.pardir, "timeout-budget.json")
        with open(path, encoding="utf-8") as f:
            value = json.load(f).get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value / 1000.0
    except Exception:
        pass
    return fallback_s

_store_timeout_warned = False


def _store_timeout_s():
    """ZMEM_STORE_RECALL_TIMEOUT_S: finite positive float, default 8.0.

    Non-finite, non-positive, and values above 8.0 all use 8.0; each
    deviation writes exactly ONE warning on stderr per process (issue #121
    contract; values below 8.0 are honored for operator headroom control).
    """
    global _store_timeout_warned
    raw = os.environ.get("ZMEM_STORE_RECALL_TIMEOUT_S", "")
    value = _budget_default_s("store_recall_ms", 8.0)  # fallback is SECONDS
    warned = False
    if raw.strip():
        try:
            value = float(raw)
        except ValueError:
            value = 8.0
            warned = True
        if not math.isfinite(value) or value <= 0:
            value = 8.0
            warned = True
        elif value > 8.0:
            value = 8.0
            warned = True
    if warned and not _store_timeout_warned:
        _store_timeout_warned = True
        try:
            sys.stderr.write(
                "zmem: invalid ZMEM_STORE_RECALL_TIMEOUT_S=%r; using 8.0\n" % (raw,))
        except Exception:
            pass
    return value


def _safe_sid(session_id):
    return re.sub(r"[^A-Za-z0-9._-]", "_", (session_id or ""))[:128] or "unknown"


def _log_dir():
    """The payload-side data dir (mirrors the pre-#121 inline block): the
    same chain the bash writer and the shared hook body use, expanded."""
    log_dir = ""
    store_env = os.environ.get("ZMEM_STORE", "")
    if store_env:
        log_dir = os.path.expanduser(os.path.dirname(store_env))
    if not log_dir:
        log_dir = os.path.expanduser(os.environ.get("ZMEM_DATA", ""))
    if not log_dir:
        for pd_var in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
            pd_val = os.environ.get(pd_var, "")
            if pd_val:
                log_dir = os.path.expanduser(pd_val)
                break
    if not log_dir:
        log_dir = os.path.join(os.path.expanduser("~"), ".zmem")
    return log_dir


def _rotate_log(store_py, log_path):
    """Issue #129: rotate, never truncate — the decisions log is the audit
    evidence substrate. Fail-open on any error."""
    try:
        if store_py and os.path.isfile(store_py):
            saved = sys.path[:]
            try:
                sys.path.insert(0, os.path.dirname(store_py))
                from storelib.log_rotate import rotate_on_append as _rota
                _rota(log_path)
            finally:
                sys.path[:] = saved
    except Exception:
        pass


def build_tier0_context(core_path, agents_path, host):
    """Tier 0 only — pure local file reads, no store subprocess (issue #121).

    `host` is part of the frozen signature; the AGENTS.md gating (ZMEM_TIER0)
    is already applied by the caller, which passes an empty agents path on
    the native host, so it is intentionally unreferenced here.
    """
    del host
    parts = []
    if core_path and os.path.isfile(core_path):
        try:
            with open(core_path, encoding="utf-8", errors="replace") as f:
                parts.append("# Loaded from memory (Tier 0 — core.md, user-level):\n\n" + f.read())
        except OSError:
            pass
    if agents_path and os.path.isfile(agents_path):
        try:
            with open(agents_path, encoding="utf-8", errors="replace") as f:
                parts.append("# Loaded from memory (Tier 0 — AGENTS.md, project-level):\n\n" + f.read())
        except OSError:
            pass
    return "\n\n".join(parts)


def _ledger_module(store_py):
    """Import delivery_ledger (issue #117) from the storelib parent dir.

    Fail-open: any import error returns None and the caller degrades to the
    pre-#117 behavior (no exclusion list).
    """
    try:
        saved = sys.path[:]
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(store_py), "storelib"))
            import delivery_ledger as ledger
        finally:
            sys.path[:] = saved
        return ledger
    except Exception:
        return None


def _data_dir_for_ledger():
    # Copilot dedupe: identical resolution chain to _log_dir — one
    # implementation, two names (call-site readability).
    return _log_dir()


def _write_decision_line(store_py, text):
    """Append one line to <data dir>/zmem-decisions.log (rotation first).
    Fail-open: the audit log never blocks session start."""
    try:
        log_dir = _log_dir()
        log_path = os.path.join(log_dir, "zmem-decisions.log")
        if os.path.isdir(log_dir):
            _rotate_log(store_py, log_path)
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(text)
    except Exception:
        pass


def _import_renderer(store_py):
    """Import the SHARED fenced-recall renderer from the storelib parent dir.
    Returns None when unavailable (a stub store dir has no storelib package)
    — the PRR-006 rule then omits Tier 2 entirely, decision line included,
    rather than emitting untrusted text unfenced or logging an injection
    that never rendered."""
    try:
        saved = sys.path[:]
        try:
            sys.path.insert(0, os.path.dirname(store_py))
            sys.path.insert(0, os.path.join(os.path.dirname(store_py), "storelib"))
            from storelib import _format_fenced_recall
        finally:
            sys.path[:] = saved
        return _format_fenced_recall
    except Exception:
        return None


def _render_fenced(store_py, rows, header):
    render = _import_renderer(store_py)
    if render is None:
        return ""
    try:
        return render(rows, header=header)
    except Exception:
        return ""


def _unwrap_rows(raw_out):
    rows = json.loads(raw_out) if raw_out.strip() else []
    extras = {"reason": None, "all": None, "tokens_used": None,
              "tokens_budget": None, "excluded": None,
              "margin": None, "margin_pruned_ids": None}
    if isinstance(rows, dict):
        er = rows.get("reason")
        if isinstance(er, str) and er:
            extras["reason"] = er
        ec = rows.get("candidate_ids")
        if isinstance(ec, list):
            extras["all"] = [str(x) for x in ec if isinstance(x, str)]
        tu = rows.get("tokens_used")
        tb = rows.get("tokens_budget")
        if isinstance(tu, int):
            extras["tokens_used"] = tu
        if isinstance(tb, int):
            extras["tokens_budget"] = tb
        ee = rows.get("excluded")
        if isinstance(ee, int) and not isinstance(ee, bool):
            extras["excluded"] = ee
        # Issue #182: score-margin diagnostics are optional envelope
        # fields. Validate each independently so malformed telemetry
        # never hides a valid sibling field or changes the legacy
        # decision line.
        if "margin" in rows:
            raw_margin = rows.get("margin")
            margin_value = None
            if (isinstance(raw_margin, (int, float))
                    and not isinstance(raw_margin, bool)):
                margin_value = raw_margin
            elif isinstance(raw_margin, str):
                try:
                    margin_value = float(raw_margin)
                except (TypeError, ValueError, OverflowError):
                    pass
            try:
                if (margin_value is not None
                        and math.isfinite(float(margin_value))):
                    extras["margin"] = float(margin_value)
            except (TypeError, ValueError, OverflowError):
                pass
        if "margin_pruned_ids" in rows:
            raw_pruned = rows.get("margin_pruned_ids")
            if (isinstance(raw_pruned, list) and raw_pruned
                    and all(isinstance(mid, str) for mid in raw_pruned)):
                extras["margin_pruned_ids"] = raw_pruned
    try:
        import inject as _inj_mod
        rows = _inj_mod.envelope_results(rows)
    except Exception:
        if isinstance(rows, dict):
            rows = rows.get("results", [])
        if not isinstance(rows, list):
            rows = []
    return rows, extras


def _decision_line(rows, extras, moment, session_id, pull_ran,
                  lane=None, version=None, t_ms=None):
    status = "injected" if rows else "silent"
    reason = extras["reason"] or ("injected" if rows else "empty-pool")
    tok = ""
    if extras["tokens_used"] is not None:
        tok = " tokens=%s/%s" % (
            extras["tokens_used"],
            extras["tokens_budget"] if extras["tokens_budget"] is not None else "-")
    exc = ""
    if extras["excluded"]:
        exc = " exc=%d" % extras["excluded"]
    ids = [r.get("id") for r in rows] if isinstance(rows, list) else []
    all_ids = extras["all"] if extras["all"] is not None else ids
    margin_ss = ""
    if extras["margin"] is not None:
        margin_ss = " margin=%.6f" % extras["margin"]
    margin_pruned_ss = ""
    if extras["margin_pruned_ids"]:
        # Issue #182: the envelope carries untrusted memory IDs. Match
        # the shared writer's tools=/paths= charset rule and component
        # cap before list repr enters the decision log.
        safe_pruned = [
            re.sub(r"[^A-Za-z0-9._-]", "_", mid)[:64]
            for mid in extras["margin_pruned_ids"]
        ]
        margin_pruned_ss = " margin_pruned=%s" % safe_pruned
    # Issue #153: attribution is emitted atomically only when the host lane,
    # manifest semver, and nonnegative attempt duration are all valid.  Keep
    # the exact insertion point after moment and before additive tails.
    attr = ""
    attr = _format_attribution(lane, version, t_ms)
    return ("[%d] zmem-hook status=%s reason=%s ids=%s all=%s%s%s sid=%s moment=%s%s%s%s\n" % (
        int(__import__("time").time()), status, reason, ids, all_ids, tok, exc,
        _safe_sid(session_id), moment, attr, margin_ss, margin_pruned_ss)) if pull_ran else None


def _record_ledger(ledger, data_dir, session_id, rows, block, parts, moment):
    if ledger is None or not session_id:
        return
    try:
        budget = int(os.environ.get("ZMEM_CTX_BUDGET", "25000") or 25000)
        projected = sum(len(x) for x in parts) + len(block) + 512
        scoped_rows = rows
        if budget > 0 and projected > budget:
            scoped_rows = ledger.rows_present_in(
                rows, block[:max(0, budget - (sum(len(x) for x in parts)))])
        if scoped_rows:
            ledger.record(data_dir, session_id, scoped_rows, moment)
    except Exception:
        pass


def build_tier2_context(store_py, namespace, session_id, budget,
                          context_parts=None, attribution_lane=None,
                          attribution_version=None):
    """ONE bounded `store.py recent` pull + fence + decision line + ledger.

    Issue #121: the pre-fix 30 s triple-retry loop is replaced by exactly one
    attempt capped at ZMEM_STORE_RECALL_TIMEOUT_S. On timeout, Tier 2 is
    empty and a `reason=omitted store_timeout=1` decision line lands in the
    decisions log; on a fast failure the block degrades silently (the
    pre-#121 fail-open behavior). Returns the fenced Tier 2 block (""
    when nothing rendered). The issue #117 ledger exclusion argv is computed
    internally from session_id.
    """
    if not (store_py and os.path.isfile(store_py)):
        return ""
    try:
        floor_raw = os.environ.get("ZMEM_INJECT_FLOOR_RECENT", "")
        try:
            recent_floor = float(floor_raw) if floor_raw else 0.5
        except ValueError:
            recent_floor = 0.5

        exclude_argv = []
        ledger = _ledger_module(store_py)
        data_dir = ""
        if ledger is not None:
            data_dir = _data_dir_for_ledger()
            for did in ledger.delivered_ids(data_dir, session_id)[:ledger.cap()]:
                exclude_argv.extend(["--exclude", did])

        argv = [sys.executable, store_py, "recent",
                "--namespace", namespace, "--limit", "3",
                "--min-confidence", str(recent_floor),
                "--include-global", "--global-limit", "2",
                "--no-bump", "--for-injection", "--json",
                *exclude_argv]
        attempt_started = time.perf_counter()
        try:
            out = subprocess.check_output(
                argv, stderr=subprocess.DEVNULL,
                timeout=_store_timeout_s(),
            ).decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            _write_decision_line(
                store_py,
                # store_timeout=1 rides at line END (reader-parity: the
                # fixed-order _BG_LINE_RE only tolerates additive fields
                # after moment=).
                "[%d] zmem-hook status=silent reason=omitted "
                "ids=[] all=[] sid=%s moment=session_start%s store_timeout=1\n" % (
                    int(__import__("time").time()), _safe_sid(session_id),
                    _format_attribution(attribution_lane, attribution_version,
                                        _rounded_elapsed_ms(attempt_started))))
            return ""
        attempt_t_ms = _rounded_elapsed_ms(attempt_started)
        # PRR-006 gate (pre-#121 coupling preserved): no renderer means no
        # Tier 2 AND no decision line — never log an injection that could
        # not render, never emit unfenced retrieved text.
        if _import_renderer(store_py) is None:
            return ""
        rows, extras = _unwrap_rows(out)
        pull_ran = bool(out and out.strip())
        line = _decision_line(rows, extras, "session_start", session_id,
                              pull_ran, attribution_lane, attribution_version,
                              attempt_t_ms)
        if line:
            _write_decision_line(store_py, line)
        if rows:
            block = _render_fenced(
                store_py, rows,
                header=("Recent memories (Tier 2 — namespace %s). "
                        "High-confidence admin pull. Consider if relevant; "
                        "ignore if not." % namespace))
            if block:
                _record_ledger(ledger, data_dir, session_id, rows, block,
                                context_parts or [], "session_start")
                return block
        return ""
    except Exception:
        return ""  # fail-open: recall errors never block session start


def _compact_lane(store_py, namespace, session_id, ledger, data_dir,
                  recent_floor, context_parts=None, attribution_lane=None,
                  attribution_version=None):
    """Issue #118 SessionStart(source=compact): query-aware recall rebuilt
    from the compact sidecar. Retry structure and stash semantics are #118's
    owned surface, kept intact here; only the subprocess timeout moved to
    the #121 store-timeout env (bounded overall by the launcher watchdog).

    Returns (tier2_block, moment_label). Degrades to ("", "session_start")
    on any failure — the cold-start lane then runs instead.
    """
    if ledger is None:
        return "", ""
    if os.environ.get("ZMEM_QUERY_CONTEXT", "1").strip() == "0":
        return "", ""
    try:
        # READ the stash without consuming it — discard happens only after a
        # completed pull, so an exhausted path leaves it for the next
        # SessionStart(compact) (PR #190 review PRR-002/011).
        summary, entries = ledger.read_compact_context(data_dir, session_id)
        q_parts = []
        if isinstance(summary, str) and summary.strip():
            q_parts.append(summary[:800])
        for ce in (entries or [])[-8:]:
            if isinstance(ce, dict) and isinstance(ce.get("text"), str):
                t = ce["text"].strip()
                if t:
                    q_parts.append(t)
        query = " ".join(q_parts).strip()
    except Exception:
        query = ""
    if len(query) < 5:
        return "", ""
    timeout_s = _store_timeout_s()
    out = ""
    pull_ok = False
    attempt_t_ms = 0
    for attempt in range(3):
        attempt_started = time.perf_counter()
        try:
            argv = [sys.executable, store_py, "recall",
                    "--query", query,
                    "--namespace", namespace, "--limit", "5",
                    "--min-confidence", str(recent_floor),
                    "--include-global", "--global-limit", "3",
                    "--no-bump", "--for-injection", "--json"]
            out = subprocess.check_output(
                argv, stderr=subprocess.DEVNULL, timeout=timeout_s,
            ).decode("utf-8", "replace")
            pull_ok = True
            attempt_t_ms = _rounded_elapsed_ms(attempt_started)
            break
        except subprocess.TimeoutExpired:
            out = ""
            attempt_t_ms = _rounded_elapsed_ms(attempt_started)
            # F-008: a compact-lane timeout must land a decision line too,
            # symmetric with the cold-start lane (store_timeout=1 at line
            # END for reader parity).
            _write_decision_line(
                store_py,
                "[%d] zmem-hook status=silent reason=omitted "
                 "ids=[] all=[] sid=%s moment=session_start_compact%s store_timeout=1\n" % (
                    int(__import__("time").time()), _safe_sid(session_id),
                    _format_attribution(attribution_lane, attribution_version,
                                        attempt_t_ms)))
            break  # a hang is pathological — do not triple the stall
        except Exception:
            out = ""
            if attempt < 2:
                __import__("time").sleep(1.5)
    if not pull_ok:
        return "", ""
    # PRR-006 gate (same coupling as the cold-start lane).
    if _import_renderer(store_py) is None:
        return "", ""
    try:
        ledger.discard_compact_context(data_dir, session_id)
    except Exception:
        pass
    rows, extras = _unwrap_rows(out)
    moment = "session_start_compact"
    line = _decision_line(rows, extras, moment, session_id,
                          bool(out and out.strip()), attribution_lane,
                          attribution_version, attempt_t_ms)
    if line:
        _write_decision_line(store_py, line)
    if rows:
        block = _render_fenced(
            store_py, rows,
            header=("Post-compaction memories (Tier 2 — namespace %s). "
                    "Query-aware recall rebuilt from the compaction summary "
                    "and the pre-compaction deliveries of this session. "
                    "Consider if they apply; ignore if not." % namespace))
        if block:
            _record_ledger(ledger, data_dir, session_id, rows, block,
                            context_parts or [], moment)
            return block, moment
    # Zero-row compact recall must not leave the moment silent (the #113
    # relevance gate may legitimately drop everything) — fall back to the
    # recency lane once, relabeling the moment to match the rendered rows.
    try:
        fallback_started = time.perf_counter()
        out = subprocess.check_output(
            [sys.executable, store_py, "recent",
             "--namespace", namespace, "--limit", "3",
             "--min-confidence", str(recent_floor),
             "--include-global", "--global-limit", "2",
             "--no-bump", "--for-injection", "--json"],
            stderr=subprocess.DEVNULL, timeout=timeout_s,
        ).decode("utf-8", "replace")
        attempt_t_ms = _rounded_elapsed_ms(fallback_started)
        rows, extras = _unwrap_rows(out)
        if rows:
            line = _decision_line(rows, extras, "session_start", session_id,
                                  bool(out and out.strip()), attribution_lane,
                                  attribution_version, attempt_t_ms)
            if line:
                _write_decision_line(store_py, line)
            block = _render_fenced(
                store_py, rows,
                header=("Recent memories (Tier 2 — namespace %s). "
                        "High-confidence admin pull. Consider if relevant; "
                        "ignore if not." % namespace))
            if block:
                _record_ledger(ledger, data_dir, session_id, rows, block,
                                context_parts or [], "session_start")
                return block, "session_start"
    except Exception:
        pass
    return "", ""


def _soft_trim(ctx, budget):
    """Soft budget cap (belt-and-suspenders; the launcher enforces the hard
    encoded budget). A naive slice can cut mid-fence — trim at the fence
    closer instead so the block is shorter, never unclosed."""
    if budget > 0 and len(ctx) > budget:
        closer = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
        cut = ctx[:budget]
        last_open = cut.rfind("<<<ZMEM_UNTRUSTED_FENCE>>>")
        last_close = cut.rfind(closer)
        if last_open > last_close:
            ctx = cut.rstrip() + "\n" + closer + "\n[recall truncated]"
        else:
            ctx = cut + "\n[recall truncated]"
    return ctx


def main():
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
    # tolerance of the argv ladder means an older wrapper/python skew
    # degrades to the cold-start lane instead of crashing the hook).
    try:
        source = sys.argv[14]
    except IndexError:
        source = ""
    if not isinstance(source, str):
        source = ""
    try:
        validated_host_lane = sys.argv[15]
    except IndexError:
        validated_host_lane = host
    if not isinstance(validated_host_lane, str):
        validated_host_lane = host

    # Issue #153: the runtime host name is the local lane identity.  Keep the
    # functional host value untouched (native-memory nudges still depend on
    # it), but only a closed-set value with a valid release semver can opt into
    # enriched decision lines.
    attribution_lane = _validated_attribution_lane(validated_host_lane)
    attribution_version = _release_version()

    # Issue #107: the operator-facing drift notice rides systemMessage.
    _drift_msg = ""
    if drift_json:
        try:
            dj = json.loads(drift_json)
            if (isinstance(dj, dict) and dj.get("status") == "drifted"
                    and isinstance(dj.get("system_message"), str)
                    and dj["system_message"]):
                _drift_msg = dj["system_message"]
        except ValueError:
            _drift_msg = ""

    # issue #110 (P0-5): ZMEM_INJECT=0 is the passive-injection kill switch —
    # Tier 0, Tier 2 and every nudge are suppressed; log the disabled
    # decision and emit the (sentinel-wrapped since #121 — the emission moved
    # here from the bash wrapper) empty envelope.
    if os.environ.get("ZMEM_INJECT", "1").strip() == "0":
        try:
            if data_dir and os.path.isdir(data_dir):
                _rotate_log(store_py, os.path.join(data_dir, "zmem-decisions.log"))
                with open(os.path.join(data_dir, "zmem-decisions.log"),
                          "a", encoding="utf-8") as lf:
                    lf.write(
                        "[%d] zmem-hook status=silent reason=disabled ids=[] all=[] "
                        "sid=%s moment=session_start%s\n" % (
                            int(__import__("time").time()), _safe_sid(session_id),
                            _format_attribution(attribution_lane,
                                                attribution_version, 0)))
        except Exception:
            pass  # fail-open: the audit log never blocks session start
        payload = {"systemMessage": _drift_msg} if _drift_msg else {}
        _emit(payload)
        sys.exit(0)

    # ---- Envelope 1: Tier 0 + local-only parts, before any store subprocess.
    lead_parts = []
    tier0 = build_tier0_context(core, agents, host)

    # Pending live-capture correction candidates (issue #47): a COUNT only.
    # PREPENDED: the payload is trimmed from the front-boundary logic with
    # the tail dropped, so leading placement guarantees the nudge survives.
    correction_note = ""
    if store_py and os.path.isfile(store_py):
        try:
            saved = sys.path[:]
            try:
                sys.path.insert(0, os.path.dirname(store_py))
                import correction_queue as cq
                pending = sum(1 for it in cq.load_queue(ns) if not it.get("stale"))
                if pending:
                    correction_note = (
                        "zmem: %d captured correction candidate(s) pending review — "
                        "run the closeout skill to process." % pending)
            finally:
                sys.path[:] = saved
        except Exception:
            pass  # fail-open: queue read errors never block session start

    # Store-path note (local string, no subprocess).
    store_note = ""
    if store_py and os.path.isfile(store_py):
        store_note = ("# Memory skill: invoke `%s <subcommand>` to "
                      "recall/add/search memories." % store_py)

    # Native-memory nudge (CC only, P6): one-time, best-effort, fail-open.
    nudge_note = ""
    if host == "claude" and nudge_marker:
        try:
            already_shown = os.path.isfile(nudge_marker)
            native_disabled = bool(os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"))
            if not native_disabled and settings_dir:
                for fname in ("settings.json", "settings.local.json"):
                    fpath = os.path.join(settings_dir, fname)
                    try:
                        with open(fpath, encoding="utf-8") as f:
                            sdata = json.load(f)
                        if isinstance(sdata, dict) and sdata.get("autoMemoryEnabled") is False:
                            native_disabled = True
                            break
                    except (OSError, ValueError):
                        continue
            if not already_shown and not native_disabled:
                show_notice = False
                try:
                    os.makedirs(os.path.dirname(nudge_marker), exist_ok=True)
                    fd = os.open(nudge_marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    try:
                        os.write(fd, b"shown\n")
                    finally:
                        os.close(fd)
                    show_notice = True
                except FileExistsError:
                    show_notice = False
                except OSError:
                    show_notice = True  # fail-open toward showing
                if show_notice:
                    nudge_note = (
                        "# ZMem notice (one-time): Claude Code native memory still looks "
                        "enabled. ZMem is replacing it as your sole memory system - add "
                        "\"autoMemoryEnabled\": false to ~/.claude/settings.json so the two "
                        "systems do not double-run (a plugin cannot set this for you).")
        except Exception:
            pass  # fail-open: nudge logic never blocks session start

    first_parts = ([p for p in [correction_note] if p]
                   + [p for p in [tier0] if p]
                   + [p for p in [store_note, nudge_note] if p])
    first_ctx = _soft_trim("\n\n".join(first_parts) if first_parts else "", budget)
    first_payload = {}
    if first_ctx:
        first_payload["additionalContext"] = first_ctx
    if _drift_msg:
        first_payload["systemMessage"] = _drift_msg
    _emit(first_payload if first_payload else {})

    # ---- ONE bounded store lane (issue #121), then envelope 2.
    tier2_block = ""
    if store_py and os.path.isfile(store_py):
        floor_raw = os.environ.get("ZMEM_INJECT_FLOOR_RECENT", "")
        try:
            recent_floor = float(floor_raw) if floor_raw else 0.5
        except ValueError:
            recent_floor = 0.5
        ledger = _ledger_module(store_py)
        data_dir_known = _data_dir_for_ledger() if ledger is not None else ""
        if source == "compact" and session_id and ledger is not None:
            tier2_block, _ = _compact_lane(
                store_py, ns, session_id, ledger, data_dir_known,
                recent_floor,
                context_parts=[p for p in [correction_note, tier0] if p],
                attribution_lane=attribution_lane,
                attribution_version=attribution_version)
        if not tier2_block:
            tier2_block = build_tier2_context(
                store_py, ns, session_id, budget,
                context_parts=[p for p in [correction_note, tier0] if p],
                attribution_lane=attribution_lane,
                attribution_version=attribution_version)

    # Promotion candidates (non-blocking, one-line suggestion) — a store
    # subprocess by design; it runs AFTER envelope 1 so Tier 0 is never
    # delayed behind it.
    promote_note = ""
    if store_py and os.path.isfile(store_py):
        try:
            promote_out = subprocess.check_output(
                [sys.executable, store_py, "promote", "--dry-run"],
                stderr=subprocess.DEVNULL, timeout=5,
            ).decode("utf-8", "replace")
            for line in promote_out.strip().split("\n"):
                if "promotion candidate" in line.lower():
                    promote_note = line.strip()
                    break
        except Exception:
            pass  # fail-open: promotion check errors never block session start

    final_parts = ([p for p in [correction_note] if p]
                   + [p for p in [tier0] if p]
                   + [p for p in [tier2_block, promote_note] if p]
                   + [p for p in [store_note, nudge_note] if p])
    ctx = _soft_trim("\n\n".join(final_parts) if final_parts else "", budget)
    payload = {}
    if ctx:
        payload["additionalContext"] = ctx
    if _drift_msg:
        payload["systemMessage"] = _drift_msg
    _emit(payload if payload else {})


if __name__ == "__main__":
    main()
