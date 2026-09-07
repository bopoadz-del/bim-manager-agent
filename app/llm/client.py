"""LLM client, behind a narrow interface.

The interface is deliberately one method. A rich client invites use, and the
whole design of this package is that a language model is used twice, in two
well-understood places, and nowhere else.

When no key is configured the client raises. It does not degrade to a canned
answer: an unconfigured rule extractor that returned plausible clearances would
be the single most dangerous failure mode this service has, because the output
looks exactly like the real thing.
"""
from __future__ import annotations

import os
from typing import Protocol

DEFAULT_MODEL = "claude-sonnet-5"


class LLMUnavailable(RuntimeError):
    """No model is configured or reachable."""


class LLMClient(Protocol):
    model: str

    def complete(self, system: str, prompt: str, max_tokens: int = 2000) -> str:
        """Return the model's text response."""


class AnthropicClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        try:
            import anthropic
        except ImportError as exc:
            raise LLMUnavailable(f"anthropic SDK not installed: {exc}") from exc
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def complete(self, system: str, prompt: str, max_tokens: int = 2000) -> str:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text")


def get_client(model: str = DEFAULT_MODEL) -> LLMClient:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMUnavailable(
            "ANTHROPIC_API_KEY is not set. Rule extraction needs a model; "
            "clash judgement never does."
        )
    return AnthropicClient(key, model=model)
