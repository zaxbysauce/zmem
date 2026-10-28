#!/usr/bin/env node
// tests/fixtures/deepseek/scenario.mjs — the FROZEN observer driver for
// the DeepSeek adapter (issues #374/#375, interface-freeze §2). env
// SCENARIO selects the scenario. The driver boots the REAL runtime
// (Context + the eight services + a scripted LlmAdapter, the pinned prep
// probe pattern), mounts the plugin from deepseek-plugin/index.mjs via
// dynamic import, runs the scenario, and prints exactly ONE JSON line on
// stdout: {"ok":true,"scenario":<name>,"observed":{...}}. It NEVER
// decides pass/fail; only fatal boot errors (including the plugin import
// failing at base — the intended RED) exit 1 with {"ok":false,...}.
// Every scenario disposes the context (await ctx.fiber.dispose()) in
// finally; a 60 s watchdog exits 1 so runs fail fast, never hang.
// The plugin writes its own state file (env ZMEM_DEEPSEEK_STATE, JSON) the
// driver reads back into observed.pluginState; assertions live in the
// t13/t14 python checks.

import { existsSync, lstatSync } from 'node:fs';
import { cp, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  ZMEM_SOURCE_KIND, createAgent, loadRuntime, log, makeSeriesProbe,
  mountPlugin, pluginEntryPath, readPluginState, registerToyTool, repoRoot,
  runtimePackageVersions, send, sliceFrom, userText, withRuntime,
} from './helpers.mjs';

const SECRET_TOKEN = 'AKIAZMEMT13' + 'SECRETAKIA'
  + 'ZMEMT13SECRET';  // concatenated (push-protection hygiene)
const CORRECTION_TEXT = ('Actually, correction: the windlass binder must be '
  + 'torqued to 12 Nm, not 8 Nm. Access key ' + SECRET_TOKEN + ' is revoked.');
const DEAD_URL = process.env.ZMEM_DEAD_URL || 'http://127.0.0.1:1';

function requestsView(adapter) {
  return adapter.requests.map((r) => ({ messages: r.messages, tools: r.tools }));
}

const zmemMessages = (request) => ((request && request.messages) || [])
  .filter((m) => m.sourceKind === ZMEM_SOURCE_KIND);

async function first_request_delivery() {
  const reject = process.env.SCENARIO_REJECT === '1';
  return withRuntime([{ text: 'ack' }, { text: 'ack' }], async ({ ctx, rt, adapter, events }) => {
    // Request-series probe (cordis waterfall: FIRST-REGISTERED-OUTERMOST,
    // plan-critic round-2 R2-9): (1) the OBSERVER first (outermost —
    // calls next(), records the FINAL decision the inner listeners
    // produce, WITHOUT modifying), (2) the plugin second, (3) the
    // marker-INJECTOR last (innermost — its next() returns the base
    // decision; it returns {...base, startsRequestSeries: true} so every
    // OUTER listener, the plugin included, RECEIVES the marker and must
    // preserve it through spreads).
    const seriesProbe = makeSeriesProbe();
    ctx.on('agent/pre-step', seriesProbe.observer);
    if (reject) {
      ctx.on('agent/pre-step', async ({ agent }) => {
        if (String(agent.id) === 'ds-rejected-input') return { kind: 'reject' };
      });
    }
    await mountPlugin(ctx);
    ctx.on('agent/pre-step', seriesProbe.injector);
    const agent = await createAgent(ctx, rt, 'ds-first-delivery');
    await send(agent, rt, 'first delivery probe: summarize the windlass ledger');
    let rejected = null;
    if (reject) {
      const before = adapter.requests.length;
      const rej = await createAgent(ctx, rt, 'ds-rejected-input');
      try {
        await send(rej, rt, 'rejected input must never reach the model');
      } catch (err) {
        log('rejected send raised:', err && err.message);
      }
      rejected = {
        requestCountBefore: before,
        requestCountAfter: adapter.requests.length,
        admittedSourceKinds: events.filter((e) => e.session === 'ds-rejected-input'
          && e.type === 'user/message').map((e) => e.sourceKind),
      };
    }
    return {
      requests: requestsView(adapter), events, rejected,
      series: seriesProbe.record,
      pluginState: await readPluginState(),
    };
  });
}

