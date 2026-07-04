from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from evidencedesk.chat.service import ChatService
from evidencedesk.db.session import engine
from evidencedesk.db.stream_slots import StreamSlot
from evidencedesk.errors import Problem
from evidencedesk.events.stream import stream_events
from evidencedesk.identity.oidc import OIDC
from evidencedesk.identity.service import IdentityService
from evidencedesk.pagination import Cursors

from tests.support import headers, sign_in

pytestmark = pytest.mark.integration


async def test_stream_limits_apply_across_connections(database):
    slots = []
    user = uuid4()
    try:
        for _ in range(3):
            slots.append(await StreamSlot.acquire(database["ed_identity"], user))
        with pytest.raises(Problem) as error:
            await StreamSlot.acquire(database["ed_identity"], user)
        assert error.value.code == "stream_limit"
        for _ in range(7):
            slots.append(await StreamSlot.acquire(database["ed_identity"], uuid4()))
        with pytest.raises(Problem):
            await StreamSlot.acquire(database["ed_identity"], uuid4())
    finally:
        for slot in slots:
            await slot.close()
    replacement = await StreamSlot.acquire(database["ed_identity"], user)
    await replacement.close()


async def test_active_stream_stops_after_workspace_tombstone(auth_client, database):
    client, state = auth_client
    user = sign_in(client, state)
    h = headers(user)
    workspace = client.post("/v1/workspaces", json={"name": "Revocation"}, headers=h).json()["id"]
    base = f"/v1/workspaces/{workspace}"
    conversation = client.post(
        base + "/conversations", json={"title": "Active stream"}, headers=h
    ).json()["id"]
    kb = uuid4()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.knowledge_bases(workspace_id,id,name,created_by) VALUES(%s,%s,%s,%s)",
            (workspace, kb, "Sources", user["user"]["id"]),
        )
    run = client.post(
        base + f"/conversations/{conversation}/runs",
        json={
            "query": "Question",
            "kb_ids": [str(kb)],
            "model_key": "local_standard",
            "intent": "ask",
            "expected_conversation_version": 1,
        },
        headers=h,
    ).json()
    db, identity_db = engine(database["ed_app"]), engine(database["ed_identity"])
    settings = client.app.state.settings

    async def connected():
        return False

    async with httpx.AsyncClient() as http:
        identity = IdentityService(settings, identity_db, OIDC(settings, http))
        chat = ChatService(db, Cursors(settings.cursor_key_file.read_bytes().strip()), settings)
        slot = await StreamSlot.acquire(database["ed_identity"], UUID(user["user"]["id"]))
        stream = stream_events(
            identity,
            chat,
            slot,
            client.cookies.get("ed_session"),
            UUID(workspace),
            UUID(run["id"]),
            uuid4(),
            0,
            connected,
        )
        try:
            assert b"run.accepted" in await anext(stream)
            assert client.delete(base, headers=h).status_code == 202
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
        finally:
            await stream.aclose()
            await db.dispose()
            await identity_db.dispose()
