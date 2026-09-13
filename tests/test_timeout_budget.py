"""Issue #121 — timeout budget + Tier 0 fast path (launcher-side tests).

NOTE(#160): the deterministic executor/clock seam used here is local to this
module because tests/support/fake_executor.py does not exist on main yet
(issue #160 owns that file and its FakeExecutor). When #160 lands, migrate
this seam to the shared one (submit(fn) / advance(seconds) / now()).

No test asserts elapsed wall time: every deadline assertion drives the
injected clock (the two integration tests bound total runtime with generous
external guards purely to fail fast on a broken build, never to measure).
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "hooks" / "zmem-launch.js"
FIXTURES_TIMEOUT = REPO_ROOT / "tests" / "fixtures" / "timeout"

sys.path.insert(0, str(REPO_ROOT / "tests" / "fixtures"))
from eval_store import BASE_ENV  # noqa: E402

SLOW_STORE_SHA256 = "bb3aad47f70624f06c0bb9a0024c1062c003ee4a87ad40b5cf57ec4e5f962f9e"
EXPECTED_TIMEOUT_SHA256 = "0849377aca82bb181e2fb4d16433082f3c576bdfdced55a7a844dabe92ac375d"

SENTINEL_START = "<<<ZMEM_JSON>>>"
SENTINEL_END = "<<<END>>>"


def _scratch(prefix):
    base = Path(tempfile.mkdtemp(prefix=prefix))
    return base


def _node_probe(script, env_extra=None, timeout=90):
    env = dict(os.environ)
    env.update(BASE_ENV)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True, text=True, env=env,
        cwd=str(REPO_ROOT), timeout=timeout)
    return proc


class TimeoutBudgetTest(unittest.TestCase):
    """AC1/AC2/AC4/AC5 surfaces: watchdog, namespace cache, fixtures, bench."""

    maxDiff = None

    # ---- AC4 surface: exact fixture bytes -------------------------------

    def test_fixture_digest(self):
        # PR #198 review F-003: hash the COMMITTED bytes FIRST — the pinned
        # SHA-256 values must describe what is actually checked in, not what
        # the generator would produce. Then run the generator into a scratch
        # dir (never the source tree: a read-only checkout must work) and
        # require byte parity with the committed files.
        slow = FIXTURES_TIMEOUT / "slow_store.json"
        expected = FIXTURES_TIMEOUT / "expected_timeout.json"
        self.assertEqual(
            hashlib.sha256(slow.read_bytes()).hexdigest(), SLOW_STORE_SHA256,
            "committed slow_store.json bytes drifted from the frozen fixture")
        self.assertEqual(
            hashlib.sha256(expected.read_bytes()).hexdigest(), EXPECTED_TIMEOUT_SHA256,
            "committed expected_timeout.json bytes drifted from the frozen fixture")
        scratch = _scratch("zmem-121-fixgen-")
        gen = subprocess.run(
            [sys.executable, str(FIXTURES_TIMEOUT / "generate.py"),
             "--out-dir", str(scratch)],
            capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT))
        self.assertEqual(gen.returncode, 0, gen.stderr)
        self.assertEqual(slow.read_bytes(), (scratch / "slow_store.json").read_bytes(),
                         "generator output diverged from committed slow_store.json")
        self.assertEqual(expected.read_bytes(), (scratch / "expected_timeout.json").read_bytes(),
                         "generator output diverged from committed expected_timeout.json")
        # timeout-budget.json parity with the documented constants.
        budget = json.loads((REPO_ROOT / "hooks" / "timeout-budget.json").read_text(
            encoding="utf-8"))
        self.assertEqual(budget, {
            "launcher_watchdog_ms": 12000,
            "namespace_resolve_ms": 2000,
            "store_recall_ms": 8000,
            "sqlite_busy_ms": 5000,
            "hermes_manager_join_ms": 8000,
            "hermes_provider_deadline_ms": 6000,
        })

    # ---- AC1 surface: watchdog deadline on the injected clock ------------

    def test_watchdog_deadline(self):
        script = r"""