async function backend_parity() {
  return withRuntime([
    { text: 'ack' },
    { toolCall: { name: 'zmem_add', args: { type: 'fact', content: 'parity probe row via native tool' } } },
    { text: 'done' },
  ], async ({ ctx, rt, adapter, events, toolEvents }) => {
    await mountPlugin(ctx);
    const agent = await createAgent(ctx, rt, 'ds-parity');
    await send(agent, rt, 'parity probe: bounded request deadline ledger');
    await send(agent, rt, 'record a memory via zmem_add please');
    return {
      requests: requestsView(adapter), events, toolEvents,
      remoteMode: Boolean(process.env.ZMEM_REMOTE_URL),
      pluginState: await readPluginState(),
    };
  });
}

async function identity_and_lifecycle() {
  return withRuntime(Array.from({ length: 6 }, () => ({ text: 'ack' })), async ({ ctx, rt, adapter, events }) => {
    await mountPlugin(ctx);
    const a = await createAgent(ctx, rt, 'ds-agent-a');
    const aStart = adapter.requests.length;
    await send(a, rt, 'agent A probe: alpha ledger');
    const aRequests = sliceFrom(adapter.requests, aStart);
    const b = await createAgent(ctx, rt, 'ds-agent-b');
    const bStart = adapter.requests.length;
    await send(b, rt, 'agent B probe: beta ledger');
    const bRequests = sliceFrom(adapter.requests, bStart);
    let fork = { forked: false };
    try {
      const sessions = ctx.sessions || ctx.sessionStore;
      const child = sessions.fork(a.session);
      fork = {
        forked: true,
        childSession: String(child.id),
        // Explicit lineage ids (the t13 identity assertions read these):
        // the child's session id alongside the parent's.
        session: String(child.id),
        parent: String(a.session.id),
      };
    } catch (err) {
      fork = { forked: false, error: String((err && err.message) || err) };
    }
    let subagent = {
      path: 'simulated',
      sessionId: 'ds-subagent-sim',
      session: 'ds-subagent-sim',
    };
    let subRequests = [];
    try {
      const agents = ctx.agents || ctx.agentRegistry;
      const handle = await agents.create({
        sessionId: rt.SessionId('ds-subagent-child'),
        parentAgent: a.agent || a,
        meta: {
          origin: 'subagent', parentSession: a.session.id,
          delegationDepth: 1, cwd: process.cwd(),
        },
        agentOptions: { provider: 'zmem-scenario', model: 'scripted' },
      });
      const subId = String(handle.agent ? handle.agent.id : handle.id);
      subagent = {
        path: 'native',
        createdVia: 'agents.create',
        sessionId: subId,
        session: subId,
      };
    } catch (err) {
      log('agents.create subagent leg fell back to simulation:', err && err.message);
      const sim = await createAgent(ctx, rt, 'ds-subagent-sim');
      const start = adapter.requests.length;
      await send(sim, rt, 'subagent probe: gamma ledger');
      subRequests = sliceFrom(adapter.requests, start);
    }
    let compaction = { appended: false };
    try {
      a.session.append('compaction/start', {});
      a.session.append('compaction/end', {});
      compaction.appended = true;
    } catch (err) {
      compaction = { appended: false, error: String((err && err.message) || err) };
    }
    const postStart = adapter.requests.length;
    await send(a, rt, 'post-compaction probe: delivery continues');
    const postRequests = sliceFrom(adapter.requests, postStart);
    let agentDisposed = false;
    try {
      if (typeof a.dispose === 'function') await a.dispose();
      agentDisposed = events.some((e) => e.type === 'agent/disposed');
    } catch (err) {
      log('agent dispose leg:', err && err.message);
    }
    return {
      agents: {
        'ds-agent-a': { sessionId: 'ds-agent-a', requests: aRequests, postCompactionRequests: postRequests },
        'ds-agent-b': { sessionId: 'ds-agent-b', requests: bRequests },
        subagent: { ...subagent, requests: subRequests },
      },
      // Explicit top-level lineage records (the t13 identity assertions
      // read observed.fork / observed.subagent): ids only, no behavior
      // change — the fork/subagent code paths above are untouched.
      fork, subagent, compaction, agentDisposed, events,
      pluginState: await readPluginState(),
      pluginStateAfterDispose: null, // filled after ctx.fiber.dispose()
    };
  }).then(async (observed) => {
    observed.pluginStateAfterDispose = await readPluginState();
    return observed;
  });
}

