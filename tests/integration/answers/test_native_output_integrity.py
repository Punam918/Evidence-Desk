import pytest

from tests.integration.answers.test_execution import grounded as grounded

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "text", ['The policy says "invented quotation".', "Misleading [E8]", "Visible\u202ehidden"]
)
async def test_invalid_output_never_becomes_a_draft_or_saved_answer(grounded, text):
    h = grounded
    h.observed["answer"] = {
        "status": "answered",
        "sentences": [{"text": text, "citation_ids": ["E1"]}],
        "limitations": [],
    }
    run = h.admit()
    await h.execute()
    row = h.row(run)
    assert row[0:2] == ("failed", "invalid_model_output")
    events = h.client.get(h.base + f"/runs/{run['id']}/events").text
    assert "answer.delta" not in events and "answer.completed" not in events
    assert "invented quotation" not in events and "hidden" not in events
    messages = h.client.get(h.base + f"/conversations/{run['conversation_id']}/messages").json()[
        "items"
    ]
    assert all(m["answer"] is None for m in messages)
    assert len(h.observed["calls"]) == 1


async def test_verified_source_quote_completes_through_native_graph(grounded):
    h = grounded
    h.observed["answer"] = {
        "status": "answered",
        "sentences": [
            {"text": "The source says “Fixture evidence passage 0.”", "citation_ids": ["E1"]}
        ],
        "limitations": [],
    }
    run = h.admit()
    await h.execute()
    assert h.row(run)[0] == "completed"
    events = h.client.get(h.base + f"/runs/{run['id']}/events").text
    assert "answer.completed" in events
