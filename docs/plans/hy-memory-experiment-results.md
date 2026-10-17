# HY Memory experiments against ZMem - 2026-10-17

Recommendation: **one separate PR for conservative subject-compatible dedup; no separate lineage subsystem PR now**. An opt-in model curator deserves a separate evaluation/prototype, with Tencent's writer as one candidate, before a production integration PR. None of this expands Workstream T or changes production behavior.

## Evidence and boundaries

Executed on checkout `91996f29a3e1fcb4a489b37edca5290a810f7772`, with real ZMem SQLite schema, writer, update, evidence and episode APIs. Every store was created in an owned temporary directory under the research results directory and removed after completion. The canonical store was never opened. The only shared runtime asset read was the cached MiniLM model/tokenizer. No HY model was downloaded or served.

Drivers: [policy/lineage experiments](../../research/hy-memory-experiments/run_experiments.py), [real semantic experiment](../../research/hy-memory-experiments/real_semantic.py). Generated JSON results and bounded process receipts are retained locally, not published. All source hashes are recorded by the policy driver. Rebase/replay against current main before implementation; this is a checkout-bound experiment, not a merge-readiness result.

The first policy run exceeded the 512 MiB process budget while optional native ML dependencies were available. No replacement was launched before terminal cleanup. The model-absent driver now explicitly denies optional ML imports, and completed at roughly 25 MiB. A separate deliberate real-model run uses the known 90 MB cached ONNX model, two intra-op threads and one inter-op thread, with a finite 1 GiB/process and 1.5 GiB/job budget. It completed near 335 MiB. A second early driver run attempted to tombstone an already updated row; ZMem correctly refused. The corrected driver uses the replacement for the retraction probe.

## Subject-aware dedup

Six constructed pairs were each run with current policy and an experimental explicit-subject guard. Both the exact-cosine test index and the **normal sqlite-vec path** produced the same decisions using the cached real MiniLM model, SHA-256 `bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5`.

| Pair | Real cosine | Current writer | Experimental subject guard |
|---|---:|---|---|
| Different task closeouts: deadline vs cancellation | 0.948711 | One row | Two rows |
| Same wording, issue 41 vs issue 42 | 0.976841 | One row | Two rows |
| Same fix template, client.py vs worker.py | 0.900250 | One row | Two rows |
| Same-task paraphrase | 0.763633 | Two rows | Two rows |
| Identical general lesson | 1.000000 | One row | One row |
| Unknown subject vs known subject, identical text | 1.000000 | One row | One row |

The three different-task/file pairs exceed the production 0.85 threshold. The original second statement is not retained as a new memory row. A separate exact-match probe showed both task tags and both evidence associations on the survivor, while only its original source reference remained. Namespace separation already prevents cross-namespace dedup and remained intact with the guard.

The ablation supplies **oracle subject identities** in the driver. It does not test a production extractor, add a subject column, or establish false-merge prevalence. The same-task paraphrase remains below threshold: the guard does not improve missed-duplicate recall. The controlled semantic-candidate cases in the policy driver are separately labelled and must not be counted as measured model results.

**PR warranted:** protect explicitly incompatible task/fact identities before destructive dedup. Start with high-precision repository-qualified issue/task identity, rather than treating every different session, source reference, entity, tag or mentioned file as a conflicting subject. General reusable lessons should still merge across tasks; missing/ambiguous identity should preserve documented compatibility. The file pair is evidence of the mechanism, not justification for a universal different-file veto.

Scope must cover add, update, ingest and maintenance consolidation through a shared compatibility rule; checking only one hook or only semantic matching leaves bypasses. A filtered nearest neighbor must not hide a compatible runner-up; exact fallback and ingest caches must also honor the rule. Preserve polarity/taint/authz checks and original evidence. Acceptance needs the measured cases above plus qualified-id parsing, multi-subject facts, namespace collisions, generic lessons with different task origins, same-subject duplicates, missing identity, compatible runner-up, update/ingest/consolidate parity, and deterministic no-model behavior. No threshold reduction or general dedup redesign is justified by this corpus.

## Evidence-to-lesson lineage

The executed lineage probe created a task fact and a separately reviewed lesson sharing original test evidence, then updated task progress with different evidence.

- Original task evidence survived; the replacement had its own evidence and `update_of` pointed to the original task.
- The lesson retained its original evidence, signal and confidence. Updating task state did not manufacture a new test result for the lesson.
- Historical episode membership survived. The replacement was not automatically added to the old episode; an explicit existing `episode_add` restored discovery of the lesson from current task progress.
- Retracting current task progress left the lesson and original evidence addressable. This is not proof the lesson remains valid: a reviewed derivation/retraction policy is distinct from preserving evidence bytes.

