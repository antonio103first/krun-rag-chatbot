"""BGE-M3 embedding wrapper.

We load the model once and reuse it. Embeddings are L2-normalized so cosine
similarity reduces to a dot product (LanceDB defaults).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import numpy as np


class Embedder:
    """Lazy-loaded BGE-M3 wrapper."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        device: str = "auto",
        batch_size: int = 32,
        max_seq_length: int = 8192,
    ) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self.max_seq_length = max_seq_length
        self._device = device
        self._model: Any = None

    def _resolve_device(self) -> str:
        if self._device != "auto":
            return self._device
        try:
            import torch
            return "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"

    def load(self) -> None:
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer

        device = self._resolve_device()
        self._model = SentenceTransformer(self.model_name, device=device)
        # SentenceTransformer respects this attr at encode time.
        if hasattr(self._model, "max_seq_length"):
            self._model.max_seq_length = self.max_seq_length

    @property
    def device(self) -> str:
        return self._resolve_device()

    @property
    def dimension(self) -> int:
        return 1024  # BGE-M3 fixed

    def encode(self, texts: list[str], show_progress: bool = False) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        self.load()
        vecs = self._model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=show_progress,
            convert_to_numpy=True,
        )
        return vecs.astype(np.float32, copy=False)

    def encode_iter(
        self,
        texts: Iterable[str],
        show_progress: bool = False,
    ) -> np.ndarray:
        return self.encode(list(texts), show_progress=show_progress)


_DEFAULT: Embedder | None = None


def get_default_embedder(
    model_name: str = "BAAI/bge-m3",
    device: str = "auto",
    batch_size: int = 32,
    max_seq_length: int = 8192,
) -> Embedder:
    """Return a process-wide default Embedder, building it on first call."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Embedder(
            model_name=model_name,
            device=device,
            batch_size=batch_size,
            max_seq_length=max_seq_length,
        )
    return _DEFAULT
