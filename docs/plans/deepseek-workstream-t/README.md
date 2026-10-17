# DeepSeek Harness additions to Workstream T

Append two implementation slots to [Workstream T, epic #292](https://github.com/zaxbysauce/zmem/issues/292): **T-13, native DeepSeek Harness delivery and tools**, then **T-14, DeepSeek learning and operational parity**. They extend the fleet parity contract rather than introducing another memory backend or a separate workstream.

This is preparation, not a support announcement. The runnable probes use the published DeepSeek production agent-loop packages with a scripted model. They do not install a plugin into the user's harness, contact a model provider, or modify the canonical ZMem store. Reference snapshot prepared before filing T-13 and T-14. Consult epic #292 for the live issue assignments; no implementation PR is included.

## Proposed sequence

| Slot | Scope | Must merge first | Additional coordination |
|---|---|---|---|
| T-13 | Cordis bundle, broker-backed automatic recall and explicit tools, identity, delivery acknowledgement, installation, release coverage, native canary | T-10 #289 and T-11 #290 | Rebase on the merged T-02 through T-10 contracts. Reconcile delivery concurrency #362 and release integrity #359 with their owners. |
| T-14 | Corrections, tool-failure evidence, reflection/closeout, retention, source resolution, fleet diagnostics and full support claims | T-13, T-12 #291, K-06 #177 | Preserve the #177 normalized evidence and review-queue boundary. Run schema compatibility checks if K-07 #178 lands first. |

T-10 already depends on the typed queue and broker; the broker depends on shared authorization policy, delivery reservations, protocol negotiation, and deadlines. T-13 should consume those settled contracts, not implement a second transport. T-11 is an explicit dependency because the new adapter's declared offline behavior includes the shared snapshot path. T-14 consumes T-12's operational diagnostics, so T-12 does not acquire a dependency on DeepSeek and no cycle is introduced.

Keep T-01 through T-12 and in-flight PRs #365, #370, and #372 unchanged. Update the epic with two appended rows and explain that older child titles retain their original “of 12” numbering. T-13 extends the generated fleet matrix and its exact-set tests for the native adapter; this preparation matrix does not alter the production support set.

## Executed runtime probes

Runtime: published `@deepseek-ai/dsh-agent-loop@0.2.1-rc.2`, pinned with the companion packages in the probe lockfile. Source reference: DeepSeek commit `687f282e8625c6b8d11ff58211a90524be2e4317`. The npm release is version-bound; this does not claim a byte-for-byte reproducible build from that source commit.

| Probe | Observed result |
|---|---|
| Current accepted pre-step | Appending a plugin-sourced user message to the decision returned by `await next()` put context in the **first** model-bound request and the in-memory session's `user/message` log. The raw decision is captured in the fixture; append preserves its `startsRequestSeries` value. |
| `agent.inject()` inside pre-step | Context missed request 1 and appeared in request 2. The queued context caused an additional model step. |
| Rejected pre-step | A controlled inner rejection passed through the same append wrapper unchanged; the runtime made no model request and admitted no user-message record. |
| Cancellation before admission | No model request and no admitted user-message record, even though the callback had prepared context. Selection is not proof of delivery. |
| Sequential independent agents in one runtime | Each request contained only its own agent's context. Concurrent subagents were not exercised. |

The initial probe run used the wrong teardown API and failed; the corrected driver uses `await ctx.fiber.dispose()`. The final five probes pass. Every context is disposed and the bounded process receipt verifies descendant cleanup. Synthetic request/history shapes are in [runtime-delivery.json](../../../research/deepseek-workstream-t/fixtures/runtime-delivery.json); the driver and reproduction instructions are in [the probe directory](../../../research/deepseek-workstream-t/README.md).

Persisted resume/fork, actual compaction, native tools, desktop/Web/headless carriers, the broker, and the real ZMem selector remain implementation acceptance tests. A scripted model request is not proof that a real provider used a memory or that a full harness installation works.

## Adapter decisions

- Canonical harness/lane: `deepseek`; product command: `dsh`. Reuse native `agent.id` and T-05's principal/harness/session/subagent identity contract. Do not generate an unrelated UUID per hook or use process-global cwd as agent identity.
- Startup context and prompt recall enter the **first accepted pre-step**. Await `next()`, preserve `reject`, preserve all decision fields, and append a separately sourced message only on `enter`. Do not mutate the supplied frozen messages, use `agent/request` to change content, or queue same-step memory through `agent.inject()`.
- Consume the broker/store's canonical fenced `rendered` text. The adapter owns no ranking, row rendering, SQLite access, new schema, or model API implementation. Trusted Tier-0 instructions and untrusted retrieved rows retain their established distinction.
- Acknowledge reservations against the specific admitted context identity after the durable admission is observed. Own and drain asynchronous acknowledgements. A selected envelope, queued injection, callback return, and provider consumption are different events; the documented guarantee must stop at the boundary actually observed.
- `tools/pre-execute` can collect operation context, but context queued there cannot change the model decision that selected the current tool. Claim only next-request guidance unless a separate executable probe proves a stronger contract. Native batch recall is an adapter aggregation over a completed step, not an invented DeepSeek `PostToolBatch` event.
- Observe compaction markers synchronously, then serialize reset/rehydration before the next accepted request. `session/event` is a post-commit notification, not an awaited hook for asynchronous cleanup. Turn end and agent disposal do not prove persisted-session termination.
- Prefer public session APIs/exports for provenance. Any cold native-log parser must handle the supported versioned format and refuse unsupported generations; it must not treat DeepSeek JSONL as Claude JSONL.
- Installation design, unverified against a running 0.2.1-rc.2 installation: use native profile/bundle handling and allow restart without HMR. Give it separate discovery/installation handling; do not add its name to the Claude/ZCode registry codec. Package version discovery and runtime drift hashing need explicit `package.json`/new-directory coverage.

## Reviewable artifacts

- [T-13 proposed issue](T13-native-adapter.md)
- [T-14 proposed issue](T14-learning-parity.md)
- [Preparation capability matrix](capability-matrix.json)

Implementation support claims require a native canary exercising actual model-bound delivery, shared-store behavior, explicit writes, cancellation/acknowledgement, compaction/resume/fork, subagent isolation, offline behavior, redaction, installation, and teardown. The current probe result supports five narrowly stated lifecycle properties only.
