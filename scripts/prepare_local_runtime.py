"""Prepare a disposable, isolated CPU Ollama smoke runtime and pin its artifacts.

Never reconfigures or stops the operator's existing Ollama service.
"""
import hashlib
import json
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT=Path(__file__).resolve().parents[1]
IMAGE='ollama/ollama:0.40.3'
MODEL='qwen3:8b'
cache=ROOT/'.cache/ollama'
cache.mkdir(parents=True,exist_ok=True)
for _ in range(900):
    result=subprocess.run(['docker','image','inspect',IMAGE,'--format','{{index .RepoDigests 0}}'],capture_output=True,text=True)
    if result.returncode==0: break
    time.sleep(1)
else: raise RuntimeError('Pinned Ollama image is not available after15min; inspect image pull log')
image=result.stdout.strip()
name='ed-local-smoke-'+uuid4().hex[:10]
subprocess.run(['docker','run','--rm','-d','--name',name,'--memory','9g','--memory-swap','9g','--cpus','4','-p','127.0.0.1::11434',
    '-e','OLLAMA_NUM_PARALLEL=1','-e','OLLAMA_MAX_LOADED_MODELS=1','-e','OLLAMA_NO_CLOUD=1','-v',f'{cache}:/root/.ollama',image],check=True,capture_output=True)
try:
    port=subprocess.check_output(['docker','port',name,'11434/tcp'],text=True).strip().rsplit(':',1)[1]
    endpoint=f'http://127.0.0.1:{port}'
    for _ in range(100):
        try:
            version=httpx.get(endpoint+'/api/version',timeout=2).json()['version']
            break
        except (httpx.HTTPError,KeyError): time.sleep(.2)
    else: raise RuntimeError('Isolated Ollama failed startup')
    state={'container':name,'endpoint':endpoint,'image':image,'runtime_version':version}
    (ROOT/'.cache/local-smoke-runtime.json').write_text(json.dumps(state,indent=2)+'\n')
    print('Runtime started:',name,endpoint,flush=True)
    with httpx.Client(timeout=1200,trust_env=False) as client:
        response=client.post(endpoint+'/api/pull',json={'model':MODEL,'stream':False});response.raise_for_status()
        if response.json().get('status')!='success': raise RuntimeError('Model pull did not finish')
        models=client.get(endpoint+'/api/tags').json()['models']
        model=next(item for item in models if item['name']==MODEL)
        alias='evidencedesk-qwen3-8b:'+model['digest'][:16]
        client.post(endpoint+'/api/copy',json={'source':MODEL,'destination':alias}).raise_for_status()
        shown=client.post(endpoint+'/api/show',json={'model':alias});shown.raise_for_status();details=shown.json()
    lock={'runtime_version':version,'runtime_image':image,'upstream_model':MODEL,'model_id':alias,'digest':model['digest'],'size_bytes':model['size'],
        'details':model['details'],'template_sha256':hashlib.sha256(details['template'].encode()).hexdigest(),'license_sha256':hashlib.sha256(details.get('license','').encode()).hexdigest(),
        'capabilities':details.get('capabilities',[]),'tokenizer_lock':'contracts/config/local-tokenizer.lock.json','conformance':'not_yet_probed'}
    (ROOT/'contracts/config/local-runtime.lock.json').write_text(json.dumps(lock,indent=2)+'\n')
    print('Pinned local model:',alias,'bytes:',model['size'],flush=True)
except BaseException:
    subprocess.run(['docker','rm','-f',name],capture_output=True)
    raise
