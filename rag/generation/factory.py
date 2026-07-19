"""Pick the generation client for the configured provider.

Callers (`apps/fastapi_server.py`, `rag/query.py`) construct through here so a
provider switch is a config edit, not a code edit. Both clients expose the same
`stream` / `stream_iter` surface, so nothing downstream branches on provider.
"""

from __future__ import annotations

from typing import Protocol

from rag.config import get_settings
from rag.generation.claude_client import ClaudeClient, GenerationResult


class GenerationClient(Protocol):
    """The surface `fastapi_server` and `query` actually depend on."""

    def stream(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int | None = ...,
        on_text=...,
    ) -> GenerationResult: ...

    def stream_iter(
        self, system_prompt: str, user_message: str, max_tokens: int | None = ...
    ): ...


def build_client(provider: str | None = None) -> GenerationClient:
    """Construct the client for `provider` (default: settings).

    No silent cross-provider fallback: if Gemini is selected and its key is
    missing, the GeminiUnavailable error propagates with the fix. Quietly
    switching to Anthropic would start charging the user for a run they
    explicitly moved off the paid API.
    """
    s = get_settings()
    name = (provider or s.generation.provider).lower()
    if name == "gemini":
        from rag.generation.gemini_client import GeminiClient

        return GeminiClient()
    return ClaudeClient()


def active_model_name(provider: str | None = None) -> str:
    """Model id for the active provider — for /health and UI display."""
    s = get_settings()
    name = (provider or s.generation.provider).lower()
    return s.generation.gemini_model if name == "gemini" else s.generation.gen_model
