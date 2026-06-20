# Evaluation artifacts

This folder specifies experiments and contains eight original synthetic smoke cases with five source documents. No benchmark has been run. `datasets/manifest.json` hashes source bytes; `datasets/smoke.jsonl` defines expected evidence/abstention and adversarial behavior. Labels are designed fixtures, not independently reviewed ground truth.

Implementation T24 expands to400 reviewed questions plus200 adversarial and50 workflow cases; T25 implements the runner and report import. Read [evaluation protocol](../docs/07-evaluation/evaluation-plan.md) and [human rubric](rubrics/human-review.md). `configs/benchmark.yaml` deliberately references the future frozen benchmark, not the smoke dataset. Do not replace that distinction with a fabricated benchmark report.

## v2 required comparisons

Freeze concrete Qdrant collection/profile hash including sparse IDF population. Report dense, dense+sparse/RRF, reranking and one rewrite variants under identical authorization. Qwen3.5-4B and Qwen3 embedding/reranking are current challenger candidates in current-stack.md; do not silently replace tiny CPU references or compare unequal hardware without separate reports. LangGraph recovery tests measure side effects/attempt counts, not answer quality. Runtime vendor probes use synthetic vectors and are not retrieval benchmarks.

## Curation implementation checkpoint

`datasets/benchmark-v1-draft` now contains400/200/50 original synthetic draft cases with exact source spans and hashes. It is **not human-reviewed gold**. `python -m evals.curation validate` checks structure; `export-review`, `import-reviews` and `freeze` implement the manual review boundary. The prepared review HTML/CSV is under `review/benchmark-v1-draft`. See `docs/07-evaluation/label-report.md` for commands, limitations and current zero-review status. Only eight independent archetype groups exist; cross-split template grouping is deliberate. Model-quality promotion stays disabled.

## Runner implementation checkpoint

`python -m evals.runner` aggregates recorded retrieval with explicit draft/freeze modes and optional dashboard exports. `python -m evals.runner.provenance` captures source/lock hashes; `python -m evals.runner.generation` aggregates hash-bound common-context observations and attested human judgments. Neither command collects model outputs or invokes a hosted judge. See [runner checkpoint and commands](../docs/10-testing/evaluation-runner-report.md). T25 remains in progress pending actual collection and reviewed acceptance.
