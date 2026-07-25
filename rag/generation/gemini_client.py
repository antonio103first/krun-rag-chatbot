"""Streaming Gemini client — drop-in replacement for `ClaudeClient`.

Exists because the Anthropic API is pay-as-you-go and this is a single-operator
tool: a company briefing costs ~245 KRW per run on Sonnet, and the credit
balance ran out mid-use. Gemini's free tier covers this workload at zero cost,
and the vault automation already authenticates against it, so the key is
already on the machine.

Mirrors `ClaudeClient.stream` / `.stream_iter` exactly — same arguments, same
`GenerationResult` — so `apps/fastapi_server.py` and `rag/query.py` don't care
which provider is behind them.

Two Anthropic-only concepts have no Gemini equivalent and are reported as zero
rather than silently faked:
  - prompt caching: `cache_creation_tokens` / `cache_read_tokens` stay 0.
  - ZDR: there is no per-request retention header. If `zdr_enabled` is set, the
    caller is told at construction time instead of being quietly ignored.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from rag.config import GenerationConfig, get_settings
from rag.generation.claude_client import GenerationResult


class GeminiUnavailable(RuntimeError):
    """Raised when the Gemini provider is selected but cannot be used.

    Separate from a generic error so the API layer can surface a fix-it message
    (which key to set, where) instead of a bare stack trace.
    """


def resolve_gemini_key(settings=None) -> str:
    """GEMINI_API_KEY from .env, the process env, or the vault automation's .env.

    The automation at `_automation/.env` has held this key since 2026-06 and is
    the copy the user actually maintains; reading it here avoids asking them to
    duplicate a secret into a second file and keep the two in sync.
    """
    s = settings or get_settings()
    key = getattr(s, "gemini_api_key", "") or os.environ.get("GEMINI_API_KEY", "")
    if key:
        return key

    fallback = getattr(s.generation, "gemini_env_fallback", "") or ""
    if fallback and os.path.isfile(fallback):
        try:
            with open(fallback, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEY"):
                        _, _, value = line.partition("=")
                        value = value.strip().strip("'\"")
                        if value:
                            return value
        except OSError:
            pass
    return ""


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


class GeminiClient:
    """Streaming wrapper around `google.genai` with the ClaudeClient interface."""

    def __init__(
        self,
        api_key: str | None = None,
        config: GenerationConfig | None = None,
    ) -> None:
        s = get_settings()
        self.config = config or s.generation

        try:
            from google import genai
        except ImportError as e:  # pragma: no cover - dependency is declared
            raise GeminiUnavailable(
                "google-genai 가 설치되지 않았습니다. `uv sync --extra phase1d --extra api` 를 실행하세요."
            ) from e

        key = api_key or resolve_gemini_key(s)
        if not key:
            raise GeminiUnavailable(
                "GEMINI_API_KEY 가 없습니다. 프로젝트 .env 에 추가하거나 "
                "`_automation/.env` 의 키를 사용하도록 config.yaml 의 "
                "`generation.gemini_env_fallback` 경로를 확인하세요."
            )

        self._genai = genai
        self.client = genai.Client(api_key=key)
        self.model = self.config.gemini_model

    # --- Helpers ------------------------------------------------------
    def _gen_config(self, max_tokens: int | None):
        """System prompt travels as `system_instruction`, not as a turn.

        thinking_budget=0 disables Gemini's thinking tokens. This workload is
        synthesis over supplied context, not reasoning from scratch, and the
        vault automation settled on the same value after finding thinking cost
        ~25% more tokens for no quality gain.
        """
        return self._genai.types.GenerateContentConfig(
            temperature=self.config.temperature,
            max_output_tokens=max_tokens or self.config.max_tokens,
            thinking_config=self._genai.types.ThinkingConfig(thinking_budget=0),
        )

    def _config_with_system(self, system_prompt: str, max_tokens: int | None):
        cfg = self._gen_config(max_tokens)
        cfg.system_instruction = system_prompt
        return cfg

    @staticmethod
    def _usage_of(response) -> _Usage:
        u = getattr(response, "usage_metadata", None)
        if u is None:
            return _Usage()
        return _Usage(
            input_tokens=getattr(u, "prompt_token_count", 0) or 0,
            output_tokens=getattr(u, "candidates_token_count", 0) or 0,
        )

    # --- Streaming generation ----------------------------------------
    def stream(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> GenerationResult:
        chunks: list[str] = []
        usage = _Usage()
        stop_reason = ""

        stream = self.client.models.generate_content_stream(
            model=self.model,
            contents=user_message,
            config=self._config_with_system(system_prompt, max_tokens),
        )
        for event in stream:
            text = getattr(event, "text", None)
            if text:
                chunks.append(text)
                if on_text:
                    on_text(text)
            # Usage and finish_reason only appear on the final chunk; keep the
            # most recent non-empty values rather than assuming a position.
            got = self._usage_of(event)
            if got.input_tokens or got.output_tokens:
                usage = got
            for cand in getattr(event, "candidates", None) or []:
                fr = getattr(cand, "finish_reason", None)
                if fr:
                    stop_reason = getattr(fr, "name", None) or str(fr)

        return GenerationResult(
            text="".join(chunks),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_tokens=0,  # no Gemini equivalent
            cache_read_tokens=0,
            stop_reason=stop_reason,
            model=self.model,
        )

    def stream_iter(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        stream = self.client.models.generate_content_stream(
            model=self.model,
            contents=user_message,
            config=self._config_with_system(system_prompt, max_tokens),
        )
        for event in stream:
            text = getattr(event, "text", None)
            if text:
                yield text
