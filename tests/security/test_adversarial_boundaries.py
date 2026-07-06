"""Structural defenses for draft attack strings; these are not semantic model evals."""

import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from evidencedesk.ai.quoting import quoted
from evidencedesk.providers.schema import parse_output
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.support import headers, sign_in

CASES = [
    json.loads(line)
    for line in Path("evals/datasets/benchmark-v1-draft/adversarial.jsonl").read_text().splitlines()
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
def test_draft_attack_cannot_break_quoted_records_or_add_model_effect_fields(case):
    text = case["untrusted_text"]
    record = {"carrier": case["carrier"], "text": text}
    encoded = quoted(record)
    assert json.loads(encoded) == record
    assert "<" not in encoded and ">" not in encoded and "&" not in encoded
    # Even a provider obeying the injected effect instruction cannot extend the output protocol.
    raw = json.dumps(
        {
            "status": "answered",
            "sentences": [{"text": text, "citation_ids": ["E1"]}],
            "limitations": [],
            "tool_calls": [{"name": "save_brief", "arguments": record}],
        }
    )
    with pytest.raises(ValidationError):
        parse_output("answer", raw)
    with pytest.raises(ValidationError):
        parse_output(
            "answer",
            json.dumps(
                {
                    "status": "answered",
                    "sentences": [{"text": text, "citation_ids": ["E9"]}],
                    "limitations": [],
                }
            ),
        )


@settings(max_examples=300, derandomize=True)
@given(st.text(max_size=2048))
def test_arbitrary_unicode_roundtrips_as_data_without_new_markup_records(text):
    encoded = quoted({"text": text})
    assert json.loads(encoded) == {"text": text}
    assert not any(char in encoded for char in "<>&")


@pytest.mark.integration
def test_200_attack_queries_cannot_admit_runs_to_another_creators_conversation(
    auth_client, database
):
    client, state = auth_client
    victim = sign_in(client, state)
    h = headers(victim)
    wid = client.post(
        "/v1/workspaces", json={"name": "Private adversarial fixture"}, headers=h
    ).json()["id"]
    base = f"/v1/workspaces/{wid}"
    conversation = client.post(
        base + "/conversations", json={"title": "PRIVATE-SECRET-CANARY"}, headers=h
    ).json()["id"]
    kb = client.post(base + "/knowledge-bases", json={"name": "Knowledge"}, headers=h).json()["id"]
    for index, case in enumerate(CASES):
        if index % 25 == 0:
            # Use real independent owner sessions so the auth boundary, rather than the
            # per-user rate limiter, is reached for every case. No authorization mocks.
            state["subject"] = str(uuid4())
            attacker = sign_in(client, state)
            with psycopg.connect(database["operator"]) as conn:
                conn.execute(
                    "INSERT INTO app.memberships(workspace_id,user_id,role) VALUES(%s,%s,'owner')",
                    (wid, attacker["user"]["id"]),
                )
        # All strings are intentionally used as queries here. Carrier-specific model behavior is unmeasured.
        response = client.post(
            base + f"/conversations/{conversation}/runs",
            json={
                "query": case["untrusted_text"],
                "kb_ids": [kb],
                "model_key": "local_standard",
                "intent": "ask",
                "expected_conversation_version": 1,
            },
            headers=headers(attacker),
        )
        assert response.status_code == 404, (case["case_id"], response.status_code, response.text)
        assert "PRIVATE-SECRET-CANARY" not in response.text
        assert client.get(base + f"/conversations/{conversation}/messages").status_code == 404
    with psycopg.connect(database["operator"]) as conn:
        for table in ("runs", "jobs", "model_usage", "briefs", "tool_executions"):
            # Table names are a code-owned fixed tuple, never an attack string.
            assert conn.execute(
                f"SELECT count(*) FROM app.{table} WHERE workspace_id=%s", (wid,)
            ).fetchone() == (0,)
