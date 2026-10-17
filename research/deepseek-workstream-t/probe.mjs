// Model-free probes against published production DeepSeek packages.
import assert from 'node:assert/strict';
import { writeFile, mkdir, readFile } from 'node:fs/promises';
import { Context } from '@deepseek-ai/cordis';
import LlmRuntime, { LlmAdapter, createUserMessage } from '@deepseek-ai/dsh-llm';
import SessionStore, { SessionId } from '@deepseek-ai/dsh-session';
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import AgentRegistry from '@deepseek-ai/dsh-agent';
import AgentLoop from '@deepseek-ai/dsh-agent-loop';

const MEMORY = 'SYNTHETIC_ZMEM_CONTEXT: use a bounded request deadline.';
const msg = (text, kind = 'zmem-prep') => createUserMessage({
  content: [{ type: 'text', text }], source: { kind },
});
class ScriptedAdapter extends LlmAdapter {
  requests = [];
  async resolveModel(provider, model) { return { provider, id: model, name: model }; }
  async *stream(options) {
    assert(this.requests.length < 8, 'unexpected model-request loop');
    this.requests.push(JSON.parse(JSON.stringify(options.messages)));
    yield { type: 'block-start', index: 0, blockType: 'text' };
    yield { type: 'text-delta', index: 0, text: 'done' };
    yield { type: 'block-end', index: 0, block: { type: 'text', text: 'done' } };
    yield { type: 'finish', reason: { kind: 'stop' } };
  }
}
async function mount(run) {
  const ctx = new Context();
  try {
    for (const plugin of [LlmRuntime, SessionStore, SessionProjectionRegistry, SystemPrompt, ToolRuntime, AgentRegistry]) {
      await ctx.plugin(plugin);
    }
    await ctx.plugin(AgentLoop, { agents: [] });
    const adapter = new ScriptedAdapter();
    ctx.llm.registerAdapter(['prep'], adapter);
    const events = [];
    ctx.on('session/event', (session, event) => {
      assert(events.length < 300, 'event cap');
      events.push({ session: String(session.id), type: event.type });
    });
    return await run(ctx, adapter, events);
  } finally { await ctx.fiber.dispose(); }
}
const create = (ctx, id) => ctx.agentLoop.create(SessionId(id), { provider: 'prep', model: 'scripted' });
async function send(agent, text) {
  agent.followup(msg(text, 'user'));
  await agent.whenIdle();
}
const contains = (request, text) => JSON.stringify(request).includes(text);
async function appendMemory(next, signal) {
  const decision = await next();
  if (decision.kind !== 'enter' || signal?.aborted) return decision;
  return { ...decision, messages: [...decision.messages, msg(MEMORY)] };
}
const results = [];
const fixtures = { normalization: 'Message ids are assigned per case in first-observed order; no timing fields are retained.' };
function normalizedRequests(requests) {
  const ids = new Map();
  return requests.map(request => request.map(message => {
    if (!ids.has(message.id)) ids.set(message.id, 'message-' + (ids.size + 1));
    return { ...message, id: ids.get(message.id) };
  }));
}

await mount(async (ctx, adapter, events) => {
  let ready = false;
  ctx.on('agent/created', async () => { await Promise.resolve(); ready = true; });
  ctx.on('agent/pre-step', async ({ signal }, next) => {
    assert(ready, 'created initialization was not awaited');
    const decision = await next();
    fixtures.raw_pre_append_decision = {
      kind: decision.kind,
      startsRequestSeries: decision.startsRequestSeries ?? null,
      keys: Object.keys(decision).sort(),
    };
    const returned = await appendMemory(async () => decision, signal);
    assert.equal(returned.startsRequestSeries, decision.startsRequestSeries);
    return returned;
  });
  const agent = await create(ctx, 'prep-current-step');
  await send(agent, 'Probe current-step delivery');
  assert.equal(adapter.requests.length, 1);
  assert(contains(adapter.requests[0], MEMORY));
  assert(agent.session.snapshotEvents().some(e => e.type === 'user/message' && JSON.stringify(e.data).includes(MEMORY)));
  fixtures.current_step = normalizedRequests(adapter.requests);
  fixtures.event_types = events;
  results.push({ id: 'current-step', passed: true, requests: 1, context_in_first_request: true, context_logged: true });
});

