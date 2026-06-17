# Private operational monitoring

T26 supplies optional authenticated scrape listeners, an OTLP receiver, Prometheus rules and a provisioned Grafana dashboard. No monitoring port is added to the public API. The dashboard measures operational behavior; it does not display invented model-quality scores. Image versions and immutable registry digests are in [the monitoring lock](../../infra/monitoring/images.lock.json).

Create separate random metrics and Grafana credentials outside the repository, make them readable only by the operator/container identity, and supply their absolute filenames as `ED_METRICS_TOKEN_FILE` and `ED_GRAFANA_PASSWORD_FILE`. Docker Compose file-backed secrets must be readable by the consuming container UID; use an appropriate host group/ACL, not a world-readable real credential. Then validate/start the optional monitoring services:

```bash
docker compose -f infra/monitoring/compose.yml config --quiet
docker compose -f infra/monitoring/compose.yml up -d
```

This creates an internal `evidencedesk-monitoring` network. Prometheus and the collector have no host-published ports. Grafana is reachable only at `http://localhost:3001`, requires login, and disables anonymous access and reporting/update downloads. Its 18 panels are provisioned from versioned JSON. Alerts are visible in Prometheus/Grafana; no email, webhook or paging destination is configured. Delivery to an operator is a deployment integration, not an already functioning pager.

Attach API and worker containers to this network with the aliases `api`, `chat-worker`, `brief-worker`, `ingest-worker`, `index-worker`, and `metrics-worker`. Keep their existing service networks for database/object/provider access. In each process, mount the metrics token file and set:

```text
ED_METRICS_HOST=0.0.0.0
ED_METRICS_PORT=9108
ED_METRICS_TOKEN_FILE=/run/secrets/metrics_token
ED_OTLP_ENDPOINT=http://collector:4318/v1/traces
```

Do not publish port9108 or4318 on a public interface. The collector trusts this private network and only receives explicit content-free spans; it is not a public authenticated ingestion API. For a host process, keep `ED_METRICS_HOST=127.0.0.1` and use an operator-managed local collector/scraper. Export stays disabled without the endpoint. Automatic LangSmith/LangChain content tracing is rejected at startup.

After migration0019, provision a password for `ed_monitor` through the existing operator secret workflow. Run `evidencedesk metrics-worker` with only `ED_MONITOR_DATABASE_URL` and telemetry settings. The role cannot select tenant tables, access checkpoints or mutate application data. Its single fixed SQL function returns deployment aggregates with bounded labels. Exactly one monitor worker per database should be scraped to avoid double-counting these gauges. `evidencedesk metrics-worker --once` validates one snapshot and exits nonzero on failure. The daemon refreshes every15s; after45s without a successful refresh, retained metrics disappear and the freshness alert fires. Scrapes never issue SQL.

Prometheus keeps metrics up to30days with a2GB size cap. Collector traces are a bounded, ephemeral local diagnostic spool (96MiB tmpfs,10MiB rotation, at most7 backups,7-day rotation policy), lost on restart. The file exporter is an upstream alpha component, so its real pinned binary is exercised by our validation script. This spool is not a durable trace search service or backup. Hard wall-clock retention cleanup and durable operational storage integration belong to deployment/lifecycle work. Do not infer a guaranteed seven-day retention period from file rotation alone.

## Investigating alerts

| Alert group | First checks and safe recovery |
|---|---|
| Scrape unavailable / monitor stale | Check process readiness, token mounts and monitor DB role/timeouts. An absent snapshot is unknown health, not zero cost or zero backlog. |
| API failure / metadata latency | Inspect bounded route/status signals and database readiness. Exclude event/upload/original streaming from metadata latency. Correlate requests with their application request IDs through restricted logs. |
| Provider open | Check configured provider and readiness probe. Browsing should remain healthy. Never bypass local-only policy or issue paid probes without explicit spend authorization. Disabled models do not fire the open-breaker alert. |
| Retrieval unavailable / candidate filtered / index unavailable | Check Qdrant reachability and manifests; run the scoped index audit. Payload/authorization mismatches remain rejected. Do not remove filters to make search succeed. |
| Checkpoint failure / overdue graph or approval | Restore saver connectivity and inspect durable workflow/application records. Uncertain model attempts must not be invoked again; expired approvals do not grant permission. |
| Dead jobs / old ingest queue / old outbox | Diagnose the original bounded failure and dependency availability before explicit retry. Fencing and lease recovery remain authoritative. |
| Overdue purge / uncertain usage | Restore lifecycle/settlement workers; preserve tombstones and conservative estimated charges. Do not erase ledger rows to silence alerts. |

Thresholds are initial operational hypotheses. Prometheus unit scenarios verify duration/traffic gates; they are not measured SLO attainment. Stage histograms include errors and cancellations. Cost/token totals are retained-ledger gauges, so do not apply `rate()` to them. Thinking tokens are already included in output. Candidate counts are recorded only after PostgreSQL scope/payload validation. Recorded manifest status is not a live Qdrant probe; actual query failures are reported separately.

Run `.venv/bin/python scripts/validate_monitoring.py` from the project root to exercise the pinned Prometheus rules, real OTLP delivery and collector loss, and Grafana provisioning/authentication. It uses synthetic ephemeral credentials and owned temporary containers, removes only those containers, and saves [machine-readable evidence](../10-testing/monitoring-validation.json). The actual application-process and monitor-role tests are under `tests/integration/observability`.

Current traces are standalone stage/HTTP spans with explicit code-owned attributes,10% normal and100% error sampling. They do not yet provide linked admission-to-job traces, user-visible TTFT histograms or a durable trace UI. Those remain observability refinements; ledger accounting and existing authorization/recovery do not depend on them. See [ADR016](../adr/016-private-operational-telemetry.md) and [the reliability runbook](../06-security/reliability.md).

Configuration syntax was checked against the pinned binaries and the primary [Prometheus configuration reference](https://prometheus.io/docs/prometheus/latest/configuration/configuration/) and [collector file-exporter reference](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.162.0/exporter/fileexporter/README.md).