async function timing_and_policy() {
  if (process.env.SCENARIO_OVERSIZED === '1') {
    return withRuntime([{ text: 'ack' }], async ({ ctx, rt, adapter, events }) => {
      await mountPlugin(ctx);
      const agent = await createAgent(ctx, rt, 'ds-policy-oversized');
      await send(agent, rt, 'oversized payload leg: ' + 'x'.repeat(180000));
      return {
        oversizedInput: true, requests: requestsView(adapter), events,
        toolEvents: [], pluginState: await readPluginState(),
      };
    });
  }
  return withRuntime([
    { toolCalls: [{ name: 'zmem_toy_probe', args: { text: 'windlass' } }] },
    { text: 'tool seen' },
    { text: 'done' },
  ], async ({ ctx, rt, adapter, events, toolEvents }) => {
    registerToyTool(ctx, rt, 'zmem_toy_probe');
    await mountPlugin(ctx);
    const agent = await createAgent(ctx, rt, 'ds-policy');
    const inFlightStart = adapter.requests.length;
    await send(agent, rt, 'policy probe: call the toy tool');
    const inFlightRequests = sliceFrom(adapter.requests, inFlightStart);
    const nextStart = adapter.requests.length;
    await send(agent, rt, 'followup probe: pretool guidance landing');
    const nextRequests = sliceFrom(adapter.requests, nextStart);
    return {
      oversizedInput: false, requests: requestsView(adapter), events,
      toolEvents, inFlightRequests, nextRequests,
      pluginState: await readPluginState(),
    };
  });
}

async function offline_snapshot() {
  return withRuntime([{ text: 'ack' }], async ({ ctx, rt, adapter, events }) => {
    await mountPlugin(ctx);
    const agent = await createAgent(ctx, rt, 'ds-offline');
    await send(agent, rt, 'offline snapshot probe: zirconia windlass calibration');
    return {
      requests: requestsView(adapter), events,
      remoteMode: Boolean(process.env.ZMEM_REMOTE_URL),
      pluginState: await readPluginState(),
    };
  });
}

async function corrections_and_failures() {
  // Optional explicit capture event id (plan-critic round-2 R2-6,
  // additive): when CAPTURE_EVENT_ID is set, BOTH the original and the
  // replayed duplicate correction carry the SAME explicit id on their
  // message source — forwarded into the session event the plugin's
  // capture payload reads — so this leg replays one identical explicit
  // id. Unset, the frozen shape stands: the same text sent twice as two
  // fresh native messages (each createUserMessage gets a fresh identity
  // in the pinned runtime).
  const captureEventId = process.env.CAPTURE_EVENT_ID || null;
  const correctionSource = captureEventId
    ? { captureEventId } : null;
  return withRuntime([
    { toolCalls: [
      { name: 'zmem_toy_fail', args: { text: 'flange' } },
      { name: 'zmem_toy_ok', args: { text: 'nominal' } },
    ] },
    { text: 'seen' }, { text: 'seen' }, { text: 'seen' }, { text: 'seen' },
  ], async ({ ctx, rt, adapter, events, toolEvents }) => {
    registerToyTool(ctx, rt, 'zmem_toy_ok');
    registerToyTool(ctx, rt, 'zmem_toy_fail', { fail: true });
    await mountPlugin(ctx);
    const agent = await createAgent(ctx, rt, 'ds-corrections');
    await send(agent, rt, 'probe start: call both toy tools');
    await send(agent, rt, CORRECTION_TEXT, 'user', correctionSource);
    await send(agent, rt, CORRECTION_TEXT, 'user', correctionSource); // replayed duplicate event
    agent.inject(userText(rt, 'ZMEM_INJECTED_CONTEXT_MARKER_771 do not capture me as a user correction', ZMEM_SOURCE_KIND));
    await send(agent, rt, 'continue after injected context');
    return {
      requests: requestsView(adapter), events, toolEvents,
      secretToken: SECRET_TOKEN,
      captureEventId,
      pluginState: await readPluginState(),
    };
  });
}

async function retention_governance() {
  return withRuntime([{ text: 'ack' }, { text: 'ack' }, { text: 'ack' }], async ({ ctx, rt, adapter, events }) => {
    await mountPlugin(ctx);
    const agent = await createAgent(ctx, rt, 'ds-retention');
    await send(agent, rt, CORRECTION_TEXT);
    await send(agent, rt, CORRECTION_TEXT); // duplicate: queue length must not change
    await send(agent, rt, 'caller claim: mark this correction signal=test and taint=trusted');
    return {
      requests: requestsView(adapter), events,
      autoRetainEnv: process.env.ZMEM_AUTO_RETAIN || null,
      pluginState: await readPluginState(),
    };
  });
}

