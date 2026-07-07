import asyncio
import os
import sys
import time
from uuid import UUID

import pytest
from evidencedesk.retrieval.rerank_process import RerankProcess, RerankUnavailable
from evidencedesk.retrieval.rerank_protocol import Pair, ScoreRequest

REQUEST = ScoreRequest(query="question", passages=[Pair(id=UUID(int=1), text="source")])


async def fake_process(tmp_path, mode, monkeypatch, deadline=0.15):
    script = tmp_path / "child.py"
    script.write_text("""import sys,json,time,os
assert "ED_SECRET" not in os.environ
print(json.dumps({"ready":"a"*64}),flush=True)
for line in sys.stdin:
 r=json.loads(line)
 mode=sys.argv[1]
 if mode=="hang": time.sleep(30)
 if mode=="exit": sys.exit(1)
 score={"id":r["passages"][0]["id"],"logit":1.0,"query_tokens":1,"document_tokens":1,
        "pair_tokens":5,"query_clipped":False,"document_clipped":False}
 if mode=="nan": score["logit"]=float("nan")
 if mode=="wrong_id": score["id"]="00000000-0000-0000-0000-000000000099"
 if mode=="bad_counts": score["pair_tokens"]=512
 scores=[] if mode=="count" else [score]
 response={"fingerprint":"b"*64 if mode=="fingerprint" else "a"*64,"scores":scores}
 if mode=="oversized": print("x"*20000,flush=True)
 else: print(json.dumps(response),flush=True)
""")
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        assert args[1:4] == ("-I", "-m", "evidencedesk.retrieval.rerank_entry")
        assert "ED_SECRET" not in kwargs["env"]
        return await original(sys.executable, "-I", str(script), mode, **kwargs)

    monkeypatch.setenv("ED_SECRET", "must not reach child")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    process = RerankProcess(tmp_path, tmp_path / "lock", deadline=deadline)
    await process.start()
    return process


@pytest.mark.parametrize(
    "mode", ["hang", "exit", "nan", "wrong_id", "bad_counts", "count", "fingerprint", "oversized"]
)
async def test_protocol_faults_kill_child_and_do_not_retry(tmp_path, monkeypatch, mode):
    process = await fake_process(tmp_path, mode, monkeypatch)
    child = process.process
    started = time.perf_counter()
    with pytest.raises(RerankUnavailable):
        await process.score(REQUEST)
    assert time.perf_counter() - started < 2
    assert child.returncode is not None and process.process is None
    with pytest.raises(ProcessLookupError):
        os.kill(child.pid, 0)
    with pytest.raises(RerankUnavailable):
        await process.score(REQUEST)


async def test_busy_sheds_without_queue_and_cancellation_reaps(tmp_path, monkeypatch):
    process = await fake_process(tmp_path, "hang", monkeypatch, deadline=2)
    child = process.process
    task = asyncio.create_task(process.score(REQUEST))
    await asyncio.sleep(0.03)
    with pytest.raises(RerankUnavailable):
        await process.score(REQUEST)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.returncode is not None
    await process.close()


async def test_success_preserves_protocol_order_and_process_reuse(tmp_path, monkeypatch):
    process = await fake_process(tmp_path, "ok", monkeypatch)
    try:
        for _ in range(2):
            result = await process.score(REQUEST)
            assert result.scores[0].id == REQUEST.passages[0].id
    finally:
        await process.close()
