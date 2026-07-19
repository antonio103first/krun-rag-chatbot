"""FastAPI server wrapping the KRUN RAG core for the Obsidian plugin.

Endpoints
---------
GET  /health                    → quick liveness + index stats
POST /ask         (JSON body)   → streams SSE events: analysis, citations, delta, done, error
POST /ask/json    (JSON body)   → non-streaming JSON (analysis + citations + answer + usage)
POST /reindex     (JSON body)   → run incremental ingest in a background thread

Run:
    uv run uvicorn apps.fastapi_server:app --host 127.0.0.1 --port 8765 --reload

CORS is opened for `app://obsidian.md` and localhost so the Obsidian plugin
can hit this from its renderer process.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from rag.config import get_settings
from rag.generation.factory import GenerationClient, active_model_name, build_client
from rag.generation.prompts import SYSTEM_PROMPT_KO, build_user_message
from rag.retrieval.citations import Citation, build_citations
from rag.retrieval.hybrid_search import company_brief_search, hybrid_search
from rag.retrieval.query_analyzer import QueryAnalysis, analyze_query
from rag.store.bm25_index import open_bm25_index
from rag.store.lancedb_store import open_store

log = logging.getLogger("krun_rag.api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="KRUN RAG API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["app://obsidian.md", "capacitor://localhost", "http://localhost", "*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Singletons -------------------------------------------------------------
_lock = threading.Lock()
_state: dict[str, Any] = {"store": None, "bm25": None, "client": None, "reindex_running": False}


def _get_store_and_bm25():
    with _lock:
        if _state["store"] is None:
            _state["store"] = open_store()
        if _state["bm25"] is None:
            _state["bm25"] = open_bm25_index(store=_state["store"])
    return _state["store"], _state["bm25"]


def _get_client() -> GenerationClient:
    with _lock:
        if _state["client"] is None:
            _state["client"] = build_client()
    return _state["client"]


def _reset_index_singletons() -> None:
    """After a reindex we want fresh BM25 + LanceDB handles."""
    with _lock:
        _state["store"] = None
        _state["bm25"] = None


# --- Schemas ----------------------------------------------------------------
class AskRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int | None = None
    no_analyze: bool = False
    reranker: bool | None = None  # None = use config default
    max_chunks_per_file: int | None = None
    active_note: str | None = None  # vault-relative path, optional context hint
    # Explicit retrieval mode. None = let the analyzer decide (hybrid/enumerate).
    # "company_brief" bypasses the analyzer entirely and requires `company`.
    mode: str | None = Field(None, pattern="^(company_brief)$")
    company: str | None = None


class CitationOut(BaseModel):
    n: int
    title: str
    vault_relative: str
    header_path: str
    obsidian_uri: str
    doc_type: str
    category: str = ""
    company: str | None = None
    person: str | None = None
    date: str | None = None
    snippet: str = ""


class AskJsonResponse(BaseModel):
    query: str
    analysis: dict
    citations: list[CitationOut]
    answer: str
    usage: dict
    elapsed_seconds: float


class ReindexRequest(BaseModel):
    mode: str = Field("incremental", pattern="^(incremental|full|refresh-metadata)$")


# --- Helpers ----------------------------------------------------------------
def _citation_to_out(c: Citation) -> CitationOut:
    return CitationOut(
        n=c.n,
        title=c.title,
        vault_relative=c.vault_relative,
        header_path=c.header_path,
        obsidian_uri=c.obsidian_uri,
        doc_type=c.doc_type,
        category=getattr(c, "category", ""),
        company=c.company,
        person=c.person,
        date=c.date,
        snippet=c.snippet,
    )


def _build_retrieval_cfg(req: AskRequest):
    cfg = get_settings().retrieval
    update: dict[str, Any] = {}
    if req.top_k:
        update["final_top_k"] = req.top_k
    if req.reranker is not None:
        update["reranker_enabled"] = req.reranker
    if req.max_chunks_per_file is not None:
        update["max_chunks_per_file"] = req.max_chunks_per_file
    return cfg.model_copy(update=update) if update else cfg


def _expand_query_with_active_note(req: AskRequest) -> str:
    """If the plugin sent an `active_note` path, prepend a soft hint to the query.

    The analyzer/retrieval still does the real work; this just nudges
    BM25/vector toward the user's current focus.
    """
    if not req.active_note:
        return req.query
    stem = req.active_note.rsplit("/", 1)[-1].removesuffix(".md")
    return f"[현재 노트: {stem}] {req.query}"


def _empty_result_message(req: AskRequest) -> str:
    if req.mode == "company_brief":
        return (
            f"'{req.company}' 이름으로 `company` 메타데이터가 설정된 노트가 없습니다. "
            "회사명 철자를 확인하거나, 사명이 바뀐 경우 `config/aliases.yaml`에 "
            "별칭을 추가한 뒤 다시 시도하세요."
        )
    return "제공된 자료에서 관련 청크를 찾지 못했습니다. 질문을 다시 표현하거나 필터를 완화해보세요."


def _analyze_and_retrieve(req: AskRequest, store, bm25):
    """Resolve (query, analysis, retrieval result) for both /ask and /ask/json.

    company_brief skips the analyzer: the company is already known, and the
    analyzer's bare-mention rule deliberately returns `companies=[]` for a plain
    company name, which would produce no WHERE at all.
    """
    query = _expand_query_with_active_note(req)

    # company_brief deliberately ignores top_k / max_chunks_per_file: the mode
    # exists to return the complete history, not a ranked slice.
    if req.mode == "company_brief":
        company = (req.company or "").strip()
        if not company:
            raise HTTPException(400, "mode=company_brief requires a non-empty `company`")
        analysis = QueryAnalysis(
            raw=query,
            rewritten_query=query,
            companies=[company],
            used_fallback=True,
        )
        return query, analysis, company_brief_search(company=company, store=store)

    if req.no_analyze:
        analysis = QueryAnalysis(raw=query, rewritten_query=query, used_fallback=True)
    else:
        analysis = analyze_query(query)
    result = hybrid_search(
        query=query,
        analysis=analysis,
        store=store,
        bm25=bm25,
        settings=_build_retrieval_cfg(req),
    )
    return query, analysis, result


# A full briefing is a timeline + synthesis + open-items list over every note
# filed under the company, so it routinely outruns the 2048-token default —
# 레디로버스트머신 (5 notes) was truncated mid-item at the ceiling. Other modes
# keep the configured limit.
COMPANY_BRIEF_MAX_TOKENS = 8192


def _max_tokens_for(mode: str) -> int | None:
    """Per-mode generation ceiling. None = use the configured default."""
    return COMPANY_BRIEF_MAX_TOKENS if mode == "company_brief" else None


def _friendly_error(e: Exception) -> str:
    """Turn a provider exception into a message that names the fix.

    The raw text is always appended — a bare "오류: 400" sent the last debugging
    session hunting through server logs for what turned out to be an exhausted
    credit balance. Whatever we fail to classify still reaches the user intact.
    """
    raw = str(e)
    low = raw.lower()
    hint = ""

    if "credit balance is too low" in low or "insufficient" in low:
        hint = (
            "Anthropic API 크레딧이 소진됐습니다. "
            "console.anthropic.com 에서 충전하거나, config.yaml 의 "
            "`generation.provider` 를 `gemini`(무료)로 바꾸세요."
        )
    elif "quota" in low or "resource_exhausted" in low or "429" in low:
        hint = "무료 사용량 한도에 걸렸습니다. 잠시 후 다시 시도하세요."
    elif "api key" in low or "unauthenticated" in low or "401" in low or "403" in low:
        hint = (
            "API 키가 없거나 잘못됐습니다. .env 의 GEMINI_API_KEY / "
            "ANTHROPIC_API_KEY 를 확인하세요."
        )
    elif "GeminiUnavailable" in type(e).__name__:
        hint = raw  # already actionable Korean
    elif isinstance(e, (ConnectionError, TimeoutError)) or "timeout" in low:
        hint = "네트워크 연결에 실패했습니다. 인터넷 상태를 확인하세요."

    return f"{hint}\n\n(원문: {raw})" if hint else raw


def _sse(event: str, data: Any) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


# --- Health -----------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    settings = get_settings()
    try:
        store = open_store()
        rows = store.count()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "rows": rows,
        "vault": str(settings.vault.resolved_path),
        "vault_name": settings.vault.resolved_path.name,
        "provider": settings.generation.provider,
        "gen_model": active_model_name(),
        "analyzer_model": (
            settings.generation.gemini_analyzer_model
            if settings.generation.provider == "gemini"
            else settings.generation.analyzer_model
        ),
        "zdr_enabled": settings.generation.zdr_enabled,
        "reindex_running": _state["reindex_running"],
    }


# --- /ask (SSE) -------------------------------------------------------------
@app.post("/ask")
def ask_stream(req: AskRequest):
    def gen() -> Iterator[str]:
        started = time.perf_counter()
        try:
            store, bm25 = _get_store_and_bm25()
            if store.count() == 0:
                yield _sse("error", {"message": "LanceDB is empty. Run an ingest first."})
                return

            # 1-2. Analyze + retrieve
            query, analysis, result = _analyze_and_retrieve(req, store, bm25)
            yield _sse("analysis", analysis.__dict__)
            citations = build_citations(result.rows)
            yield _sse(
                "citations",
                {
                    "where_clause": result.where_clause,
                    "mode": getattr(result, "mode", "hybrid"),
                    "items": [_citation_to_out(c).model_dump() for c in citations],
                },
            )

            if not citations:
                yield _sse("delta", {"text": _empty_result_message(req)})
                yield _sse("done", {"elapsed_seconds": round(time.perf_counter() - started, 2)})
                return

            # 3. Generate
            client = _get_client()
            mode = getattr(result, "mode", "lookup")
            user_message = build_user_message(req.query, citations, mode=mode)
            chunks: list[str] = []
            for chunk in client.stream_iter(
                SYSTEM_PROMPT_KO, user_message, max_tokens=_max_tokens_for(mode)
            ):
                chunks.append(chunk)
                yield _sse("delta", {"text": chunk})

            # Parse `[n]` tokens from the final answer so the plugin can hide
            # citation cards the LLM didn't actually use. Retrieval pool can
            # include weak matches (e.g. vector top-K filling slots when the
            # query has only one strong hit), and the LLM correctly ignores
            # them — we shouldn't display them as if they were sources.
            full = "".join(chunks)
            used = sorted({int(m) for m in re.findall(r"\[(\d+)\]", full)
                           if 1 <= int(m) <= len(citations)})
            yield _sse(
                "done",
                {
                    "elapsed_seconds": round(time.perf_counter() - started, 2),
                    "answer_length": len(full),
                    "used_citation_indices": used,  # 1-indexed, matches [n] in answer
                },
            )
        except Exception as e:  # noqa: BLE001
            log.exception("ask_stream error")
            yield _sse("error", {"message": _friendly_error(e)})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- /ask/json (non-streaming) ---------------------------------------------
@app.post("/ask/json", response_model=AskJsonResponse)
def ask_json(req: AskRequest) -> AskJsonResponse:
    started = time.perf_counter()
    store, bm25 = _get_store_and_bm25()
    if store.count() == 0:
        raise HTTPException(503, "LanceDB is empty. Run an ingest first.")

    query, analysis, result = _analyze_and_retrieve(req, store, bm25)
    citations = build_citations(result.rows)

    if not citations:
        return AskJsonResponse(
            query=req.query,
            analysis=analysis.__dict__,
            citations=[],
            answer=_empty_result_message(req),
            usage={},
            elapsed_seconds=round(time.perf_counter() - started, 2),
        )

    client = _get_client()
    mode = getattr(result, "mode", "lookup")
    try:
        gen = client.stream(
            SYSTEM_PROMPT_KO,
            build_user_message(req.query, citations, mode=mode),
            max_tokens=_max_tokens_for(mode),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("ask_json generation error")
        raise HTTPException(502, _friendly_error(e)) from e
    return AskJsonResponse(
        query=req.query,
        analysis=analysis.__dict__,
        citations=[_citation_to_out(c) for c in citations],
        answer=gen.text,
        usage={
            "input_tokens": gen.input_tokens,
            "output_tokens": gen.output_tokens,
            "cache_creation_tokens": gen.cache_creation_tokens,
            "cache_read_tokens": gen.cache_read_tokens,
        },
        elapsed_seconds=round(time.perf_counter() - started, 2),
    )


# --- /companies -------------------------------------------------------------
@app.get("/companies")
def companies(q: str | None = None, limit: int = 500) -> dict:
    """Company names present in the index, for the briefing picker.

    Sorted by note count desc so the companies with real history surface first.
    `q` does a case-insensitive substring match.
    """
    store, _ = _get_store_and_bm25()
    items = store.distinct_companies()
    if q:
        needle = q.strip().lower()
        items = [(name, n) for name, n in items if needle in name.lower()]
    return {
        "total": len(items),
        "items": [{"company": name, "notes": n} for name, n in items[:limit]],
    }


# --- /reindex ---------------------------------------------------------------
def _run_reindex(mode: str) -> None:
    from rag.ingest.pipeline import index_full_vault, index_incremental, refresh_metadata

    _state["reindex_running"] = True
    try:
        log.info("reindex start (mode=%s)", mode)
        if mode == "full":
            index_full_vault(show_progress=False)
        elif mode == "refresh-metadata":
            refresh_metadata(show_progress=False)
        else:
            index_incremental(show_progress=False)
        _reset_index_singletons()
        log.info("reindex done")
    except Exception:
        log.exception("reindex failed")
    finally:
        _state["reindex_running"] = False


@app.post("/reindex")
def reindex(req: ReindexRequest) -> dict:
    if _state["reindex_running"]:
        raise HTTPException(409, "Reindex already running")
    t = threading.Thread(target=_run_reindex, args=(req.mode,), daemon=True)
    t.start()
    return {"started": True, "mode": req.mode}
