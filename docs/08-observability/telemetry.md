# Observability and LLMOps

OpenTelemetry traces + Prometheus metrics + structured JSON logs. Use project metric names below as stable contracts; map selected attributes to [GenAI conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) at a pinned instrumentation version. Optional Langfuse export receives redacted traces only, with explicit opt-in to content capture; no full self-host stack in baseline.

## Log event schema

```json
{"timestamp":"2026-10-10T10:00:00Z","level":"INFO","service":"chat-worker","event":"model.attempt.completed","request_id":"00000000-0000-4000-8000-000000000001","trace_id":"00000000000000000000000000000001","workspace_hash":"hmac:opaque","run_id":"00000000-0000-4000-8000-000000000002","attempt":1,"model_key":"paid_standard","model_revision":"gpt-6-luna","prompt_version":"grounded-assistant/v1","config_sha256":"example-hash","duration_ms":1200,"input_tokens":1200,"output_tokens":200,"cost_micro_usd":220,"usage_estimated":false,"error_code":null}
```

This is an illustrative event, not a measured request. Required: time/level/service/event/request_id or job_id; run events include run_id, attempt, config/model/prompt versions. Error stack traces only in restricted logs after sanitization. Never log authorization/cookie headers, API keys, raw prompts, document bodies, full tool arguments/results, user email, object signed URLs or private reasoning. IDs are pseudonymous but still potentially sensitive; logs7d, traces7d, metrics30d, audit90d. Content-bearing database records retain separate authorization.

Spans: `http.request`→`auth.session`→`workspace.authorize`→`run.admit`; worker link to admission span, then `context.history`, `retrieval.embed_query`, `retrieval.dense`, `retrieval.lexical`, `retrieval.fuse`, `retrieval.rerank`, `context.pack`, `model.attempt`, `answer.validate`, `run.finalize`. Ingestion: `upload.store`, `ingest.parse`, `ingest.chunk`, `ingest.embed_batch`, `ingest.verify`, `ingest.publish`. Tools: `tool.authorize`, `tool.approve`, `tool.execute`. Span links bridge asynchronous jobs; do not keep HTTP span open throughout queue wait.

| Metric | Type / labels (bounded only) | Meaning / alert |
|---|---|---|
| ed_http_requests_total | counter route_template,method,status_class |5xx>2% for5min at≥20 requests alerts |
| ed_http_duration_seconds | histogram route_template |metadata p95>.3s for10min warning |
| ed_run_duration_seconds | histogram model_key,intent,status |hosted p95>20s for10min warning |
| ed_ttft_seconds | histogram model_key |User-visible first draft, including queue/retrieval; p95>5s |
| ed_model_tokens | gauge model_key,direction |Retained ledger input/output; reasoning subset not added twice |
| ed_model_cost_micro_usd | gauge model_key,estimated |Retained direct billed/estimated cost; never sampled |
| ed_retrieval_duration_seconds | histogram branch,profile |p95>1.5s total target |
| ed_retrieval_candidates | histogram branch |Detect empty/under-return trends |
| ed_jobs_queue_age_seconds | gauge kind |oldest queued ingest>300s for5min |
| ed_jobs_dead | gauge kind |Current dead jobs require investigation |
| ed_budget_rejections_total | counter reason |Unexpected spikes indicate abuse/misconfiguration |
| ed_provider_breaker_state | gauge model_key |open>5min alerts |
| ed_source_purge_age_seconds | gauge |max>24h pages operator |
| ed_active_runs | gauge state |Admission/slot reconciliation |
| ed_schema_invalid_total | counter model_key,prompt_version |Any spike blocks promotion |
| ed_process_resident_memory_bytes | gauge service |OOM risk/service budget |

No user/workspace/run/document IDs in metric labels; use DB aggregate report for tenant breakdown. Cardinality stays bounded to registered models/profiles/routes. HTTP latency includes request overhead; model tokens/sec only measured over generation duration and clearly distinguishes visible vs billed output. TTFT for buffer-only adapter reported separately.

Dashboards: service availability/errors/latency; queue/ingestion funnel/failure reasons; retrieval branch/recall experiment trends; model TTFT/cost/fallback/schema errors; tenant budget aggregate; deletion/backup age. Evaluation dashboard shows dataset size/hash, judge version, confidence intervals and actual failures; no online “faithfulness score” for every request without a validated judge.

Alert playbooks link reliability runbook. Instrumentation tests inject canary secrets into headers, model error and upload metadata and assert absence across every sink. Trace sampling10% normal,100% errors where safe; accounting and audit100%. Trace outage must not cause request failure, but missing usage commit must prevent run being marked completed.

## v2 operational signals

Record Qdrant query latency/errors, per-branch candidate counts after authorization, index outbox age/dead rows, manifest mismatch count, purge lag and rebuild duration. Graph spans record graph/node/version, safe run identifier, checkpoint latency/failure, replay count, pending approval age and ambiguous issued-attempt count. No full state/messages/prompts/vector payloads in default traces. LangSmith tracing is off unless an explicitly reviewed redacted integration is configured. Avoid workspace/thread IDs as Prometheus labels; use bounded graph/node/error/model dimensions and hashed correlation in logs.

Alert: any manifest mismatch or unauthorized candidate attempt; outbox/purge older24h; checkpoint save errors>0 sustained5min; approval recovery stuck beyond expiry; uncertain attempt costs awaiting settlement. Thresholds are initial operational hypotheses to tune during load/chaos, not proof of reliability.

Private runtime transport and retained-ledger gauge semantics are specified in [ADR016](../adr/016-private-operational-telemetry.md). Export/listeners are optional and use mounted credentials; browser credentials cannot access deployment-wide metrics.
