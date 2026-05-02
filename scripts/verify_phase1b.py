"""Phase 1B Gate verification.

Smoke-tests the full pipeline (analyzer + hybrid search + generation) against
a small built-in question set, reporting:
- BM25 sidecar status
- query analyzer latency
- hybrid search hit counts
- Claude generation success + citation coverage

Run:
    uv run python scripts/verify_phase1b.py
    uv run python scripts/verify_phase1b.py --questions Blueward DEKIST
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


DEFAULT_QUESTIONS = [
    "최근 검토 단계 회사 중 핵심 리스크가 있는 곳",
    "지난주 미팅에서 나온 액션아이템",
    "Blueward 1차DD 리스크 요인",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--questions",
        nargs="+",
        default=None,
        help="Override the default question list",
    )
    args = parser.parse_args(argv)

    print("=" * 60)
    print("KRUN RAG — Phase 1B Gate verification")
    print("=" * 60)

    from rag.config import get_settings
    from rag.generation.claude_client import ClaudeClient
    from rag.generation.prompts import SYSTEM_PROMPT_KO, build_user_message
    from rag.retrieval.citations import build_citations
    from rag.retrieval.hybrid_search import hybrid_search
    from rag.retrieval.query_analyzer import analyze_query
    from rag.store.bm25_index import open_bm25_index
    from rag.store.lancedb_store import open_store

    s = get_settings()
    store = open_store()
    if store.count() == 0:
        print("[FAIL] LanceDB is empty. Run pipeline --full first.")
        return 1
    print(f"\n[1] LanceDB rows: {store.count()}")

    # --- BM25 build / load ----------------------------------------------
    print("\n[2] BM25 sidecar")
    t0 = time.perf_counter()
    bm25 = open_bm25_index(store=store)
    print(f"    tokenizer={bm25.tokenizer_name}  rows={bm25.row_count}  ({time.perf_counter() - t0:.2f}s)")
    if bm25.row_count != store.count():
        print(f"    [WARN] BM25 row count {bm25.row_count} != LanceDB {store.count()}")

    # --- Quick BM25 self-test ------------------------------------------
    sample_hits = bm25.search("리스크 요인", top_k=3)
    if not sample_hits:
        print("    [FAIL] BM25 returned 0 hits for '리스크 요인'")
        return 1
    print(f"    BM25 self-test: top hit score={sample_hits[0][1]:.3f}")

    # --- Run full pipeline on sample questions ------------------------
    questions = args.questions or DEFAULT_QUESTIONS
    client: ClaudeClient | None = None

    print(f"\n[3] Running {len(questions)} sample question(s)")
    failures = 0
    for i, q in enumerate(questions, start=1):
        print("\n" + "-" * 60)
        print(f"Q{i}: {q}")

        # 3a. Analyze
        t0 = time.perf_counter()
        analysis = analyze_query(q)
        print(
            f"   analyze: rewritten='{analysis.rewritten_query}'  "
            f"companies={analysis.companies}  doc_types={analysis.doc_types}  "
            f"date=[{analysis.date_from or '-'}, {analysis.date_to or '-'}]  "
            f"({analysis.latency_ms} ms{' fallback' if analysis.used_fallback else ''})"
        )

        # 3b. Hybrid search
        result = hybrid_search(query=q, analysis=analysis, store=store, bm25=bm25)
        citations = build_citations(result.rows)
        print(
            f"   search: bm25={result.bm25_hit_count}  "
            f"vector={result.vector_hit_count}  fused={result.fused_count}  "
            f"WHERE={result.where_clause or '(none)'}"
        )
        if not citations:
            print("   [WARN] no citations — skipping generation")
            failures += 1
            continue

        # 3c. Generate
        if client is None:
            client = ClaudeClient()
        gen_start = time.perf_counter()
        user_msg = build_user_message(q, citations)
        gen = client.stream(SYSTEM_PROMPT_KO, user_msg, max_tokens=1024)
        gen_elapsed = time.perf_counter() - gen_start

        # Count [n] citations in output.
        cites_in_answer = set(int(m) for m in re.findall(r"\[(\d+)\]", gen.text))
        coverage_ok = bool(cites_in_answer)
        print(
            f"   generate: {gen.output_tokens} out tokens  ({gen_elapsed:.1f}s)  "
            f"input={gen.input_tokens}  cache_write={gen.cache_creation_tokens}  "
            f"cache_read={gen.cache_read_tokens}"
        )
        print(f"   citations in answer: {sorted(cites_in_answer) or '(none)'}")
        print(f"   stop_reason: {gen.stop_reason}")
        # Echo first 240 chars of answer.
        snippet = gen.text.replace("\n", " ").strip()[:240]
        print(f"   preview: {snippet}{'…' if len(gen.text) > 240 else ''}")

        if not coverage_ok:
            print("   [WARN] answer has no [n] citations — prompt or context may be off")
            failures += 1

    print("\n" + "=" * 60)
    if failures:
        print(f"[PARTIAL] {len(questions) - failures}/{len(questions)} questions passed")
        return 1
    print(f"[PASS] Phase 1B Gate: {len(questions)} questions answered with citations")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
