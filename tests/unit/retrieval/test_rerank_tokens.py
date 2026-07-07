from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import torch
from evidencedesk.embeddings.artifacts import verified_snapshot
from evidencedesk.retrieval.rerank_model import MiniLM
from evidencedesk.retrieval.rerank_protocol import Pair, ScoreRequest
from tokenizers import Tokenizer


def test_scoring_uses_exact_first_last_query_ids_and_preserves_full_source():
    snapshot, _ = verified_snapshot(
        Path(".cache/models"), Path("contracts/config/model-artifacts.lock.json"), "reranker"
    )
    tokenizer = Tokenizer.from_file(str(snapshot / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    inputs = []

    class Spy:
        def __call__(self, **features):
            inputs.append(features)
            return SimpleNamespace(logits=torch.zeros((features["input_ids"].shape[0], 1)))

    model = object.__new__(MiniLM)
    model.model, model.tokenizer = Spy(), tokenizer
    model.cls, model.sep, model.pad = [
        tokenizer.token_to_id(t) for t in ("[CLS]", "[SEP]", "[PAD]")
    ]
    model.fingerprint = "a" * 64
    query = "head " * 48 + "omitted " * 10 + "tail " * 48
    text = "source " * 1000
    request = ScoreRequest(query=query, passages=[Pair(id=uuid4(), text=text) for _ in range(20)])
    result = model.score(request)
    assert [v["input_ids"].shape[0] for v in inputs] == [8, 8, 4]
    query_ids = tokenizer.encode(query, add_special_tokens=False).ids
    first = inputs[0]["input_ids"][0].tolist()
    assert first[1:97] == query_ids[:48] + query_ids[-48:]
    assert first[97] == model.sep and first[-1] == model.sep and len(first) == 512
    assert inputs[0]["token_type_ids"][0][:98].sum() == 0
    assert inputs[0]["token_type_ids"][0][98:].sum() == 414
    assert all(s.query_tokens == 106 and s.document_tokens == 1000 for s in result.scores)
    assert request.query == query and all(p.text == text for p in request.passages)
    # English-only quality scope is not an encoding restriction or an implicit translation step.
    multilingual = ScoreRequest(
        query="東京 backup नीति", passages=[Pair(id=uuid4(), text="保存 policy café")]
    )
    model.score(multilingual)
    assert inputs[-1]["input_ids"].shape[1] <= 512
    assert multilingual.query == "東京 backup नीति"


@pytest.mark.parametrize("query,passages", [("", [Pair(id=uuid4(), text="source")]), ("q", [])])
def test_empty_rerank_inputs_are_rejected(query, passages):
    with pytest.raises(ValueError):
        ScoreRequest(query=query, passages=passages)
