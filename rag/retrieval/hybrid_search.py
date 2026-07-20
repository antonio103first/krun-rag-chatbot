"""Hybrid retrieval: BM25 ∪ ANN, fused by Reciprocal Rank Fusion (+ optional rerank).

Pipeline:
1. Translate the QueryAnalysis filters into a LanceDB SQL `where` clause.
2. Run BM25 on the chunk corpus (top `bm25_top_k`).
3. Run ANN vector search with the same `where` (top `vector_top_k`).
4. Apply the same `where` to BM25 hits post-hoc (BM25 has no metadata index).
5. Reciprocal-rank-fuse the two ranked lists.
6. If `reranker_enabled`, score the fused pool with a cross-encoder and reorder.
7. Diversify by file_path: cap chunks per file at `max_chunks_per_file` so
   one note's many sections can't crowd out other relevant files.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
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
    diversified: bool = False
    distinct_files: int = 0
    augmented_files: int = 0  # head chunks added via filter-aware breadth augmentation
    mode: str = "hybrid"  # "hybrid" | "enumerate" | "company_brief"


# --- Enumerate mode: pure metadata WHERE, one chunk per file --------------
# 200 covers "올해 미팅" (~88 files) and most year-scoped enumerations while
# keeping total tokens (head chunks only, ~500 tokens each) under ~100K.
ENUMERATE_LIMIT = 200


def _safe_str(v) -> str:
    """NaN-safe stringification. pandas leaks float('nan') for missing string
    columns, and NaN is truthy, so `r.get(k) or ""` doesn't catch it — but
    comparing NaN to a real str during sort raises TypeError."""
    if v is None:
        return ""
    if isinstance(v, float) and v != v:  # NaN check
        return ""
    return str(v)


def _safe_int(v) -> int:
    if v is None:
        return 0
    if isinstance(v, float) and v != v:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def enumerate_search(
    *,
    where: str,
    store: VaultChunkStore,
    limit: int = ENUMERATE_LIMIT,
) -> list[dict]:
    """Metadata-only listing: return one representative chunk per file.

    For 'list all April meetings' style queries — bypasses BM25/vector ranking
    so that the answer reflects the full set of matching files, not the top-K
    semantically-ranked chunks.

    Implementation: pull every matching row and keep each file's *lowest*
    chunk_idx.

    This used to filter on `chunk_idx = 0` in SQL, with a whole-query fallback
    if that returned nothing. That fallback couldn't help the real failure
    mode, which is per-file: purging body-less chunks (an H1 heading with no
    text under it is very common in these template-driven notes) removes some
    files' index-0 row while leaving others intact, so those files silently
    vanished from every enumeration and company briefing. Selecting the lowest
    surviving index per file is correct whether or not gaps exist.
    """
    rows = store.search_by_filter(where, limit=10000)
    rows.sort(key=lambda r: (_safe_str(r.get("date")), _safe_str(r.get("file_path")), _safe_int(r.get("chunk_idx"))))
    seen2: set[str] = set()
    out2: list[dict] = []
    for r in rows:
        fp = _safe_str(r.get("file_path"))
        if fp in seen2:
            continue
        seen2.add(fp)
        out2.append(r)
        if len(out2) >= limit:
            break
    return out2


def _diversify_by_file(rows: list[dict], max_per_file: int, top_k: int) -> list[dict]:
    """Cap how many chunks a single file may contribute, preserving order.

    `max_per_file <= 0` disables the cap.
    """
    if max_per_file <= 0 or not rows:
        return rows[:top_k]
    seen: Counter[str] = Counter()
    out: list[dict] = []
    for r in rows:
        fp = r.get("file_path") or ""
        if seen[fp] >= max_per_file:
            continue
        out.append(r)
        seen[fp] += 1
        if len(out) >= top_k:
            break
    # If diversification truncated below top_k (rare: very few distinct files),
    # backfill from remaining rows ignoring the cap so we still return top_k.
    if len(out) < top_k:
        chosen = {id(r) for r in out}
        for r in rows:
            if len(out) >= top_k:
                break
            if id(r) not in chosen:
                out.append(r)
    return out


def _strip_filter_entities(query: str, analysis) -> str:
    """Drop company/person names that WHERE already filters on.

    Only the names the analyzer actually put into the filter are removed — a
    name mentioned in the query but not extracted still carries signal and
    stays. Alias variants are stripped too, since the user may type "지엘캠"
    while the filter resolved to "지엘켐".

    Returns "" when stripping would leave nothing meaningful behind; the caller
    treats that as "no second pass" rather than searching on an empty string.
    """
    if analysis is None:
        return ""
    names: list[str] = []
    for attr in ("companies", "persons"):
        for n in getattr(analysis, attr, None) or []:
            if not n:
                continue
            names.append(str(n))
            try:
                from rag.aliases import all_variants

                names.extend(all_variants(str(n), kind=attr) or [])
            except Exception:  # aliases are an enhancement, never a hard dep
                pass
    if not names:
        return ""

    out = query
    # Longest first so "레디로버스트머신" is removed before a shorter alias
    # could carve it into fragments.
    for n in sorted({n for n in names if n}, key=len, reverse=True):
        out = re.sub(re.escape(n), " ", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+", " ", out).strip()

    # A residue of one short token ("의", "관련") is noise, not a query.
    return out if len(out) >= 2 else ""


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

    from rag.aliases import all_variants

    parts: list[str] = []
    if analysis.companies:
        # Expand each company through the alias table so a query referring to
        # a predecessor name (e.g. "ISTN") still matches files filed under the
        # current canonical name (Blueward). Dedupe while preserving order.
        seen: set[str] = set()
        expanded: list[str] = []
        for c in analysis.companies:
            for v in all_variants(c, kind="companies"):
                if v not in seen:
                    seen.add(v)
                    expanded.append(v)
        joined = ", ".join(f"'{_q(c)}'" for c in expanded)
        parts.append(f"company IN ({joined})")
    if analysis.persons:
        seen_p: set[str] = set()
        expanded_p: list[str] = []
        for p in analysis.persons:
            for v in all_variants(p, kind="persons"):
                if v not in seen_p:
                    seen_p.add(v)
                    expanded_p.append(v)
        joined = ", ".join(f"'{_q(p)}'" for p in expanded_p)
        parts.append(f"person IN ({joined})")
    if analysis.date_from:
        parts.append(f"date >= '{_q(analysis.date_from)}'")
    if analysis.date_to:
        parts.append(f"date <= '{_q(analysis.date_to)}'")
    # doc_type intentionally NOT used as WHERE in hybrid mode. We tried it as
    # a fallback when no other filter was set, but the analyzer too often emits
    # doc_types alone (e.g. "시너지 검토 진행" → just `project` because the
    # word 시너지 reads as "synergy"). The resulting narrow WHERE drops the
    # actual answer files. enumerate mode does its own doc_type handling.
    return " AND ".join(parts) if parts else None


# --- Company brief mode ----------------------------------------------------
# Pre-meeting context restore for a single company: every note filed under that
# company (complete list, no semantic ranking) plus the *full* text of the most
# recent few so the answer can cover risks/conclusions/open items — head chunks
# alone are usually just the attendee list.
COMPANY_BRIEF_DEEP_FILES = 5
# Character budget for the deep-loaded files. Korean runs ~1-1.5 chars/token, so
# 90K chars is roughly 60-90K tokens — well inside the context window while
# leaving room for the head chunks of older notes, which are never dropped.
COMPANY_BRIEF_CHAR_BUDGET = 90_000


def build_company_where(company: str) -> str:
    """`company IN (...)` expanded across the alias table.

    Scope is deliberately metadata-only: notes whose `company` field is set.
    Passing mentions inside other companies' notes have `company = NULL` and are
    out of scope by design — the briefing answers "미팅한 이력", not "언급된 곳".
    No doc_type filter: 예비검토보고서 and other resource-typed notes carry real
    deal context and would be lost by narrowing to meeting/company.
    """
    from rag.aliases import all_variants

    variants = all_variants(company, kind="companies") or [company]
    joined = ", ".join(f"'{_q(v)}'" for v in dict.fromkeys(variants))
    return f"company IN ({joined})"


def company_brief_search(
    *,
    company: str,
    store: VaultChunkStore | None = None,
    deep_files: int = COMPANY_BRIEF_DEEP_FILES,
    char_budget: int = COMPANY_BRIEF_CHAR_BUDGET,
) -> HybridSearchResult:
    """Two-stage retrieval: complete file list + deep text for recent notes.

    Stage A (`enumerate_search`) fixes the file list — one head chunk per note,
    date-ascending, nothing missing. Stage B re-fetches every chunk of the most
    recent `deep_files` notes. Budget is spent newest-first, but rows come back
    chronologically so citation numbers read as a timeline.
    """
    store = store or open_store()
    where = build_company_where(company)
    heads = enumerate_search(where=where, store=store, limit=ENUMERATE_LIMIT)
    if not heads:
        return HybridSearchResult(
            rows=[],
            where_clause=where,
            bm25_hit_count=0,
            vector_hit_count=0,
            fused_count=0,
            mode="company_brief",
        )

    ordered_paths = [_safe_str(r.get("file_path")) for r in heads]  # date-asc
    head_by_path = {_safe_str(r.get("file_path")): r for r in heads}
    deep_paths = ordered_paths[-deep_files:] if deep_files > 0 else []

    by_path: dict[str, list[dict]] = {}
    if deep_paths:
        joined = ", ".join(f"'{_q(p)}'" for p in deep_paths)
        full = store.search_by_filter(
            f"({where}) AND file_path IN ({joined})", limit=5000
        )
        for r in full:
            by_path.setdefault(_safe_str(r.get("file_path")), []).append(r)
        for chunks in by_path.values():
            chunks.sort(key=lambda r: _safe_int(r.get("chunk_idx")))

    # Spend the budget newest-first. A file that doesn't fit degrades to its
    # head chunk rather than disappearing — completeness of the list wins.
    selected: dict[str, list[dict]] = {}
    deep_used = 0
    for path in reversed(ordered_paths):
        chunks = by_path.get(path)
        if chunks:
            cost = sum(len(_safe_str(c.get("text"))) for c in chunks)
            if deep_used + cost <= char_budget:
                selected[path] = chunks
                deep_used += cost
                continue
        head = head_by_path.get(path)
        if head is not None:
            selected[path] = [head]

    rows: list[dict] = []
    seen_chunks: set[str] = set()
    for path in ordered_paths:  # chronological output
        for c in selected.get(path, []):
            cid = _safe_str(c.get("chunk_id"))
            if cid and cid in seen_chunks:
                continue
            if cid:
                seen_chunks.add(cid)
            rows.append(c)

    return HybridSearchResult(
        rows=rows,
        where_clause=where,
        bm25_hit_count=0,
        vector_hit_count=0,
        fused_count=len(rows),
        distinct_files=len(selected),
        mode="company_brief",
    )


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

    # Enumerate mode: when the analyzer flags an enumeration intent and we
    # have at least one structured filter (date / company / person), bypass
    # BM25/vector ranking and pull all matching files (deduped) up to a cap.
    # This is the right answer to "list all April meetings" — semantic top-K
    # would only surface a few of the 47 matching files.
    if (
        analysis is not None
        and getattr(analysis, "intent", "lookup") == "enumerate"
        and where  # require a structural narrowing (date/company/person);
        # doc_types-only enumeration is too broad and dumps random head chunks
        # for content-conditional queries like "남부권에 해당하는 회사 리스트".
    ):
        # In enumerate mode we DO honor doc_type from the analyzer (unlike the
        # default hybrid path), because narrowing to e.g. only meeting notes is
        # exactly what gives a clean enumerable list. When there's no other
        # WHERE filter (e.g. "Antonio가 투자한 업체들 모두" → just doc_types=
        # ["company"]), the doc_type clause alone carries the enumeration.
        if analysis.doc_types:
            joined = ", ".join(f"'{_q(t)}'" for t in analysis.doc_types)
            enum_where = f"({where}) AND doc_type IN ({joined})"
        else:
            enum_where = where
        rows = enumerate_search(where=enum_where, store=store, limit=ENUMERATE_LIMIT)
        return HybridSearchResult(
            rows=rows,
            where_clause=enum_where,
            bm25_hit_count=0,
            vector_hit_count=0,
            fused_count=len(rows),
            distinct_files=len({r.get("file_path") or "" for r in rows}),
            mode="enumerate",
        )

    # 1. BM25
    bm25_hits = bm25.search(rewritten, top_k=cfg.bm25_top_k)
    bm25_ids = [cid for cid, _ in bm25_hits]

    # 2. Vector
    qvec = embedder.encode([rewritten])[0]
    vector_rows = store.vector_search(qvec, limit=cfg.vector_top_k, where=where)
    vector_ids = [r["chunk_id"] for r in vector_rows]

    # 2b. Entity-stripped vector pass.
    # Once WHERE has narrowed the corpus to one company, every candidate shares
    # that name — it carries no discriminating signal, but the embedding still
    # weights it, which favours documents that *repeat* the name (index notes,
    # "투자 검토 요약" sections) over the meeting chunk that answers the
    # question. Measured on "레디로버스트머신 투심 지적사항": the chunk holding
    # the actual criticisms ranked 42/115, while dropping the company name from
    # the query moved comparable content into the top 10.
    # Fusing both passes rather than replacing keeps the name-bearing overview
    # chunks available for questions that genuinely want them ("회사 개요").
    stripped_ids: list[str] = []
    stripped = _strip_filter_entities(rewritten, analysis)
    if where and stripped and stripped != rewritten:
        svec = embedder.encode([stripped])[0]
        stripped_rows = store.vector_search(svec, limit=cfg.vector_top_k, where=where)
        stripped_ids = [r["chunk_id"] for r in stripped_rows]

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
        # Empty-result fallback: when WHERE was non-trivial, retry without it.
        # Common cause: analyzer extracts a name into `companies` / `persons`
        # that doesn't match any structured metadata (e.g. "로텀 관련한 내용" →
        # companies=['로텀'], but 로텀 is only mentioned inside a person meeting
        # note where the chunk's `company` field is NULL). BM25/vector without
        # the filter usually find the right chunk by content.
        if where:
            bm25_ids = [cid for cid, _ in bm25.search(rewritten, top_k=cfg.bm25_top_k)]
            vector_rows = store.vector_search(qvec, limit=cfg.vector_top_k, where=None)
            vector_ids = [r["chunk_id"] for r in vector_rows]
            where = None  # downstream steps (augmentation, etc.) skip filter
        if not bm25_ids and not vector_ids:
            return HybridSearchResult(
                rows=[], where_clause=where,
                bm25_hit_count=0, vector_hit_count=0, fused_count=0,
            )

    # We always fuse to a larger pool than `final_top_k` so that diversification
    # and (optional) reranking have headroom. When the reranker is on, this
    # also gives the cross-encoder more signal to work with.
    use_reranker = bool(getattr(cfg, "reranker_enabled", False))
    if use_reranker and reranker is None:
        try:
            from rag.retrieval.reranker import get_default_reranker
            reranker = get_default_reranker()
        except ImportError:
            use_reranker = False  # FlagEmbedding not installed; gracefully fall back

    max_per_file = getattr(cfg, "max_chunks_per_file", 0) or 0
    diversify = max_per_file > 0
    pool_size = max(cfg.bm25_top_k, cfg.vector_top_k) if (use_reranker or diversify) else cfg.final_top_k
    ranked_lists = [bm25_ids, vector_ids]
    if stripped_ids:
        ranked_lists.append(stripped_ids)
    fused = rrf_fuse(ranked_lists, k=cfg.rrf_k, top_k=pool_size)
    fused_ids = [cid for cid, _ in fused]

    # 5. Hydrate full rows for the fused IDs.
    rows_by_id: dict[str, dict] = {}
    for batch in _batched(fused_ids, 500):
        clause = "chunk_id IN ({})".format(", ".join(f"'{_q(c)}'" for c in batch))
        for r in store.search_by_filter(clause, limit=len(batch)):
            rows_by_id[r["chunk_id"]] = r
    pool = [rows_by_id[cid] for cid in fused_ids if cid in rows_by_id]
    augmented = 0

    # 5.5. Filter-aware breadth augmentation. When WHERE narrows the corpus to a
    # small set of files (e.g. company='메타씨앤아이'), the pool can be filled
    # entirely by chunks from one chunk-rich file (the company profile note),
    # leaving zero shot for sibling meeting notes to reach top-K via
    # diversification. We append each WHERE-matching file's first surviving
    # chunk when the pool doesn't already cover that file, so diversification
    # can distribute representation across all matching files. Only meaningful
    # when diversification is on (otherwise pool[:top_k] never sees them).
    #
    # `enumerate_search` picks the lowest chunk_idx per file rather than a
    # literal chunk_idx == 0, so files whose index-0 chunk was purged as
    # body-less are still represented here.
    if where and diversify:
        pool_files = {r.get("file_path") or "" for r in pool}
        head_rows = enumerate_search(
            where=where, store=store, limit=cfg.final_top_k * 4
        )
        # Order by date desc so the most recent siblings get added first when capped.
        # NaN-safe: date can be NaN (float) when missing; coerce to "" for sort.
        def _sort_key(r):
            d = r.get("date")
            return d if isinstance(d, str) else ""
        head_rows.sort(key=_sort_key, reverse=True)
        for r in head_rows:
            fp = r.get("file_path") or ""
            if fp in pool_files or r["chunk_id"] in rows_by_id:
                continue
            pool.append(r)
            pool_files.add(fp)
            rows_by_id[r["chunk_id"]] = r
            augmented += 1
            if augmented >= cfg.final_top_k:
                break

    # 6. Optional cross-encoder rerank on the fused pool.
    if use_reranker and pool:
        passages = [r.get("text") or "" for r in pool]
        try:
            ranked = reranker.rerank(rewritten, passages, top_k=len(pool))  # type: ignore[union-attr]
            pool = [pool[i] for i, _ in ranked]
        except Exception:
            use_reranker = False

    # 7. Diversify by file_path, then cut to final_top_k.
    if diversify:
        ordered = _diversify_by_file(pool, max_per_file=max_per_file, top_k=cfg.final_top_k)
    else:
        ordered = pool[: cfg.final_top_k]

    distinct_files = len({r.get("file_path") or "" for r in ordered})
    return HybridSearchResult(
        rows=ordered,
        where_clause=where,
        bm25_hit_count=len(bm25_hits),
        vector_hit_count=len(vector_rows),
        fused_count=len(ordered),
        reranked=use_reranker,
        rerank_pool_size=len(pool) if use_reranker else 0,
        diversified=diversify,
        distinct_files=distinct_files,
        augmented_files=augmented,
    )


def _batched(items: list[str], n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]