const l = require('./hooks/zmem-launch.js');
class FakeClock {
  constructor() { this.t = 0; this.timers = []; this.nextId = 1; }
  setTimeout(fn, ms) { const id = this.nextId++; this.timers.push({id, fn, at: this.t + ms}); return id; }
  clearTimeout(id) { this.timers = this.timers.filter(x => x.id !== id); }
  advance(ms) {
    const target = this.t + ms;
    for (;;) {
      const due = this.timers.filter(x => x.at <= target).sort((a,b) => a.at - b.at)[0];
      if (!due) break;
      this.timers = this.timers.filter(x => x.id !== due.id);
      this.t = due.at;
      due.fn();
    }
    this.t = target;
  }
  now() { return this.t; }
}
function fakeChild() {
  return { pid: undefined, killed: 0, kill() { this.killed++; } };
}
const clock = new FakeClock();
const child = fakeChild();
let fired = 0;
const h = l.startWatchdog(child, 12000, clock, () => fired++);
clock.advance(11999);
console.log(JSON.stringify({step: 'pre', fired, killed: child.killed}));
clock.advance(1);
console.log(JSON.stringify({step: 'at-12000', fired, killed: child.killed}));
clock.advance(5000);
console.log(JSON.stringify({step: 'post', fired, killed: child.killed}));
// clear-before-fire disarms permanently
const clock2 = new FakeClock();
const child2 = fakeChild();
let fired2 = 0;
const h2 = l.startWatchdog(child2, 12000, clock2, () => fired2++);
h2.clear();
clock2.advance(30000);
console.log(JSON.stringify({step: 'cleared', fired: fired2, killed: child2.killed}));
// invalid env falls back to the default with exactly one warning
const warnings = [];
const a = l.readPositiveIntMs({ZMEM_LAUNCHER_WATCHDOG_MS: 'not-a-number'}, 'ZMEM_LAUNCHER_WATCHDOG_MS', 12000, (s) => warnings.push(s));
const b = l.readPositiveIntMs({ZMEM_LAUNCHER_WATCHDOG_MS: '-5'}, 'ZMEM_LAUNCHER_WATCHDOG_MS', 12000, (s) => warnings.push(s));
const c = l.readPositiveIntMs({ZMEM_LAUNCHER_WATCHDOG_MS: '2500'}, 'ZMEM_LAUNCHER_WATCHDOG_MS', 12000, (s) => warnings.push(s));
console.log(JSON.stringify({step: 'env', a, b, c, warnings: warnings.length}));
"""
        proc = _node_probe(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        steps = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
        by_step = {s["step"]: s for s in steps}
        self.assertEqual(by_step["pre"], {"step": "pre", "fired": 0, "killed": 0},
                         "nothing may fire before the deadline")
        self.assertEqual(by_step["at-12000"],
                         {"step": "at-12000", "fired": 1, "killed": 1},
                         "the child is killed at exactly 12000 ms")
        self.assertEqual(by_step["post"], {"step": "post", "fired": 1, "killed": 1},
                         "the watchdog fires exactly once")
        self.assertEqual(by_step["cleared"], {"step": "cleared", "fired": 0, "killed": 0},
                         "clear() disarms the watchdog")
        self.assertEqual(by_step["env"]["a"], 12000)
        self.assertEqual(by_step["env"]["b"], 12000)
        self.assertEqual(by_step["env"]["c"], 2500)
        # F-004/cubic: warnings are keyed per env-var NAME (warnOnce), so two
        # invalid values of the same name warn once; the valid value warns never.
        self.assertEqual(by_step["env"]["warnings"], 1,
                         "one warning per invalid NAME, none for the valid one")

    def test_terminate_child_tree(self):
        """RC1: on win32 the kill must take the bash child's python grandchild
        tree down with it (taskkill /T), not just the direct child."""
        if sys.platform != "win32":
            self.skipTest("win32 taskkill branch")
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("no bash on PATH")
        script = r"""
