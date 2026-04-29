"""Hybrid retrieval: BM25 ∪ ANN, fused by Reciprocal Rank Fusion (+ optional rerank).

Pipeline:
1. Translate the QueryAnalysis filters into a LanceDB SQL `where` clause.
2. Run BM25 on the chunk corpus (top `bm25_top_k`).
3. Run ANN vector search with the same `where` (top `vector_top_k`).
4. Apply the same `where` to BM25 hits post-hoc (BM25 has no metadata index).
5. Reciprocal-rank-fuse the two ranked lists.
6. If `reranker_enabled`, score the fused pool with a cross-encoder and
   reorder. Otherwise truncate to `final_top_k` directly.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from rag.config import RetrievalConfig, get_settings
from rag.ingest.embedder import Embedder, get_default_embedder
from rag.retrieval.query_analyzer import QueryAnalysis
from rag.store.bm25_index import BM25Index, open_bm25_index
from rag.store.lancedb_store import VaultChunkStore, open_store


@dataclass
class HybridSearchResult:
    rows: list[dict]
    where_clause: str | None
    bm25_hit_count: int
    vector_hit_count: int
    fused_count: int
    reranked: bool = False
    rerank_pool_size: int = 0


# --- WHERE clause builder --------------------------------------------------
def _q(s: str) -> str:
    """Single-quote-escape for LanceDB SQL."""
    return s.replace("'", "''")


def build_where_clause(analysis: QueryAnalysis | None) -> str | None:
    """Translate QueryAnalysis filters into LanceDB SQL.

    Filters are conjunctive across categories (company AND date), but
    disjunctive within a category (company IN (...)). doc_type is treated as a
    *suggestion* unless the analyzer is highly confident, so we leave it OFF
    by default — the LLM analyzer often over-narrows.
    """
    if analysis is None:
        return None

    parts: list[str] = []
    if analysis.companies:
        joined = ", ".join(f"'{_q(c)}'" for c in analysis.companies)
        parts.append(f"company IN ({joined})")
    if analysis.persons:
        joined = ", ".join(f"'{_q(p)}'" for p in analysis.persons)
        parts.append(f"person IN ({joined})")
    if analysis.date_from:
        parts.append(f"date >= '{_q(analysis.date_from)}'")
    if analysis.date_to:
        parts.append(f"date <= '{_q(analysis.date_to)}'")
    return " AND ".join(parts) if parts else None


# --- Reciprocal Rank Fusion ------------------------------------------------
def rrf_fuse(
    ranked_lists: list[list[str]],
    k: int = 60,
    top_k: int = 8,
) -> list[tuple[str, float]]:
    """RRF: sum 1/(k+rank) across lists, then sort descending."""
    scores: dict[str, float] = defaultdict(float)
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked, start=1):
            scores[item_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]


# --- Main entry point ------------------------------------------------------
def hybrid_search(
    query: str,
    *,
    analysis: QueryAnalysis | None = None,
    store: VaultChunkStore | None = None,
    bm25: BM25Index | None = None,
    embedder: Embedder | None = None,
    reranker: object | None = None,  # rag.retrieval.reranker.Reranker
    settings: RetrievalConfig | None = None,
    where_override: str | None = None,
) -> HybridSearchResult:
    """Run hybrid retrieval (+ optional rerank) and return the final top-K rows."""
    s = get_settings()
    cfg = settings or s.retrieval
    store = store or open_store()
    bm25 = bm25 or open_bm25_index(store=store)
    embedder = embedder or get_default_embedder(
        model_name=s.embedding.model_name,
        device=s.embedding.device,
        batch_size=s.embedding.batch_size,
        max_seq_length=s.embedding.max_seq_length,
    )

    rewritten = analysis.rewritten_query if analysis else query
    where = where_override if where_override is not None else build_where_clause(analysis)

    # 1. BM25
    bm25_hits = bm25.search(rewritten, top_k=cfg.bm25_top_k)
    bm25_ids = [cid for cid, _ in bm25_hits]

    # 2. Vector
    qvec = embedder.encode([rewritten])[0]
    vector_rows = store.vector_search(qvec, limit=cfg.vector_top_k, where=where)
    vector_ids = [r["chunk_id"] for r in vector_rows]

    # 3. Apply WHERE to BM25 results post-hoc (BM25 has no metadata index).
    if where and bm25_ids:
        kept_ids: set[str] = set()
        for batch in _batched(bm25_ids, 500):
            clause = "chunk_id IN ({}) AND ({})".format(
                ", ".join(f"'{_q(c)}'" for c in batch), where
            )
            kept_rows = store.search_by_filter(clause, limit=len(batch), columns=["chunk_id"])
            kept_ids.update(r["chunk_id"] for r in kept_rows)
        bm25_ids = [cid for cid in bm25_ids if cid in kept_ids]

    # 4. Fuse
    if not bm25_ids and not vector_ids:
        return HybridSearchResult(
            rows=[], where_clause=where,
            bm25_hit_count=0, vector_hit_count=0, fused_count=0,
        )

    # When the reranker is on, we fuse to a *bigger* candidate pool than
    # `final_top_k` so the cross-encoder has more signal to work with.
    use_reranker = bool(getattr(cfg, "reranker_enabled", False))
    if use_reranker and reranker is None:
        try:
            from rag.retrieval.reranker import get_default_reranker
            reranker = get_default_reranker()
        except ImportError:
            use_reranker = False  # FlagEmbedding not installed; gracefully fall back

    pool_size = max(cfg.bm25_top_k, cfg.vector_top_k) if use_reranker else cfg.final_top_k
    fused = rrf_fuse([bm25_ids, vector_ids], k=cfg.rrf_k, top_k=pool_size)
    fused_ids = [cid for cid, _ in fused]

    # 5. Hydrate full rows for the fused IDs.
    rows_by_id: dict[str, dict] = {}
    for batch in _batched(fused_ids, 500):
        clause = "chunk_id IN ({})".format(", ".join(f"'{_q(c)}'" for c in batch))
        for r in store.search_by_filter(clause, limit=len(batch)):
            rows_by_id[r["chunk_id"]] = r
    pool = [rows_by_id[cid] for cid in fused_ids if cid in rows_by_id]

    # 6. Optional cross-encoder rerank on the fused pool.
    if use_reranker and pool:
        passages = [r.get("text") or "" for r in pool]
        try:
            ranked = reranker.rerank(rewritten, passages, top_k=cfg.final_top_k)  # type: ignore[union-attr]
            ordered = [pool[i] for i, _ in ranked]
        except Exception:
            ordered = pool[: cfg.final_top_k]
            use_reranker = False
    else:
        ordered = pool[: cfg.final_top_k]

    return HybridSearchResult(
        rows=ordered,
        where_clause=where,
        bm25_hit_count=len(bm25_hits),
        vector_hit_count=len(vector_rows),
        fused_count=len(ordered),
        reranked=use_reranker,
        rerank_pool_size=len(pool) if use_reranker else 0,
    )


def _batched(items: list[str], n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]
