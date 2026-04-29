"""Phase 1A Gate verification.

Runs after `python -m rag.ingest.pipeline --full` has populated LanceDB.

Checks:
1. The vault_chunks table exists and has > 0 rows.
2. doc_type counts look reasonable (companies + persons + meetings + ...).
3. Metadata-filter queries by `company` return matching chunks.
4. A vector search runs end-to-end and returns top-K.

Run:
    uv run python scripts/verify_phase1a.py
    uv run python scripts/verify_phase1a.py --company Blueward
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--company",
        default=None,
        help="Company name to test metadata filter (defaults to the most chunked company)",
    )
    parser.add_argument(
        "--query",
        default="투자 검토 핵심 리스크",
        help="Korean query for vector search smoke test",
    )
    args = parser.parse_args(argv)

    print("=" * 60)
    print("KRUN RAG — Phase 1A Gate verification")
    print("=" * 60)

    from rag.config import get_settings
    from rag.ingest.embedder import get_default_embedder
    from rag.store.lancedb_store import open_store

    s = get_settings()
    store = open_store()
    total = store.count()
    print(f"\n[1] LanceDB rows: {total}")
    if total == 0:
        print("[FAIL] Table is empty. Run:")
        print("    uv run python -m rag.ingest.pipeline --full")
        return 1

    # --- doc_type breakdown -------------------------------------------
    df = store.table.to_pandas(columns=["doc_type", "company", "person", "top_folder"])
    counts: Counter[str] = Counter(df["doc_type"].fillna("").tolist())
    print("\n[2] doc_type distribution:")
    for k, v in counts.most_common():
        print(f"    {k:12s} {v}")

    folder_counts: Counter[str] = Counter(df["top_folder"].fillna("").tolist())
    print("\n    top_folder distribution:")
    for k, v in folder_counts.most_common():
        print(f"    {k:14s} {v}")

    # --- company filter ------------------------------------------------
    company_counts = Counter(c for c in df["company"].fillna("").tolist() if c)
    if not company_counts:
        print("\n[FAIL] No `company` values found in any row. metadata.py is not extracting companies.")
        return 1

    target_company = args.company or company_counts.most_common(1)[0][0]
    print(f"\n[3] company filter test → '{target_company}'")
    rows = store.search_by_company(target_company, limit=5)
    if not rows:
        print(f"    [FAIL] search_by_company returned 0 rows for '{target_company}'")
        return 1
    print(f"    returned {len(rows)} chunks. samples:")
    for r in rows[:3]:
        snippet = (r.get("text") or "").replace("\n", " ")[:100]
        print(f"      - {r.get('vault_relative')}  [{r.get('doc_type')}]  {snippet}…")

    # --- vector search smoke ------------------------------------------
    print(f"\n[4] vector search smoke test → '{args.query}'")
    embedder = get_default_embedder(
        model_name=s.embedding.model_name,
        device=s.embedding.device,
        batch_size=s.embedding.batch_size,
    )
    vec = embedder.encode([args.query])[0]
    hits = store.vector_search(vec, limit=5)
    if not hits:
        print("    [FAIL] vector search returned 0 hits")
        return 1
    print(f"    top {len(hits)} hits:")
    for r in hits[:5]:
        snippet = (r.get("text") or "").replace("\n", " ")[:100]
        print(
            f"      - [{r.get('doc_type'):8s}] {r.get('vault_relative')}  "
            f"({r.get('company') or r.get('person') or '-'})  {snippet}…"
        )

    # --- Gate ----------------------------------------------------------
    print("\n" + "=" * 60)
    print("[PASS] Phase 1A Gate: metadata filter + vector search both work")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
