#!/usr/bin/env python3
"""Disposable synthetic vendor-contract probes; no application, models or paid APIs."""
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import time
import uuid
from typing import TypedDict
import urllib.request
import urllib.error

import yaml
import psycopg
from psycopg.rows import dict_row
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command, Interrupt
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from qdrant_client import QdrantClient, models

ROOT=Path(__file__).resolve().parents[1]
PG='postgres@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336'
QD='qdrant/qdrant@sha256:b7b0444c4c351c970b98e90a6f89c2ee4287c65b44e52b4cb503fa5b2aa927ad'
containers=[]
checks=[]
os.environ['LANGSMITH_TRACING']='false'
os.environ['LANGCHAIN_TRACING_V2']='false'

def shell(args):
    return subprocess.run(args,check=True,text=True,capture_output=True).stdout.strip()

def start(image,port,extra):
    name='ed-contract-'+uuid.uuid4().hex[:12]
    shell(['docker','run','--rm','-d','--name',name,'--memory','1g','-p',f'127.0.0.1::{port}',*extra,image])
    containers.append(name)
    hostport=shell(['docker','port',name,f'{port}/tcp']).split(':')[-1]
    return name,int(hostport)

class ProbeJSON:
    """Probe codec: primitives plus explicit Interrupt/tuple envelopes, no dynamic imports."""
    @staticmethod
    def encode(obj, depth=0):
        if depth>32: raise ValueError('depth')
        if obj is None or type(obj) in (str,int,float,bool): return obj
        if type(obj) is Interrupt:
            if obj.response_schema is not None: raise ValueError('unsupported response schema')
            return {'__ed_type__':'interrupt','value':ProbeJSON.encode(obj.value,depth+1),'id':obj.id}
        if type(obj) is tuple:
            return {'__ed_type__':'tuple','items':[ProbeJSON.encode(v,depth+1) for v in obj]}
        if type(obj) is list:return [ProbeJSON.encode(v,depth+1) for v in obj]
        if type(obj) is dict:
            if '__ed_type__' in obj or not all(type(k) is str for k in obj):raise ValueError('reserved/non-string key')
            return {k:ProbeJSON.encode(v,depth+1) for k,v in obj.items()}
        raise TypeError('not allowlisted')
    @staticmethod
    def decode(obj,depth=0):
        if depth>32:raise ValueError('depth')
        if type(obj) is list:return [ProbeJSON.decode(v,depth+1) for v in obj]
        if type(obj) is dict:
            tag=obj.get('__ed_type__')
            if tag=='interrupt' and set(obj)=={'__ed_type__','value','id'}:
                if type(obj['id']) is not str:raise ValueError('invalid interrupt ID')
                return Interrupt(value=ProbeJSON.decode(obj['value'],depth+1),id=obj['id'])
            if tag=='tuple' and set(obj)=={'__ed_type__','items'} and type(obj['items']) is list:
                return tuple(ProbeJSON.decode(v,depth+1) for v in obj['items'])
            if tag is not None:raise ValueError('unknown codec tag')
            return {k:ProbeJSON.decode(v,depth+1) for k,v in obj.items()}
        return obj
    def dumps_typed(self,obj):
        payload=json.dumps(self.encode(obj),allow_nan=False).encode()
        if len(payload)>1024*1024: raise ValueError('oversize')
        return 'ed_json_v1',payload
    def loads_typed(self,data):
        kind,payload=data
        if kind!='ed_json_v1' or len(payload)>1024*1024: raise ValueError('unsafe codec')
        return self.decode(json.loads(payload,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite'))))

async def graph_probe(dsn):
    async with await psycopg.AsyncConnection.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        await conn.execute('CREATE SCHEMA lg_checkpoints')
        await conn.execute('CREATE TABLE public.probe_effect (id text PRIMARY KEY)')
        await conn.execute('SET search_path TO lg_checkpoints')
        saver=AsyncPostgresSaver(conn,serde=ProbeJSON())
        await saver.setup()
        class State(TypedDict):
            execution_id: str
            done: bool
        async def approve(state):
            decision=interrupt({'execution_id':state['execution_id']})
            if decision is not True: raise ValueError('rejected')
            await conn.execute('INSERT INTO public.probe_effect(id) VALUES(%s) ON CONFLICT DO NOTHING',(state['execution_id'],))
            return {'done':True}
        def build(s):
            b=StateGraph(State);b.add_node('approval',approve)
            b.add_edge(START,'approval');b.add_edge('approval',END)
            return b.compile(checkpointer=s)
        thread=str(uuid.uuid4());execution=str(uuid.uuid4())
        config={'configurable':{'thread_id':thread}}
        result=await build(saver).ainvoke({'execution_id':execution,'done':False},config,durability='sync')
        assert result.get('__interrupt__')
        assert await saver.aget_tuple(config) is not None
    # New database connection, saver, compiled graph: no retained in-memory graph state.
    async with await psycopg.AsyncConnection.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        await conn.execute('SET search_path TO lg_checkpoints')
        saver=AsyncPostgresSaver(conn,serde=ProbeJSON())
        result=await build(saver).ainvoke(Command(resume=True),config,durability='sync')
        assert result['done'] is True
        await build(saver).ainvoke(None,config,durability='sync')
        cur=await conn.execute('SELECT count(*) AS n FROM public.probe_effect')
        assert (await cur.fetchone())['n']==1
        await saver.adelete_thread(thread)
        assert await saver.aget_tuple(config) is None
        cur=await conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='lg_checkpoints' ORDER BY tablename")
        vendor_tables=[r['tablename'] for r in await cur.fetchall()]
    try: ProbeJSON().dumps_typed(object())
    except TypeError: pass
    else: raise AssertionError('arbitrary object accepted')
    checks.extend(['AsyncPostgresSaver.setup in dedicated schema','sync interrupt persisted across new saver/connection/graph','same-thread resume completed synthetic unique DB effect','completed graph replay did not duplicate effect','delete_thread removed readable checkpoint','allowlisted JSON envelope codec rejected arbitrary object'])
    return vendor_tables

try:
    password=uuid.uuid4().hex
    _,pgport=start(PG,5432,['--tmpfs','/var/lib/postgresql','-e','POSTGRES_PASSWORD='+password])
    dsn=f'postgresql://postgres:{password}@127.0.0.1:{pgport}/postgres'
    for _ in range(80):
        try:
            with psycopg.connect(dsn,connect_timeout=1) as c:
                version=c.execute('SELECT version()').fetchone()[0]
            break
        except psycopg.OperationalError:time.sleep(.25)
    else: raise RuntimeError('PostgreSQL readiness timeout')
    vendor_tables=asyncio.run(graph_probe(dsn))
    key=uuid.uuid4().hex;readonly=uuid.uuid4().hex
    _,qdport=start(QD,6333,['--tmpfs','/qdrant/storage','-e','QDRANT__SERVICE__API_KEY='+key,'-e','QDRANT__SERVICE__READ_ONLY_API_KEY='+readonly])
    base=f'http://127.0.0.1:{qdport}'
    def request(method,path,body=None,credential=key):
        req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json','api-key':credential},method=method)
        with urllib.request.urlopen(req,timeout=5) as response:return json.load(response)
    for _ in range(80):
        try:request('GET','/collections');break
        except (OSError,urllib.error.URLError):time.sleep(.25)
    else: raise RuntimeError('Qdrant readiness timeout')
    cfg=yaml.safe_load((ROOT/'contracts/config/qdrant.yaml').read_text());collection='contract_probe'
    request('PUT','/collections/'+collection,cfg['create_collection'])
    for index in cfg['payload_indexes']:request('PUT',f'/collections/{collection}/index?wait=true',index)
    # Synthetic orthogonal vectors, not model-generated embeddings.
    workspace,foreign,kb,rev,staged=[str(uuid.uuid4()) for _ in range(5)]
    points=[]
    for tenant,r in [(workspace,rev),(foreign,rev),(workspace,staged)]:
        points.append({'id':str(uuid.uuid4()),'vector':{'dense':[1.0]+[0.0]*383,'bm25':{'indices':[7,11],'values':[1.0,0.5]}},'payload':{'workspace_id':tenant,'kb_id':kb,'document_id':str(uuid.uuid4()),'revision_id':r,'profile_id':'bge_small_en_v15','text_sha256':'a'*64,'manifest_sha256':'b'*64}})
    request('PUT',f'/collections/{collection}/points?wait=true&ordering=strong',{'points':points})
    filt={'must':[{'key':'workspace_id','match':{'value':workspace}},{'key':'kb_id','match':{'any':[kb]}},{'key':'revision_id','match':{'any':[rev]}},{'key':'profile_id','match':{'value':'bge_small_en_v15'}}]}
    client=QdrantClient(url=base,api_key=readonly,check_compatibility=True)
    f=models.Filter.model_validate(filt)
    for using,q in [('dense',[1.0]+[0.0]*383),('bm25',models.SparseVector(indices=[7],values=[1.0]))]:
        result=client.query_points(collection,query=q,using=using,query_filter=f,limit=40,with_vectors=False,search_params=models.SearchParams(exact=True))
        assert [p.id for p in result.points]==[points[0]['id']],using
    try:request('POST',f'/collections/{collection}/points/query',{'query':[1.0]+[0.0]*383,'using':'dense','filter':{'must':[{'key':'not_indexed','match':{'value':'x'}}]},'limit':1})
    except urllib.error.HTTPError as e:assert e.code==400
    else:raise AssertionError('strict mode allowed unindexed filter')
    try:request('PUT',f'/collections/{collection}/points?wait=true',{'points':points},readonly)
    except urllib.error.HTTPError as e:assert e.code in (401,403)
    else:raise AssertionError('read-only key allowed write')
    request('PUT',f'/collections/{collection}/points?wait=true',{'points':points})
    assert request('POST',f'/collections/{collection}/points/count',{'exact':True})['result']['count']==3
    request('POST',f'/collections/{collection}/points/delete?wait=true',{'filter':filt})
    assert request('POST',f'/collections/{collection}/points/count',{'filter':filt,'exact':True})['result']['count']==0
    client.close()
    checks.extend(['selected Qdrant collection and payload index config accepted','qdrant-client dense and sparse queries exclude foreign/staging points with explicit filters','strict mode rejects unindexed filter','read-only API key rejects writes','identical upsert retains three unique points','filtered delete followed by exact count is zero'])
    report={'status':'pass','scope':'Synthetic vendor contract probes only; no application auth, full job/recovery implementation, actual embeddings, model calls, load or paid services tested.','python':platform.python_version(),'runtime_target_note':'Host probe Python is not certification of the selected Python3.14 application stack.','versions':{p:importlib.metadata.version(p) for p in ['langgraph','langgraph-checkpoint-postgres','langgraph-checkpoint','psycopg','qdrant-client']},'images':{'postgres':PG,'qdrant':QD},'postgres_version':version,'vendor_tables':vendor_tables,'checks':checks,'artifact_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in ['contracts/config/qdrant.yaml','contracts/config/workflows.yaml','scripts/validate_runtime_contracts.py']},'network':'temporary loopback-only ports; random ephemeral credentials','cleanup':'containers removed in finally; tmpfs data discarded'}
    (ROOT/'docs/10-testing/runtime-contract-results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
finally:
    for name in containers:subprocess.run(['docker','rm','-f',name],capture_output=True)
