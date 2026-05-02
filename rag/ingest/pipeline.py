"""Indexing pipeline: vault -> chunks -> embeddings -> LanceDB.

CLI:
    uv run python -m rag.ingest.pipeline --full
    uv run python -m rag.ingest.pipeline --paths path/to/note.md
    uv run python -m rag.ingest.pipeline --dry-run
    uv run python -m rag.ingest.pipeline --refresh-metadata
    uv run python -m rag.ingest.pipeline --incremental
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
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


def refresh_metadata(
    *,
    settings: Settings | None = None,
    store: VaultChunkStore | None = None,
    show_progress: bool = True,
) -> IngestStats:
    """Re-derive metadata for every existing row without re-embedding.

    Useful when only the metadata extraction logic (`metadata.py`) has
    changed: we keep the stored chunk text + vector untouched and rewrite
    the structured fields (doc_type, company, person, date, …).
    """
    settings = settings or get_settings()
    store = store or open_store()
    vault_root = settings.vault.resolved_path

    df = store.table.to_pandas()
    stats = IngestStats()
    started = time.perf_counter()

    if df.empty:
        stats.elapsed_seconds = time.perf_counter() - started
        return stats

    file_paths = df["file_path"].dropna().unique().tolist()
    iterator = tqdm(file_paths, desc="refreshing", disable=not show_progress)
    new_rows: list[dict] = []

    for fp in iterator:
        path = Path(fp)
        stats.files_seen += 1
        if not path.exists():
            stats.files_skipped += 1
            tqdm.write(f"  [missing] {fp}")
            continue
        try:
            note = load_note(path, vault_root=vault_root)
            meta = derive_metadata(note)
        except Exception as e:
            stats.files_with_errors += 1
            tqdm.write(f"  [refresh fail] {path.name}: {e!r}")
            continue

        meta_dict = meta.as_lance_row_partial()
        file_chunks = df[df["file_path"] == fp]
        for _, row in file_chunks.iterrows():
            vec = row["vector"]
            if isinstance(vec, np.ndarray):
                vec_list = vec.tolist()
            else:
                vec_list = list(vec)

            chunk_idx_raw = row["chunk_idx"]
            chunk_idx = int(chunk_idx_raw.item()) if hasattr(chunk_idx_raw, "item") else int(chunk_idx_raw)

            # pandas NaN is truthy in Python, so `row.get(x) or default` lets
            # NaN through and breaks pyarrow ("Expected bytes, got float"). Use
            # an explicit NaN check before string coercion.
            def _nan_safe(v, default=None):
                if v is None:
                    return default
                if isinstance(v, float) and v != v:  # NaN
                    return default
                return v

            new_row = chunk_to_row(
                chunk_meta=meta_dict,
                chunk_text=_nan_safe(row.get("text"), default="") or "",
                vector=vec_list,
                chunk_id=row["chunk_id"],
                chunk_idx=chunk_idx,
                header_path=_nan_safe(row.get("header_path"), default="") or "",
                raw_frontmatter=meta.raw_frontmatter,
                source=_nan_safe(row.get("source"), default="md") or "md",
                parent_path=_nan_safe(row.get("parent_path")),
            )
            new_rows.append(new_row)
            stats.chunks_total += 1
            stats.chunks_per_doc_type[meta_dict["doc_type"]] += 1

        stats.files_indexed += 1

    if new_rows:
        store.upsert_rows(new_rows)

    stats.elapsed_seconds = time.perf_counter() - started
    return stats


# --- Incremental change detection ----------------------------------------
@dataclass
class VaultDiff:
    new: list[Path] = field(default_factory=list)
    changed: list[Path] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)  # absolute file_path strings


def _parse_ingested_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # `datetime.utcnow().isoformat()` produced naive UTC strings.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def detect_vault_changes(
    settings: Settings | None = None,
    store: VaultChunkStore | None = None,
) -> VaultDiff:
    """Diff the vault against LanceDB to find what's new / changed / deleted.

    A file is **new** if it's in the vault but not in any existing row.
    **changed** if the on-disk mtime is newer than the most-recent
    `ingested_at` of any of its chunks. **deleted** if it has rows in
    LanceDB but no longer exists on disk.
    """
    settings = settings or get_settings()
    store = store or open_store()
    vault_root = settings.vault.resolved_path

    vault_paths = list_vault_md(
        vault_root=vault_root,
        include_dirs=settings.vault.include_dirs,
        exclude_patterns=settings.vault.exclude_patterns,
    )
    vault_set = {str(p): p for p in vault_paths}

    df = store.table.to_pandas()
    diff = VaultDiff()

    if df.empty:
        diff.new = list(vault_set.values())
        return diff

    # Most-recent ingested_at per file.
    last_seen: dict[str, str] = (
        df.groupby("file_path")["ingested_at"].max().to_dict()
    )

    db_set = set(last_seen.keys())
    for fp_str, p in vault_set.items():
        if fp_str not in db_set:
            diff.new.append(p)
            continue
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        ingested = _parse_ingested_at(last_seen[fp_str])
        if ingested is None or mtime > ingested:
            diff.changed.append(p)

    for fp_str in db_set:
        if fp_str not in vault_set:
            diff.deleted.append(fp_str)

    return diff


def index_incremental(
    *,
    settings: Settings | None = None,
    store: VaultChunkStore | None = None,
    embedder: Embedder | None = None,
    show_progress: bool = True,
    dry_run: bool = False,
) -> tuple[IngestStats, VaultDiff]:
    """Detect new/changed/deleted files and reindex only what's needed.

    Returns the ingest stats plus the diff (so callers know what was deleted).
    """
    settings = settings or get_settings()
    store = store or open_store()

    diff = detect_vault_changes(settings=settings, store=store)
    targets = diff.new + diff.changed

    if dry_run:
        stats = IngestStats()
        stats.files_seen = len(targets)
        stats.files_indexed = len(targets)
        return stats, diff

    # Delete vanished files first so their stale chunks don't show in search.
    for fp in diff.deleted:
        try:
            store.delete_by_file(fp)
        except Exception as e:
            tqdm.write(f"  [delete fail] {fp}: {e!r}")

    if not targets:
        stats = IngestStats()
        stats.elapsed_seconds = 0.0
        return stats, diff

    stats = index_files(
        targets,
        settings=settings,
        store=store,
        embedder=embedder,
        show_progress=show_progress,
        dry_run=False,
    )
    return stats, diff


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
    g.add_argument(
        "--refresh-metadata",
        action="store_true",
        help="Re-derive metadata for existing rows without re-embedding (fast)",
    )
    g.add_argument(
        "--incremental",
        action="store_true",
        help="Detect new + changed + deleted files since last ingest and reindex only those",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load + chunk + count, but do not embed or write to LanceDB",
    )
    parser.add_argument("--no-progress", action="store_true")
    args = parser.parse_args(argv)

    if args.full:
        stats = index_full_vault(show_progress=not args.no_progress, dry_run=args.dry_run)
    elif args.refresh_metadata:
        stats = refresh_metadata(show_progress=not args.no_progress)
    elif args.incremental:
        stats, diff = index_incremental(
            show_progress=not args.no_progress, dry_run=args.dry_run
        )
        print(
            f"\n[diff] new={len(diff.new)}  changed={len(diff.changed)}  "
            f"deleted={len(diff.deleted)}"
        )
        if diff.new:
            print("  new files:")
            for p in diff.new[:10]:
                print(f"    + {p.name}")
            if len(diff.new) > 10:
                print(f"    + … ({len(diff.new) - 10} more)")
        if diff.changed:
            print("  changed files:")
            for p in diff.changed[:10]:
                print(f"    ~ {p.name}")
            if len(diff.changed) > 10:
                print(f"    ~ … ({len(diff.changed) - 10} more)")
        if diff.deleted:
            print("  deleted files (chunks dropped):")
            for fp in diff.deleted[:10]:
                print(f"    - {Path(fp).name}")
            if len(diff.deleted) > 10:
                print(f"    - … ({len(diff.deleted) - 10} more)")
    else:
        paths = [Path(p).resolve() for p in args.paths]
        stats = index_files(paths, show_progress=not args.no_progress, dry_run=args.dry_run)

    _print_stats(stats, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