await mount(async (ctx, adapter) => {
  let injected = false;
  ctx.on('agent/pre-step', async ({ agent }, next) => {
    if (!injected) { injected = true; agent.inject(msg(MEMORY)); }
    return next();
  });
  const agent = await create(ctx, 'prep-queued-context');
  await send(agent, 'Probe queued delivery');
  assert.equal(adapter.requests.length, 2);
  assert(!contains(adapter.requests[0], MEMORY));
  assert(contains(adapter.requests[1], MEMORY));
  fixtures.queued_context = normalizedRequests(adapter.requests);
  results.push({ id: 'inject-inside-pre-step', passed: true, requests: 2, context_in_first_request: false, context_in_second_request: true });
});

await mount(async (ctx, adapter) => {
  let rejectCalls = 0;
  const rejected = { kind: 'reject' };
  ctx.on('agent/pre-step', async ({ signal }) => {
    const returned = await appendMemory(async () => { rejectCalls++; return rejected; }, signal);
    assert.strictEqual(returned, rejected);
    return returned;
  });
  const agent = await create(ctx, 'prep-rejected');
  await send(agent, 'Rejected input');
  assert.equal(adapter.requests.length, 0);
  assert.equal(rejectCalls, 1);
  assert(!agent.session.snapshotEvents().some(e => e.type === 'user/message'));
  results.push({ id: 'rejection', passed: true, requests: 0, user_messages_logged: 0, controlled_inner_rejections: rejectCalls });
});

await mount(async (ctx, adapter) => {
  let started;
  const entered = new Promise(resolve => { started = resolve; });
  ctx.on('agent/pre-step', async ({ signal }, next) => {
    const decision = await next();
    started();
    await new Promise(resolve => {
      if (signal.aborted) resolve();
      else signal.addEventListener('abort', resolve, { once: true });
    });
    return decision.kind === 'enter' ? { ...decision, messages: [...decision.messages, msg(MEMORY)] } : decision;
  });
  const agent = await create(ctx, 'prep-cancelled');
  agent.followup(msg('Cancel before admission', 'user'));
  await entered;
  agent.cancel({ kind: 'user' });
  await agent.whenIdle();
  assert.equal(adapter.requests.length, 0);
  assert(!agent.session.snapshotEvents().some(e => e.type === 'user/message'));
  results.push({ id: 'cancel-before-admission', passed: true, requests: 0, user_messages_logged: 0 });
});

await mount(async (ctx, adapter) => {
  ctx.on('agent/pre-step', async ({ agent }, next) => {
    const decision = await next();
    return decision.kind === 'enter' ? { ...decision, messages: [...decision.messages, msg('CONTEXT_FOR_' + agent.id)] } : decision;
  });
  const a = await create(ctx, 'prep-agent-a');
  const b = await create(ctx, 'prep-agent-b');
  await send(a, 'Input A'); await send(b, 'Input B');
  assert.equal(adapter.requests.length, 2);
  assert(contains(adapter.requests[0], 'CONTEXT_FOR_prep-agent-a'));
  assert(!contains(adapter.requests[0], 'CONTEXT_FOR_prep-agent-b'));
  assert(contains(adapter.requests[1], 'CONTEXT_FOR_prep-agent-b'));
  assert(!contains(adapter.requests[1], 'CONTEXT_FOR_prep-agent-a'));
  results.push({ id: 'two-agent-isolation', passed: true, requests: 2, cross_agent_context: false });
});

const pkg = JSON.parse(await readFile(new URL('./node_modules/@deepseek-ai/dsh-agent-loop/package.json', import.meta.url), 'utf8'));
assert.equal(pkg.version, '0.2.1-rc.2');
const report = { schema: 'zmem-deepseek-preparation-v1', node: process.version, runtime: pkg.version,
  source_commit: '687f282e8625c6b8d11ff58211a90524be2e4317',
  model: 'scripted-in-process-no-network', results,
  limitations: ['No real provider, desktop/web carrier, broker, ZMem selector, persisted resume/fork, or compaction backend was exercised.'] };
await mkdir(new URL('./results/', import.meta.url), { recursive: true });
await writeFile(new URL('./results/probe-results.json', import.meta.url), JSON.stringify(report, null, 2) + '\n');
await writeFile(new URL('./fixtures/runtime-delivery.json', import.meta.url), JSON.stringify(fixtures, null, 2) + '\n');
console.log(JSON.stringify(report, null, 2));