**No separate lineage storage PR now.** Existing `memory_evidence`, `episode_memory`, and `update_of` support the demonstrated preservation. Add equivalent acceptance fixtures when K-06 #177's normalized-evidence/review-queue consumer is implemented. Automatic replacement membership or dependent-lesson review is a future product decision if a concrete workflow needs it; it is not an existing append-only API defect. Do not silently reparent evidence or automatically invalidate every lesson when task progress changes.

## Optional larger-model curator

Tencent describes [Hy-Memory-30B-A3B](https://huggingface.co/tencent/Hy-Memory-30B-A3B) as a long-term-memory writer with about 3B active parameters, trained for retention/extraction and reconciliation across several memory frameworks. Its evaluation changes the writer with retrieval and answer components fixed, on fixed 20% benchmark subsamples. This motivates a ZMem evaluation; it does not demonstrate ZMem quality or safe autonomous merging. The [published BF16 repository](https://huggingface.co/tencent/Hy-Memory-30B-A3B/tree/main) is about 60.1 GB. Active parameters are not the resident-weight footprint. No model-quality, throughput, hardware-fit or quantization result was measured here.

ZMem already has `organize`/`consolidate --belief-heads --llm-local`; the built-in adapter defaults to no actions. The executed seam probe created one belief head, invoked an injected no-op adapter once with source/evidence ids and bounded source rows, and applied zero actions. The allowed operations are grounded belief-head edits (`replace_quote`, `add_source`, `mark_contested`, `retract_source`), not arbitrary trusted-memory synthesis. Tencent's example `memories` JSON is therefore not a drop-in action contract.

Proposed opt-in design:

1. A maintenance-only curator provider configured with an explicit endpoint and model/revision. Default off; no endpoint call on add/recall/injection. Local/private serving is the natural first deployment, with remote evidence transmission separately opt-in.
2. Snapshot a bounded authorized candidate set, redacted evidence and revision stamps. Run inference **outside the writer transaction**. Today's belief maintenance calls its adapter inside `BEGIN IMMEDIATE`; putting a slow network model directly there would extend writer contention.
3. Return typed proposals: keep-distinct, duplicate candidate, supersession candidate, collated lesson or review-needed, each with original memory/evidence ids and a rationale. Validate shape, ids, namespace, scope, size and model-output trust before enqueueing. A model confidence score never becomes a `test` or `reviewer` signal.
4. Reuse reviewed promotion for novel lessons and destructive changes. Recheck source revisions and subject compatibility before applying; stale snapshots produce a review/retry outcome, not a blind merge. Preserve all original evidence edges, operation ids and pending/committed receipts.
5. Fixed per-run row/token/output/request/time budgets, explicit cancellation and no-action failure. Record model revision, prompt version, proposed/accepted actions and changes in provenance without logging raw sensitive evidence.

Evaluate current deterministic behavior, a general-purpose writer and Tencent's model on the same synthetic plus held-out corpus. Measure distinct-task false merges, true duplicates missed, unsupported claims, evidence-id precision, contradiction/retraction handling, accepted useful lessons, tokens, latency and cost. Include prompt injection, fabricated ids, mixed namespaces, conflicting subjects and stale proposals. Select the model by these results; keep the transport/model choice pluggable.

## Disposition ledger

| Item | Disposition | Re-evaluation trigger |
|---|---|---|
| Distinct explicit task facts collapse at real high similarity | Recommend a separate bounded dedup PR; implementation policy still needs the high-precision subject contract above. No issue filed. | Before coding, replay at current main and freeze cross-surface checks. |
| Evidence preservation and lesson lineage | RECORD: existing primitives passed; no new subsystem needed. | K-06 consumer fails an equivalent end-to-end fixture. |
| Replacement not automatically in an episode | RECORD: explicit append-only membership is the current API contract. | Maintainer chooses automatic task-chain discovery semantics. |
| Source retraction does not auto-review every linked lesson | RECORD: no typed derivation dependency policy was tested or promised. | A reviewed derivation contract and concrete affected workflow are specified. |
| Same-task paraphrase below current dedup threshold | RECORD: one constructed miss does not justify threshold changes. | Held-out precision/recall evaluation establishes a better policy. |
| Opt-in Tencent/general-model curator | Recommend a separate evaluation/prototype, not a production claim. No inference endpoint was supplied or model served. | Candidate writer passes the shared ZMem evaluation and review boundary. |

No issue, PR, automation, deployment or production/store change was made for these experiments.

## Review status

Primary verified the generated measurements, native sqlite-vec result, cited source and absence of production-file diffs. An independent read-only GLM 5.3 high-effort critique was attempted with a 600-second deadline and bounded 1 GiB/process, 2 GiB/job limits. The runner terminated it after 16.45 seconds with `HOST_MEMORY_GUARD`; descendant cleanup was verified and stdout contained no verdict. No critic approval is claimed and no heavier/fallback worker was launched. Re-run the independent critique when host memory admission is healthy before treating the proposed architecture as implementation-approved.
