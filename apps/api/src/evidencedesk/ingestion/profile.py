import hashlib
from dataclasses import asdict, dataclass

from evidencedesk.identity.crypto import canonical_json


@dataclass(frozen=True)
class IngestionProfile:
    parser_version: str = "parse_v1"
    chunk_profile_id: str = "structure_v1"
    embedding_profile_id: str = "bge_small_en_v15"
    dense_revision: str = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    sparse_revision: str = "22b8d2af71a76161e18dd432d2cee0eefa66e412"
    sparse_library: str = "fastembed==0.9.0"
    sparse_language: str = "english"
    sparse_k: float = 1.2
    sparse_b: float = 0.75
    sparse_avg_len: int = 256
    sparse_token_max_length: int = 40
    target_tokens: int = 320
    max_tokens: int = 384
    overlap_tokens: int = 48
    normalization: str = "nfc_v1"

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(asdict(self)).encode()).hexdigest()
