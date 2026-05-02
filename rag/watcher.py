"""Live vault watcher: re-index notes as they are saved.

Wraps `watchdog` with a per-path debounce so Obsidian's "save twice in a
row" behavior doesn't trigger duplicate work. New / modified / moved files
are routed through the standard ingest pipeline; deletions are removed
from LanceDB by file_path.

Run:
    uv run python -m rag.watcher

Stop with Ctrl-C. Pair with the Streamlit UI for hands-off indexing.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from threading import Lock, Timer

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from rag.config import Settings, get_settings
from rag.ingest.md_loader import is_excluded
from rag.ingest.pipeline import index_files
from rag.store.lancedb_store import open_store

log = logging.getLogger("rag.watcher")


class VaultEventHandler(FileSystemEventHandler):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.vault_root = settings.vault.resolved_path
        self.exclude_patterns = settings.vault.exclude_patterns
        self.debounce = settings.watcher.debounce_seconds or 2.0
        self._timers: dict[Path, Timer] = {}
        self._timers_lock = Lock()

    # --- Helpers ----------------------------------------------------
    def _is_target(self, path_str: str) -> bool:
        if not path_str.endswith(".md"):
            return False
        try:
            rel = Path(path_str).resolve().relative_to(self.vault_root.resolve())
        except ValueError:
            return False
        return not is_excluded(rel, self.exclude_patterns)

    def _schedule(self, path: Path, action: str) -> None:
        with self._timers_lock:
            existing = self._timers.pop(path, None)
            if existing is not None:
                existing.cancel()
            timer = Timer(self.debounce, self._dispatch, args=[path, action])
            self._timers[path] = timer
            timer.daemon = True
            timer.start()

    def _dispatch(self, path: Path, action: str) -> None:
        with self._timers_lock:
            self._timers.pop(path, None)
        try:
            if action == "delete":
                store = open_store()
                removed = store.delete_by_file(str(path))
                log.info("deleted %s (%s chunks dropped)", path.name, removed)
            else:
                if not path.exists():
                    return
                stats = index_files([path], settings=self.settings, show_progress=False)
                log.info(
                    "indexed %s (%s chunks, %.1fs)",
                    path.name,
                    stats.chunks_total,
                    stats.elapsed_seconds,
                )
        except Exception as e:
            log.exception("watcher error on %s: %r", path, e)

    # --- watchdog hooks --------------------------------------------
    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_target(event.src_path):
            self._schedule(Path(event.src_path), "modify")

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_target(event.src_path):
            self._schedule(Path(event.src_path), "modify")

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_target(event.src_path):
            self._schedule(Path(event.src_path), "delete")

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # Treat moves as delete-old + index-new.
        if self._is_target(event.src_path):
            self._schedule(Path(event.src_path), "delete")
        if self._is_target(event.dest_path):
            self._schedule(Path(event.dest_path), "modify")


def watch(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    vault_root = settings.vault.resolved_path
    if not vault_root.exists():
        log.error("vault not found: %s", vault_root)
        return 1

    handler = VaultEventHandler(settings)
    observer = Observer()
    observer.schedule(handler, str(vault_root), recursive=True)
    observer.daemon = True
    observer.start()

    print(f"[watcher] watching {vault_root}")
    print(f"[watcher] debounce={handler.debounce}s  Ctrl-C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[watcher] stopping...")
    finally:
        observer.stop()
        observer.join()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [watcher] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return watch()


if __name__ == "__main__":
    sys.exit(main())
