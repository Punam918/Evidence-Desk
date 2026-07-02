"""Live HTTPS/OIDC/upload/retrieval/local-generation smoke using public application APIs.

Creates an explicitly synthetic workspace and leaves it available for browser inspection.
Does not read runtime DB credentials, modify readiness, call model endpoints, or retry a run.
"""

import argparse
import hashlib
import json
import re
import socket
import ssl
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from uuid import uuid4

import httpx

if TYPE_CHECKING:
    from scripts.validate_compose import ORIGIN, login, mounted_file
elif __package__:
    from .validate_compose import ORIGIN, login, mounted_file
else:
    from validate_compose import ORIGIN, login, mounted_file

ROOT = Path(__file__).resolve().parents[1]
POLICY = b"""# Harbor support records retention policy

This is a fictional organization policy created for an EvidenceDesk integration smoke.

## Retention period

Completed support case records are retained for 37 days after the case is closed.
At the end of the 37-day retention period, the records are permanently deleted.
The support operations team owns this retention schedule.
"""
QUESTION = "For how many days are completed support case records retained after case closure?"
TERMINAL_EVENTS = {"answer.completed", "run.failed", "run.cancelled", "run.refused"}
TERMINAL_RUNS = {"completed", "failed", "cancelled", "interrupted", "refused"}


class SmokeFailure(RuntimeError):
    pass


def runtime_images(project: str) -> dict[str, str]:
    # Only exact image identities, never docker-inspect environment or command fields.
    services = ("api", "chat", "ingest", "index", "parser", "ollama", "qdrant", "postgres")
    values = subprocess.check_output(
        [
            "docker",
            "inspect",
            "--format",
            "{{.Image}}",
            *[f"{project}-{service}-1" for service in services],
        ],
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=10,
    ).splitlines()
    if len(values) != len(services) or any(
        not re.fullmatch(r"sha256:[a-f0-9]{64}", value) for value in values
    ):
        raise SmokeFailure("runtime_image_identity_unavailable")
    return dict(zip(services, values, strict=True))


def expect(response: httpx.Response, status: int) -> dict[str, Any]:
    if response.status_code != status:
        code = "unknown"
        try:
            candidate = response.json().get("code")
            if isinstance(candidate, str) and re.fullmatch(r"[a-z0-9_]{1,80}", candidate):
                code = candidate
        except ValueError, AttributeError:
            pass
        raise SmokeFailure(f"http_{response.status_code}_expected_{status}_{code}")
    if status == 204:
        return {}
    value = response.json()
    if not isinstance(value, dict):
        raise SmokeFailure("non_object_response")
    return value


def stage(report: dict[str, Any], name: str) -> None:
    report["stage"] = name
    print(json.dumps({"stage": name}), flush=True)


def headers(identity: dict[str, Any]) -> dict[str, str]:
    return {
        "origin": ORIGIN,
        "x-csrf-token": identity["csrf_token"],
        "idempotency-key": str(uuid4()),
    }


