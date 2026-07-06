import json
from uuid import uuid4

from evidencedesk.errors import Problem
from evidencedesk.workers.runtime import consume, event


async def test_worker_failure_does_not_print_exception_codes_or_details(capfd):
    async def claim():
        return object()

    async def run(_):
        raise Problem(503, "SECRET-CANARY-CODE", "SECRET-CANARY-DETAIL", True)

    async def sweep():
        return 0

    assert await consume(claim, run, sweep, once=True) == 1
    output = capfd.readouterr()
    assert "SECRET-CANARY" not in output.out + output.err
    record = json.loads(output.out)
    assert record["event"] == "worker_operation_failed" and record["status"] == 503


def test_index_operator_logs_accept_only_typed_fixed_fields(capfd):
    workspace = uuid4()
    event(
        "index_audit",
        workspace_id=workspace,
        checked=10,
        repairs=1,
        failures="SECRET-CANARY",
        next_revision="SECRET-CANARY",
        extra="SECRET-CANARY",
    )
    event("SECRET-CANARY", secret="SECRET-CANARY")
    output = capfd.readouterr()
    assert "SECRET-CANARY" not in output.out + output.err
    record = json.loads(output.out)
    assert record["workspace_id"] == str(workspace) and record["checked"] == 10
    assert "failures" not in record and "next_revision" not in record
