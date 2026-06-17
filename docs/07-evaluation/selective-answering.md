# Selective-answer diagnostics

Implemented in `evals/runner/selective.py`. This offline diagnostic evaluates whether withholding lower-scored recorded answers changes answer coverage and human-reviewed rubric risk. It makes no inference calls, changes no serving policy and never chooses a threshold. Scores are ordering signals, not calibrated confidence probabilities.

Use the existing frozen generation plan, observations and attested human judgments described in [evaluation plan](evaluation-plan.md). A selection plan contains `generation_plan_sha256`, `scorer_sha256`, and up to64 distinct increasing `thresholds` in `[0,1]`. Each score JSONL record contains `observation_sha256`, the same `scorer_sha256`, and `score`. Hashes use the runner's canonical model identity. Bind scores only to answered observations. Unknown, duplicate, stale, nonfinite or changed-scorer inputs fail validation. Never invent human reviews to fill missing labels.

```bash
.venv/bin/python -m evals.runner.selective \
  --dataset evals/datasets/benchmark-v1-draft \
  --plan /path/to/generation-plan.json \
  --selection-plan /path/to/selection-plan.json \
  --observations /path/to/observations.jsonl \
  --judgments /path/to/attested-judgments.jsonl \
  --scores /path/to/scores.jsonl \
  --source-manifest /path/to/source-manifest.json \
  --output /path/to/new-selection-report.json
```

These are input placeholders, not existing benchmark results. Capture the source manifest before collecting observations with `python -m evals.runner.provenance --output NEW.json`. The CLI verifies current source/locks, includes input hashes and refuses to overwrite a report. Source identity now includes packaged `.txt` prompts; prompt-only edits invalidate a snapshot. Old snapshots cannot be silently reused for changed prompts.

For each candidate and threshold, `score >= threshold` selects the whole tie. The denominator is all planned question/repetition attempts. Selected answers, withheld answers, unscored answers, native abstentions, failed attempts and missing attempts are separate counts. Selected plus all other categories equals planned attempts. Withheld reviewed-good answers are reported so that excessive refusal remains visible. Costs include recorded attempts before withholding.

A reviewed answer is good only when the case is answerable, it has factual claims, all claims are supported, all required claims are cited, all supplied citation links are supported and authorized, relevance/completeness both score2, and no security failure is present. `reviewed_risk` is reviewed failures divided by reviewed selections. For all selected answers, lower risk is known failures/selected; upper risk is (known failures + unreviewed)/selected. Empty denominators are null. These bounds express missing-label uncertainty, **not statistical confidence intervals**. Three repetitions of a question are correlated, not three independent questions. The rubric does not independently establish real-world truth.

Security failures across all reviewed outputs stay visible even if a threshold withholds those outputs. `promotion_eligible` is always false and `selected_threshold` is null. Freeze tuning on development data before held-out evaluation; this tool cannot attest when a plan was written. Post-generation withholding saves no generation cost. A separate calibrated, privacy-compatible scorer and live evaluation are still needed before any serving change.
