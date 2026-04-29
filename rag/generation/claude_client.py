"""Streaming Claude client with prompt caching and optional ZDR.

Uses `messages.stream()` (the SDK helper) so we can iterate text deltas live
and still get a `final_message` with `usage` (input / output / cache tokens)
when the stream finishes.

Caching strategy
----------------
The system prompt is stable across requests (frozen text in `prompts.py`), so
we put a single `cache_control: ephemeral` breakpoint on it. With render order
`tools → system → messages` and no tools attached here, the breakpoint covers
the entire stable prefix; the volatile per-request content (citations + user
question) lands after the cached prefix in the user message.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from anthropic import Anthropic

from rag.config import GenerationConfig, get_settings


@dataclass
class GenerationResult:
    text: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    stop_reason: str
    model: str


class ClaudeClient:
    """Streaming wrapper around `anthropic.Anthropic.messages.stream`."""

    def __init__(
        self,
        api_key: str | None = None,
        config: GenerationConfig | None = None,
    ) -> None:
        s = get_settings()
        self.config = config or s.generation
        if not api_key and not s.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY missing. Add it to .env before calling ClaudeClient."
            )
        self.client = Anthropic(api_key=api_key or s.anthropic_api_key)

    # --- Helpers ------------------------------------------------------
    def _extra_headers(self) -> dict[str, str] | None:
        if self.config.zdr_enabled:
            return {"anthropic-zero-retention-window": "0"}
        return None

    def _system_blocks(self, system_prompt: str) -> list[dict]:
        block: dict = {"type": "text", "text": system_prompt}
        if self.config.prompt_caching:
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    # --- Streaming generation ----------------------------------------
    def stream(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> GenerationResult:
        """Stream a response. Calls `on_text(chunk)` per delta when provided.

        Returns a `GenerationResult` once the stream is fully drained.
        """
        max_tok = max_tokens or self.config.max_tokens

        chunks: list[str] = []
        with self.client.messages.stream(
            model=self.config.gen_model,
            max_tokens=max_tok,
            temperature=self.config.temperature,
            system=self._system_blocks(system_prompt),
            messages=[{"role": "user", "content": user_message}],
            extra_headers=self._extra_headers(),
        ) as stream:
            for text in stream.text_stream:
                chunks.append(text)
                if on_text:
                    on_text(text)
            final = stream.get_final_message()

        usage = final.usage
        return GenerationResult(
            text="".join(chunks),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            stop_reason=final.stop_reason or "",
            model=final.model,
        )

    def stream_iter(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        """Yield text chunks one by one. Stops when the stream completes."""
        max_tok = max_tokens or self.config.max_tokens
        with self.client.messages.stream(
            model=self.config.gen_model,
            max_tokens=max_tok,
            temperature=self.config.temperature,
            system=self._system_blocks(system_prompt),
            messages=[{"role": "user", "content": user_message}],
            extra_headers=self._extra_headers(),
        ) as stream:
            yield from stream.text_stream
