"""KRUN RAG — Streamlit MVP UI (Phase 1C).

Chat interface over the LanceDB-backed RAG core. Sidebar lets the user
override / refine the analyzer's filters and trigger a metadata reindex
without leaving the browser.

Run:
    uv run streamlit run apps/streamlit_app.py

Open http://localhost:8501 in your browser.
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

# Make the project importable when launched via `streamlit run apps/streamlit_app.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from rag.config import get_settings
from rag.generation.claude_client import ClaudeClient
from rag.generation.prompts import SYSTEM_PROMPT_KO, build_user_message
from rag.retrieval.citations import Citation, build_citations
from rag.retrieval.hybrid_search import hybrid_search
from rag.retrieval.query_analyzer import KNOWN_DOC_TYPES, QueryAnalysis, analyze_query
from rag.store.bm25_index import BM25Index, open_bm25_index
from rag.store.lancedb_store import VaultChunkStore, open_store


# --- Pricing (USD per 1M tokens) — Sonnet 4.6 + Haiku 4.5 -----------------
PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.0, "output": 5.0,  "cache_write": 1.25, "cache_read": 0.10},
}


# --- Cached resources -----------------------------------------------------
@st.cache_resource(show_spinner=False)
def cached_settings() -> Any:
    return get_settings()


@st.cache_resource(show_spinner=False)
def cached_store() -> VaultChunkStore:
    return open_store()


@st.cache_resource(show_spinner=False)
def cached_bm25() -> BM25Index:
    return open_bm25_index(store=cached_store())


@st.cache_resource(show_spinner=False)
def cached_client() -> ClaudeClient:
    return ClaudeClient()


# --- Session state initialization ----------------------------------------
def _init_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages: list[dict] = []
    if "stats" not in st.session_state:
        st.session_state.stats = {
            "queries": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_write_tokens": 0,
            "cache_read_tokens": 0,
            "elapsed_seconds": 0.0,
        }


def _accumulate_stats(input_tok: int, output_tok: int, cw: int, cr: int, elapsed: float) -> None:
    s = st.session_state.stats
    s["queries"] += 1
    s["input_tokens"] += input_tok
    s["output_tokens"] += output_tok
    s["cache_write_tokens"] += cw
    s["cache_read_tokens"] += cr
    s["elapsed_seconds"] += elapsed


def _estimated_cost_usd(stats: dict) -> float:
    p = PRICING["claude-sonnet-4-6"]
    return (
        stats["input_tokens"]        * p["input"]       / 1e6
        + stats["output_tokens"]     * p["output"]      / 1e6
        + stats["cache_write_tokens"]* p["cache_write"] / 1e6
        + stats["cache_read_tokens"] * p["cache_read"]  / 1e6
    )


# --- Sidebar --------------------------------------------------------------
def render_sidebar() -> dict[str, Any]:
    s = cached_settings()
    store = cached_store()
    stats = st.session_state.stats

    with st.sidebar:
        st.title("🔍 KRUN RAG")
        st.caption("케이런 VC 내부 자료 Q&A")

        st.divider()
        st.subheader("필터")
        sb_doc_types = st.multiselect(
            "문서 유형 (doc_type)",
            sorted(KNOWN_DOC_TYPES - {"other"}),
            help="비워두면 분석기가 자동 결정",
        )
        sb_company = st.text_input("회사 (정확 일치)", placeholder="예: Blueward")
        sb_person = st.text_input("인물 (정확 일치)", placeholder="예: 강규식 상무")

        col1, col2 = st.columns(2)
        with col1:
            sb_date_from = st.date_input("시작일", value=None, format="YYYY-MM-DD")
        with col2:
            sb_date_to = st.date_input("종료일", value=None, format="YYYY-MM-DD")

        st.divider()
        st.subheader("옵션")
        use_analyzer = st.toggle(
            "Haiku 자동 필터 추출",
            value=True,
            help="OFF로 두면 사이드바 필터만 사용 (검색 ~1초 빨라짐)",
        )
        use_reranker = st.toggle(
            "리랭커 (bge-reranker-v2-m3)",
            value=s.retrieval.reranker_enabled,
            help="ON이면 cross-encoder가 top-30을 재랭크. CPU에서 +200ms, 정확도 ↑.",
        )
        show_search_details = st.toggle("검색 세부 정보 표시", value=False)
        top_k = st.slider("최종 청크 개수 (top-K)", 4, 20, value=s.retrieval.final_top_k)
        max_per_file = st.slider(
            "파일당 최대 청크 (다양화)", 1, 10,
            value=max(s.retrieval.max_chunks_per_file, 1),
            help="한 노트의 여러 섹션이 top-K를 독점하지 않도록 제한",
        )

        st.divider()
        st.subheader("관리")
        incremental_btn = st.button(
            "🔄 신규/변경 노트 인덱싱",
            help="신규·변경·삭제된 노트만 감지해서 인덱싱 (보통 수 초~1분)",
        )
        reindex_metadata = st.button(
            "📥 메타데이터만 새로고침",
            help="기존 행 메타데이터만 재추출 (임베딩 재사용, ~1분)",
        )
        rebuild_bm25 = st.button("🧮 BM25 인덱스 재구축", help="BM25 사이드카만 다시 빌드")

        st.divider()
        st.subheader("세션 통계")
        st.metric("질문 수", stats["queries"])
        c1, c2 = st.columns(2)
        c1.metric("input tok", f"{stats['input_tokens']:,}")
        c2.metric("output tok", f"{stats['output_tokens']:,}")
        c3, c4 = st.columns(2)
        c3.metric("cache_read", f"{stats['cache_read_tokens']:,}")
        c4.metric("cache_write", f"{stats['cache_write_tokens']:,}")
        st.metric("예상 비용", f"${_estimated_cost_usd(stats):.4f}")

        st.divider()
        st.caption(f"인덱스: **{store.count():,}** 청크")
        st.caption(f"모델: {s.generation.gen_model}")
        st.caption(f"ZDR: {'ON' if s.generation.zdr_enabled else 'OFF'}")

    # Trigger admin actions outside the with block (Streamlit reruns on click).
    if incremental_btn:
        from rag.ingest.pipeline import index_incremental

        with st.spinner("신규/변경 노트 인덱싱 중..."):
            stats_run, diff = index_incremental()
        msg = (
            f"신규 {len(diff.new)} · 변경 {len(diff.changed)} · "
            f"삭제 {len(diff.deleted)} · 청크 {stats_run.chunks_total} "
            f"({stats_run.elapsed_seconds:.1f}s)"
        )
        if not (diff.new or diff.changed or diff.deleted):
            st.info("변경 사항이 없습니다.")
        else:
            st.success(msg)
        cached_store.clear()
        cached_bm25.clear()
        st.rerun()

    if reindex_metadata:
        from rag.ingest.pipeline import refresh_metadata as _refresh

        with st.spinner("메타데이터 새로고침 중 (약 1분)..."):
            stats_run = _refresh()
        st.success(
            f"완료: {stats_run.files_indexed} files / "
            f"{stats_run.chunks_total} chunks 갱신 ({stats_run.elapsed_seconds:.1f}s)"
        )
        cached_store.clear()
        cached_bm25.clear()
        st.rerun()

    if rebuild_bm25:
        with st.spinner("BM25 인덱스 재구축..."):
            cached_bm25.clear()
            bm25 = open_bm25_index(store=cached_store(), force_rebuild=True)
        st.success(f"BM25 rebuilt: {bm25.row_count:,} rows")
        st.rerun()

    return {
        "doc_types": sb_doc_types,
        "company": sb_company.strip(),
        "person": sb_person.strip(),
        "date_from": sb_date_from.isoformat() if isinstance(sb_date_from, date) else None,
        "date_to": sb_date_to.isoformat() if isinstance(sb_date_to, date) else None,
        "use_analyzer": use_analyzer,
        "use_reranker": use_reranker,
        "show_search_details": show_search_details,
        "top_k": top_k,
        "max_per_file": max_per_file,
    }


# --- Citation rendering --------------------------------------------------
def _render_citations(citations: list[Citation]) -> None:
    """Render citations as expandable cards with obsidian:// links."""
    if not citations:
        return
    with st.expander(f"📚 출처 {len(citations)}개", expanded=False):
        for c in citations:
            cols = st.columns([5, 1])
            with cols[0]:
                meta_bits: list[str] = [c.doc_type]
                if c.company:
                    meta_bits.append(c.company)
                if c.person:
                    meta_bits.append(c.person)
                if c.date:
                    meta_bits.append(c.date)
                st.markdown(f"**[{c.n}] {c.title}**  ·  *{' · '.join(meta_bits)}*")
                if c.header_path:
                    st.caption(f"📍 {c.header_path}")
                if c.snippet:
                    st.caption(c.snippet)
            with cols[1]:
                st.link_button("🔗 노트", c.obsidian_uri, use_container_width=True)
            st.divider()


