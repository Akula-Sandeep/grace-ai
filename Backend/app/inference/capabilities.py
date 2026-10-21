"""What each configured model can do, so callers never send a request a model cannot serve.

The values describe Groq's hosted models as documented by Groq. A model that is
not listed is assumed to support tool calls and JSON mode (Groq returns a 400
for an unsupported feature, which the chain treats as that model's failure and
moves on). Context sizes bound how much conversation is sent.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapabilities:
    context_tokens: int = 32_768
    tools: bool = True
    json_mode: bool = True
    reasoning_effort: bool = False


_KNOWN: dict[str, ModelCapabilities] = {
    "openai/gpt-oss-20b": ModelCapabilities(131_072, True, True, True),
    "openai/gpt-oss-120b": ModelCapabilities(131_072, True, True, True),
    "llama-3.3-70b-versatile": ModelCapabilities(131_072, True, True, False),
    "llama-3.1-8b-instant": ModelCapabilities(131_072, True, True, False),
}


def capabilities(model: str | None) -> ModelCapabilities:
    return _KNOWN.get((model or "").strip(), ModelCapabilities())


def supports_tools(model: str | None) -> bool:
    return capabilities(model).tools
