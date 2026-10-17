# Memory maintenance research - cutoff 2026-10-17

Focused online state-of-the-art pass for ZMem's subject-compatible dedup and optional nightly curation. Only work available by October 17, 2026 is considered; this does not cover the rest of October. Primary papers, author repositories and official documentation are used. Published scores from different harnesses are not a comparable leaderboard and were not reproduced here.

## Findings that change the proposed issues

| Primary source | Verified contribution | Implication for ZMem / limit |
|---|---|---|
| [Hy-Memory-30B-A3B, Oct 15 2026](https://arxiv.org/html/2610.22196v1) | Specialized writer trained across memory frameworks, with writer/reader/judge separation, evidence-attributed reward, trap probes and storage-growth penalties. Main comparison uses fixed 20% benchmark subsets. | Evaluate Tencent as a candidate, freeze reader/retrieval/judge and measure unsupported storage as well as recall. Framework transfer does not establish ZMem quality. |
| [Tencent model card](https://huggingface.co/tencent/Hy-Memory-30B-A3B) and [files](https://huggingface.co/tencent/Hy-Memory-30B-A3B/tree/main) | Approximately 3B active parameters in a 30B model; BF16 repository about 60.1 GB; Apache-2.0 release. | Model serving remains explicit and opt-in. Active parameters are a compute distinction, not a 3B-weight memory requirement. No deployment or inference was performed in this session. |
| [CogMem, Oct 8 2026](https://arxiv.org/html/2610.11314v1) | Distinguishes source-attributed claims from facts/events; reconciles temporally scoped views and preserves provenance. Includes a semantic-collapse probe. | Subject, provenance and temporal compatibility must constrain dedup. Similarity is candidate discovery, not sufficient identity. Do not copy a new graph schema merely to add the guard. |
| [Sleep-time Compute, Apr 2025](https://arxiv.org/html/2504.13171v1) | Moves context processing before queries; studies amortization and query predictability, with math tasks and a SWE case study. | Budget offline processing and measure downstream benefit versus total nightly cost. This is not evidence that all-night inference or arbitrary forgetting improves ZMem. |
| [Letta Memory & dreaming, inspected Oct 17 2026](https://docs.letta.com/configuration/memory/) | Background memory consolidation, optional agent review, and backups before larger reorganizations. | Separate interactive operation from background maintenance and retain review/recovery artifacts. Agent review is not human authorization or independent ground truth. |
| [A-MEM, 2025](https://arxiv.org/abs/2502.12110) | Structured notes, dynamic linking and memory evolution. | Evaluate linking/representation changes separately from destructive merging. Existing ZMem organize/link/head mechanisms provide a baseline. |
| [Mem0, 2025](https://arxiv.org/html/2504.19413v1) | Extraction and consolidation of salient information with retrieval evaluation. | Compare resulting answerability, latency and token cost; do not optimize for fewer memory rows alone. |
| [Zep/Graphiti, 2025](https://arxiv.org/html/2501.13956v1) | Temporal graph memory retains changing relationships and historical context. | Do not collapse a transition into corroboration or discard old valid-time evidence during cleanup. A graph-engine replacement is out of scope. |
| [LongMemEval](https://arxiv.org/abs/2410.10813) | Tests extraction, multi-session reasoning, temporal reasoning, knowledge updates and abstention. | Test stale answers, correct non-answers and multi-session evidence, not only positive semantic recall. |
| [MemPoison, Jul 2026](https://arxiv.org/abs/2607.14651) | Studies direct, compositional and dormant memory poisoning. | A nightly curator can amplify several individually innocuous records; evaluate compositional/triggered cases and keep model proposals untrusted. No defense efficacy is claimed here. |

Also reviewed [SCM, Apr 2026](https://arxiv.org/abs/2604.20943), a research preview with sleep-inspired phases and small-scale tests. Its reported ten-turn results do not justify production forgetting rules or biological equivalence. “Sleep” is an engineering analogy here: bounded offline replay, consolidation, verification and policy-governed cleanup.

## Proposed nightly pipeline

1. Admit one run per authoritative store inside an explicitly configured night/idle window; skip busy or resource-constrained hosts. Record a run id, scope and high-watermark. Coalesce missed runs rather than launching a backlog storm.
2. Snapshot a bounded changed-record/evidence set plus a bounded rotating older sample. Preserve explicit user preferences, protected/high-signal memories and unresolved conflicts. Respect namespace authorization and retention rules.
3. Run deterministic health/index/enrichment checks and policy-eligible cleanup as a separately reported phase. Reuse organize/consolidate cadence and locks; a nightly trigger must not silently bypass the existing seven-day/growth gate with `--force`.
4. Optionally ask a configured writer to propose collated lessons, duplicates, contradictions, supersessions and review candidates. Inference owns no writer transaction, store mutation tools or promotion authority.
5. Validate grounded ids and scope, replay quality/safety checks, recheck source revisions and emit review artifacts. Destructive semantic merges, novel lessons and changed preferences remain proposals under the existing review policy.
6. Only separately authorized deterministic maintenance may commit automatically in the evaluation prototype; produce before/after counts, checkpoint/receipts and a morning summary of applied, proposed, skipped and failed work. Pruning/expiry is a policy decision, never a consequence of low recent use alone.

Start with nightly shadow runs against copied/synthetic stores. A production scheduler or unattended model-writing mode requires a later explicit go decision based on repeated-night measurements. No live schedule was created by this issue-preparation work.

## Existing ZMem overlap

Graph refreshed 2026-10-17T16:03:57Z, complete, last_error null. #62 (sleep-time organize), #137 (belief heads) and #138 (curated pages) are closed implementations to reuse. #177 (K-06) remains the evidence-gated retention/review-queue contract. #331 concerns bridged-subagent injection delivery dedup, not memory-row write dedup; it is a separate issue. No new Workstream T slot or dependency is introduced.

At experimental checkout `91996f29a3e1fcb4a489b37edca5290a810f7772`, organize already has bounds, shared cadence, idle gating and single-flight. The belief adapter runs during a writer transaction and defaults to no actions. A real network curator must not be inserted directly into that transaction. These source observations are distinct from the online research.

The [executed ZMem experiment report](hy-memory-experiment-results.md) supplies the local evidence. Six real MiniLM/sqlite-vec pairs and fifteen policy/lineage/seam observations were validated. It is a constructed corpus, not held-out proof of a production subject extractor or Tencent inference quality. The earlier independent critique was stopped by the host memory guard and has no verdict; neither this research nor issue filing turns it into an approval.

A second read-only GLM 5.3 high-effort issue-plan critique was attempted after a fresh successful admission check (34.86 GiB physical available, 19.84 GiB commit available, 88.32% commit used). Host-wide pressure subsequently crossed the runner guard: the run was stopped after 48.375 seconds, cleanup verified, with no stdout verdict. No fallback heavy worker was launched. The filed issues are primary-verified research/implementation contracts, not independently approved implementations; implementation gates still apply.
