"""Exercise pinned operational binaries locally, then remove only owned containers."""

import io
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
from evidencedesk.observability.exporter import PrivateOTLPExporter
from evidencedesk.observability.stages import current, stage
from evidencedesk.observability.telemetry import Telemetry

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "infra/monitoring"


def command(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(
            f"Validation command failed: {result.stdout[-2000:]} {result.stderr[-2000:]}"
        )
    return result.stdout.strip()


def await_http(client: httpx.Client, url: str) -> dict:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            response = client.get(url)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("Local monitoring service did not become ready")


def main() -> None:
    locks = json.loads((CONFIG / "images.lock.json").read_text())
    owned: list[str] = []
    evidence = {"images": locks, "checks": [], "external_publication": False}
    with tempfile.TemporaryDirectory(prefix="ed-monitor-check-") as tmp:
        temp = Path(tmp)
        temp.chmod(0o755)
        token = temp / "token"
        token.write_text(secrets.token_urlsafe(32))
        token.chmod(0o644)  # Synthetic ephemeral secret, readable by container's unprivileged UID.
        data = temp / "traces"
        data.mkdir()
        try:
            prom = locks["prometheus"]["image"]
            common = ("docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/promtool")
            command(
                *common,
                "-v",
                f"{CONFIG}:/etc/prometheus:ro",
                "-v",
                f"{token}:/run/secrets/metrics_token:ro",
                prom,
                "check",
                "config",
                "/etc/prometheus/prometheus.yml",
            )
            command(
                *common,
                "-v",
                f"{CONFIG}:/checks:ro",
                "-w",
                "/checks",
                prom,
                "test",
                "rules",
                "rules.test.yml",
            )
            evidence["checks"] += [
                "prometheus-config",
                "15-alert-rules",
                "six-rule-behavior-scenarios",
            ]
            command(
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{CONFIG}/collector.yml:/etc/otelcol/config.yaml:ro",
                locks["collector"]["image"],
                "validate",
                "--config=/etc/otelcol/config.yaml",
            )
            evidence["checks"].append("collector-config")

            def start(service: str, port: int, args: list[str]) -> str:
                name = "ed-monitor-check-" + service + "-" + uuid4().hex[:12]
                owned.append(name)
                command(
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--read-only",
                    "--memory=512m",
                    "--cpus=1",
                    "--pids-limit=128",
                    "-p",
                    f"127.0.0.1::{port}",
                    *args,
                )
                mapping = command("docker", "port", name, str(port)).splitlines()[0]
                return "http://" + mapping

            endpoint = start(
                "collector",
                4318,
                [
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "-v",
                    f"{data}:/var/lib/otel",
                    "-v",
                    f"{CONFIG}/collector.yml:/etc/otelcol/config.yaml:ro",
                    locks["collector"]["image"],
                    "--config=/etc/otelcol/config.yaml",
                ],
            )
            # The receiver returns a protobuf success body, so readiness uses a bounded empty OTLP request.
            with httpx.Client(timeout=2, trust_env=False) as client:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        response = client.post(
                            endpoint + "/v1/traces",
                            content=b"",
                            headers={"Content-Type": "application/x-protobuf"},
                        )
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError("Collector receiver unavailable")
                    time.sleep(0.2)
            telemetry = Telemetry(
                frozenset(),
                service="chat-worker",
                sample_rate=1,
                exporter=PrivateOTLPExporter(endpoint + "/v1/traces"),
                log_stream=io.StringIO(),
            )
            context = current.set(telemetry)
            try:
                try:
                    with stage("checkpoint.write"):
                        raise RuntimeError("PRIVATE-CANARY-NEVER-EXPORT")
                except RuntimeError:
                    pass
                assert telemetry.provider.force_flush(timeout_millis=3000)
                deadline = time.monotonic() + 10
                while (
                    not (data / "traces.json").exists() or not (data / "traces.json").stat().st_size
                ):
                    if time.monotonic() > deadline:
                        raise RuntimeError("Collector did not persist the synthetic span")
                    time.sleep(0.1)
                received = (data / "traces.json").read_text()
                assert "checkpoint.write" in received and "PRIVATE-CANARY" not in received
                command("docker", "stop", "-t", "1", owned[0])
                with stage("worker.claim"):
                    pass
                assert telemetry.provider.force_flush(timeout_millis=3000)
                evidence["checks"] += [
                    "native-otlp-to-real-collector",
                    "collector-content-canary",
                    "collector-outage-keeps-stage-successful",
                ]
            finally:
                current.reset(context)
                telemetry.close()

            grafana = start(
                "grafana",
                3000,
                [
                    "--tmpfs",
                    "/var/lib/grafana:uid=472,gid=472,mode=0700,size=96m",
                    "--tmpfs",
                    "/tmp:size=32m",
                    "-e",
                    "GF_SECURITY_ADMIN_PASSWORD__FILE=/run/secrets/admin",
                    "-e",
                    "GF_ANALYTICS_REPORTING_ENABLED=false",
                    "-e",
                    "GF_ANALYTICS_CHECK_FOR_UPDATES=false",
                    "-e",
                    "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false",
                    "-e",
                    "GF_PLUGINS_PREINSTALL_DISABLED=true",
                    "-v",
                    f"{token}:/run/secrets/admin:ro",
                    "-v",
                    f"{CONFIG}/provisioning:/etc/grafana/provisioning:ro",
                    "-v",
                    f"{CONFIG}/dashboards:/var/lib/grafana/dashboards:ro",
                    locks["grafana"]["image"],
                ],
            )
            with httpx.Client(timeout=3, trust_env=False) as client:
                assert await_http(client, grafana + "/api/health")["database"] == "ok"
                path = grafana + "/api/dashboards/uid/evidencedesk-operations"
                assert client.get(path).status_code == 401
                response = client.get(path, auth=("admin", token.read_text()))
                response.raise_for_status()
                assert len(response.json()["dashboard"]["panels"]) == 18
                evidence["checks"] += [
                    "grafana-real-provisioning-18-panels",
                    "grafana-anonymous-denied",
                ]
            evidence["status"] = "passed"
            evidence["measured_at_unix"] = time.time()
            (ROOT / "docs/10-testing/monitoring-validation.json").write_text(
                json.dumps(evidence, indent=2) + "\n"
            )
            print(json.dumps(evidence, indent=2))
        finally:
            for name in reversed(owned):
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
