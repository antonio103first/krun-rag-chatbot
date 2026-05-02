"""Diagnose 'why isn't X showing up?' — list LanceDB rows for a filter.

When a query produces unexpected results (or none), use this to inspect
what's actually indexed for a given person / company / file. Bypasses
ranking entirely so you can tell whether the issue is retrieval-side
(matching rows exist, ranking buried them) or ingest-side (no matching
rows in the store at all).

Run:
    uv run python scripts/diagnose_search.py --person "정경원 사장"
    uv run python scripts/diagnose_search.py --company Blueward
    uv run python scripts/diagnose_search.py --file 정경원
    uv run python scripts/diagnose_search.py --doc-type meeting --date-from 2026-04-22
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--company", help="Exact company match")
    p.add_argument("--person", help="Exact person match")
    p.add_argument("--file", help="Substring match on file_path")
    p.add_argument("--doc-type", help="Filter by doc_type")
    p.add_argument("--date-from", help="ISO date lower bound (inclusive)")
    p.add_argument("--date-to", help="ISO date upper bound (inclusive)")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--show-text", action="store_true", help="Print first 120 chars of each chunk")
    args = p.parse_args()

    print("=" * 70)
    print("KRUN RAG — Search diagnostic")
    print("=" * 70)

    from rag.config import get_settings
    from rag.store.lancedb_store import open_store

    s = get_settings()
    store = open_store()
    print(f"Vault     : {s.vault.resolved_path}")
    print(f"DB rows   : {store.count():,}")

    df = store.table.to_pandas()
    if df.empty:
        print("[FAIL] LanceDB is empty.")
        return 1

    mask = df["file_path"].notna()
    if args.company:
        mask &= df["company"] == args.company
    if args.person:
        mask &= df["person"] == args.person
    if args.file:
        mask &= df["file_path"].str.contains(args.file, na=False, regex=False)
    if args.doc_type:
        mask &= df["doc_type"] == args.doc_type
    if args.date_from:
        mask &= df["date"].fillna("").astype(str) >= args.date_from
    if args.date_to:
        mask &= df["date"].fillna("").astype(str) <= args.date_to

    matched = df[mask]
    print(f"\nFilter    : {vars(args)}")
    print(f"Matched   : {len(matched):,} chunks")

    if len(matched) == 0:
        print("\n[no matches]")
        # Suggest near-misses for company / person filters.
        if args.company:
            print("\nClosest companies in store:")
            comp_counts = Counter(c for c in df["company"].dropna().tolist() if c)
            for c, n in comp_counts.most_common(15):
                if args.company.lower() in c.lower() or c.lower() in args.company.lower():
                    print(f"  {n:4d}  {c!r}")
        if args.person:
            print("\nClosest persons in store:")
            person_counts = Counter(p for p in df["person"].dropna().tolist() if p)
            for c, n in person_counts.most_common(15):
                if args.person.lower() in c.lower() or c.lower() in args.person.lower():
                    print(f"  {n:4d}  {c!r}")
        return 1

    # Group by file.
    files = matched.groupby("file_path").size().sort_values(ascending=False)
    print(f"\nDistinct files matched : {len(files)}")
    print("\nChunks per file (top {}):".format(min(args.limit, len(files))))
    for fp, count in files.head(args.limit).items():
        print(f"  {count:3d}  {fp}")

    # doc_type distribution within matches.
    print("\ndoc_type distribution:")
    for dt, n in matched["doc_type"].value_counts().head(10).items():
        print(f"  {n:4d}  {dt}")

    if args.show_text:
        print("\nFirst {} chunk previews:".format(min(args.limit, len(matched))))
        for _, r in matched.head(args.limit).iterrows():
            head = r["header_path"] or "(no header)"
            text = (r["text"] or "").replace("\n", " ")[:120]
            print(f"  - [{r['doc_type']}] {Path(r['file_path']).name}  >  {head}")
            print(f"    {text}…")

    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