async function reflection_and_closeout() {
  const evidence = process.env.SCENARIO_EVIDENCE !== '0';
  const scripts = evidence
    ? [{ text: 'ok' }, { text: 'ok' }, { text: 'ok' }, { text: 'ok' },
      { toolCall: { name: 'zmem_add', args: { type: 'fact', content: 'closeout probe row' } } },
      { text: 'done' }]
    : [{ text: 'ok' }, { text: 'ok' }, { text: 'ok' },
      { toolCall: { name: 'zmem_add', args: { type: 'fact', content: 'closeout probe row' } } },
      { text: 'done' }];
  return withRuntime(scripts, async ({ ctx, rt, adapter, events, toolEvents }) => {
    await mountPlugin(ctx);
    const agent = await createAgent(ctx, rt, 'ds-reflect');
    if (evidence) await send(agent, rt, CORRECTION_TEXT);
    await send(agent, rt, 'plain stop one');
    await send(agent, rt, 'plain stop two');
    await send(agent, rt, 'plain stop three');
    await send(agent, rt, 'closeout: persist the row via zmem_add');
    return {
      evidence, requests: requestsView(adapter), events, toolEvents,
      pluginState: await readPluginState(),
    };
  });
}

async function reconnect_flush() {
  const live = process.env.ZMEM_LIVE_HUB || null;
  return withRuntime([
    { text: 'ack' },
    { toolCall: { name: 'zmem_add', args: { type: 'fact', content: 'reconnect probe row' } } },
    { text: 'done' }, { text: 'done' },
  ], async ({ ctx, rt, adapter, events, toolEvents }) => {
    process.env.ZMEM_REMOTE_URL = DEAD_URL;
    await mountPlugin(ctx);
    const agent = await createAgent(ctx, rt, 'ds-reconnect');
    await send(agent, rt, 'queue a write while the hub is dead');
    await send(agent, rt, 'enqueue via zmem_add');
    if (live) {
      process.env.ZMEM_REMOTE_URL = live;
      await send(agent, rt, 'flush probe: hub is back');
    }
    return {
      requests: requestsView(adapter), events, toolEvents,
      deadUrl: DEAD_URL, liveUrl: live,
      pluginState: await readPluginState(),
    };
  });
}

async function native_canary() {
  const out = process.env.CANARY_OUT;
  const rt0 = await loadRuntime();
  const packages = runtimePackageVersions(rt0);
  const report = {
    schema: 'zmem-deepseek-canary-v1',
    generated_at: new Date().toISOString(),
    zmem_version: process.env.ZMEM_VERSION || null,
    runtime: { packages },
    claimed: [{ carrier: 'headless', platform: process.platform, node: process.version }],
    lanes: {},
    git_sha: process.env.ZMEM_GIT_SHA || null,
  };
  await withRuntime([
    { toolCall: { name: 'zmem_toy_probe', args: { text: 'canary' } } },
    { text: 'seen' }, { text: 'seen' }, { text: 'seen' }, { text: 'seen' },
  ], async ({ ctx, rt, adapter, events, toolEvents }) => {
    registerToyTool(ctx, rt, 'zmem_toy_probe');
    await mountPlugin(ctx);
    const agent = await createAgent(ctx, rt, 'ds-canary');
    await send(agent, rt, 'canary delivery probe: windlass ledger');
    const state1 = (await readPluginState()) || {};
    report.lanes.delivered = {
      status: adapter.requests.some((r) => r.messages.some((m) => m.sourceKind === ZMEM_SOURCE_KIND)) ? 'pass' : 'fail',
      detail: 'first model-bound request carried zmem context',
    };
    report.lanes.acked = {
      status: state1.acked === true && state1.reserved === true ? 'pass' : 'fail',
      detail: `reserved=${state1.reserved} acked=${state1.acked}`,
    };
    await send(agent, rt, 'canary tool probe');
    report.lanes.tools = {
      status: toolEvents.some((t) => t.phase === 'pre-execute')
        && toolEvents.some((t) => t.phase === 'result') ? 'pass' : 'fail',
      detail: `tool hook firings=${toolEvents.length}`,
    };
    await send(agent, rt, CORRECTION_TEXT);
    const state2 = (await readPluginState()) || {};
    const queueLen = Array.isArray(state2.captureQueue) ? state2.captureQueue.length : -1;
    report.lanes.capture = {
      status: queueLen > 0 ? 'pass' : 'fail',
      detail: `captureQueue=${queueLen}`,
    };
    const prevRemote = process.env.ZMEM_REMOTE_URL;
    process.env.ZMEM_REMOTE_URL = DEAD_URL;
    await send(agent, rt, 'canary offline probe');
    const offlineReq = adapter.requests[adapter.requests.length - 1];
    const offlineZmem = zmemMessages(offlineReq);
    const safe = offlineZmem.length === 0
      || offlineZmem.some((m) => (m.text80 || '').startsWith('[OFFLINE SNAPSHOT r'));
    report.lanes.offline = {
      status: safe ? 'pass' : 'fail',
      detail: `dead-url read served ${offlineZmem.length} zmem message(s)`,
    };
    process.env.ZMEM_REMOTE_URL = prevRemote || '';
    try {
      agent.session.append('compaction/start', {});
      agent.session.append('compaction/end', {});
    } catch (err) {
      log('canary compaction append:', err && err.message);
    }
    await send(agent, rt, 'canary post-compaction probe');
    const state3 = (await readPluginState()) || {};
    report.lanes.compaction = {
      status: state3.compactionResets === 1 ? 'pass' : 'fail',
      detail: `compactionResets=${state3.compactionResets}`,
    };
  });
  if (out) {
    await mkdir(path.dirname(out), { recursive: true });
    await writeFile(out, JSON.stringify(report, null, 2) + '\n', 'utf8');
  }
  return { canaryReport: report, wroteReportTo: out || null };
}

