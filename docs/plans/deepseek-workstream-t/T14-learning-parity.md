# Proposed Workstream T PR 14 of 14

**Issue title:** [Workstream T] PR 14 of 14: DeepSeek capture, retention, provenance and fleet support parity

**Parent:** [#292](https://github.com/zaxbysauce/zmem/issues/292). Implementation contract appended to Workstream T. One issue equals one implementation PR.

## Outcome

DeepSeek participates in the existing evidence-to-review-to-memory pipeline, and supported fleet installations can be diagnosed and verified. It consumes the shared retention engine rather than introducing a DeepSeek-only extractor or allowing unreviewed tool output to become trusted memory.

## Dependencies

- **Depends on:** T-13, native DeepSeek delivery and explicit tools.
- **Depends on:** T-12 [#291](https://github.com/zaxbysauce/zmem/issues/291), fleet installation and operational diagnostics.
- **Depends on:** K-06 [#177](https://github.com/zaxbysauce/zmem/issues/177), normalized evidence, evidence-gated retention, and the existing bounded review queue.
- Rebase and freeze acceptance checks after all dependencies merge. If K-07 #178 or K-08 #179 lands first, test compatibility through the shared backend; neither is an automatic prerequisite for this adapter.

## Scope

Capture original user corrections and immutable final tool outcomes through the native agent/tool APIs. Distinguish user input from plugin context, tools, replayed history, and compaction summaries. Normalize tool identity and evidence coordinates at the client; apply bounded redaction before persistence, spooling, or transport. Use the shared T-09 typed queue and canonical-id receipts.

Feed normalized evidence into #177's review-queue path with stable original provenance. Keep `auto_retain` default off and honor the shared store setting/override. Reflection uses an explicit eligibility predicate and per-turn/event idempotency so no host continuation can recursively trigger itself. Any continuation required to show a nudge has a fixed maximum and is part of the canary; ordinary idle context injection must not be mistaken for immediate delivery.

Make memory and closeout skills discoverable through DeepSeek's supported skill provider, with tested profile roots and visible invocation names. Writes retain the existing evidence/signal/taint validation and review policy. Register any skill-promotion target deliberately and test collisions/approval; do not infer that copying skill files establishes working closeout behavior.

Support source resolution, mining and evidence inspection through an explicitly versioned DeepSeek session export or bounded native parser. Preserve original session/event identity and distinguish durable history from the currently projected conversation. Reject unsupported generations, unsafe paths and malformed records. Do not read a remote machine's local paths on the hub.

Extend T-12 doctor/fleet diagnostics with installed runtime/bundle version, selected profile, activation/restart status, backend/principal resolution, compatibility, snapshot state, pending queue, and privacy-safe provenance diagnostics. Full support documentation follows the native end-to-end parity canary.

## Acceptance criteria

1. **AC1 — Corrections and failures.** Synthetic direct user corrections and actual failed tool outcomes produce bounded, redacted, correctly attributed review candidates. Injected ZMem text, replayed records, successful tools, denials and cancellation are not blindly classified as tested lessons. Secret-shaped fixture values never survive into queue, logs, rendered output, or network payloads.
2. **AC2 — Retention governance.** The shared #177 extractor is called through its normalized boundary; `auto_retain=off` leaves automatic retention off. Replayed event ids do not duplicate candidates. No capture path bypasses review or escalates caller-claimed signal/confidence/taint.
3. **AC3 — Reflection and closeout.** Skill discovery and real invocation work on supported profiles. No eligible evidence produces no continuation. Repeated stop notifications and model continuations produce at most the documented bounded nudge, and no recursive loop or unbounded queue. Approved closeout writes return the backend's actual pending/committed receipt.
4. **AC4 — Provenance.** Native session fixtures cover tool failure, correction, edit, resume, fork and compaction. Source lookup resolves the intended bounded evidence; it cannot reinterpret the format as Claude JSONL, follow unsafe paths, mix agent scopes, or silently accept an unsupported format generation.
5. **AC5 — Recovery and fleet diagnostics.** Disconnect/reconnect, queue replay, revocation, snapshot expiry, skew, read-your-writes and shutdown produce the T contract's declared behavior. `doctor --fleet` identifies an inactive/incompatible DeepSeek plugin and avoids exposing credentials or raw sensitive evidence.
6. **AC6 — Compatibility and claim gate.** Run targeted existing harness contracts plus the native DeepSeek full canary on the supported platform/carrier matrix. Document tested floors and capability gaps. First-class support claims are allowed only for combinations with passing evidence; a single Windows model-free probe does not establish macOS/Linux/Desktop/Web parity.

Planned discriminating checks belong in `tests/test_t14_deepseek_learning.py`, source-contract fixtures, and the native canary. They do not exist yet and no red/green implementation evidence is claimed. Re-freeze on the actual merged dependency tree. Keep release coverage/version alignment from T-13 current.

## Exclusions

No new retention engine, automatic LLM promotion of every session into trusted facts, fact-store duplication, HY Memory runtime dependency, or coding-diff storage feature. Those core-memory experiments are assessed separately and must not enlarge this adapter PR.
