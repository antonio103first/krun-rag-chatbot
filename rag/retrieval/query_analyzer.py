"""Use Claude Haiku to extract structured filters from a Korean query.

Output JSON shape:
    {
        "rewritten_query": str,
        "companies":   list[str],
        "persons":     list[str],
        "doc_types":   list[str],
        "tags":        list[str],
        "date_range":  {"from": "YYYY-MM-DD" | null, "to": "YYYY-MM-DD" | null}
    }

The analyzer is best-effort: on any error we return a "pass-through" result so
retrieval still runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

from anthropic import Anthropic

from rag.config import get_settings


# Stable doc_type vocabulary used by metadata.py.
KNOWN_DOC_TYPES = {
    "company", "person", "meeting", "daily", "periodic",
    "project", "resource", "inbox", "dashboard", "other",
}


SYSTEM_PROMPT = """You analyze short Korean queries for a VC's internal Obsidian RAG system and extract structured filters.

Output ONLY a single JSON object on one line, no prose, no code fences. Keys (use null/empty list when not implied):
  - "rewritten_query": cleaned Korean query for embedding (remove date words, leave subject + intent)
  - "companies":  list of company names mentioned (Korean or English, no [[brackets]])
  - "persons":    list of person names mentioned
  - "doc_types":  subset of [company, person, meeting, daily, periodic, project, resource, inbox, dashboard]
  - "tags":       list of explicit #tags or hashtag-like keywords mentioned
  - "date_range": {"from": "YYYY-MM-DD"|null, "to": "YYYY-MM-DD"|null} resolved against `today`

Rules:
- "지난 분기" / "최근 3개월" / "작년" must be resolved into concrete YYYY-MM-DD bounds using `today`.
- If the query mentions "1차DD" or "2차DD" or "투심위" or "킥오프" or "주간회의", add "meeting" to doc_types.
- If the query is about a company without time qualifiers, add both "company" and "meeting" to doc_types.
- If the query mentions an LP / 투자자 / 운용사 / 사장 / 대표 / 부사장, add "person" and "meeting".
- Never invent names. If unsure, leave the list empty.

Examples (today=2026-04-29):
Q: 위밋모빌리티 1차DD 핵심 리스크
A: {"rewritten_query":"위밋모빌리티 1차DD 핵심 리스크","companies":["위밋모빌리티"],"persons":[],"doc_types":["meeting","company"],"tags":[],"date_range":{"from":null,"to":null}}

Q: 지난 분기 만난 LP 정리
A: {"rewritten_query":"LP 미팅 정리","companies":[],"persons":[],"doc_types":["meeting","person"],"tags":["LP"],"date_range":{"from":"2026-01-01","to":"2026-03-31"}}

Q: 강규식 상무와의 최근 미팅 액션아이템
A: {"rewritten_query":"강규식 상무 미팅 액션아이템","companies":[],"persons":["강규식 상무"],"doc_types":["meeting"],"tags":[],"date_range":{"from":null,"to":null}}
"""


@dataclass
class QueryAnalysis:
    raw: str
    rewritten_query: str
    companies: list[str] = field(default_factory=list)
    persons: list[str] = field(default_factory=list)
    doc_types: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
    used_fallback: bool = False
    latency_ms: int = 0

    def is_empty_filter(self) -> bool:
        return not (
            self.companies or self.persons or self.doc_types or self.tags
            or self.date_from or self.date_to
        )


def _strip_to_json(text: str) -> str:
    """Best-effort extraction of the first {...} block from model output."""
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    return m.group(0) if m else text


def _validate_doc_types(values: list[str]) -> list[str]:
    return [v for v in values if v in KNOWN_DOC_TYPES]


def _coerce_date_str(s: str | None) -> str | None:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def _fallback(query: str) -> QueryAnalysis:
    return QueryAnalysis(raw=query, rewritten_query=query, used_fallback=True)


def analyze_query(
    query: str,
    *,
    client: Anthropic | None = None,
    today: date | None = None,
) -> QueryAnalysis:
    """Call Haiku to analyze the query. Returns a fallback on any error."""
    import time

    s = get_settings()
    if not query.strip():
        return _fallback(query)

    today = today or date.today()
    client = client or Anthropic(api_key=s.anthropic_api_key)

    extra_headers: dict[str, str] = {}
    if s.generation.zdr_enabled:
        extra_headers["anthropic-zero-retention-window"] = "0"

    started = time.perf_counter()
    try:
        msg = client.messages.create(
            model=s.generation.analyzer_model,
            max_tokens=400,
            temperature=0.0,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"today={today.isoformat()}\nQ: {query}\nA:",
                }
            ],
            extra_headers=extra_headers or None,
        )
    except Exception:
        return _fallback(query)

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    text = msg.content[0].text if msg.content else ""
    payload = _strip_to_json(text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        out = _fallback(query)
        out.latency_ms = elapsed_ms
        return out

    rewritten = (data.get("rewritten_query") or query).strip() or query
    date_range = data.get("date_range") or {}
    return QueryAnalysis(
        raw=query,
        rewritten_query=rewritten,
        companies=[c for c in (data.get("companies") or []) if isinstance(c, str)],
        persons=[p for p in (data.get("persons") or []) if isinstance(p, str)],
        doc_types=_validate_doc_types(data.get("doc_types") or []),
        tags=[t for t in (data.get("tags") or []) if isinstance(t, str)],
        date_from=_coerce_date_str(date_range.get("from")),
        date_to=_coerce_date_str(date_range.get("to")),
        used_fallback=False,
        latency_ms=elapsed_ms,
    )
