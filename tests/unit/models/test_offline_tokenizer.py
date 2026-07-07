import hashlib
import socket
from pathlib import Path

import pytest
import tiktoken
from evidencedesk.models.tokenizer import generation_tokenizer


def test_offline_tokenizer_matches_frozen_reference_and_never_downloads(monkeypatch, tmp_path):
    legacy = tmp_path / "reference-cache"
    legacy.mkdir()
    url = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
    (legacy / hashlib.sha1(url.encode()).hexdigest()).write_bytes(
        Path(".cache/models/tiktoken/o200k_base.tiktoken").read_bytes()
    )
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(legacy))
    generation_tokenizer.cache_clear()

    def no_network(*args, **kwargs):
        raise AssertionError("Serving tokenization cannot download")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    reference = tiktoken.get_encoding("o200k_base")
    tokenizer = generation_tokenizer()
    for text in [
        "Hello world!",
        "नेपाल काठमाडौँ café 👩🏽‍🔬",
        "<|endoftext|> is user text.",
        "\n\tNumbers:123456789; citations[E1].",
    ]:
        assert tokenizer.encode(text, disallowed_special=()) == reference.encode(
            text, disallowed_special=()
        )
    with pytest.raises(FileNotFoundError):
        generation_tokenizer(tmp_path)
    target = tmp_path / "tiktoken/o200k_base.tiktoken"
    target.parent.mkdir()
    target.write_bytes(b"not valid ranks")
    with pytest.raises(ValueError, match="integrity"):
        generation_tokenizer(tmp_path)