def await_model(client: httpx.Client, base: str, report: dict[str, Any], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_notice = 0.0
    while time.monotonic() < deadline:
        items = expect(client.get(base + "/models"), 200)["items"]
        local = next((item for item in items if item["key"] == "local_standard"), None)
        report["local_model"] = local
        if local and local["available"]:
            if local["provider"] != "ollama" or local["privacy_class"] != "private":
                raise SmokeFailure("unexpected_model_provider_or_privacy")
            return
        if time.monotonic() - last_notice >= 20:
            print(json.dumps({"waiting": "local_model_readiness"}), flush=True)
            last_notice = time.monotonic()
        time.sleep(2)
    raise SmokeFailure("local_model_readiness_timeout")


def await_publication(
    client: httpx.Client, base: str, accepted: dict[str, Any], report: dict[str, Any], timeout: int
) -> None:
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        job = expect(client.get(base + "/jobs/" + accepted["job"]["job_id"]), 200)
        document = expect(client.get(base + "/documents/" + accepted["document"]["id"]), 200)
        report["ingestion"] = {"job": job, "document": document}
        status = (job["status"], document["status"])
        if status != last_status:
            print(json.dumps({"ingestion_status": status}), flush=True)
            last_status = status
        if job["status"] in ("dead", "cancelled"):
            raise SmokeFailure("ingestion_terminal_failure")
        if (
            job["status"] == "succeeded"
            and document["active_revision_id"] == accepted["revision_id"]
        ):
            revisions = expect(
                client.get(base + "/documents/" + document["id"] + "/revisions"), 200
            )
            revision = next(
                item for item in revisions["items"] if item["id"] == accepted["revision_id"]
            )
            report["ingestion"]["revision"] = revision
            if revision["status"] != "published" or revision["chunk_count"] < 1:
                raise SmokeFailure("ingestion_not_published_with_chunks")
            return
        time.sleep(2)
    raise SmokeFailure("ingestion_publication_timeout")


def collect_events(
    client: httpx.Client, base: str, run: dict[str, Any], result: dict[str, Any], timeout: int
) -> None:
    expected_url = base + "/runs/" + run["id"] + "/events"
    if run["events_url"] != expected_url:
        raise SmokeFailure("unexpected_run_events_url")
    deadline = time.monotonic() + timeout
    last_sequence = 0
    summaries: list[dict[str, Any]] = []
    result["events"] = summaries
    total_bytes = 0
    finished = False
    while not finished and time.monotonic() < deadline:
        with client.stream(
            "GET", expected_url, headers={"last-event-id": str(last_sequence)}
        ) as response:
            if response.status_code != 200:
                raise SmokeFailure(f"events_http_{response.status_code}")
            if not response.headers.get("content-type", "").startswith("text/event-stream"):
                raise SmokeFailure("events_wrong_content_type")
            pending: dict[str, str] = {}
            for line in response.iter_lines():
                total_bytes += len(line.encode())
                if total_bytes > 4 * 1024 * 1024 or time.monotonic() >= deadline:
                    raise SmokeFailure("event_stream_limit")
                if line.startswith(":"):
                    continue
                if line:
                    key, separator, value = line.partition(":")
                    if separator and key in ("id", "event", "data"):
                        if key in pending:
                            raise SmokeFailure("duplicate_sse_field")
                        pending[key] = value.lstrip(" ")
                    continue
                if not pending:
                    continue
                event = json.loads(pending["data"])
                sequence = int(pending["id"])
                if (
                    sequence != last_sequence + 1
                    or event["seq"] != sequence
                    or event["run_id"] != run["id"]
                    or event["type"] != pending["event"]
                ):
                    raise SmokeFailure("event_order_or_identity_invalid")
                last_sequence = sequence
                summaries.append({"seq": sequence, "type": event["type"]})
                if event["type"] == "run.phase":
                    print(json.dumps({"run_phase": event["data"]}), flush=True)
                if event["type"] in TERMINAL_EVENTS:
                    result["terminal_event"] = event
                    finished = True
                    break
                pending = {}
        result["run"] = expect(client.get(base + "/runs/" + run["id"]), 200)
        if result["run"]["status"] in TERMINAL_RUNS and not finished:
            raise SmokeFailure("terminal_run_without_terminal_event")
    if not finished:
        raise SmokeFailure("run_event_timeout")
    if result["run"]["status"] != "completed":
        raise SmokeFailure("run_did_not_complete")


def ask(
    client: httpx.Client,
    identity: dict[str, Any],
    base: str,
    kb_id: str,
    report: dict[str, Any],
    key: str,
    timeout: int,
) -> dict[str, Any]:
    conversation = expect(
        client.post(
            base + "/conversations",
            headers=headers(identity),
            json={"title": "Retention policy" if key == "answer" else "Empty knowledge base"},
        ),
        201,
    )
    result: dict[str, Any] = {"conversation": conversation, "query": QUESTION, "kb_id": kb_id}
    report[key] = result
    run = expect(
        client.post(
            base + "/conversations/" + conversation["id"] + "/runs",
            headers=headers(identity),
            json={
                "query": QUESTION,
                "kb_ids": [kb_id],
                "model_key": "local_standard",
                "intent": "ask",
                "expected_conversation_version": conversation["version"],
            },
        ),
        202,
    )
    result["run"] = run
    collect_events(client, base, run, result, timeout)
    messages = expect(client.get(base + "/conversations/" + conversation["id"] + "/messages"), 200)[
        "items"
    ]
    assistant = next(
        message for message in messages if message["id"] == result["run"]["message_id"]
    )
    if not isinstance(assistant, dict):
        raise SmokeFailure("invalid_assistant_message")
    result["message"] = assistant
    if assistant["state"] != "final" or assistant["source_status"] != "available":
        raise SmokeFailure("assistant_message_not_final_available")
    return assistant


def run(project: str, report: dict[str, Any], timeout: int) -> None:
    ca = mounted_file(project, "trust/ca.crt")
    password = mounted_file(project, "operator/demo_password").decode().strip()
    context = ssl.create_default_context(cadata=ca.decode())
    report["ca_sha256"] = hashlib.sha256(ca).hexdigest()
    report["runtime_images"] = runtime_images(project)
    resolver = socket.getaddrinfo

    def local_dns(host: str, *args: Any, **kwargs: Any) -> Any:
        return resolver("127.0.0.1" if host == "evidencedesk.localhost" else host, *args, **kwargs)

    with (
        patch("socket.getaddrinfo", local_dns),
        httpx.Client(
            base_url=ORIGIN,
            verify=context,
            trust_env=False,
            timeout=httpx.Timeout(20, connect=5, write=15, pool=5),
            follow_redirects=False,
        ) as client,
    ):
        stage(report, "native_oidc_login")
        identity = login(client, password)
        stage(report, "synthetic_workspace")
        workspace = expect(
            client.post(
                "/v1/workspaces",
                headers=headers(identity),
                json={
                    "name": "Harbor operations · integration "
                    + datetime.now(UTC).strftime("%H%M%S")
                },
            ),
            201,
        )
        report["workspace"] = workspace
        base = "/v1/workspaces/" + workspace["id"]
        current = expect(client.get(base + "/settings"), 200)
        policy = {key: value for key, value in current.items() if key != "version"}
        policy.update(
            privacy_mode="local_only",
            retrieval_profile="hybrid_rerank",
            expected_version=current["version"],
        )
        report["policy"] = expect(
            client.patch(base + "/settings", headers=headers(identity), json=policy), 200
        )
        kb = expect(
            client.post(
                base + "/knowledge-bases",
                headers=headers(identity),
                json={"name": "Harbor policies"},
            ),
            201,
        )
        report["knowledge_base"] = kb
        stage(report, "upload_and_publish")
        accepted = expect(
            client.post(
                base + "/knowledge-bases/" + kb["id"] + "/documents",
                headers=headers(identity),
                files={"file": ("harbor-retention-policy.md", POLICY, "text/markdown")},
            ),
            202,
        )
        report["upload"] = accepted
        await_publication(client, base, accepted, report, timeout)
        stage(report, "local_model_readiness")
        await_model(client, base, report, timeout)
        stage(report, "native_grounded_answer")
        message = ask(client, identity, base, kb["id"], report, "answer", timeout)
        answer = message.get("answer") or {}
        if answer.get("status") != "answered" or not re.search(
            r"\b37\s+days\b", message["text"], re.I
        ):
            raise SmokeFailure("answer_does_not_match_synthetic_retention_fact")
        cited = {label for sentence in answer["sentences"] for label in sentence["citation_ids"]}
        citations = {citation["evidence_id"]: citation for citation in message["citations"]}
        if not cited or not cited <= citations.keys():
            raise SmokeFailure("answer_missing_bound_citations")
        stage(report, "citation_source_inspection")
        chunks = []
        for label in sorted(cited):
            citation = citations[label]
            if (
                not citation["available"]
                or citation["document_id"] != accepted["document"]["id"]
                or citation["revision_id"] != accepted["revision_id"]
            ):
                raise SmokeFailure("citation_does_not_reference_published_fixture")
            chunk = expect(client.get(base + "/chunks/" + citation["chunk_id"]), 200)
            chunks.append(chunk)
        report["answer"]["inspected_chunks"] = chunks
        if not any("37 days" in chunk["text"] for chunk in chunks):
            raise SmokeFailure("cited_source_does_not_contain_retention_fact")
        stage(report, "empty_knowledge_base_abstention")
        empty = expect(
            client.post(
                base + "/knowledge-bases",
                headers=headers(identity),
                json={"name": "Empty policy collection"},
            ),
            201,
        )
        await_model(client, base, report, timeout)
        message = ask(client, identity, base, empty["id"], report, "abstention", timeout)
        answer = message.get("answer") or {}
        if (
            answer.get("status") != "abstained"
            or answer.get("sentences")
            or message["citations"]
            or not answer.get("limitations")
        ):
            raise SmokeFailure("empty_scope_did_not_abstain")
        stage(report, "logout")
        expect(client.post("/auth/logout", headers=headers(identity)), 204)
        if runtime_images(project) != report["runtime_images"]:
            raise SmokeFailure("runtime_images_changed_during_smoke")
        report["status"] = "passed"
        stage(report, "complete")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="evidencedesk")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new report path; evidence is not overwritten")
    if not 30 <= args.timeout_seconds <= 900:
        parser.error("Each stage timeout must be between30and900seconds")
    report: dict[str, Any] = {
        "version": 1,
        "collected_at": datetime.now(UTC).isoformat(),
        "project": args.project,
        "origin": ORIGIN,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "status": "failed",
        "fixture": {
            "kind": "fictional organization policy",
            "sha256": hashlib.sha256(POLICY).hexdigest(),
            "content": POLICY.decode(),
        },
        "model_lock": json.loads((ROOT / "contracts/config/local-runtime.lock.json").read_text()),
        "limitations": [
            "One synthetic integrated workflow, not a retrieval or answer-quality benchmark",
            "Existing verified model weights are reused; cold-cache downloads are not tested",
            "Synthetic workspace remains available for live UI inspection",
        ],
    }
    try:
        run(args.project, report, args.timeout_seconds)
    except Exception as error:
        report["failure"] = {
            "type": type(error).__name__,
            "code": str(error) if isinstance(error, SmokeFailure) else "operation_failed",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {"status": report["status"], "stage": report.get("stage"), "report": str(args.output)}
        ),
        flush=True,
    )
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
