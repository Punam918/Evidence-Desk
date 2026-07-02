#!/usr/bin/env python3
"""Validate specification artifacts only; no application or paid API calls."""
from pathlib import Path
import hashlib
import os
import json
import re
import sys
import warnings

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from openapi_spec_validator import validate as validate_openapi
from pglast import parse_sql

warnings.filterwarnings('ignore', category=DeprecationWarning)
ROOT = Path(__file__).resolve().parents[1]
errors = []
checks = []

def check(name, fn):
    try:
        detail = fn()
        checks.append({'name': name, 'status': 'pass', 'detail': detail})
    except Exception as exc:
        errors.append({'name': name, 'status': 'fail', 'detail': str(exc)})

def repository_files():
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in {'node_modules','dist','coverage','artifacts'}]
        for name in files:
            yield Path(directory)/name

def parsed_files():
    counts = {'json': 0, 'yaml': 0, 'jsonl': 0}
    for path in repository_files():
        if not path.is_file() or any(x.startswith('.') for x in path.relative_to(ROOT).parts):
            continue
        if path.suffix == '.json':
            json.loads(path.read_text()); counts['json'] += 1
        elif path.suffix in ('.yaml', '.yml'):
            yaml.safe_load(path.read_text()); counts['yaml'] += 1
        elif path.suffix == '.jsonl':
            for line in path.read_text().splitlines():
                if line.strip(): json.loads(line)
            counts['jsonl'] += 1
    return counts

spec = yaml.safe_load((ROOT / 'contracts/openapi.yaml').read_text())

def openapi():
    validate_openapi(spec)
    ids = [o['operationId'] for p in spec['paths'].values() for o in p.values()]
    assert len(ids) == len(set(ids)), 'Duplicate operation ID'
    count = 0
    registry = Registry().with_resource('urn:evidencedesk:openapi', Resource.from_contents(spec, default_specification=DRAFT202012))
    for path, methods in spec['paths'].items():
        for method, operation in methods.items():
            assert 'x-authorization' in operation and 'x-rate-limit' in operation
            for body in [operation.get('requestBody', {}), *operation['responses'].values()]:
                for media in body.get('content', {}).values():
                    if 'example' in media:
                        example_schema=json.loads(json.dumps(media['schema']).replace('#/components/', 'urn:evidencedesk:openapi#/components/'))
                        Draft202012Validator(example_schema, registry=registry,
                                             format_checker=FormatChecker()).validate(media['example'])
                        count += 1
    return {'paths': len(spec['paths']), 'operations': len(ids), 'schemas': len(spec['components']['schemas']), 'examples_validated': count}

def schemas():
    paths = list(ROOT.glob('contracts/**/*.schema.json'))
    for path in paths:
        Draft202012Validator.check_schema(json.loads(path.read_text()))
    answer_path = ROOT/'contracts/schemas/answer.schema.json'
    answer_schema = json.loads(answer_path.read_text())
    Draft202012Validator(answer_schema).validate(json.loads((ROOT/'contracts/examples/answer.json').read_text()))
    # Negative contract checks guard the intended schema boundary, not implementation.
    invalid = {'status':'answered','sentences':[],'limitations':[]}
    assert not Draft202012Validator(answer_schema).is_valid(invalid), 'Empty answered payload accepted'
    invalid = {'status':'answered','sentences':[{'text':'claim','citation_ids':[]}],'limitations':[]}
    assert not Draft202012Validator(answer_schema).is_valid(invalid), 'Uncited sentence accepted'
    event_path = ROOT/'contracts/events/run-event.schema.json'
    event_schema = json.loads(event_path.read_text())
    resources = []
    for path in paths:
        document=json.loads(path.read_text())
        resource=Resource.from_contents(document)
        resources.append((path.as_uri(),resource))
        if '$id' in document: resources.append((document['$id'],resource))
    registry=Registry().with_resources(resources)
    event_schema['$id']=event_path.as_uri()
    validator = Draft202012Validator(event_schema, registry=registry, format_checker=FormatChecker())
    rows = [json.loads(x) for x in (ROOT/'contracts/examples/run-events.jsonl').read_text().splitlines()]
    for event in rows: validator.validate(event)
    assert [e['seq'] for e in rows] == list(range(1,len(rows)+1))
    return {'schemas':len(paths),'event_examples':len(rows),'negative_answer_checks':2}

