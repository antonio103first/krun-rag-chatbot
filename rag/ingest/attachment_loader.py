"""Index PDF attachments referenced by `![[file.pdf]]` embeds in vault notes.

PDFs in the user's vault are typically DD decks, IR decks, term sheets — high
information density. We extract text via PyMuPDF and index each PDF page (or
group of pages, depending on length) as its own chunk with `source=pdf` and
`parent_path=<note that embeds it>`.

Workflow:
1. Walk the vault for .md notes (already indexed).
2. For each note, parse `![[*.pdf]]` embeds via md_loader.embeds.
3. Resolve each embed to a real file (try same dir, then `_attachments/`).
4. Open with PyMuPDF, extract page text, group into chunks.
5. Embed + write to LanceDB with the parent note's metadata + source=pdf.

Run:
    uv sync --extra phase1d            # ensure pymupdf installed
    uv run python -m rag.ingest.attachment_loader --full
    uv run python -m rag.ingest.attachment_loader --paths note.md
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from tqdm import tqdm

from rag.config import Settings, get_settings
from rag.ingest.embedder import Embedder, get_default_embedder
from rag.ingest.md_loader import LoadedNote, list_vault_md, load_note
from rag.ingest.metadata import derive_metadata
from rag.store.lancedb_store import VaultChunkStore, chunk_to_row, open_store


# ~ tokens; we group PDF pages until they reach this size.
_PAGE_GROUP_TOKEN_LIMIT = 800


@dataclass
class AttachmentStats:
    notes_seen: int = 0
    pdfs_seen: int = 0
    pdfs_indexed: int = 0
    pdfs_missing: int = 0
    pdfs_with_errors: int = 0
    chunks_total: int = 0
    elapsed_seconds: float = 0.0
    errors: list[tuple[str, str]] = field(default_factory=list)


# --- PDF resolution ------------------------------------------------------
def _candidate_paths(parent_note: Path, embed_target: str, vault_root: Path) -> list[Path]:
    """Where to look for an embed target. Obsidian resolves these implicitly."""
    target = Path(embed_target)
    candidates = [
        parent_note.parent / target,
        vault_root / target,
        vault_root / "_attachments" / target,
        parent_note.parent / "_attachments" / target,
    ]
    # Strip vault-relative paths if the embed already includes one.
    return [p for p in candidates if p.suffix.lower() == ".pdf"]


def _resolve_pdf(parent_note: Path, embed_target: str, vault_root: Path) -> Path | None:
    for c in _candidate_paths(parent_note, embed_target, vault_root):
        if c.exists():
            return c
    return None


# --- PDF text extraction -------------------------------------------------
def _estimate_tokens(s: str) -> int:
    return max(1, int(max(len(s) / 2.0, len(s.split()))))


def _extract_pdf_text(pdf_path: Path) -> list[tuple[int, str]]:
    """Return [(page_number, text)] using PyMuPDF.

    Falls back gracefully if a page is unreadable. Empty pages are skipped.
    """
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "PyMuPDF (`pymupdf`) is required for PDF attachment indexing. "
            "Install with: uv sync --extra phase1d"
        ) from e

    pages: list[tuple[int, str]] = []
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc, start=1):
            try:
                text = page.get_text("text") or ""
            except Exception:
                text = ""
            text = text.strip()
            if text:
                pages.append((i, text))
    return pages


def _group_pages(pages: list[tuple[int, str]], token_limit: int) -> list[tuple[str, str]]:
    """Group pages into ~token_limit-sized chunks.

    Returns [(page_range_label, combined_text)].
    Single-page chunks are emitted with `p.N`; multi-page with `p.N-M`.
    """
    chunks: list[tuple[str, str]] = []
    cur_pages: list[int] = []
    cur_text_parts: list[str] = []
    cur_tokens = 0

    for page_num, text in pages:
        page_text = f"[p.{page_num}]\n{text}"
        page_tokens = _estimate_tokens(page_text)
        if cur_tokens + page_tokens > token_limit and cur_pages:
            label = f"p.{cur_pages[0]}" if len(cur_pages) == 1 else f"p.{cur_pages[0]}-{cur_pages[-1]}"
            chunks.append((label, "\n\n".join(cur_text_parts)))
            cur_pages, cur_text_parts, cur_tokens = [], [], 0
        cur_pages.append(page_num)
        cur_text_parts.append(page_text)
        cur_tokens += page_tokens

    if cur_pages:
        label = f"p.{cur_pages[0]}" if len(cur_pages) == 1 else f"p.{cur_pages[0]}-{cur_pages[-1]}"
        chunks.append((label, "\n\n".join(cur_text_parts)))

    return chunks


# --- Indexing ------------------------------------------------------------
def index_pdf_attachments(
    notes: list[Path],
    *,
    settings: Settings | None = None,
    store: VaultChunkStore | None = None,
    embedder: Embedder | None = None,
    show_progress: bool = True,
) -> AttachmentStats:
    settings = settings or get_settings()
    store = store or open_store()
    embedder = embedder or get_default_embedder(
        model_name=settings.embedding.model_name,
        device=settings.embedding.device,
        batch_size=settings.embedding.batch_size,
        max_seq_length=settings.embedding.max_seq_length,
    )
    vault_root = settings.vault.resolved_path
    started = time.perf_counter()

    stats = AttachmentStats()
    seen_pdfs: set[Path] = set()
    pending: list[tuple[Path, dict, list[tuple[str, str]]]] = []  # (pdf_path, meta_dict, chunks)

    iterator = tqdm(notes, desc="scanning notes", disable=not show_progress)
    for note_path in iterator:
        stats.notes_seen += 1
        try:
            note: LoadedNote = load_note(note_path, vault_root=vault_root)
        except Exception:
            continue

        for embed in note.embeds:
            if not embed.lower().endswith(".pdf"):
                continue
            pdf_path = _resolve_pdf(note_path, embed, vault_root)
            stats.pdfs_seen += 1
            if pdf_path is None:
                stats.pdfs_missing += 1
                stats.errors.append((str(note_path), f"missing PDF: {embed}"))
                continue
            if pdf_path in seen_pdfs:
                continue
            seen_pdfs.add(pdf_path)

            try:
                pages = _extract_pdf_text(pdf_path)
            except Exception as e:
                stats.pdfs_with_errors += 1
                stats.errors.append((str(pdf_path), repr(e)))
                tqdm.write(f"  [pdf fail] {pdf_path.name}: {e!r}")
                continue
            if not pages:
                continue
            grouped = _group_pages(pages, _PAGE_GROUP_TOKEN_LIMIT)

            # Inherit metadata from the parent note (company / person / date).
            parent_meta = derive_metadata(note).as_lance_row_partial()
            pdf_rel = pdf_path.relative_to(vault_root).as_posix() if pdf_path.is_relative_to(vault_root) else pdf_path.as_posix()
            attach_meta = parent_meta | {
                "file_path": str(pdf_path),
                "vault_relative": pdf_rel,
                "title": pdf_path.stem,
                "doc_type": "attachment",
                "top_folder": parent_meta.get("top_folder"),
            }
            pending.append((pdf_path, attach_meta, grouped))
            stats.pdfs_indexed += 1
            stats.chunks_total += len(grouped)

    if not pending:
        stats.elapsed_seconds = time.perf_counter() - started
        return stats

    # Embed all chunks in one batch.
    flat_texts: list[str] = []
    flat_index: list[tuple[int, int]] = []
    for i, (_pdf, _meta, chunks) in enumerate(pending):
        for j, (_label, text) in enumerate(chunks):
            flat_texts.append(text)
            flat_index.append((i, j))

    if show_progress:
        print(f"\nEmbedding {len(flat_texts)} PDF chunks (batch={embedder.batch_size}, device={embedder.device})...")
    vectors = embedder.encode(flat_texts, show_progress=show_progress)

    # Build rows.
    rows = []
    for vec_idx, (i, j) in enumerate(flat_index):
        pdf_path, meta_dict, chunks = pending[i]
        label, text = chunks[j]
        chunk_id = hashlib.sha256(
            f"{pdf_path}|{label}|{j}|{text[:100]}".encode("utf-8")
        ).hexdigest()
        # Drop stale chunks for this PDF on first chunk only.
        if j == 0:
            try:
                store.delete_by_file(str(pdf_path))
            except Exception:
                pass
        row = chunk_to_row(
            chunk_meta=meta_dict,
            chunk_text=text,
            vector=vectors[vec_idx],
            chunk_id=chunk_id,
            chunk_idx=j,
            header_path=label,  # "p.3" or "p.1-2" — stand-in for breadcrumb
            raw_frontmatter=None,
            source="pdf",
            parent_path=str(_find_parent_note(pdf_path, notes)),
        )
        rows.append(row)

    if rows:
        store.upsert_rows(rows)

    stats.elapsed_seconds = time.perf_counter() - started
    return stats


def _find_parent_note(pdf_path: Path, notes: list[Path]) -> Path:
    """Pick the closest .md note that embeds this PDF (best-effort)."""
    same_dir = [n for n in notes if n.parent == pdf_path.parent]
    if same_dir:
        return same_dir[0]
    return notes[0] if notes else pdf_path


# --- CLI -----------------------------------------------------------------
def _print_stats(stats: AttachmentStats) -> None:
    print("=" * 60)
    print("Attachment ingest summary")
    print("=" * 60)
    print(f"  Notes scanned        : {stats.notes_seen}")
    print(f"  PDF embeds seen      : {stats.pdfs_seen}")
    print(f"  PDFs indexed         : {stats.pdfs_indexed}")
    print(f"  PDFs missing         : {stats.pdfs_missing}")
    print(f"  PDFs with errors     : {stats.pdfs_with_errors}")
    print(f"  Chunks total         : {stats.chunks_total}")
    print(f"  Elapsed              : {stats.elapsed_seconds:.1f}s")
    if stats.errors:
        print("\n  Errors (first 10):")
        for src, err in stats.errors[:10]:
            print(f"    - {Path(src).name}: {err}")
    print("=" * 60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Index PDF attachments embedded in vault notes")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--full", action="store_true", help="Scan every note in the vault for PDF embeds")
    g.add_argument("--paths", nargs="+", help="Scan only these notes")
    parser.add_argument("--no-progress", action="store_true")
    args = parser.parse_args(argv)

    s = get_settings()
    if args.full:
        notes = list_vault_md(
            vault_root=s.vault.resolved_path,
            include_dirs=s.vault.include_dirs,
            exclude_patterns=s.vault.exclude_patterns,
        )
    else:
        notes = [Path(p).resolve() for p in args.paths]

    stats = index_pdf_attachments(notes, show_progress=not args.no_progress)
    _print_stats(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
