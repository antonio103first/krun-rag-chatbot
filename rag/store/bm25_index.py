"""BM25 sidecar index for the vault chunks.

Why a separate index when LanceDB has FTS?
- BM25 with character n-gram tokenization handles Korean morphological
  variation gracefully without an external morphological analyzer.
- It runs in-memory in milliseconds for our ~5K-chunk corpus.
- It is decoupled from the vector store so we can swap tokenizers (kiwipiepy,
  lancedb_fts) without touching the rest of the pipeline.

The index is persisted to `data/bm25_index.pkl` and rebuilt automatically when
the LanceDB row count changes (file added/removed/re-ingested).
"""

from __future__ import annotations

import pickle
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from rank_bm25 import BM25Okapi

from rag.config import get_settings


# --- Tokenizers ------------------------------------------------------------
_NON_TOKEN_CHARS = re.compile(r"[^0-9A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ]+")


def _normalize(text: str) -> str:
    """Lowercase + collapse non-tokenizable runs to a single space."""
    text = text.lower()
    return _NON_TOKEN_CHARS.sub(" ", text).strip()


def char_trigram_tokenize(text: str) -> list[str]:
    """Sliding 3-character windows over alphanumerics + Korean.

    For Korean: '위밋모빌리티' -> ['위밋모', '밋모빌', '모빌리', '빌리티']
    For mixed:  'Blueward 1차DD' -> ['blu', 'lue', 'uew', ..., '1차d', '차dd']

    Tokens within a "word" only; we don't bridge across whitespace because
    that creates noisy n-grams between unrelated words.
    """
    if not text:
        return []
    normalized = _normalize(text)
    tokens: list[str] = []
    for word in normalized.split():
        if len(word) < 3:
            tokens.append(word)
        else:
            tokens.extend(word[i : i + 3] for i in range(len(word) - 2))
    return tokens


def kiwipiepy_tokenize(text: str) -> list[str]:
    """Korean morphological tokenizer (lazy import — heavy)."""
    from kiwipiepy import Kiwi  # type: ignore[import-not-found]

    kiwi = _get_kiwi()
    return [
        t.form.lower()
        for t in kiwi.tokenize(text)
        if t.tag.startswith(("N", "V", "M", "SL", "SN")) and len(t.form) >= 1
    ]


_KIWI: Any = None


def _get_kiwi() -> Any:
    global _KIWI
    if _KIWI is None:
        from kiwipiepy import Kiwi  # type: ignore[import-not-found]

        _KIWI = Kiwi()
    return _KIWI


TOKENIZERS = {
    "char_trigram": char_trigram_tokenize,
    "kiwipiepy": kiwipiepy_tokenize,
}


# --- Index data ------------------------------------------------------------
@dataclass
class _BM25Snapshot:
    chunk_ids: list[str]
    tokenizer_name: str
    bm25: BM25Okapi
    n_rows: int
    built_at: float


# --- Public class ----------------------------------------------------------
class BM25Index:
    """Char-n-gram BM25 over LanceDB chunk text."""

    def __init__(
        self,
        tokenizer: str = "char_trigram",
        k1: float = 1.5,
        b: float = 0.75,
        cache_path: Path | None = None,
    ) -> None:
        if tokenizer not in TOKENIZERS:
            raise ValueError(f"Unknown tokenizer: {tokenizer}")
        self.tokenizer_name = tokenizer
        self._tokenize = TOKENIZERS[tokenizer]
        self.k1 = k1
        self.b = b
        self.cache_path = cache_path
        self._snap: _BM25Snapshot | None = None

    # --- Build ---------------------------------------------------------
    def build(self, chunks: Iterable[dict[str, Any]]) -> None:
        chunks = list(chunks)
        chunk_ids = [c["chunk_id"] for c in chunks]
        tokenized = [self._tokenize(c.get("text") or "") for c in chunks]
        # rank_bm25 errors on empty corpora; pad with a dummy.
        if not tokenized:
            tokenized = [["__empty__"]]
            chunk_ids = ["__empty__"]
        bm25 = BM25Okapi(tokenized, k1=self.k1, b=self.b)
        self._snap = _BM25Snapshot(
            chunk_ids=chunk_ids,
            tokenizer_name=self.tokenizer_name,
            bm25=bm25,
            n_rows=len(chunk_ids),
            built_at=time.time(),
        )

    def build_from_store(self, store: Any) -> None:
        df = store.table.to_pandas()
        if df.empty:
            self.build([])
            return
        rows = df[["chunk_id", "text"]].to_dict(orient="records")
        self.build(rows)

    # --- Persistence ---------------------------------------------------
    def save(self, path: Path | None = None) -> None:
        path = path or self.cache_path
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            pickle.dump({"snap": self._snap, "k1": self.k1, "b": self.b}, f)

    def load(self, path: Path | None = None) -> bool:
        path = path or self.cache_path
        if not path or not path.exists():
            return False
        try:
            with path.open("rb") as f:
                data = pickle.load(f)
            snap = data["snap"]
            if not isinstance(snap, _BM25Snapshot):
                return False
            if snap.tokenizer_name != self.tokenizer_name:
                return False
            self._snap = snap
            self.k1 = data.get("k1", self.k1)
            self.b = data.get("b", self.b)
            return True
        except Exception:
            return False

    # --- Search --------------------------------------------------------
    @property
    def is_ready(self) -> bool:
        return self._snap is not None

    @property
    def row_count(self) -> int:
        return self._snap.n_rows if self._snap else 0

    def search(self, query: str, top_k: int = 30) -> list[tuple[str, float]]:
        if not self.is_ready or not query.strip():
            return []
        q_tokens = self._tokenize(query)
        if not q_tokens:
            return []
        scores = self._snap.bm25.get_scores(q_tokens)
        if not isinstance(scores, np.ndarray):
            scores = np.asarray(scores)
        if scores.size == 0:
            return []
        # argpartition is faster than full sort for large N.
        k = min(top_k, scores.size)
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx])]
        return [
            (self._snap.chunk_ids[i], float(scores[i]))
            for i in idx
            if scores[i] > 0
        ]


# --- Convenience: configured index, with auto-rebuild --------------------
def open_bm25_index(store: Any | None = None, force_rebuild: bool = False) -> BM25Index:
    """Return a BM25Index built from LanceDB, using the disk cache when fresh."""
    s = get_settings()
    cache_path = s.storage.resolved_path / "bm25_index.pkl"
    idx = BM25Index(
        tokenizer=s.bm25.tokenizer,
        k1=s.bm25.k1,
        b=s.bm25.b,
        cache_path=cache_path,
    )

    if not force_rebuild and idx.load():
        if store is None:
            return idx
        live = int(store.count())
        if idx.row_count == live:
            return idx
        # Stale; fall through to rebuild.

    if store is None:
        from rag.store.lancedb_store import open_store

        store = open_store()
    idx.build_from_store(store)
    idx.save()
    return idx
