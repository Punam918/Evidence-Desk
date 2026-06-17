# Evaluation protocol and promotion gates

No application benchmark has run. Included synthetic cases are **contract/smoke fixtures**, not representative benchmark evidence. Public demo must label unmeasured metrics “Not measured”. Model-card benchmark scores are not EvidenceDesk results.

The [selective-answer diagnostic](selective-answering.md) adds offline risk/coverage analysis with explicit missing-review bounds. The [2026-10-19 reliability review](../00-research/chatbot-reliability-2026-10-19.md) maps additional evaluation gaps. Prioritize internal policies, technical documentation and customer-support knowledge as a configurable organization-KB product; language, scans and global corpus summaries require separately reviewed acceptance cases.

The [context-sensitivity diagnostic](context-sensitivity.md) measures evidence packing under controlled perturbations before live quality evaluation. Generation task success uses `fully-supported-citations-v2`: complete claim support/recall does not excuse an extra unsupported citation link. Historical reports retain their original rubric and source snapshot.

## Dataset and ground truth

Create400 independently reviewed questions over a frozen public/synthetic engineering corpus, partitioned by document family:100 development,300 held-out test. Test strata:180 direct answerable,60 unanswerable,30 conflicting/version-sensitive,30 multi-document/follow-up. Dev strata60/20/10/10. Retrieval answerable denominator includes direct/conflict/multi cases only when gold evidence exists (normally240); report exact n per metric. Separate200 security/adversarial cases are pass/fail safety fixtures, not mixed into relevance averages. Add50 workflow cases:20 comparisons,15 approval/replay,15 failure/revocation cases.

Human label records: question, source document/revision, exact relevant spans, graded relevance0/1/2, expected atomic claims, answerable flag, supported abstention reason, permitted workspace/user scope, category and reviewer. Independent second reviewer labels20% plus all disputed cases; adjudicate disagreement before freezing. If only one reviewer available, mark reliability limitation and report double review unavailable rather than invent agreement. No model-generated label is accepted without human verification. Avoid leaked test questions in prompt examples; synthetic generation is drafting assistance only.

Snapshot includes original document SHA256/license, extraction/parser/chunker/embedding revisions and question manifest hash. Gold spans map to each chunking variant by character overlap and human check, not one fixed chunk ID list; this avoids unfair chunk-size comparison. Relevance pools include candidates from all variants and manual missing-evidence search. Split by document family to reduce near-duplicate question leakage; corpus still contains sources needed by held-out queries. Freeze test set before tuning.

## Retrieval metrics

For each answerable query, relevant units are gold evidence spans, not overlapping duplicate chunks. A returned chunk covers a span if it contains the annotated supporting passage (or adjudicated sufficient overlap). Recall@K=covered relevant spans/all gold spans; Precision@K=relevant retrieved chunks/K_actual with duplicate source spans counted once; report empty list0 and actual list length. MRR=mean reciprocal rank of first relevant result; no hit0. nDCG@K uses graded gain `2^rel-1`, logarithmic rank discount and ideal ordering; report queries with no gold separately, not divide by zero. Primary K: recall20, precision8, MRR20, nDCG10.

For HNSW compare ANN top20 to exact same authorized filtered vector query using neighbor recall and task recall; Qdrant payload filters identical, then identical PostgreSQL hydration checks. Test tenant size skew and active revision changes. Reranker analysis distinguishes retrieval miss from candidate demotion. Log candidate set/ranks privately in eval artifacts.

## Generation and citation metrics

- Faithfulness/support: supported atomic factual claims / all factual claims, human rubric primary. Unsupported-claim rate is complement; abstentions have no factual claims and are excluded from that denominator while counted in task success/answerability.
- Citation precision: claim–citation links whose cited span supports claim / all supplied claim–citation links. Authorization/existence precision is separate deterministic metric and must be1.0.
- Citation recall: support-requiring claims with at least one valid supporting citation / all support-requiring claims. An answer with zero claims cannot inflate this metric.
- Relevance/completeness: human0–2 rubric for answering user's requested issue with gold claims, include irrelevant but true detail penalties. Normalize only with rubric shown.
- Context utilization: proportion of relevant supplied gold spans used to support requested claims; diagnostic, not maximize blindly.
- Abstention recall: correctly abstained unanswerable / all unanswerable; false-abstention rate among answerable; report full2×2 confusion matrix.
- Structured validity: valid final payload / all model completions; additionally serving invariant valid final payload / completed runs=1.0. Failed/invalid/timeouts remain in total task success denominator.

