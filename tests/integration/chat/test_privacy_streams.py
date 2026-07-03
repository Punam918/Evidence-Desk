import json
from uuid import uuid4

import psycopg
import pytest

from tests.support import headers, sign_in

pytestmark = pytest.mark.integration


def test_workspace_owner_cannot_read_another_members_chat(auth_client, database):
    client, state = auth_client
    first = sign_in(client, state)
    h = headers(first)
    workspace = client.post("/v1/workspaces", json={"name": "Private chats"}, headers=h).json()[
        "id"
    ]
    base = f"/v1/workspaces/{workspace}"
    private = client.post(
        base + "/conversations", json={"title": "Private conversation"}, headers=h
    )
    assert private.status_code == 201, private.text
    conversation = private.json()["id"]
    assert (
        client.post(
            base + "/conversations", json={"title": "Private conversation"}, headers=h
        ).json()
        == private.json()
    )
    state["subject"] = str(uuid4())
    second = sign_in(client, state)
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.memberships(workspace_id,user_id,role) VALUES(%s,%s,'owner')",
            (workspace, second["user"]["id"]),
        )
    assert client.get(base + "/conversations").json()["items"] == []
    assert client.get(base + f"/conversations/{conversation}").status_code == 404
    assert client.get(base + f"/conversations/{conversation}/messages").status_code == 404
    assert (
        client.delete(base + f"/conversations/{conversation}", headers=headers(second)).status_code
        == 404
    )


def test_completed_sse_replay_reads_committed_events(auth_client, database):
    client, state = auth_client
    identity = sign_in(client, state)
    h = headers(identity)
    workspace = client.post("/v1/workspaces", json={"name": "Replay"}, headers=h).json()["id"]
    base = f"/v1/workspaces/{workspace}"
    conversation = client.post(base + "/conversations", json={"title": "Saved"}, headers=h).json()[
        "id"
    ]
    actor = identity["user"]["id"]
    run, user_message, assistant = uuid4(), uuid4(), uuid4()
    answer = {"status": "abstained", "sentences": [], "limitations": ["No supporting sources."]}
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.messages(workspace_id,id,conversation_id,user_id,sequence,role,state,text) VALUES(%s,%s,%s,%s,1,'user','final','Question')",
            (workspace, user_message, conversation, actor),
        )
        conn.execute(
            "INSERT INTO app.messages(workspace_id,id,conversation_id,user_id,sequence,role,state,text,answer) VALUES(%s,%s,%s,%s,2,'assistant','final','',%s::jsonb)",
            (workspace, assistant, conversation, actor, json.dumps(answer)),
        )
        conn.execute(
            "INSERT INTO app.runs(workspace_id,id,conversation_id,user_id,user_message_id,assistant_message_id,status,intent,requested_model_key,config_sha256,prompt_version,next_event_seq,completed_at) VALUES(%s,%s,%s,%s,%s,%s,'completed','ask','local_standard','fixture','v1',3,now())",
            (workspace, run, conversation, actor, user_message, assistant),
        )
        conn.execute(
            "INSERT INTO app.run_events(workspace_id,run_id,sequence,type,data) VALUES(%s,%s,1,'run.accepted',%s::jsonb),(%s,%s,2,'answer.completed',%s::jsonb)",
            (
                workspace,
                run,
                json.dumps({"status": "queued"}),
                workspace,
                run,
                json.dumps({"message_id": str(assistant), "answer": answer, "citations": []}),
            ),
        )
    response = client.get(base + f"/runs/{run}/events")
    assert response.status_code == 200, response.text
    assert "id: 1\nevent: run.accepted" in response.text
    assert "id: 2\nevent: answer.completed" in response.text
    replay = client.get(base + f"/runs/{run}/events", headers={"Last-Event-ID": "1"})
    assert "run.accepted" not in replay.text and "answer.completed" in replay.text
    assert (
        client.get(base + f"/runs/{run}/events", headers={"Last-Event-ID": "3"}).status_code == 409
    )
    assert (
        client.get(base + f"/runs/{run}/events", headers={"Last-Event-ID": "bad"}).status_code
        == 409
    )
    assert client.get(base + f"/runs/{run}/events", headers={"Last-Event-ID": "2"}).text == ""
    messages = client.get(base + f"/conversations/{conversation}/messages").json()["items"]
    assert len(messages) == 2 and messages[1]["answer"] == answer
    assert messages[0]["run_id"] is None and messages[1]["run_id"] == str(run)
    cancelled = client.post(base + f"/runs/{run}/cancel", headers=h)
    assert cancelled.status_code == 202 and cancelled.json()["status"] == "completed"
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM app.run_events WHERE workspace_id=%s AND run_id=%s",
                (workspace, run),
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM app.model_usage WHERE workspace_id=%s AND run_id=%s",
                (workspace, run),
            ).fetchone()[0]
            == 0
        )
    assert client.delete(base + f"/conversations/{conversation}", headers=h).status_code == 202
    assert client.get(base + f"/runs/{run}/events").status_code == 404


