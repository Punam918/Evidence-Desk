#!/usr/bin/env python3
"""Resolve public model revisions and verify real CPU inference without remote code."""
from pathlib import Path
import hashlib
import json
import time
import numpy as np
from huggingface_hub import HfApi, snapshot_download
from sentence_transformers import SentenceTransformer, CrossEncoder
from fastembed import SparseTextEmbedding

ROOT=Path(__file__).resolve().parents[1]
cache=ROOT/'.cache/models';cache.mkdir(parents=True,exist_ok=True)
records=[]
for model_id,kind in [('BAAI/bge-small-en-v1.5','embedding'),('cross-encoder/ms-marco-MiniLM-L6-v2','reranker')]:
    info=HfApi().model_info(model_id)
    revision=info.sha
    assert revision and len(revision)==40
    local=snapshot_download(model_id,revision=revision,cache_dir=cache,allow_patterns=['*.json','*.safetensors','vocab.txt','*.model','1_Pooling/*'])
    files={str(p.relative_to(local)):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(local).rglob('*') if p.is_file()}
    started=time.monotonic()
    if kind=='embedding':
        model=SentenceTransformer(local,device='cpu',trust_remote_code=False)
        values=model.encode(['EvidenceDesk publishes complete revisions.','Search requires authorized sources.'],normalize_embeddings=True)
        assert values.shape==(2,384) and np.isfinite(values).all()
        assert np.allclose(np.linalg.norm(values,axis=1),1,atol=1e-3)
        proof={'dimensions':384,'rows':2,'finite':True,'unit_norm':True}
    else:
        model=CrossEncoder(local,device='cpu',trust_remote_code=False)
        values=model.predict([('How are revisions published?','Complete revisions are atomically published.'),('How are revisions published?','Bananas are yellow.')])
        assert np.isfinite(values).all() and values[0]>values[1]
        proof={'rows':2,'relevant_pair_ranked_first':True}
    records.append({'kind':kind,'model_id':model_id,'revision':revision,'files_sha256':files,'smoke':proof,'elapsed_seconds':round(time.monotonic()-started,3),'license':info.card_data.get('license') if info.card_data else None})
    print(kind,revision,proof,flush=True)
sparse_revision=HfApi().model_info('Qdrant/bm25').sha
assert sparse_revision and len(sparse_revision)==40
sparse_path=Path(snapshot_download('Qdrant/bm25',revision=sparse_revision,cache_dir=cache/'fastembed',allow_patterns=['*.txt','config.json']))
sparse_settings={'language':'english','k':1.2,'b':0.75,'avg_len':256,'token_max_length':40,'disable_stemmer':False}
sparse=SparseTextEmbedding(model_name='Qdrant/bm25',specific_model_path=str(sparse_path),local_files_only=True,**sparse_settings)
docs=list(sparse.embed(['Published immutable revisions provide evidence.']))
query=list(sparse.query_embed('published revisions'))
assert docs[0].indices.size>0 and query[0].indices.size>0
assert np.isfinite(docs[0].values).all()
records.append({'kind':'sparse','model_id':'Qdrant/bm25','revision':sparse_revision,'library':'fastembed','settings':sparse_settings,'smoke':{'documents':1,'queries':1,'finite':True},'files_sha256':{str(p.relative_to(sparse_path)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sparse_path.rglob('*') if p.is_file()}})
out=ROOT/'contracts/config/model-artifacts.lock.json';out.write_text(json.dumps({'resolved_on':'2026-10-11','models':records},indent=2)+'\n')
print('Wrote',out)