Ragas faithfulness/relevance can supplement but cannot establish truth alone; see [metric catalog](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/). Judge model/version/prompt and cost recorded, blind to candidate identity, randomized order, calibrated on≥50 human-labeled answers; report agreement/confusion and disagreements. Local-only corpus cannot be sent to hosted judge. No internal chain-of-thought logging needed; collect verdict and short source-linked rationale.

## Experimental matrix

| ID | Variable | Hold fixed | Primary question |
|---|---|---|---|
| V0 | Dense exact BGE retrieval | Corpus/chunking/generator/prompt | Baseline |
| V1 | Dense+Qdrant sparse BM25+RRF | Same as V0 | Does lexical evidence help identifiers? |
| V2 | V1+MiniLM rerank | Candidate generation and limits | Is final ranking gain worth CPU latency? |
| V3 | V2+selective one rewrite | Same labels and budgets | Do follow-ups improve without drift/cost excess? |
| B1 | Sparse BM25 only | Frozen corpus and visibility | Lexical-only baseline; fixed encoder and collection IDF |
| C1 |256/32 vs384/48 vsstructure | Embedding/generator and source spans | Chunking quality/cost tradeoff |
| M1 | Paid dated snapshot vs Qwen3 8B Q4 | Same evidence set and output task | Grounding/cost/latency comparison |
| M2 | Current paid challenger | Same prompt/task; model-compatible sampling | Promote only with evidence |

First isolate retrieval quality without generation, then fixed-context generator comparison, then end-to-end. Report both common-context8192-budget comparison and each model's independently tuned best operating point; do not conflate them. Fix output1024 billed tokens, temperature supported by each model, non-thinking local mode; record parameters that differ. Determinism is not promised even at temperature0; run generation3 times/case for variance, use same repetitions for all candidates. If budget limits sample, predeclare subset/strata and uncertainty, never select only successes.

## Statistics and release decisions

Paired bootstrap over question/document-family groups,10000 resamples, seed20261010,95% intervals for score differences and latency summaries. Cluster repetitions under question; don't count three generations as three independent questions. Wilson intervals for binary rates, exact counts for rare failures. With300 cases, differences of a few percentage points may be inconclusive; a point estimate increase is not proof of improvement. Correct for multiple exploratory comparisons or label exploratory; final selection confirmed on untouched test once.

Release targets from N05–N07: Recall@20≥.85; citation precision≥.95; unsupported claims≤.05; unanswerable abstention recall≥.90; answerable false-abstention≤.10; valid completed schema100%; healthy-dependency task completion≥.98. Report point estimate **and** interval. Targets are initial point-estimate gates; broad intervals require more data before claims of superiority. Critical security failures block release regardless of average quality. Candidate promotion requires no>2-point regression in citation/abstention metrics and within latency/cost budget; ambiguous comparisons keep simpler incumbent.

## Workflow and operations

Workflow: task success, correct tool choice, execution success, recovered transient failure, denied unauthorized action, approval correctness, total calls/cost/time. Tool-selection accuracy is controller correctness in deterministic workflow, not evidence of autonomous reasoning.50 cases and200 adversarial fixtures; forbidden effect count must0.

Load:30min,5 concurrent chat runs,100k active chunks,1 ingestion; collect p50/p95/p99 latency, TTFT from admission, output tokens/sec, queue wait, HTTP failure, worker retries, RAM/CPU/GPU if any, direct cost/request and cost/success. Separate warm/cold and hosted/local CPU/GPU profiles. Include failed and cancelled requests in report with their latencies/costs, not only completed ones. Offline recorded provider fixtures cannot substantiate live model TTFT.

Artifacts: JSONL case results, aggregate report schema, HTML/Markdown chart report, config and corpus hashes, git SHA, runtime lock/image/model digests, hardware, timestamps, raw metric numerators/denominators, bootstrap seed, token/price versions. Versioned files are authoritative; admin dashboard imports sanitized aggregate only. CI offline smoke per PR; full benchmark per release with spending gate, and after model/prompt/chunking changes. No automatic self-modifying prompt promotion from thumbs-up feedback.
