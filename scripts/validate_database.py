#!/usr/bin/env python3
"""Apply reference DDL and negative invariants in disposable network-isolated Postgres."""
from pathlib import Path
import hashlib
import json
import subprocess
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
IMAGE='postgres@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336'
name='evidencedesk-spec-'+uuid.uuid4().hex[:12]

def run(args,stdin=None,check=True):
    return subprocess.run(args,input=stdin,text=True,capture_output=True,check=check)

started=False
try:
    result=run(['docker','run','--rm','-d','--name',name,'--network','none','--tmpfs','/var/lib/postgresql','-e','POSTGRES_HOST_AUTH_METHOD=trust',IMAGE])
    started=True
    for _ in range(60):
        ready=run(['docker','exec',name,'pg_isready','-h','127.0.0.1','-U','postgres'],check=False)
        if ready.returncode==0: break
        time.sleep(.5)
    else: raise RuntimeError('Isolated PostgreSQL did not become ready')
    outputs=[]
    hashes={}
    for relative in ['docs/05-database-and-storage/schema.sql','scripts/schema-invariants.sql']:
        content=(ROOT/relative).read_text()
        outputs.append(run(['docker','exec','-i',name,'psql','-h','127.0.0.1','-U','postgres','-v','ON_ERROR_STOP=1'],stdin=content).stdout)
        hashes[relative]=hashlib.sha256(content.encode()).hexdigest()
    version=run(['docker','exec',name,'psql','-h','127.0.0.1','-U','postgres','-Atc',"SELECT version();"]).stdout.strip()
    report={'status':'pass','scope':'Reference DDL and database constraints only; application authorization and concurrency are covered by separate integration suites.','image':IMAGE,'database_version':version,'artifact_sha256':hashes,'checks':['DDL applied in empty PostgreSQL','RLS without scope returns zero tenant rows','Tenant scope sees only own row','Cross-tenant insert rejected','Cross-tenant parent FK rejected','Second active run rejected','Second terminal event rejected','Incomplete verified index manifest rejected','Wrong graph thread creator rejected','Duplicate answer thread rejected'],'fixtures_rolled_back':True,'network':'none','host_ports':[]}
    (ROOT/'docs/10-testing/database-validation-results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
except subprocess.CalledProcessError as exc:
    raise SystemExit(exc.stderr or exc.stdout or str(exc))
finally:
    if started: run(['docker','rm','-f',name],check=False)