def links():
    missing = []
    count = 0
    for path in (p for p in repository_files() if p.suffix == '.md'):
        # File targets only; external URLs and heading anchors have a separate review boundary.
        for match in re.finditer(r'\[[^\]]+\]\(([^)]+)\)', path.read_text()):
            dest = match.group(1).strip().strip('<>')
            if '://' in dest or dest.startswith(('#','mailto:')): continue
            dest = dest.split('#')[0]
            if not dest: continue
            target = (path.parent/dest).resolve()
            count += 1
            if not target.exists(): missing.append(f'{path.relative_to(ROOT)} -> {dest}')
    assert not missing, '\n'.join(missing)
    return {'local_file_links':count}

def tasks():
    tasks = json.loads((ROOT/'tasks/backlog.json').read_text())
    required = {'id','objective','priority','stage','dependencies','files','requirements','io','edges','security','tests','acceptance','definition_of_done','deliverable'}
    seen = set()
    for task in tasks:
        assert required <= task.keys(), task['id']
        assert task['id'] not in seen
        assert set(task['dependencies']) <= seen, f'Unresolved/non-topological dependencies: {task["id"]}'
        assert all(task[k] for k in required-{'dependencies'}), task['id']
        seen.add(task['id'])
    req = json.loads((ROOT/'docs/01-product/requirements.json').read_text())
    matrix = (ROOT/'tasks/acceptance-matrix.md').read_text()
    catalog = (ROOT/'docs/10-testing/test-catalog.md').read_text()
    for feature in req:
        assert feature['task'] in seen
        assert feature['id'] in matrix and feature['test'] in catalog
    for n in range(1,15): assert f'N{n:02d}' in matrix
    for task in tasks:
        for test in task['tests'].split(): assert test in catalog, test
    return {'tasks':len(tasks),'functional_requirements':len(req),'nonfunctional_requirements':14,'dependency_edges':sum(len(t['dependencies']) for t in tasks)}

