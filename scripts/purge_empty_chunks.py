"""Delete body-less chunks left in LanceDB by the pre-2026-07-20 chunker.

The chunker used to emit a chunk for every heading, including template sections
the author never filled in ("## 주요 논의" with nothing under it). Those chunks
carry only the breadcrumb, so they still match queries lexically and
semantically through the heading text — and then occupy top-K slots that real
content should have. On the 2026-07-20 index that was 2,032 chunks, 16% of the
table.

`chunker.chunk_note` no longer produces them, but rows already written stay
until their file is reindexed. Reindexing the whole vault costs hours of CPU
embedding; deleting the rows is exact and takes seconds, so do that instead.

    uv run python scripts/purge_empty_chunks.py --dry-run
    uv run python scripts/purge_empty_chunks.py
"""

from __future__ import annotations

import argparse
import re

from rag.store.lancedb_store import open_store

# The chunker prefixes "[breadcrumb]\n" to each chunk; strip it to see the body.
_BREADCRUMB = re.compile(r"^\[[^\]]*\]\s*")


def body_of(text: object) -> str:
    return _BREADCRUMB.sub("", str(text or "")).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--max-body-chars",
        type=int,
        default=0,
        help=(
            "Delete chunks whose body is this short or shorter. Default 0 "
            "(strictly empty). Raising this discards real, if terse, content — "
            "inspect with --dry-run before going above 0."
        ),
    )
    ap.add_argument("--dry-run", action="store_true", help="Report only; delete nothing.")
    args = ap.parse_args()

    store = open_store()
    df = store.table.to_pandas()
    before = len(df)
    if before == 0:
        print("index is empty")
        return 0

    df["_body"] = df["text"].map(body_of).map(len)
    doomed = df[df["_body"] <= args.max_body_chars]
    print(f"index rows: {before}")
    print(f"body <= {args.max_body_chars} chars: {len(doomed)} ({len(doomed)/before*100:.1f}%)")

    if len(doomed) == 0:
        return 0

    sample = doomed[["file_path", "header_path"]].head(5)
    print("\nsample:")
    for _, r in sample.iterrows():
        print(f"  {str(r['file_path']).split(chr(92))[-1][:44]} | {r['header_path']}")

    if args.dry_run:
        print("\n--dry-run: nothing deleted")
        return 0

    ids = [str(c) for c in doomed["chunk_id"].tolist() if c]
    deleted = 0
    # Batch: a single IN (...) with thousands of literals overruns the filter parser.
    for i in range(0, len(ids), 400):
        batch = ids[i : i + 400]
        joined = ", ".join("'" + c.replace("'", "''") + "'" for c in batch)
        try:
            store.table.delete(f"chunk_id IN ({joined})")
            deleted += len(batch)
        except Exception as e:  # surface, don't swallow — a partial purge matters
            print(f"  [delete fail] batch at {i}: {e!r}")

    after = store.count()
    print(f"\ndeleted {deleted} rows; index {before} -> {after}")

    # Purging leaves gaps in chunk_idx, and when the removed chunk was a file's
    # first one (an H1 heading with no body — common) the file no longer has a
    # chunk_idx == 0 row. Consumers that pick one representative row per file
    # must therefore select the file's *lowest* chunk_idx, not literal 0; see
    # `_head_rows_per_file` in rag/retrieval/hybrid_search.py.
    #
    # Do NOT try to renumber the rows to close the gaps. An earlier version of
    # this script did (delete_by_file + upsert_rows per file); the upsert threw
    # ArrowInvalid on files whose all-NaN string columns pandas had typed as
    # float64, and because the delete had already committed, 118 files were
    # destroyed and had to be re-embedded. The read path is the safe place to
    # absorb the gaps.
    missing_head = 0
    df2 = store.table.to_pandas()
    if len(df2):
        for _, grp in df2.groupby("file_path"):
            if 0 not in set(int(i) for i in grp["chunk_idx"].tolist()):
                missing_head += 1
    if missing_head:
        print(f"files without a chunk_idx==0 row: {missing_head} (handled at read time)")

    print("Rebuild the BM25 sidecar so it stops ranking the removed ids:")
    print("  uv run python -m rag.ingest.pipeline --refresh-metadata")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
