"""Measured synthetic local conformance smoke, not an answer-quality benchmark."""
import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path

import httpx
from evidencedesk.providers.base import ModelFault, ModelRequest, ModelResult, TextDelta
from evidencedesk.providers.local_tokens import LocalTokens
from evidencedesk.providers.ollama import OllamaAdapter

ROOT=Path(__file__).resolve().parents[1]

async def main():
    runtime=json.loads((ROOT/'.cache/local-smoke-runtime.json').read_text())
    lock=json.loads((ROOT/'contracts/config/local-runtime.lock.json').read_text())
    peak=0
    stop=threading.Event()
    def memory():
        nonlocal peak
        with httpx.Client(transport=httpx.HTTPTransport(uds='/var/run/docker.sock'),timeout=5) as docker:
            while not stop.is_set():
                try:
                    sample=docker.get(f"http://docker/containers/{runtime['container']}/stats?stream=false").json()
                    peak=max(peak,sample['memory_stats']['usage'])
                except (httpx.HTTPError,KeyError): pass
                stop.wait(.5)
    monitor=threading.Thread(target=memory,daemon=True);monitor.start()
    warm_started=time.monotonic()
    report={'scope':'One synthetic CPU-only conformance request, not a quality/latency benchmark.','runtime_image':lock['runtime_image'],'model_digest':lock['digest'],'model_id':lock['model_id'],'cpu_limit':4,'memory_limit_bytes':9*1024**3,'context_cap':8192,'network':'isolated loopback-only Docker container, no cloud mode; existing services untouched'}
    try:
        async with httpx.AsyncClient(timeout=180,trust_env=False) as client:
            warm=await client.post(runtime['endpoint']+'/api/generate',json={'model':lock['model_id'],'keep_alive':'5m','options':{'num_ctx':8192,'num_predict':160},'stream':False,'think':False})
            warm.raise_for_status()
        report['warmup_seconds']=round(time.monotonic()-warm_started,3)
        adapter=OllamaAdapter(runtime['endpoint'],lock['model_id'],lock['digest'],LocalTokens(ROOT/'.cache/models/qwen3-tokenizer',ROOT/'contracts/config/local-tokenizer.lock.json'))
        request=ModelRequest('local_standard',lock['model_id'],'Answer only from evidence. Return the required JSON with status answered, one concise sentence citing E1, and no limitations. Do not include reasoning.','How long are records retained?','E1: Records are retained for 30 days.',context_cap=8192,max_output_tokens=160)
        report['input_estimate']=adapter.estimate(request).tokens
        started=time.monotonic();first=None;terminal=None
        try:
            async for event in adapter.stream(request):
                if isinstance(event,TextDelta) and first is None: first=time.monotonic()-started
                if isinstance(event,(ModelResult,ModelFault)): terminal=event
        finally: await adapter.close()
        report['generation_seconds']=round(time.monotonic()-started,3)
        report['first_delta_seconds']=round(first,3) if first is not None else None
        if isinstance(terminal,ModelResult):
            report['status']='pass' if terminal.status=='completed' and terminal.usage and terminal.answer and terminal.answer.status=='answered' else 'fail'
            report['normalized_status']=terminal.status
            report['answer']=terminal.answer.model_dump() if terminal.answer else None
            report['usage']=terminal.usage.model_dump() if terminal.usage else None
        else:
            report['status']='fail';report['error_code']=terminal.code if terminal else 'missing_terminal'
    except Exception as exc:
        report['status']='fail';report['error_type']=type(exc).__name__
    finally:
        stop.set();monitor.join(timeout=6)
        report['peak_container_usage_bytes']=peak
        report['container_state']=json.loads(subprocess.check_output(['docker','inspect',runtime['container'],'--format','{{json .State}}'],text=True))
        (ROOT/'docs/10-testing/local-model-smoke.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2))
        subprocess.run(['docker','rm','-f',runtime['container']],check=True,capture_output=True)
    if report['status']!='pass': raise SystemExit(1)

asyncio.run(main())
