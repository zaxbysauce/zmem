// tests/fixtures/deepseek/helpers.mjs — frozen shared helpers for the
// DeepSeek adapter scenario driver (scenario.mjs, issues #374/#375).
//
// Arm's-length check companion: authored BEFORE the implementation, code
// against the frozen seams only (.agents/issue-traces/374-375-deepseek-
// adapter/interface-freeze.md). The driver only OBSERVES and reports one
// JSON line; it never decides pass/fail except fatal boot errors, which
// exit 1 with {"ok":false,"error":...} (the expected RED at base: the
// plugin import fails because deepseek-plugin/index.mjs does not exist).
//
// Dependency-free besides the pinned @deepseek-ai runtime packages
// (resolved from deepseek-plugin/node_modules, or the dir named by env
// ZMEM_DEEPSEEK_RUNTIME — either a node_modules dir itself or a dir that
// contains one). All logging goes to stderr; stdout carries exactly ONE
// JSON line (written by scenario.mjs).
//
// Runtime API pinned at @deepseek-ai 0.2.1-rc.2 (the prep probe pattern,
// research/deepseek-workstream-t/probe.mjs at 53bb5a6): new Context();
// await ctx.plugin(<8 services>); ctx.llm.registerAdapter; ctx.on(
// 'agent/pre-step' | 'session/event' | 'tools/pre-execute' |
// 'tools/result'); ctx.agentLoop.create(SessionId(id), {provider, model});
// await ctx.fiber.dispose() teardown.

