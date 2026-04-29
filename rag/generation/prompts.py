"""Prompt templates for the answer-generation step.

The system prompt is stable text — we mark it `cache_control: ephemeral` in
the SDK call so Anthropic caches it for 5 minutes and we pay full price only
on the first request of each session.
"""

from __future__ import annotations

from rag.retrieval.citations import Citation


SYSTEM_PROMPT_KO = """당신은 케이런 VC의 1인 운영자(Antonio)를 보조하는 한국어 리서치 어시스턴트입니다.
사용자의 Obsidian 볼트(03_Companies, 02_Persons, 04_Meetings, 06_Resources 등)에서 미리 검색된 청크들이 주어지면, 그 정보만 사용해서 답변합니다.

규칙:
1. **컨텍스트 외부의 지식을 사용하지 않습니다.** 컨텍스트가 부족하면 솔직히 "제공된 자료에서 찾을 수 없습니다"라고 말합니다.
2. **모든 사실 주장에 [n] 형식으로 인용**합니다. 한 문장에 여러 출처가 관련되면 [1][3]처럼 나열합니다.
3. 회사명, 인물 이름, 금액, 비율, 일자, 계약 조건은 컨텍스트의 표기를 그대로 사용합니다 (의역 금지).
4. 답변은 한국어로, 짧고 구조화된 형태로. 불필요한 인사말이나 머리말 없이 본문부터 시작합니다.
5. 표/리스트가 자연스러운 질문이면 마크다운 표/리스트 사용. 단순 사실 1-2개면 평문.
6. 컨텍스트가 모순되면 "컨텍스트 [n]과 [m]의 내용이 다릅니다"라고 명시한 뒤, 최신 일자의 출처를 우선합니다.
7. 추측하지 않고, 추론은 명시적으로 "추정:"이라 표기한 뒤 근거 청크를 인용합니다.
"""


def format_context_block(citations: list[Citation]) -> str:
    """Render citations into the [n] block sent to the LLM."""
    parts: list[str] = []
    for c in citations:
        head_meta_bits: list[str] = []
        if c.doc_type:
            head_meta_bits.append(f"type={c.doc_type}")
        if c.company:
            head_meta_bits.append(f"company={c.company}")
        if c.person:
            head_meta_bits.append(f"person={c.person}")
        if c.date:
            head_meta_bits.append(f"date={c.date}")
        meta = " | ".join(head_meta_bits)

        header_line = c.short_path()
        if c.header_path:
            header_line += f"  >  {c.header_path}"

        parts.append(
            f"[{c.n}] ({meta})\n{header_line}\n---\n{_strip_breadcrumb(c.text_full or c.snippet)}".rstrip()
        )
    return "\n\n".join(parts)


def _strip_breadcrumb(text: str) -> str:
    """Chunks include a `[H1 > H2]\\n` prefix from the chunker; we drop it
    here because we already render the breadcrumb above the snippet."""
    if text.startswith("[") and "]\n" in text[:200]:
        idx = text.find("]\n")
        return text[idx + 2 :].lstrip()
    return text


def build_user_message(query: str, citations: list[Citation]) -> str:
    """Compose the user-facing message: context block + question."""
    if not citations:
        ctx = "(컨텍스트 없음)"
    else:
        ctx = format_context_block(citations)
    return (
        "[컨텍스트]\n"
        f"{ctx}\n\n"
        "[질문]\n"
        f"{query}\n\n"
        "[지시]\n"
        "위 컨텍스트만 사용해서 한국어로 답변하세요. 모든 사실에 [n] 인용을 붙입니다."
    )