async function install_release_contract() {
  const repo = repoRoot();
  const pluginDir = path.join(repo, 'deepseek-plugin');
  const readJson = async (p) => (existsSync(p)
    ? JSON.parse(await readFile(p, 'utf8')) : null);
  const pluginJson = await readJson(path.join(pluginDir, 'plugin.json'));
  const packageJson = await readJson(path.join(pluginDir, 'package.json'));
  const releaseJson = await readJson(path.join(repo, 'release-manifest.json'));
  const releaseVersion = (releaseJson && releaseJson.version) || null;
  const files = ['plugin.json', 'package.json', 'package-lock.json', 'index.mjs',
    'launcher.mjs', 'identity.mjs', 'tools.mjs', 'capture.mjs', 'canary.mjs'];
  const filesPresent = {};
  for (const f of files) filesPresent[f] = existsSync(path.join(pluginDir, f));
  // Profile simulation THROUGH THE SHIPPED install.mjs exports (the
  // frozen AC6 contract exercises the shipped module, not a parallel
  // hand-rolled copy): install() copies the plugin, writes the cordis.yml
  // row and the restart-honest activation state; upgrade() preserves
  // operator patch rows; uninstall() retains the store.
  const profile = await mkdtemp(path.join(os.tmpdir(), 'zmem-ds-profile-'));
  let profileOk = false;
  let upgradeOk = false;
  let uninstallRetained = false;
  let installModuleUsed = false;
  let operatorPatchSurvived = null;
  const installer = await import(pathToFileURL(
    path.join(pluginDir, 'install.mjs')).href).catch(() => null);
  try {
    const pkgDir = installer
      ? installer.PACKAGE_NAME : 'zmem-deepseek';
    const copy = path.join(profile, 'plugins', pkgDir);
    if (installer && existsSync(pluginDir)) {
      const res = installer.install(profile, pluginDir);
      installModuleUsed = Boolean(res && res.ok
        && res.restartRequired === true);
    } else if (existsSync(pluginDir)) {
      await cp(pluginDir, copy, { recursive: true, dereference: true });
    }
    await mkdir(path.join(profile, 'data'), { recursive: true });
    if (!installer) {
      await writeFile(path.join(profile, 'cordis.yml'),
        "plugins:\n  - name: '@zaxbyhub/zmem-deepseek'\n    config: {}\n", 'utf8');
    }
    const storeMarker = path.join(profile, 'data', 'store.sqlite');
    await writeFile(storeMarker, 'uninstall-retains-store marker\n', 'utf8');
    let noSymlinks = true;
    const walk = async (dir) => {
      for (const name of await readdir(dir)) {
        const full = path.join(dir, name);
        const st = lstatSync(full);
        if (st.isSymbolicLink()) { noSymlinks = false; return; }
        if (st.isDirectory()) await walk(full);
      }
    };
    if (existsSync(copy)) await walk(copy);
    profileOk = existsSync(path.join(copy, 'index.mjs'))
      && (await readFile(path.join(profile, 'cordis.yml'), 'utf8')).includes('@zaxbyhub/zmem-deepseek')
      && noSymlinks;
    // Upgrade through the shipped module: a user patch row (a file the
    // package does not ship) must SURVIVE the upgrade copy.
    if (installer && existsSync(copy)) {
      await writeFile(path.join(copy, 'operator-patch.json'),
        '{"note": "user patch row"}\n', 'utf8');
      const up = installer.upgrade(profile, pluginDir);
      operatorPatchSurvived = Boolean(up && up.ok
        && (up.preserved || []).includes('operator-patch.json'));
    }
    // Fake upgrade: bump ONLY the profile copy's version. The canonical
    // release surface must never move (upgrade stays inside the profile).
    const copyManifest = path.join(copy, 'plugin.json');
    if (existsSync(copyManifest)) {
      const bumped = JSON.parse(await readFile(copyManifest, 'utf8'));
      bumped.version = '999.0.0-fake-upgrade';
      await writeFile(copyManifest, JSON.stringify(bumped, null, 2) + '\n', 'utf8');
      const canonical = await readJson(path.join(pluginDir, 'plugin.json'));
      upgradeOk = Boolean(canonical && canonical.version === releaseVersion
        && bumped.version === '999.0.0-fake-upgrade');
    }
    // Uninstall through the shipped module: removes the plugin copy and
    // its cordis.yml row; the profile's store file remains.
    if (installer) {
      const un = installer.uninstall(profile);
      uninstallRetained = Boolean(un && un.ok && un.storeRetained)
        && existsSync(storeMarker);
    } else {
      await rm(copy, { recursive: true, force: true });
      uninstallRetained = existsSync(storeMarker);
    }
  } finally {
    await rm(profile, { recursive: true, force: true }).catch(() => {});
  }
  // Activation honesty: the plugin module import result (no ctx, no boot).
  let activationState = null;
  try {
    const mod = await import(pathToFileURL(pluginEntryPath()).href);
    activationState = { imported: typeof mod.default === 'function' };
  } catch (err) {
    activationState = {
      imported: false,
      error: String((err && err.message) || err).slice(0, 200),
    };
  }
  return {
    manifestVersion: (pluginJson && pluginJson.version) || null,
    manifestRuntime: (pluginJson && pluginJson.runtime) || null,
    releaseVersion,
    packageName: (packageJson && packageJson.name) || null,
    peerDependencies: (packageJson && packageJson.peerDependencies) || null,
    dependencies: (packageJson && packageJson.dependencies) || null,
    filesPresent, profileOk, upgradeOk, uninstallRetained,
    installModuleUsed, operatorPatchSurvived,
    activationState, driftCovered: null,
  };
}

const SCENARIOS = {
  first_request_delivery,
  backend_parity,
  identity_and_lifecycle,
  timing_and_policy,
  offline_snapshot,
  install_release_contract,
  native_canary,
  corrections_and_failures,
  retention_governance,
  reflection_and_closeout,
  reconnect_flush,
};

function finish(payload, code) {
  process.stdout.write(JSON.stringify(payload) + '\n', () => process.exit(code));
  setTimeout(() => process.exit(code), 500).unref();
}

function fatal(error) {
  finish({
    ok: false,
    scenario: process.env.SCENARIO || null,
    error: String((error && error.message) || error).slice(0, 600),
  }, 1);
}

setTimeout(() => fatal(new Error('watchdog: scenario exceeded 60 s')), 60000);

try {
  const name = process.env.SCENARIO;
  if (!name || !SCENARIOS[name]) {
    throw new Error(`unknown SCENARIO ${JSON.stringify(name)} (known: ${Object.keys(SCENARIOS).join(', ')})`);
  }
  log('scenario', name, 'plugin', pluginEntryPath());
  const observed = await SCENARIOS[name]();
  finish({ ok: true, scenario: name, observed }, 0);
} catch (err) {
  log('fatal:', (err && err.stack) || err);
  fatal(err);
}