import { existsSync, readFileSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const DRIVER_DIR = path.dirname(fileURLToPath(import.meta.url));

// The frozen driver<->plugin message source kind (the plugin declares its
// own source.kind and the frozen contract pins the literal "zmem").
export const ZMEM_SOURCE_KIND = 'zmem';

export function log(...parts) {
  process.stderr.write('[zmem-scenario] ' + parts.map((p) =>
    (typeof p === 'string' ? p : JSON.stringify(p))).join(' ') + '\n');
}

export function repoRoot() {
  return process.env.ZMEM_ROOT
    || path.resolve(DRIVER_DIR, '..', '..', '..');
}

export function pluginEntryPath() {
  return process.env.ZMEM_DEEPSEEK_PLUGIN
    || path.join(repoRoot(), 'deepseek-plugin', 'index.mjs');
}

function runtimeBaseCandidates() {
  const out = [];
  if (process.env.ZMEM_DEEPSEEK_RUNTIME) out.push(process.env.ZMEM_DEEPSEEK_RUNTIME);
  out.push(path.join(repoRoot(), 'deepseek-plugin', 'node_modules'));
  return out;
}

// spec '@deepseek-ai/dsh-llm' -> { href, baseDir } for the package entry.
export function resolveRuntimePackage(spec) {
  const parts = spec.split('/');
  const tried = [];
  for (const base of runtimeBaseCandidates()) {
    for (const dir of [path.join(base, ...parts),
      path.join(base, 'node_modules', ...parts)]) {
      tried.push(dir);
      try {
        if (!existsSync(dir)) continue;
        const manifest = JSON.parse(
          readFileSync(path.join(dir, 'package.json'), 'utf8'));
        const entry = path.join(dir, manifest.main || 'lib/index.js');
        if (existsSync(entry)) {
          return { href: pathToFileURL(entry).href, baseDir: base, pkgDir: dir };
        }
      } catch { /* try the next candidate */ }
    }
  }
  throw new Error(`dsh runtime package ${spec} not resolvable (tried: ${tried.join('; ')})`);
}

const RUNTIME_PACKAGES = ['@deepseek-ai/cordis', '@deepseek-ai/dsh-llm',
  '@deepseek-ai/dsh-session', '@deepseek-ai/dsh-session-projection',
  '@deepseek-ai/dsh-system-prompt', '@deepseek-ai/dsh-tools',
  '@deepseek-ai/dsh-agent', '@deepseek-ai/dsh-agent-loop'];

export async function loadRuntime() {
  const resolved = RUNTIME_PACKAGES.map(resolveRuntimePackage);
  const mods = [];
  for (const r of resolved) mods.push(await import(r.href));
  const [cordis, llm, session, projection, systemPrompt, tools, agent,
    agentLoop] = mods;
  const rt = {
    baseDir: resolved[0].baseDir,
    pkgDirs: RUNTIME_PACKAGES.map((_, i) => resolved[i].pkgDir),
    Context: cordis.Context || (cordis.default && cordis.default.Context),
    LlmRuntime: llm.default, LlmAdapter: llm.LlmAdapter,
    createUserMessage: llm.createUserMessage,
    SessionStore: session.default, SessionId: session.SessionId,
    SessionProjectionRegistry: projection.default,
    SystemPrompt: systemPrompt.default,
    ToolRuntime: tools.default,
    defineTool: tools.defineTool,
    defineContentToolFixture: tools.defineContentToolFixture,
    AgentRegistry: agent.default,
    AgentLoop: agentLoop.default,
  };
  for (const [name, value] of Object.entries(rt)) {
    if (name !== 'baseDir' && name !== 'pkgDirs' && !value) {
      throw new Error(`dsh runtime export missing: ${name}`);
    }
  }
  return rt;
}

export function runtimePackageVersions(rt) {
  const out = {};
  for (const dir of rt.pkgDirs || []) {
    try {
      const pkg = JSON.parse(readFileSync(path.join(dir, 'package.json'), 'utf8'));
      out[pkg.name] = pkg.version;
    } catch { /* absent entry reported by key omission */ }
  }
  return out;
}

function firstText(message) {
  const content = message && message.content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.filter((b) => b && b.type === 'text' && typeof b.text === 'string')
    .map((b) => b.text).join(' ');
}

export function normalizeRequest(request) {
  const messages = (request && request.messages) || [];
  return {
    messages: messages.map((m) => ({
      role: (m && m.role) || null,
      sourceKind: (m && m.source && m.source.kind) || null,
      text80: (firstText(m).slice(0, 80)) || null,
    })),
    tools: Array.isArray(request && request.tools)
      ? request.tools.map((t) => t && t.name).filter(Boolean) : [],
  };
}

// A scripted adapter (probe pattern): one script entry consumed per model
// request. {text, finish} streams one text block; {toolCall:{name,args}}
// or {toolCalls:[...]} streams tool-call blocks then finishes tool-calls.
// Every request is recorded normalized (sourceKind + first 80 chars), so
// the python side can assert WHAT reached the model without the driver
// judging it. Requests are capped so a runaway loop fails loudly.
export function makeScriptedAdapter(rt, scripts) {
  const REQUEST_CAP = 24;
  class ScriptedAdapter extends rt.LlmAdapter {
    constructor() {
      super();
      this.requests = [];
      this.index = 0;
    }
    async resolveModel(provider, model) {
      return { provider, id: model, name: model };
    }
    async *stream(options) {
      if (this.requests.length >= REQUEST_CAP) {
        throw new Error('scenario model-request cap exceeded');
      }
      this.requests.push(normalizeRequest(options));
      const script = scripts[this.index] || { text: 'done' };
      this.index += 1;
      const calls = script.toolCalls
        || (script.toolCall ? [script.toolCall] : []);
      if (calls.length > 0) {
        for (let i = 0; i < calls.length; i += 1) {
          yield { type: 'block-start', index: i, blockType: 'tool-call' };
          yield {
            type: 'block-end', index: i,
            block: {
              type: 'tool-call',
              id: 'scenario-call-' + this.index + '-' + i,
              name: calls[i].name,
              arguments: JSON.stringify(calls[i].args || {}),
            },
          };
        }
        yield { type: 'finish', reason: { kind: 'tool-calls' } };
      } else {
        const text = script.text == null ? 'done' : script.text;
        yield { type: 'block-start', index: 0, blockType: 'text' };
        yield { type: 'text-delta', index: 0, text };
        yield { type: 'block-end', index: 0, block: { type: 'text', text } };
        yield { type: 'finish', reason: { kind: script.finish || 'stop' } };
      }
    }
  }
  return new ScriptedAdapter();
}

function sessionText(session) {
  if (session == null) return null;
  if (session.id != null) return String(session.id);
  return String(session);
}

export async function withRuntime(scripts, run) {
  const rt = await loadRuntime();
  const ctx = new rt.Context();
  const events = [];
  const toolEvents = [];
  let adapter = null;
  try {
    for (const plugin of [rt.LlmRuntime, rt.SessionStore,
      rt.SessionProjectionRegistry, rt.SystemPrompt, rt.ToolRuntime,
      rt.AgentRegistry]) {
      await ctx.plugin(plugin);
    }
    await ctx.plugin(rt.AgentLoop, { agents: [] });
    adapter = makeScriptedAdapter(rt, scripts);
    ctx.llm.registerAdapter(['zmem-scenario'], adapter);
    try {
      ctx.on('session/event', (session, event) => {
        if (events.length >= 300) return;
        const type = event && event.type;
        const record = { session: sessionText(session), type };
        if (type === 'user/message') {
          const data = (event && event.data) || {};
          record.sourceKind = (data.source && data.source.kind)
            || (event && event.source && event.source.kind) || null;
          record.text80 = (firstText(data.message).slice(0, 80)) || null;
        } else if (type === 'tool/result') {
          const data = (event && event.data) || {};
          record.isError = Boolean(data.error);
          record.toolText120 = (firstText(data.message).slice(0, 120)) || null;
        }
        events.push(record);
      });
      ctx.on('tools/pre-execute', async (payload, next) => {
        if (toolEvents.length < 100) {
          toolEvents.push({
            phase: 'pre-execute',
            name: (payload && (payload.name
              || (payload.call && payload.call.name))) || null,
          });
        }
        if (typeof next === 'function') return await next();
      });
      ctx.on('tools/result', (payload) => {
        if (toolEvents.length < 100) {
          toolEvents.push({
            phase: 'result',
            name: (payload && (payload.name
              || (payload.call && payload.call.name))) || null,
            isError: Boolean(payload && (payload.isError
              || (payload.result && payload.result.isError))),
          });
        }
      });
    } catch (err) {
      log('observer listener wiring failed:', err && err.message);
    }
    return await run({ ctx, rt, adapter, events, toolEvents });
  } finally {
    try {
      await ctx.fiber.dispose();
    } catch (err) {
      log('ctx.fiber.dispose failed:', err && err.message);
    }
  }
}

// Mount the plugin under test (deepseek-plugin/index.mjs via dynamic
// import, per the frozen driver contract). At base this import fails —
// the driver's fatal-boot exit 1 is the intended RED signal.
export async function mountPlugin(ctx) {
  const entry = pluginEntryPath();
  const mod = await import(pathToFileURL(entry).href);
  const fn = mod.default;
  if (typeof fn !== 'function') {
    throw new Error(`plugin default export at ${entry} is not a function`);
  }
  await ctx.plugin(fn, {});
  return entry;
}

export async function createAgent(ctx, rt, id) {
  return await ctx.agentLoop.create(rt.SessionId(id),
    { provider: 'zmem-scenario', model: 'scripted' });
}

// A user message whose source may carry EXTRA fields beyond kind
// (additive, issue #375 R2-6): the pinned runtime's createMessage spreads
// the input and only overrides the message id, so extra source fields
// (e.g. an explicit captureEventId) survive into the session event the
// plugin's capture payload reads.
export function userText(rt, text, kind = 'user', sourceExtra = null) {
  return rt.createUserMessage({
    content: [{ type: 'text', text }],
    source: sourceExtra ? { kind, ...sourceExtra } : { kind },
  });
}

export async function send(agent, rt, text, kind = 'user', sourceExtra = null) {
  agent.followup(userText(rt, text, kind, sourceExtra));
  await agent.whenIdle();
}

// The plugin's own state file (env ZMEM_DEEPSEEK_STATE) — the frozen
// result contract the python side asserts against.
export async function readPluginState() {
  const p = process.env.ZMEM_DEEPSEEK_STATE;
  if (!p || !existsSync(p)) return null;
  try {
    return JSON.parse(await readFile(p, 'utf8'));
  } catch (err) {
    return { unreadable: String((err && err.message) || err) };
  }
}

// Host-side toy tool (the DRIVER may register tools — that is host-side,
// not plugin surface). fail=true makes execute throw so the session gains
// a genuine tool-failure result the plugin can observe.
export function registerToyTool(ctx, rt, name, { fail = false } = {}) {
  return ctx.tools.register(rt.defineContentToolFixture({
    name,
    description: 'zmem frozen scenario probe tool',
    parameters: { text: { type: 'string' } },
    async execute(args) {
      if (fail) {
        // The failure text must be classifier-recognized (corrections.py
        // PROJECT_SPECIFIC_ERROR_PATTERNS): the capture gate only admits
        // classified tool errors, so a generic failure string would be
        // correctly dropped and the learning leg would see no failure row.
        throw new Error(
          `connection refused while running ${name} in the zmem scenario`);
      }
      return [{ type: 'text', text: `toy-ok ${String((args && args.text) || '').slice(0, 40)}` }];
    },
  }));
}

export function sliceFrom(requests, start) {
  return requests.slice(start).map((r) => ({
    messages: r.messages,
    tools: r.tools,
  }));
}

// Driver-side request-series probe (issue #374 AC1: the plugin must
// preserve an injected startsRequestSeries marker when it mutates the
// enter decision — a dropped marker would splice the zmem context into
// the user's message series). Cordis waterfall listeners run
// FIRST-REGISTERED-OUTERMOST (verified against the pinned cordis 4.0.5
// waterfall: cbs.shift() pops the earliest registration first, so a
// listener's next() returns what every LATER listener produced):
//
//   observer (registered FIRST, outermost): calls next(), records the
//     FINAL decision it receives from the inner listeners — WITHOUT
//     modifying it — and returns it untouched.
//   plugin (registered SECOND by the scenario): its next() returns the
//     marker-carrying decision below, so a correct plugin RECEIVES
//     startsRequestSeries: true and must preserve it through spreads.
//   injector (registered LAST, innermost): its next() returns the
//     runtime's BASE decision (before any listener appended context);
//     it records the base messages length and returns
//     {...base, startsRequestSeries: true} for ONE chosen turn, so every
//     OUTER listener — the plugin included — receives the marker.
//
// seriesPreserved is true iff the FINAL decision the outer observer
// received still carries the marker AND carries exactly one message more
// than the base decision (the plugin's single zmem message).
export function makeSeriesProbe() {
  const record = {
    markerInjected: false,
    finalStartsRequestSeries: null,
    seriesBaseMessages: null,
    finalMessages: null,
    seriesPreserved: null,
  };
  return {
    record,
    injector: async (payload, next) => {
      const base = await next();
      if (!base || base.kind !== 'enter') return base;
      if (!record.markerInjected) {
        record.markerInjected = true;
        record.seriesBaseMessages = Array.isArray(base.messages)
          ? base.messages.length : null;
        return { ...base, startsRequestSeries: true };
      }
      return base;
    },
    observer: async (payload, next) => {
      const decision = await next();
      if (!decision || decision.kind !== 'enter') return decision;
      if (record.finalStartsRequestSeries === null) {
        record.finalStartsRequestSeries =
          decision.startsRequestSeries === true;
        record.finalMessages = Array.isArray(decision.messages)
          ? decision.messages.length : null;
        record.seriesPreserved = (
          decision.startsRequestSeries === true
          && record.seriesBaseMessages !== null
          && record.finalMessages === record.seriesBaseMessages + 1
        );
      }
      return decision;
    },
  };
}