def fixtures():
    manifest = json.loads((ROOT/'evals/datasets/manifest.json').read_text())
    for doc in manifest['documents']:
        path=ROOT/'evals/datasets'/doc['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==doc['sha256'], str(path)
    rows=[json.loads(x) for x in (ROOT/'evals/datasets/smoke.jsonl').read_text().splitlines()]
    assert len(rows)==manifest['case_count']
    assert len({x['case_id'] for x in rows})==len(rows)
    for case in rows:
        for name in case['source_files']: assert (ROOT/'evals/datasets/smoke-corpus'/name).exists()
        for span in case['gold_spans']:
            assert any(span in (ROOT/'evals/datasets/smoke-corpus'/name).read_text() for name in case['source_files']), case['case_id']
    return {'synthetic_cases':len(rows),'source_hashes':len(manifest['documents'])}

def config():
    schema=json.loads((ROOT/'contracts/config/environment.schema.json').read_text())
    values={line.split('=',1)[0]:line.split('=',1)[1] for line in (ROOT/'contracts/config/environment.example').read_text().splitlines() if line and not line.startswith('#')}
    Draft202012Validator(schema,format_checker=FormatChecker()).validate(values)
    models=yaml.safe_load((ROOT/'contracts/config/models.yaml').read_text())
    keys={m['key'] for m in models['models']}
    assert set(spec['components']['schemas']['RunInput']['properties']['model_key']['enum'])==keys|{'auto'}
    defaults=yaml.safe_load((ROOT/'contracts/config/defaults.yaml').read_text())
    assert defaults['limits']['upload_bytes']==20971520
    assert models['embedding']['dimensions']==384
    assert defaults['generation']['max_attempts']==3
    return {'environment_variables':len(values),'model_candidates':len(keys),'config_cross_checks':4}

def sources():
    rows=json.loads((ROOT/'docs/00-research/sources.json').read_text())
    assert len({x['id'] for x in rows})==len(rows)
    assert all(x['status']=='opened' and x['url'].startswith('https://') for x in rows)
    return {'recorded_primary_sources':len(rows),'note':'Registry checks; external sources were opened during research, not refetched by this script.'}

def sql():
    content=(ROOT/'docs/05-database-and-storage/schema.sql').read_text()
    ast=parse_sql(content)
    assert 'vector(384)' not in content and 'FORCE ROW LEVEL SECURITY' in content
    assert all('CREATE TABLE app.'+name in content for name in ['index_manifests','index_outbox','workflow_threads'])
    return {'parsed_statements':len(ast),'table_definitions':len(re.findall(r'^CREATE TABLE',content,re.M)),'note':'Runtime DDL evidence is a separate isolated PostgreSQL check.'}

def v2_contracts():
    q=yaml.safe_load((ROOT/'contracts/config/qdrant.yaml').read_text())
    w=yaml.safe_load((ROOT/'contracts/config/workflows.yaml').read_text())
    models=yaml.safe_load((ROOT/'contracts/config/models.yaml').read_text())
    assert q['create_collection']['vectors']['dense']['size']==models['embedding']['dimensions']
    assert q['create_collection']['sparse_vectors']['bm25']['modifier']=='idf'
    assert q['create_collection']['strict_mode_config']['unindexed_filtering_retrieve'] is False
    assert {i['field_name'] for i in q['payload_indexes']} >= {'workspace_id','kb_id','revision_id','profile_id'}
    assert w['durability']=='sync' and w['public_stream']=='app.run_events_only'
    for name,g in w['graphs'].items():
        nodes=set(g['nodes'])|{'START','END'}
        assert all(a in nodes and b in nodes for a,b in g['edges']), name
        assert set(g['interrupts']) <= nodes
        reached={'START'}
        for _ in nodes:
            reached |= {b for a,b in g['edges'] if a in reached}
        assert 'END' in reached
        assert set(g['nodes']) <= reached|{g['error_target']}
    state=json.loads((ROOT/'contracts/schemas/workflow-state.schema.json').read_text())
    point=json.loads((ROOT/'contracts/schemas/qdrant-payload.schema.json').read_text())
    assert state['additionalProperties'] is False and point['additionalProperties'] is False
    assert not {'text','auth_context','api_key','messages'} & state['properties'].keys()
    assert 'approved' in spec['components']['schemas']['ToolExecution']['properties']['status']['enum']
    releases=json.loads((ROOT/'docs/00-research/releases-2026-10-10.json').read_text())
    assert all(x.get('version') and x.get('published_at') and x['published_at'][:10]<='2026-10-10' for x in releases)
    return {'graph_definitions':len(w['graphs']),'observed_releases':len(releases),'qdrant_dimensions':384,'public_approval_state':'approved'}

def evidence_freshness():
    count=0
    for name in ['database-validation-results.json','runtime-contract-results.json']:
        report=json.loads((ROOT/'docs/10-testing'/name).read_text())
        assert report['status']=='pass', name
        for relative,expected in report['artifact_sha256'].items():
            assert hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()==expected, f'Stale validation: {relative}'
            count+=1
    return {'hashed_artifacts':count,'scope':'Recorded contract probes only; not full application certification.'}

for name,fn in [('evidence_freshness',evidence_freshness),('v2_contracts',v2_contracts),('file_syntax' ,parsed_files),('openapi_and_examples',openapi),('json_schemas_and_events',schemas),('local_links',links),('tasks_and_traceability',tasks),('smoke_fixture_integrity',fixtures),('configuration_consistency',config),('source_registry',sources),('sql_syntax',sql)]:
    check(name,fn)
report={'scope':'specification validation only','status':'fail' if errors else 'pass','checks':checks+errors}
out=ROOT/'docs/10-testing/spec-validation-results.json'
out.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
sys.exit(1 if errors else 0)
