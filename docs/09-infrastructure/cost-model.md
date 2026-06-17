# Cost model — USD, research 2026-10-10

Prices are observed public list prices, not quotes or guaranteed regional availability. Taxes, currency conversion, operator time, paid IdP tiers and unexpected egress are excluded. Recheck before spending. Free tiers are not assumed. Usage assumptions below are design scenarios, not measured demand.

| Item | Observed unit price / assumption | Monthly scenario |
|---|---|---|
| VM | DigitalOcean Regular Basic8 vCPU/16 GiB: $96/mo |$96 |
| R2 Standard objects |$0.015/GB-month |20 GB=$0.30 before rounding/free allowance |
| R2 operations |Class A $4.50/million; Class B $0.36/million |10k A+100k B=$0.081 before billing rounding |
| Backup/domain/monitoring allowance |Planning allowance, not vendor quote |$10 |
| OIDC |Free/low-use tier hypothesis; inspect chosen vendor before deploy |$0 assumed; budget sensitivity+$10 |
| Paid standard model |GPT-6 Luna input/output $0.10/$0.50 per1M tokens |2000 answers ×(3000 in+500 out)=$1.10 |
| Retry/rewrite overhead |20% token-cost planning allowance |$0.22 |
| CPU embedding/reranking |Included VM compute, zero API fee |$0 marginal API, competes for RAM/CPU |
| Offline eval |Per-release explicit budget |$10 cap, separate from normal usage |

Sources: [DigitalOcean](https://www.digitalocean.com/pricing/droplets), [R2](https://developers.cloudflare.com/r2/pricing/), [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna). Calculation gives approximately **$107.70/month** before evaluation/rounding/exclusions, **$117.70 with a $10 evaluation run**. With a paid IdP add assumed$10, for$127.70 including evaluation under the revised$130 target. This remains a planning scenario, not a hosting quote. API hard budget$20 bounds usage more conservatively than the scenario$1.32.

Current quality challenger price snapshots: GPT-6.1 Sol $2/$10 per1M standard short-context input/output ([OpenAI pricing](https://developers.openai.com/api/docs/pricing)); Claude Sonnet5.5 $2/$10 ([catalog](https://platform.claude.com/docs/en/models/overview)); Gemini3.8 Flash text $0.75/$3.75 including thinking output through2026-12-31 ($1.50/$7.50 from2027-01-01; expired pricing disables route until refreshed) ([pricing](https://ai.google.dev/gemini-api/docs/pricing)). These are not equal-quality models or identical billing semantics. The older dated mini remains an optional reproducibility comparator. Current default Luna still requires account/conformance and paired quality/latency gates before release. Cache discounts, batch discounts and thinking modes are excluded from scenario math.

General formulas:

```text
attempt_cost = uncached_input/1e6 * input_rate
             + cached_input/1e6 * cached_rate
             + billed_output/1e6 * output_rate
request_cost = sum(all generation/rewrite/repair/summary-attributed attempts)
embedding_API_cost = source_tokens/1e6 * verified_embedding_rate
monthly = fixed + model_usage + storage + operations + backups + optional_GPU
effective_cost_per_success = total_attempt_cost / successful_tasks
```

Reasoning tokens are part of billed output when provider says so; never double-count a subset. Paid embeddings are disabled in baseline. Optional text-embedding-3-small observed$0.02/million input tokens ([model page](https://developers.openai.com/api/docs/models/text-embedding-3-small));10million source tokens≈$0.20 API cost, excluding reprocessing/query embeddings/storage. Recheck price/dimensions before enabling. Local CPU embedding cost is measured CPU-seconds/host allocation, not a separate invoice.

Optional GPU: [Runpod listed RTX4090 Pod](https://www.runpod.io/pricing) $0.89/hour at retrieval. 10h showcase≈$8.90; 730h≈$649.70 before persistent storage/network/tax. It is inappropriate for a continuously hosted low-traffic portfolio baseline. Q4 8B memory arithmetic is in model-abstraction; price does not guarantee selected model fits. Record hardware, precision, context and throughput before comparing API vs self-host break-even.

Sensitivity:10k answers at same tokens ~$6.60 including20% overhead;20k at~$13.20. A quality route at$2/$10 costs$0.011/request for the same3000/500 tokens before overhead, so2000 requests≈$26.40 with20%. Budget gate blocks rather than unexpectedly overruns. Long-context prompts can dominate costs even with short answers; never assume context capacity is free or necessary.

## v2 infrastructure impact

The16-GiB Regular Basic price above buys8 shared vCPUs, not4 dedicated CPUs. Benchmark hardware must match this revised8-vCPU/16-GiB profile. Qdrant and checkpointing share that host; there is no extra managed-service invoice, but they consume memory/storage/backup bandwidth. The$10 backup/domain/monitoring amount is an allowance, not a DigitalOcean managed-backup quote. Recompute if selecting managed PostgreSQL/Qdrant, larger disk, paid identity or a hosted trace backend. Local development can run without a public VM; no cloud resources are provisioned by this specification.
