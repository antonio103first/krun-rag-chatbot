"""Work through the ingest backlog in small, individually-persisted batches.

`index_files` embeds every pending chunk before writing a single time at the
end, so one long `--incremental` run is all-or-nothing: the 2026-07-19 backlog
(856 files / 11,438 chunks / ~11h on CPU) loses everything if it's interrupted
at hour 10. This script slices the same backlog into batches of `--batch-files`
and calls the normal ingest path once per batch, so each batch is durable as
soon as it finishes.

Resumable by construction: indexed files get a fresh `ingested_at`, so
`detect_vault_changes` stops reporting them and a restart picks up the
remainder. Safe to Ctrl-C between batches and safe to re-run.

    uv run python scripts/catchup_ingest.py                  # whole backlog
    uv run python scripts/catchup_ingest.py --only 03_Companies
    uv run python scripts/catchup_ingest.py --batch-files 25 --max-batches 4

Do not run the API server at the same time — both hold large models and
contend for the same CPU.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from rag.config import get_settings
from rag.ingest.embedder import Embedder
from rag.ingest.pipeline import detect_vault_changes, index_files
from rag.store.lancedb_store import open_store


def _pending(settings, store, only: str | None) -> list[Path]:
    diff = detect_vault_changes(settings=settings, store=store)
    targets = diff.new + diff.changed
    if only:
        targets = [p for p in targets if only in str(p)]
    return targets


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--batch-files",
        type=int,
        default=40,
        help="Files per durable batch (default 40, roughly 25-40 min on CPU).",
    )
    ap.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="Stop after N batches. 0 (default) means run until the backlog is empty.",
    )
    ap.add_argument(
        "--only",
        help="Substring filter on the file path, e.g. 03_Companies. Prioritizes one area.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Report the backlog and exit.")
    args = ap.parse_args()

    settings = get_settings()
    store = open_store()

    targets = _pending(settings, store, args.only)
    print(f"backlog: {len(targets)} files   (store has {store.count()} chunks)")
    if args.dry_run or not targets:
        return 0

    # One embedder across all batches — reloading BGE-M3 per batch costs ~10s each.
    embedder = Embedder(
        model_name=settings.embedding.model_name,
        device=settings.embedding.device,
        batch_size=settings.embedding.batch_size,
    )

    total = len(targets)
    done = 0
    batch_no = 0
    started = time.perf_counter()

    while done < total:
        if args.max_batches and batch_no >= args.max_batches:
            print(f"\nstopping after {batch_no} batches as requested; {total - done} files left")
            break
        batch = targets[done : done + args.batch_files]
        batch_no += 1
        print(f"\n=== batch {batch_no}: files {done + 1}-{done + len(batch)} of {total} ===")
        t0 = time.perf_counter()
        try:
            stats = index_files(
                batch, settings=settings, store=store, embedder=embedder, show_progress=True
            )
        except KeyboardInterrupt:
            print(f"\ninterrupted; {done} files persisted, {total - done} left. Re-run to resume.")
            return 130
        done += len(batch)
        elapsed = time.perf_counter() - t0
        rate = done / max(time.perf_counter() - started, 1e-9)
        eta_min = (total - done) / rate / 60 if rate > 0 else 0
        print(
            f"  batch done in {elapsed / 60:.1f}m "
            f"(chunks={stats.chunks_total}) | "
            f"{done}/{total} files | ETA {eta_min:.0f}m | store={store.count()} chunks"
        )

    print(f"\ntotal elapsed {(time.perf_counter() - started) / 60:.1f}m; store={store.count()} chunks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