def _merge_filters(analysis: QueryAnalysis, sidebar: dict) -> QueryAnalysis:
    """Sidebar filter overrides. Sidebar values, when set, replace analyzer output."""
    if sidebar["doc_types"]:
        analysis.doc_types = list(sidebar["doc_types"])
    if sidebar["company"]:
        analysis.companies = [sidebar["company"]]
    if sidebar["person"]:
        analysis.persons = [sidebar["person"]]
    if sidebar["date_from"]:
        analysis.date_from = sidebar["date_from"]
    if sidebar["date_to"]:
        analysis.date_to = sidebar["date_to"]
    return analysis


# --- Main flow ------------------------------------------------------------
def render_history() -> None:
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("citations"):
                _render_citations(msg["citations"])
            if msg.get("usage"):
                u = msg["usage"]
                cache_pct = (
                    100 * u["cache_read"] / max(u["input"] + u["cache_read"], 1)
                )
                st.caption(
                    f"⚡ in {u['input']} · out {u['output']} · "
                    f"cache_read {u['cache_read']} · {cache_pct:.0f}% cached · "
                    f"{u['elapsed']:.1f}s"
                )


def handle_query(query: str, sidebar: dict) -> None:
    settings = cached_settings()
    store = cached_store()
    bm25 = cached_bm25()
    client = cached_client()

    # 1. Show user turn.
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    # 2. Process in assistant turn.
    with st.chat_message("assistant"):
        started = time.perf_counter()

        # 2a. Analyze
        if sidebar["use_analyzer"]:
            with st.spinner("🧠 질의 분석 (Haiku)..."):
                analysis = analyze_query(query)
        else:
            analysis = QueryAnalysis(raw=query, rewritten_query=query, used_fallback=True)

        analysis = _merge_filters(analysis, sidebar)

        # 2b. Search
        spinner_label = "🔎 하이브리드 검색 + 리랭크..." if sidebar["use_reranker"] else "🔎 하이브리드 검색..."
        with st.spinner(spinner_label):
            cfg = settings.retrieval.model_copy(
                update={
                    "final_top_k": sidebar["top_k"],
                    "reranker_enabled": sidebar["use_reranker"],
                    "max_chunks_per_file": sidebar["max_per_file"],
                }
            )
            result = hybrid_search(
                query=query,
                analysis=analysis,
                store=store,
                bm25=bm25,
                settings=cfg,
            )
            citations = build_citations(result.rows)

        # 2c. Optional debug card
        if sidebar["show_search_details"]:
            with st.expander("🔧 검색 세부 정보"):
                st.json(
                    {
                        "rewritten_query": analysis.rewritten_query,
                        "companies": analysis.companies,
                        "persons": analysis.persons,
                        "doc_types": analysis.doc_types,
                        "date_range": [analysis.date_from, analysis.date_to],
                        "tags": analysis.tags,
                        "analyzer_fallback": analysis.used_fallback,
                        "where_clause": result.where_clause,
                        "bm25_hits": result.bm25_hit_count,
                        "vector_hits": result.vector_hit_count,
                        "fused_top_k": result.fused_count,
                        "reranked": result.reranked,
                        "rerank_pool_size": result.rerank_pool_size,
                        "diversified": result.diversified,
                        "distinct_files": result.distinct_files,
                    }
                )

        # 2d. Generate (streaming)
        if not citations:
            answer = "제공된 자료에서 관련 청크를 찾지 못했습니다. 사이드바 필터를 완화하거나 질문을 다시 표현해 보세요."
            st.warning(answer)
            elapsed = time.perf_counter() - started
            st.session_state.messages.append({
                "role": "assistant",
                "content": answer,
                "citations": [],
                "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "elapsed": elapsed},
            })
            return

        user_msg = build_user_message(query, citations)
        placeholder = st.empty()
        chunks: list[str] = []

        def on_text(t: str) -> None:
            chunks.append(t)
            placeholder.markdown("".join(chunks) + "▌")

        try:
            gen = client.stream(SYSTEM_PROMPT_KO, user_msg, on_text=on_text)
        except Exception as e:
            placeholder.error(f"생성 중 오류: {e!r}")
            return

        placeholder.markdown(gen.text)
        elapsed = time.perf_counter() - started

        _render_citations(citations)
        cache_pct = (
            100 * gen.cache_read_tokens / max(gen.input_tokens + gen.cache_read_tokens, 1)
        )
        st.caption(
            f"⚡ in {gen.input_tokens} · out {gen.output_tokens} · "
            f"cache_read {gen.cache_read_tokens} · {cache_pct:.0f}% cached · "
            f"{elapsed:.1f}s"
        )

        _accumulate_stats(
            input_tok=gen.input_tokens,
            output_tok=gen.output_tokens,
            cw=gen.cache_creation_tokens,
            cr=gen.cache_read_tokens,
            elapsed=elapsed,
        )

        st.session_state.messages.append({
            "role": "assistant",
            "content": gen.text,
            "citations": citations,
            "usage": {
                "input": gen.input_tokens,
                "output": gen.output_tokens,
                "cache_read": gen.cache_read_tokens,
                "cache_write": gen.cache_creation_tokens,
                "elapsed": elapsed,
            },
        })


# --- Entrypoint -----------------------------------------------------------
def main() -> None:
    st.set_page_config(
        page_title="KRUN RAG",
        page_icon="🔍",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _init_state()

    sidebar = render_sidebar()

    # Header strip
    st.title("🔍 KRUN RAG")
    st.caption(
        "Obsidian 볼트(`Obsidian_KRUN_Antonio`)에 대한 한국어 자연어 Q&A. "
        "BGE-M3 로컬 임베딩 + Sonnet 4.6 생성. 모든 답변은 클릭 가능한 출처로 검증 가능."
    )

    render_history()

    if query := st.chat_input("질문을 입력하세요 (예: 'Blueward 1차DD 핵심 리스크')"):
        handle_query(query.strip(), sidebar)
        st.rerun()


if __name__ == "__main__":
    main()
