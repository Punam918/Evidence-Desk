"""Frozen MiniLM scoring implementation. Imported only inside the inference process."""

import hashlib
from importlib.metadata import version
from pathlib import Path

import torch
from evidencedesk.embeddings.artifacts import verified_snapshot
from evidencedesk.identity.crypto import canonical_json
from evidencedesk.retrieval.rerank_protocol import Score, ScoreRequest, ScoreResponse
from tokenizers import Tokenizer
from transformers import AutoModelForSequenceClassification


class MiniLM:
    def __init__(self, cache: Path, lock: Path):
        snapshot, record = verified_snapshot(cache, lock, "reranker")
        if record["model_id"] != "cross-encoder/ms-marco-MiniLM-L6-v2":
            raise ValueError("Unsupported reranker")
        torch.set_num_threads(4)
        torch.set_num_interop_threads(1)
        self.tokenizer = Tokenizer.from_file(str(snapshot / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.model = (
            AutoModelForSequenceClassification.from_pretrained(
                str(snapshot),
                local_files_only=True,
                trust_remote_code=False,
                use_safetensors=True,
                dtype=torch.float32,
            )
            .to("cpu")
            .eval()
        )
        if self.model.config.num_labels != 1 or self.model.config.max_position_embeddings != 512:
            raise ValueError("Unexpected reranker configuration")
        self.cls = self.tokenizer.token_to_id("[CLS]")
        self.sep = self.tokenizer.token_to_id("[SEP]")
        self.pad = self.tokenizer.token_to_id("[PAD]")
        if None in (self.cls, self.sep, self.pad):
            raise ValueError("Missing BERT special tokens")
        self.fingerprint = hashlib.sha256(
            canonical_json(
                {
                    "model_id": record["model_id"],
                    "revision": record["revision"],
                    "files": record["files_sha256"],
                    "query_cap": 96,
                    "pair_cap": 512,
                    "batch_size": 8,
                    "dtype": "float32",
                    "threads": 4,
                    "libraries": {
                        name: version(name) for name in ("torch", "transformers", "tokenizers")
                    },
                }
            ).encode()
        ).hexdigest()

    def score(self, request: ScoreRequest) -> ScoreResponse:
        query = self.tokenizer.encode(request.query, add_special_tokens=False).ids
        original_count = len(query)
        if original_count > 96:
            query = query[:48] + query[-48:]
        output: list[Score] = []
        for start in range(0, len(request.passages), 8):
            batch = request.passages[start : start + 8]
            pairs, types, counts = [], [], []
            for passage in batch:
                document = self.tokenizer.encode(passage.text, add_special_tokens=False).ids
                counts.append(len(document))
                document = document[: 512 - len(query) - 3]
                pairs.append([self.cls, *query, self.sep, *document, self.sep])
                types.append([0] * (len(query) + 2) + [1] * (len(document) + 1))
            maximum = max(map(len, pairs))
            inputs = {
                "input_ids": torch.tensor([p + [self.pad] * (maximum - len(p)) for p in pairs]),
                "attention_mask": torch.tensor(
                    [[1] * len(p) + [0] * (maximum - len(p)) for p in pairs]
                ),
                "token_type_ids": torch.tensor([t + [0] * (maximum - len(t)) for t in types]),
            }
            with torch.inference_mode():
                logits = self.model(**inputs).logits
            if logits.shape != (len(batch), 1) or not torch.isfinite(logits).all():
                raise ValueError("Invalid reranker logits")
            for i, passage in enumerate(batch):
                output.append(
                    Score(
                        id=passage.id,
                        logit=float(logits[i, 0]),
                        query_tokens=original_count,
                        document_tokens=counts[i],
                        pair_tokens=len(pairs[i]),
                        query_clipped=original_count > 96,
                        document_clipped=counts[i] > 512 - len(query) - 3,
                    )
                )
        return ScoreResponse(fingerprint=self.fingerprint, scores=output)
