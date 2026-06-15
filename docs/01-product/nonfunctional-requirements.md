# Nonfunctional requirements and measurement conditions

All values are **initial targets**, unmeasured. Benchmark profile: 8 vCPU/16 GiB Linux host, SSD, 100k active 384-dimension chunks, five concurrent chat runs, 10k HTTP requests over 30 minutes, 200-token queries, ≤6k input and ≤800 visible output tokens for generation. CPU local generation has a separate profile and is excluded from hosted-model latency claims. Record network, model snapshot, cold/warm state and all failures.

| ID | Requirement / gate | Measurement and failure policy | Future reference (unverified) |
|---|---|---|---|
| N01 | API availability ≥99.5% over 30d, excluding announced maintenance; report end-to-end generation separately | 1-min external synthetic probe, successful eligible requests / eligible attempts; dependency failures count for relevant endpoint | 99.9% with multi-zone managed DB and ≥2 APIs; not promised by demo |
| N02 | Metadata p95 ≤300ms; retrieval p95 ≤1.5s; first draft text p95 ≤5s, full hosted answer p95 ≤20s | End-to-end clocks, 5 concurrent, include queue delay; hard run deadline 60s; ≤1% server failures excluding deliberate fault tests | 50 concurrent after independent capacity test |
| N03 | One 20-page text PDF searchable ≤120s p95 at ingestion concurrency 1 | Upload acceptance to publication; excluding OCR; no partial publication; queue backlog separately | More workers after DB/object bottleneck profiling |
| N04 | Zero observed cross-tenant/role bypass in ≥200 adversarial fixtures; all citations authorized | Deterministic SQL/API/retrieval/tool/replay tests, including revoked membership and tombstones | Defense tests remain mandatory at any scale |
| N05 | Recall@20 ≥0.85 on answerable held-out cases; citation precision ≥0.95; unsupported atomic claims ≤0.05 | Human ground truth with bootstrap intervals; report point estimates and intervals; failures block promotion | Corpus/language-specific gates |
| N06 | Abstention recall ≥0.90 on unanswerable cases, false abstention ≤0.10 on answerable | Report denominators and Wilson intervals; compare answerability confusion matrix | Recalibrate with real opt-in feedback |
| N07 | Final schema validity 100% among completed runs; invalid model outputs fail/abstain and still count in success-rate denominator | Fuzz adapters and truncated streams; target successful completion ≥98% under healthy dependencies | No silent repair to unverified facts |
| N08 | App services ≤12 GiB steady RAM on16 GiB host, no OOM over 30min load | Container RSS, CPU, disk, parser cap 1 GiB; Keycloak local dev separate profile | Separate workers/inference before adding host memory blindly |
| N09 | Mean direct generation ≤$0.01 and per-run reserved cap ≤$0.05; infrastructure budget target ≤$130/month before optional GPU | Price snapshot × tokens for every attempt; eval spend separately capped $10/run | Budget is configurable; prices rechecked |
| N10 | Logical delete effective before delete acknowledgement; live purge ≤24h; backup expiry ≤30d | Race tests, sweep metrics, restored backup tombstone replay | Compliance-specific retention out of scope |
| N11 | Backup RPO ≤24h, restore RTO ≤4h for single-host demo | Daily encrypted DB/object manifest backup and quarterly timed fresh-host restore | Managed PITR target RPO 15min/RTO 1h only after drills |
| N12 | Reproducible build and experiments; all high-risk transitions tested | Locked dependencies/digests, dataset/prompt/config hashes, CI contract checks, no secrets in logs | No percentage coverage used as substitute for invariants |
| N13 | Keyboard completion and WCAG 2.2 AA design target | Automated checks plus manual focus/contrast/live-region review at 360px/1280px | Accessibility remains release gate |
| N14 | Telemetry attribution on 100% runs; logs contain no raw document/prompt/secret by default | Correlation joins run→attempt→usage; redaction fixtures; traces sampled but usage never sampled | Cost/SLO dashboards scale independently |

Targets may be revised with evidence, documented in ADR + report. Do not tune on the held-out set, hide failed requests, or exclude cold starts without separate reporting. A portfolio demo that misses a target must show the actual result and explain the next engineering step.

N01 observation window: initial release verifies probes/alerting and load error rates; the30-day availability target is assessed only after30days of deployment. Until then show “insufficient observation window”, not an achieved availability percentage. This does not excuse failures observed in shorter windows.