def test_run_admission_is_atomic_and_idempotent(auth_client, database):
    client, state = auth_client
    identity = sign_in(client, state)
    h = headers(identity)
    workspace = client.post("/v1/workspaces", json={"name": "Runs"}, headers=h).json()["id"]
    base = f"/v1/workspaces/{workspace}"
    conversation = client.post(
        base + "/conversations", json={"title": "Run admission"}, headers=h
    ).json()["id"]
    kb = uuid4()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.knowledge_bases(workspace_id,id,name,created_by) VALUES(%s,%s,%s,%s)",
            (workspace, kb, "Fixtures", identity["user"]["id"]),
        )
    body = {
        "query": "What is the policy?",
        "kb_ids": [str(kb)],
        "model_key": "local_standard",
        "intent": "ask",
        "expected_conversation_version": 1,
    }
    response = client.post(base + f"/conversations/{conversation}/runs", json=body, headers=h)
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["status"] == "queued"
    assert (
        client.post(base + f"/conversations/{conversation}/runs", json=body, headers=h).json()
        == run
    )
    changed = {**body, "query": "Changed query"}
    assert (
        client.post(
            base + f"/conversations/{conversation}/runs", json=changed, headers=h
        ).status_code
        == 409
    )
    assert (
        client.post(
            base + f"/conversations/{conversation}/runs",
            json={**body, "expected_conversation_version": 2},
            headers=headers(identity),
        ).status_code
        == 409
    )
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM app.jobs WHERE workspace_id=%s AND run_id=%s",
                (workspace, run["id"]),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM app.workflow_threads WHERE workspace_id=%s AND run_id=%s",
                (workspace, run["id"]),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM app.messages WHERE workspace_id=%s AND conversation_id=%s",
                (workspace, conversation),
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM app.run_events WHERE workspace_id=%s AND run_id=%s",
                (workspace, run["id"]),
            ).fetchone()[0]
            == 1
        )
    cancelled = client.post(base + f"/runs/{run['id']}/cancel", headers=headers(identity))
    assert cancelled.status_code == 202
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT cancel_requested FROM app.runs WHERE workspace_id=%s AND id=%s",
            (workspace, run["id"]),
        ).fetchone()[0]


def test_history_run_comparison_rehydrates_current_metadata_and_hides_redacted_sources(
    auth_client, database
):
    client, state = auth_client
    identity = sign_in(client, state)
    h = headers(identity)
    workspace = client.post("/v1/workspaces", json={"name": "History compare"}, headers=h).json()[
        "id"
    ]
    base = f"/v1/workspaces/{workspace}"
    conversation = client.post(
        base + "/conversations", json={"title": "Saved compare"}, headers=h
    ).json()["id"]
    actor = identity["user"]["id"]
    kb, first_document, second_document = uuid4(), uuid4(), uuid4()
    first_revision, second_revision, run, user_message, assistant = (uuid4() for _ in range(5))
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.knowledge_bases(workspace_id,id,name,created_by) VALUES(%s,%s,'Policies',%s)",
            (workspace, kb, actor),
        )
        for document, revision, title, number in [
            (first_document, first_revision, "Before policy", 1),
            (second_document, second_revision, "After policy", 2),
        ]:
            conn.execute(
                "INSERT INTO app.documents(workspace_id,id,kb_id,title,created_by,active_revision_id,latest_revision_id) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (workspace, document, kb, title, actor, revision, revision),
            )
            conn.execute(
                "INSERT INTO app.document_revisions(workspace_id,id,document_id,kb_id,revision_no,status,object_key,mime_type,byte_size,content_sha256,parser_version,chunk_profile_id,profile_hash,embedding_profile_id,chunk_count) VALUES(%s,%s,%s,%s,%s,'published',%s,'text/plain',10,%s,'v1','v1','fixture','bge_small_en_v15',1)",
                (workspace, revision, document, kb, number, f"private/{revision}", "a" * 64),
            )
        conn.execute(
            "INSERT INTO app.messages(workspace_id,id,conversation_id,user_id,sequence,role,state,text) VALUES(%s,%s,%s,%s,1,'user','final','Compare policies')",
            (workspace, user_message, conversation, actor),
        )
        conn.execute(
            "INSERT INTO app.messages(workspace_id,id,conversation_id,user_id,sequence,role,state,text) VALUES(%s,%s,%s,%s,2,'assistant','final','Saved comparison')",
            (workspace, assistant, conversation, actor),
        )
        conn.execute(
            "INSERT INTO app.runs(workspace_id,id,conversation_id,user_id,user_message_id,assistant_message_id,status,intent,requested_model_key,config_sha256,prompt_version,execution_metadata,next_event_seq,completed_at) VALUES(%s,%s,%s,%s,%s,%s,'completed','compare','local_standard','fixture','v1',%s::jsonb,1,now())",
            (
                workspace,
                run,
                conversation,
                actor,
                user_message,
                assistant,
                json.dumps({"revision_ids": [str(first_revision), str(second_revision)]}),
            ),
        )
    response = client.get(base + f"/runs/{run}", headers=h)
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["intent"] == "compare"
    assert [(side["side"], side["title"], side["available"]) for side in view["comparison"]] == [
        ("A", "Before policy", True),
        ("B", "After policy", True),
    ]
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.runs SET execution_metadata=execution_metadata||'{\"source_redacted\":true}'::jsonb WHERE workspace_id=%s AND id=%s",
            (workspace, run),
        )
    redacted = client.get(base + f"/runs/{run}", headers=h).json()["comparison"]
    assert [
        (side["available"], side["title"], side["document_id"], side["revision_no"])
        for side in redacted
    ] == [
        (False, None, None, None),
        (False, None, None, None),
    ]