const { spawn } = require('child_process');
const l = require('./hooks/zmem-launch.js');
const child = spawn(process.argv[1], ['-c', 'exec python -c "import time; time.sleep(60)"']);
const done = new Promise((resolve) => child.on('close', (code) => resolve(code)));
setTimeout(() => {
  l._terminateChildTree(child);
  done.then((code) => {
    console.log(JSON.stringify({closed: true, info: l._lastTerminateInfoForTests()}));
    process.exit(0);
  });
}, 400);
setTimeout(() => {
  console.log(JSON.stringify({closed: false, info: l._lastTerminateInfoForTests()}));
  process.exit(1);
}, 8000);
""".replace("process.argv[1]", json.dumps(bash))
        proc = _node_probe(script, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads([ln for ln in proc.stdout.splitlines()
                             if ln.strip().startswith("{")][-1])
        self.assertTrue(result["closed"],
                        "the bash child tree must terminate promptly")
        self.assertEqual(result["info"]["mode"], "taskkill",
                         "win32 must select the tree-kill branch")

    def test_watchdog_kills_real_payload_on_timeout(self):
        """The watchdog against the REAL session-start path (not a stub bash
        child): a slow store stub stalls the payload; the launcher must kill
        the tree at the (overridden) deadline, retain Tier 0, and log the
        outer-timeout decision with timeout_ms matching the override."""
        scratch = _scratch("zmem-121-real-watchdog-")
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        hooks = scratch / "hooks"
        lib = hooks / "lib"
        lib.mkdir(parents=True)
        shutil.copy(REPO_ROOT / "hooks" / "zmem-session-start.sh", hooks / "zmem-session-start.sh")
        for name in ("zmem-session-start-payload.py", "zmem-tilde-expand.sh"):
            shutil.copy(REPO_ROOT / "hooks" / "lib" / name, lib / name)
        scripts = scratch / "skills" / "memory" / "scripts"
        scripts.mkdir(parents=True)
        data = scratch / "data"
        data.mkdir()
        (data / "core.md").write_text("REAL-PAYLOAD-TIER0", encoding="utf-8")
        NL = chr(10)
        stub_lines = [
            "import sys, time",
            "if len(sys.argv) > 1 and sys.argv[1] == 'recent':",
            "    time.sleep(60)",
            "else:",
            "    print('')",
        ]
        (scripts / "store.py").write_text(NL.join(stub_lines) + NL,
                                          encoding="utf-8", newline="\n")
        env = dict(os.environ)
        env.update(BASE_ENV)
        env.update({
            "ZCODE_PLUGIN_ROOT": str(scratch).replace("\\", "/"),
            "ZMEM_DATA": str(data).replace("\\", "/"),
            "ZMEM_STORE": str(data / "store.sqlite").replace("\\", "/"),
            "ZMEM_LAUNCHER_WATCHDOG_MS": "3000",
            "ZMEM_INJECT": "1",
        })
        env.pop("PLUGIN_ROOT", None)
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        env.pop("ZMEM_NAMESPACE", None)
        env.pop("ZMEM_BASH_PATH", None)
        proc = subprocess.run(
            ["node", str(LAUNCHER), "session-start"],
            input=json.dumps({"session_id": "real-watchdog", "source": "startup"}),
            capture_output=True, text=True, env=env,
            cwd=str(REPO_ROOT), timeout=30)
        self.assertEqual(proc.returncode, 0,
                         "the launcher must exit 0 after the watchdog fires: "
                         + proc.stdout + proc.stderr)
        self.assertIn("REAL-PAYLOAD-TIER0", proc.stdout,
                      "Tier 0 must be retained and emitted after termination")
        decisions = data / "zmem-decisions.log"
        self.assertTrue(decisions.exists(), "the outer-timeout decision must land")
        line = decisions.read_text(encoding="utf-8")
        for token in ("outer_timeout=1", "reason=omitted", "stage=launcher",
                      "timeout_ms=3000", "tier0_emitted=1"):
            self.assertIn(token, line)

    # ---- AC2 surface: namespace cache ------------------------------------

    def test_namespace_cache(self):
        # F-013: resolveNamespace caches only remote-derived keys. On a
        # checkout with no configured origin remote the repo root would
        # resolve to a path key and the hit/miss assertions would not hold —
        # fall back to a scratch repo with origin configured.
        root_path = REPO_ROOT
        probe = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "config", "--get", "remote.origin.url"],
            capture_output=True)
        if probe.returncode != 0:
            fallback = _scratch("zmem-121-origin-")
            subprocess.run(["git", "init", str(fallback)], capture_output=True)
            subprocess.run(["git", "-C", str(fallback), "remote", "add",
                            "origin", "https://github.com/zaxbysauce/zmem.git"],
                           capture_output=True)
            root_path = fallback
        root_str = str(root_path).replace("\\", "\\\\")
        script = r"""
