"""Select the same frozen tokenizer family as the chosen adapter, before serving."""

from evidencedesk.ai.context import ContextPacker
from evidencedesk.models.registry import ModelConfig
from evidencedesk.models.tokenizer import generation_tokenizer
from evidencedesk.providers.base import ModelAdapter
from evidencedesk.providers.local_tokens import LocalTokens
from evidencedesk.providers.ollama import OllamaAdapter
from evidencedesk.providers.openai import OpenAIAdapter


def context_packer(model: ModelConfig, adapter: ModelAdapter) -> ContextPacker:
    # Lifecycle/CPU thread: tokenizer construction and artifact hashing must not block an event loop.
    if model.adapter == "openai" and isinstance(adapter, OpenAIAdapter):
        tokenizer = generation_tokenizer(adapter.tokenizer_cache)
        return ContextPacker(
            model, adapter, lambda text: len(tokenizer.encode(text, disallowed_special=()))
        )
    if (
        model.adapter == "ollama"
        and isinstance(adapter, OllamaAdapter)
        and isinstance(adapter.tokenizer, LocalTokens)
    ):
        if adapter.model_id != model.model_id or adapter.digest != model.resolved_digest:
            raise ValueError("Local tokenizer/model binding does not match the registry")
        local = adapter.tokenizer.tokenizer
        return ContextPacker(
            model, adapter, lambda text: len(local.encode(text, add_special_tokens=False).ids)
        )
    if model.adapter in ("anthropic", "google"):
        from evidencedesk.providers.anthropic import AnthropicAdapter
        from evidencedesk.providers.google import GoogleAdapter

        if (model.adapter == "anthropic" and isinstance(adapter, AnthropicAdapter)) or (
            model.adapter == "google" and isinstance(adapter, GoogleAdapter)
        ):
            # Secondary native tokenizers are unavailable locally. Use a conservative byte bound;
            # the full request estimate is explicitly marked estimated by each adapter.
            return ContextPacker(model, adapter, lambda text: len(text.encode("utf-8")))
    raise ValueError("No verified tokenizer binding for the selected adapter")
