"""End-to-end query CLI: question -> analyzer -> hybrid search -> Claude answer.

Run:
    uv run python -m rag.query "위밋모빌리티 1차DD 핵심 리스크"
    uv run python -m rag.query --no-analyze "Blueward 검토 단계 정리"
    uv run python -m rag.query --json "지난 분기 만난 LP"
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from rag.config import get_settings
from rag.generation.claude_client import ClaudeClient
from rag.generation.prompts import SYSTEM_PROMPT_KO, build_user_message
from rag.retrieval.citations import Citation, build_citations
from rag.retrieval.hybrid_search import hybrid_search
from rag.retrieval.query_analyzer import QueryAnalysis, analyze_query
from rag.store.bm25_index import open_bm25_index
from rag.store.lancedb_store import open_store


def _print_analysis(a: QueryAnalysis) -> None:
    print("\n[1/3] Query analysis", flush=True)
    print(f"      raw       : {a.raw}")
    print(f"      rewritten : {a.rewritten_query}")
    if a.companies:
        print(f"      companies : {a.companies}")
    if a.persons:
        print(f"      persons   : {a.persons}")
    if a.doc_types:
        print(f"      doc_types : {a.doc_types}")
    if a.tags:
        print(f"      tags      : {a.tags}")
    if a.date_from or a.date_to:
        print(f"      date      : {a.date_from or '-'}  →  {a.date_to or '-'}")
    if a.used_fallback:
        print("      (analyzer fell back to pass-through)")
    else:
        print(f"      latency   : {a.latency_ms} ms")


def _print_search(citations: list[Citation], where: str | None) -> None:
    print(f"\n[2/3] Hybrid search  →  {len(citations)} citations" + (f"  WHERE {where}" if where else ""))
    for c in citations:
        meta_bits = []
        if c.company:
            meta_bits.append(c.company)
        if c.person:
            meta_bits.append(c.person)
        if c.date:
            meta_bits.append(c.date)
        meta = " | ".join(meta_bits)
        head = f"      [{c.n}] {c.short_path()}"
        if c.header_path:
            head += f"  >  {c.header_path}"
        print(head + (f"   ({meta})" if meta else ""))


def _print_answer_header() -> None:
    print("\n[3/3] Generating answer (streaming)\n")
    print("-" * 60)


def _print_sources(citations: list[Citation]) -> None:
    if not citations:
        return
    print("\n" + "-" * 60)
    print("Sources:")
    for c in citations:
        print(f"  [{c.n}] {c.display_label()}")
        print(f"       {c.obsidian_uri}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KRUN RAG: ask a question of your vault.")
    parser.add_argument("query", nargs="+", help="The question (Korean or English)")
    parser.add_argument(
        "--no-analyze",
        action="store_true",
        help="Skip the Haiku analyzer (faster, but no metadata filtering)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Override final_top_k (chunks sent to Claude)",
    )
    parser.add_argument(
        "--rebuild-bm25",
        action="store_true",
        help="Force rebuild of the BM25 sidecar index",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON (analysis + citations + answer) instead of streaming text",
    )
    args = parser.parse_args(argv)

    query = " ".join(args.query).strip()
    if not query:
        print("error: empty query", file=sys.stderr)
        return 2

    settings = get_settings()
    store = open_store()
    bm25 = open_bm25_index(store=store, force_rebuild=args.rebuild_bm25)

    if store.count() == 0:
        print("error: LanceDB is empty. Run `uv run python -m rag.ingest.pipeline --full` first.", file=sys.stderr)
        return 1

    # 1. Analyze
    started = time.perf_counter()
    if args.no_analyze:
        analysis = QueryAnalysis(raw=query, rewritten_query=query, used_fallback=True)
    else:
        analysis = analyze_query(query)
    if not args.json:
        _print_analysis(analysis)

    # 2. Retrieve
    if args.top_k:
        cfg = settings.retrieval.model_copy(update={"final_top_k": args.top_k})
    else:
        cfg = settings.retrieval
    result = hybrid_search(
        query=query,
        analysis=analysis,
        store=store,
        bm25=bm25,
        settings=cfg,
    )
    citations = build_citations(result.rows)
    if not args.json:
        _print_search(citations, where=result.where_clause)

    if not citations:
        msg = "제공된 자료에서 관련 청크를 찾지 못했습니다. 질문을 다시 표현하거나 필터를 완화해보세요."
        if args.json:
            print(json.dumps({"answer": msg, "analysis": analysis.__dict__, "citations": []}, ensure_ascii=False, default=str))
        else:
            print(f"\n{msg}\n")
        return 0

    # 3. Generate
    user_message = build_user_message(query, citations)
    client = ClaudeClient()

    if args.json:
        gen = client.stream(SYSTEM_PROMPT_KO, user_message)
        elapsed = time.perf_counter() - started
        out = {
            "query": query,
            "analysis": analysis.__dict__,
            "citations": [
                {
                    "n": c.n,
                    "title": c.title,
                    "vault_relative": c.vault_relative,
                    "header_path": c.header_path,
                    "obsidian_uri": c.obsidian_uri,
                    "doc_type": c.doc_type,
                    "company": c.company,
                    "person": c.person,
                    "date": c.date,
                }
                for c in citations
            ],
            "answer": gen.text,
            "usage": {
                "input_tokens": gen.input_tokens,
                "output_tokens": gen.output_tokens,
                "cache_creation_tokens": gen.cache_creation_tokens,
                "cache_read_tokens": gen.cache_read_tokens,
            },
            "elapsed_seconds": round(elapsed, 2),
        }
        print(json.dumps(out, ensure_ascii=False, default=str, indent=2))
        return 0

    _print_answer_header()
    gen = client.stream(
        SYSTEM_PROMPT_KO,
        user_message,
        on_text=lambda chunk: (sys.stdout.write(chunk), sys.stdout.flush()),
    )
    print()  # newline after stream
    _print_sources(citations)

    elapsed = time.perf_counter() - started
    cache_hit_pct = (
        100 * gen.cache_read_tokens / max(gen.input_tokens + gen.cache_read_tokens, 1)
    )
    print(
        f"\n[usage] input={gen.input_tokens}  output={gen.output_tokens}  "
        f"cache_write={gen.cache_creation_tokens}  cache_read={gen.cache_read_tokens}  "
        f"({cache_hit_pct:.0f}% cached)  elapsed={elapsed:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
