"""Indexing pipeline: vault -> chunks -> embeddings -> LanceDB.

CLI:
    uv run python -m rag.ingest.pipeline --full
    uv run python -m rag.ingest.pipeline --paths path/to/note.md
    uv run python -m rag.ingest.pipeline --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from rag.config import Settings, get_settings
from rag.ingest.chunker import Chunk, chunk_note
from rag.ingest.embedder import Embedder, get_default_embedder
from rag.ingest.md_loader import LoadedNote, list_vault_md, load_note
from rag.ingest.metadata import derive_metadata
from rag.store.lancedb_store import VaultChunkStore, chunk_to_row, open_store


@dataclass
class IngestStats:
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_with_errors: int = 0
    chunks_total: int = 0
    chunks_per_doc_type: Counter[str] = None  # type: ignore[assignment]
    elapsed_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.chunks_per_doc_type is None:
            self.chunks_per_doc_type = Counter()


def _build_chunks_for_note(
    note: LoadedNote,
    settings: Settings,
) -> tuple[list[Chunk], dict]:
    meta = derive_metadata(note)
    chunks = chunk_note(
        file_path=str(note.path),
        body=note.body,
        max_tokens=settings.chunking.max_tokens,
        overlap_tokens=settings.chunking.overlap_tokens,
        min_tokens=settings.chunking.min_tokens,
        preserve_breadcrumb=settings.chunking.preserve_breadcrumb,
    )
    return chunks, meta.as_lance_row_partial() | {
        "raw_frontmatter": meta.raw_frontmatter,
    }


def index_files(
    paths: list[Path],
    *,
    settings: Settings | None = None,
    store: VaultChunkStore | None = None,
    embedder: Embedder | None = None,
    show_progress: bool = True,
    dry_run: bool = False,
) -> IngestStats:
    """Run the full ingest path for the given .md files."""
    settings = settings or get_settings()
    vault_root = settings.vault.resolved_path
    embedder = embedder or get_default_embedder(
        model_name=settings.embedding.model_name,
        device=settings.embedding.device,
        batch_size=settings.embedding.batch_size,
        max_seq_length=settings.embedding.max_seq_length,
    )

    stats = IngestStats()
    started = time.perf_counter()

    # ---- 1. Load + chunk ------------------------------------------------
    pending: list[tuple[LoadedNote, list[Chunk], dict]] = []
    iterator = tqdm(paths, desc="loading", disable=not show_progress)
    for path in iterator:
        stats.files_seen += 1
        try:
            note = load_note(path, vault_root=vault_root)
        except Exception as e:
            stats.files_with_errors += 1
            tqdm.write(f"  [load fail] {path.name}: {e!r}")
            continue
        if note.parse_error:
            tqdm.write(f"  [parse warn] {note.relative_path}: {note.parse_error}")

        chunks, chunk_meta = _build_chunks_for_note(note, settings)
        if not chunks:
            stats.files_skipped += 1
            continue
        pending.append((note, chunks, chunk_meta))
        stats.files_indexed += 1
        stats.chunks_total += len(chunks)
        stats.chunks_per_doc_type[chunk_meta["doc_type"]] += len(chunks)

    if dry_run:
        stats.elapsed_seconds = time.perf_counter() - started
        return stats

    # ---- 2. Embed (batched across all pending chunks) ------------------
    all_texts: list[str] = []
    flat_index: list[tuple[int, int]] = []  # (note_idx, chunk_idx within note)
    for note_idx, (_note, chunks, _meta) in enumerate(pending):
        for j, ch in enumerate(chunks):
            all_texts.append(ch.text)
            flat_index.append((note_idx, j))

    if not all_texts:
        stats.elapsed_seconds = time.perf_counter() - started
        return stats

    embedder.load()
    if show_progress:
        print(f"\nEmbedding {len(all_texts)} chunks (batch={embedder.batch_size}, device={embedder.device})...")
    vectors = embedder.encode(all_texts, show_progress=show_progress)

    # ---- 3. Build rows + write -----------------------------------------
    store = store or open_store()
    # Drop any prior chunks for these files first (handles file shrinking).
    for note, _chunks, _meta in pending:
        try:
            store.delete_by_file(str(note.path))
        except Exception:
            pass  # table may be empty; harmless on first run

    rows = []
    for vec_idx, (note_idx, j) in enumerate(flat_index):
        note, chunks, meta = pending[note_idx]
        ch = chunks[j]
        row = chunk_to_row(
            chunk_meta=meta,
            chunk_text=ch.text,
            vector=vectors[vec_idx],
            chunk_id=ch.chunk_id,
            chunk_idx=ch.chunk_idx,
            header_path=ch.header_path,
            raw_frontmatter=meta.get("raw_frontmatter"),
            source="md",
        )
        rows.append(row)

    if rows:
        store.upsert_rows(rows)

    stats.elapsed_seconds = time.perf_counter() - started
    return stats


def index_full_vault(
    *,
    settings: Settings | None = None,
    show_progress: bool = True,
    dry_run: bool = False,
) -> IngestStats:
    settings = settings or get_settings()
    vault_root = settings.vault.resolved_path
    if not vault_root.exists():
        raise FileNotFoundError(f"Vault path not found: {vault_root}")

    paths = list_vault_md(
        vault_root=vault_root,
        include_dirs=settings.vault.include_dirs,
        exclude_patterns=settings.vault.exclude_patterns,
    )
    return index_files(paths, settings=settings, show_progress=show_progress, dry_run=dry_run)


def _print_stats(stats: IngestStats, dry_run: bool = False) -> None:
    print("=" * 60)
    print("Ingest summary" + (" (DRY RUN)" if dry_run else ""))
    print("=" * 60)
    print(f"  Files seen           : {stats.files_seen}")
    print(f"  Files indexed        : {stats.files_indexed}")
    print(f"  Files skipped (empty): {stats.files_skipped}")
    print(f"  Files with errors    : {stats.files_with_errors}")
    print(f"  Chunks total         : {stats.chunks_total}")
    if stats.chunks_per_doc_type:
        print("  Chunks per doc_type  :")
        for k, v in sorted(stats.chunks_per_doc_type.items(), key=lambda kv: -kv[1]):
            print(f"    {k:12s} {v}")
    print(f"  Elapsed              : {stats.elapsed_seconds:.1f}s")
    print("=" * 60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KRUN RAG ingest pipeline")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--full", action="store_true", help="Reindex the entire vault")
    g.add_argument("--paths", nargs="+", help="Index only these .md files (absolute or relative to cwd)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load + chunk + count, but do not embed or write to LanceDB",
    )
    parser.add_argument("--no-progress", action="store_true")
    args = parser.parse_args(argv)

    if args.full:
        stats = index_full_vault(show_progress=not args.no_progress, dry_run=args.dry_run)
    else:
        paths = [Path(p).resolve() for p in args.paths]
        stats = index_files(paths, show_progress=not args.no_progress, dry_run=args.dry_run)

    _print_stats(stats, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
