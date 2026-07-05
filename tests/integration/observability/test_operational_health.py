import asyncio
from uuid import uuid4

import psycopg
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.exc import SQLAlchemyError

from tests.integration.tenancy.test_workspaces import headers
from tests.support import sign_in

pytestmark = pytest.mark.integration


def test_health_is_scoped_sanitized_and_provider_outage_keeps_api_ready(auth_client, database):
    client, state = auth_client
    session = sign_in(client, state)
    workspace = client.post(
        "/v1/workspaces", json={"name": "Health"}, headers=headers(session)
    ).json()["id"]
    foreign = client.post(
        "/v1/workspaces", json={"name": "Unrelated"}, headers=headers(session)
    ).json()["id"]
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.provider_health SET state='open',capability_report=%s",
            ('{"raw_exception":"PRIVATE-CANARY"}',),
        )
        conn.execute(
            "INSERT INTO app.jobs(workspace_id,id,kind,status,requested_by,dedupe_key,error_code,payload) VALUES(%s,%s,'maintenance','dead',%s,'foreign','PRIVATE-CANARY','{\"prompt\":\"PRIVATE-CANARY\"}')",
            (foreign, uuid4(), session["user"]["id"]),
        )
    path = f"/v1/workspaces/{workspace}/health"
    result = client.get(path)
    assert result.status_code == 200, result.text
    assert "PRIVATE-CANARY" not in result.text
    checks = {c["name"]: c for c in result.json()["checks"]}
    assert checks["generation"]["status"] == "degraded"
    assert checks["jobs_need_recovery"]["status"] == "ok"
    assert client.get("/health/ready").status_code == 200
    assert client.get(f"/v1/workspaces/{workspace}/knowledge-bases").status_code == 200
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.jobs(workspace_id,id,kind,status,requested_by,dedupe_key,error_code) VALUES(%s,%s,'maintenance','dead',%s,'own','PRIVATE-CANARY')",
            (workspace, uuid4(), session["user"]["id"]),
        )
        conn.execute(
            "INSERT INTO app.deletion_tombstones(workspace_id,id,entity_type,entity_id,deleted_at) VALUES(%s,%s,'document',%s,now()-interval '25 hours')",
            (workspace, uuid4(), uuid4()),
        )
    result = client.get(path)
    checks = {c["name"]: c for c in result.json()["checks"]}
    assert checks["jobs_need_recovery"]["status"] == checks["purge_overdue"]["status"] == "degraded"
    assert "PRIVATE-CANARY" not in result.text
    with psycopg.connect(database["operator"]) as conn:
        conn.execute("UPDATE app.memberships SET role='viewer' WHERE workspace_id=%s", (workspace,))
    assert client.get(path).status_code == 403
    with psycopg.connect(database["operator"]) as conn:
        conn.execute("DELETE FROM app.memberships WHERE workspace_id=%s", (workspace,))
    assert client.get(path).status_code == 404


def test_http_telemetry_never_captures_request_or_exception_content(
    auth_client, monkeypatch, capfd
):
    client, state = auth_client
    session = sign_in(client, state)
    workspace = client.post(
        "/v1/workspaces", json={"name": "Safe telemetry"}, headers=headers(session)
    ).json()["id"]
    exporter = InMemorySpanExporter()
    client.app.state.telemetry.provider.add_span_processor(SimpleSpanProcessor(exporter))

    async def failed_read(conn, ctx):
        raise RuntimeError("UPSTREAM-SECRET-CANARY")

    monkeypatch.setattr("evidencedesk.observability.health.signals", failed_read)
    result = client.get(
        f"/v1/workspaces/{workspace}/health?secret=QUERY-SECRET-CANARY",
        headers={"X-Untrusted": "HEADER-SECRET-CANARY"},
    )
    assert result.status_code == 500
    assert "SECRET-CANARY" not in result.text
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["http.route"] == "/v1/workspaces/{workspace_id}/health"
    assert not spans[0].events
    assert "SECRET-CANARY" not in str(spans[0].attributes)
    assert "SECRET-CANARY" not in client.app.state.telemetry.scrape().decode()
    captured = capfd.readouterr()
    assert "SECRET-CANARY" not in captured.out + captured.err


@pytest.mark.parametrize("failure", ["database", "deadline"])
def test_health_dependency_failure_is_bounded_and_sanitized(auth_client, monkeypatch, failure):
    client, state = auth_client
    session = sign_in(client, state)
    workspace = client.post(
        "/v1/workspaces", json={"name": "Dependency health"}, headers=headers(session)
    ).json()["id"]

    async def failed_read(conn, ctx):
        if failure == "deadline":
            await asyncio.sleep(10)
        raise SQLAlchemyError("DB-SECRET-CANARY")

    monkeypatch.setattr("evidencedesk.observability.health.signals", failed_read)
    result = client.get(f"/v1/workspaces/{workspace}/health")
    assert result.status_code == 503
    assert result.json()["code"] == "health_unavailable"
    assert "SECRET-CANARY" not in result.text
