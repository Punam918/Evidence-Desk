# Context sensitivity diagnostic

`evals/runner/context_sensitivity.py` constructs authored fictional Lumen policy, API and refund examples. These are designed fixtures, not reviewed gold or organization facts. Nine variants per topic cover support alone; the same support/distractor set with support first, middle or last; relevant-looking but insufficient context; conflicting statements; stale assistant history; empty context; and support ranked beyond the eight-passage cap.

Run from the project root with the pinned local tokenizer artifacts already installed:

```bash
.venv/bin/python -m evals.runner.context_sensitivity \
  --output evals/reports/context-sensitivity-NEW.json
```

The command uses the actual `ContextPacker` and frozen tokenizer bindings for `paid_standard` and `local_standard`. It performs no inference or retrieval calls, incurs no API spend and refuses to overwrite output. A regression runs the measurement with socket connections forbidden. Candidate authorization is not exercised here; native PG/Qdrant integration tests cover that separately.

The [2026-10-19 artifact](../../evals/reports/context-sensitivity-2026-10-19-final.json) records54 measurements, source/configuration hashes, prompt/request hashes, ordered candidate/packed text hashes, source labels, history counts, input/evidence tokens and context-fit status. Each position permutation keeps source identities but changes its label and request identity. Tests verify that an old label cannot verify a quotation against a newly positioned passage. Stale history cannot supply a quotation absent from current evidence.

Observed for both configured tokenizer bindings across all three topics: support survives at E1/E4/E8 in the position variants; both conflicting passages survive; empty context abstains before generation. Relevant but insufficient context and support outside the cap still produce packable requests **without the required fact**. This is an observable limitation, not a passing answerability score. No generator was asked to answer these cases.

For live evaluation, review expected behavior first and create a separate frozen common-context generation plan for each variant; every model within that plan receives the same evidence. Preserve prompt/configuration identity and three repetitions. Do not treat context variants or repeated generations as independent questions, or reuse original answerability labels after removing supporting evidence. Evaluate claim/citation support, calibrated abstention, conflict acknowledgment and cost/latency. This diagnostic does not implement an automatic sufficiency classifier, choose retrieval thresholds, or establish that a model obeys prompt v2.