const l = require('./hooks/zmem-launch.js');
const out = {};
l.clearNamespaceCache();
const root = process.argv[1];
// miss then hit on the same path
let r1 = l.resolveNamespace(root);
let s1 = l.namespaceCacheStats();
let r2 = l.resolveNamespace(root);
let s2 = l.namespaceCacheStats();
out.first = {ns: r1, dMiss: s1.misses, dHit: s1.hits};
out.second = {ns: r2, dMiss: s2.misses - s1.misses, dHit: s2.hits - s1.hits};
// case-variant path (win32 key normalization)
const variant = root.split('').map((c, i) => i === 3 ? (c === c.toUpperCase() ? c.toLowerCase() : c.toUpperCase()) : c).join('');
if (process.platform === 'win32') {
  let r3 = l.resolveNamespace(variant);
  let s3 = l.namespaceCacheStats();
  out.variant = {ns: r3, dMiss: s3.misses - s2.misses, dHit: s3.hits - s2.hits};
} else {
  out.variant = {skipped: true};
}
// path-key projects are resolved but NEVER cached: two resolves of a
// non-remote dir are BOTH misses and return the same deterministic key
const missing = root + '/no-such-dir-121';
let m1 = l.resolveNamespace(missing);
let sm1 = l.namespaceCacheStats();
let m2 = l.resolveNamespace(missing);
let sm2 = l.namespaceCacheStats();
out.missing = {first: m1, second: m2, dMiss: sm2.misses - sm1.misses};
out.cap = l.NAMESPACE_CACHE_MAX_ENTRIES;
// TTL expiry at exactly TTL on the injected clock
l.clearNamespaceCache();
let t = 0;
const clock = () => t;
let a1 = l.resolveNamespace(root, {clock: clock, ttlMs: 60000, resolveMs: 2000});
let st1 = l.namespaceCacheStats();
t = 59999;
let a2 = l.resolveNamespace(root, {clock: clock, ttlMs: 60000, resolveMs: 2000});
let st2 = l.namespaceCacheStats();
t = 60000;
let a3 = l.resolveNamespace(root, {clock: clock, ttlMs: 60000, resolveMs: 2000});
let st3 = l.namespaceCacheStats();
out.ttl = {
  at59999: {same: a2 === a1, dHit: st2.hits - st1.hits, dMiss: st2.misses - st1.misses},
  at60000: {same: a3 === a1, dHit: st3.hits - st2.hits, dMiss: st3.misses - st2.misses},
};
console.log(JSON.stringify(out));
""".replace("process.argv[1]", json.dumps(root_str))
        proc = _node_probe(script, env_extra={"ZMEM_DATA": str(_scratch("zmem-121-ns-")).replace("\\", "/")}, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads([ln for ln in proc.stdout.splitlines()
                              if ln.strip().startswith("{")][-1])
        self.assertEqual(payload["first"]["dMiss"], 1)
        self.assertEqual(payload["first"]["dHit"], 0)
        self.assertEqual(payload["second"]["dMiss"], 0, "second resolve must HIT")
        self.assertEqual(payload["second"]["dHit"], 1)
        self.assertEqual(payload["second"]["ns"], payload["first"]["ns"])
        if not payload["variant"].get("skipped"):
            self.assertEqual(payload["variant"]["dHit"], 1,
                             "a case variant of the same path must share the entry")
            self.assertEqual(payload["variant"]["dMiss"], 0)
        self.assertEqual(payload["missing"]["first"], payload["missing"]["second"],
                         "path-key resolution must be deterministic")
        self.assertTrue(payload["missing"]["first"].startswith("project:"),
                        "a non-remote dir resolves to its path key, not user:global")
        self.assertEqual(payload["missing"]["dMiss"], 1,
                         "the second path-key resolve must MISS again (never cached)")
        self.assertEqual(payload["cap"], 128)
        self.assertEqual(payload["ttl"]["at59999"]["dHit"], 1,
                         "age < TTL is fresh (a cache hit)")
        self.assertEqual(payload["ttl"]["at59999"]["dMiss"], 0)
        self.assertEqual(payload["ttl"]["at60000"]["dHit"], 0,
                         "age == TTL must expire (no hit)")
        self.assertEqual(payload["ttl"]["at60000"]["dMiss"], 1,
                         "the expired entry re-resolves (a miss at exactly TTL)")

    # ---- AC5 surface: bench shape + baseline mismatch ---------------------

    def test_benchmark_shape(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import bench_hook_latency as bench
        finally:
            sys.path.pop(0)
        import io
        from contextlib import redirect_stdout
        scratch = _scratch("zmem-121-bench-")
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        store = scratch / "bench-store.sqlite"
        store.write_bytes(b"")
        seen = {}
        for case in ("cold", "warm", "freshness"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = bench.main(["--store", str(store),
                                 "--log", str(scratch / ("dec-%s.log" % case)),
                                 "--case", case, "--runs", "5"])
            self.assertEqual(rc, 0)
            report = json.loads(buf.getvalue())
            self.assertEqual(set(report["stages"]), {
                "launcher", "namespace", "store", "embed", "fuse", "render",
                "time-last-capture"},
                "case %s must carry EXACTLY the seven stages" % case)
            for stage_stats in report["stages"].values():
                self.assertIn("p50", stage_stats)
                self.assertIn("p95", stage_stats)
            self.assertRegex(report["input_digest"], r"^[0-9a-f]{64}$")
            seen[case] = report
        # digest stability: same argv twice -> identical digest
        buf = io.StringIO()
        with redirect_stdout(buf):
            bench.main(["--store", str(store), "--log", str(scratch / "d.log"),
                        "--case", "cold", "--runs", "5"])
        self.assertEqual(json.loads(buf.getvalue())["input_digest"],
                         seen["cold"]["input_digest"])
        # percentile contract
        self.assertEqual(bench.percentile([1.0, 2.0, 3.0, 4.0], 50), 2.5)
        self.assertEqual(bench.percentile([5.0], 95), 5.0)

    def test_baseline_mismatch(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import bench_hook_latency as bench
        finally:
            sys.path.pop(0)
        import io
        from contextlib import redirect_stdout
        scratch = _scratch("zmem-121-baseline-")
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        store = scratch / "s.sqlite"
        store.write_bytes(b"")
        argv = ["--store", str(store), "--log", str(scratch / "d.log"),
                "--case", "cold", "--runs", "3"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            bench.main(argv)
        report = json.loads(buf.getvalue())
        good = scratch / "good.json"
        good.write_text(json.dumps(report), encoding="utf-8")
        bad_keys = scratch / "bad-keys.json"
        mutated = dict(report)
        mutated["stages"] = dict(report["stages"])
        mutated["stages"]["st0re"] = mutated["stages"].pop("store")
        bad_keys.write_text(json.dumps(mutated), encoding="utf-8")
        bad_digest = scratch / "bad-digest.json"
        mutated2 = dict(report)
        mutated2["input_digest"] = "0" * 64
        bad_digest.write_text(json.dumps(mutated2), encoding="utf-8")

        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                bench.main(argv + ["--compare-baseline", str(good)]), 0,
                "an unmutated baseline must round-trip clean")
            self.assertEqual(
                bench.main(argv + ["--compare-baseline", str(bad_keys)]), 1,
                "changed stage keys must exit 1")
            self.assertEqual(
                bench.main(argv + ["--compare-baseline", str(bad_digest)]), 1,
                "changed input_digest must exit 1")
        # usage errors carry the exact stderr strings
        for args, expect in (
            ([], "error: the following arguments are required: --store, --log, --case"),
            (["--store", "s", "--log", "x", "--case", "cold", "--bad-flag"],
             "error: unrecognized arguments: --bad-flag"),
            (["--store", "s", "--log", "x", "--case", "cold", "--runs", "0"],
             "error: --runs must be positive"),
        ):
            proc = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "bench_hook_latency.py")]
                + args, capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT))
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn(expect, proc.stderr)


if __name__ == "__main__":
    unittest.main()
