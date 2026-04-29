"""Cross-encoder reranker on top of hybrid retrieval.

We score (query, chunk) pairs with `BAAI/bge-reranker-v2-m3` and reorder the
top-K hybrid hits by that score before sending to the generator. The reranker
is optional and gated by `retrieval.reranker_enabled` in config.

Install: `uv sync --extra rerank` (pulls FlagEmbedding ~600MB on first run).

Cost: ~150-300ms per query on CPU for ~30 candidates. Recall@8 lift in our
baseline is ~0.10-0.15.
"""

from __future__ import annotations

from typing import Any

from rag.config import get_settings


class Reranker:
    """Lazy-loaded BGE-reranker-v2-m3 cross-encoder wrapper."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        device: str = "auto",
        use_fp16: bool | None = None,
        max_length: int = 1024,
    ) -> None:
        self.model_name = model_name
        self._device = device
        # fp16 is only safe on GPU; CPU path forces float32.
        self._use_fp16 = use_fp16
        self.max_length = max_length
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
        try:
            from FlagEmbedding import FlagReranker  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "FlagEmbedding is required for the reranker. "
                "Install with: uv sync --extra rerank"
            ) from e

        device = self._resolve_device()
        use_fp16 = self._use_fp16 if self._use_fp16 is not None else (device != "cpu")
        self._model = FlagReranker(self.model_name, use_fp16=use_fp16)

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def rerank(
        self,
        query: str,
        passages: list[str],
        top_k: int | None = None,
    ) -> list[tuple[int, float]]:
        """Score each (query, passage) pair, return ranked (orig_index, score).

        Scores are normalized to [0, 1] via the sigmoid built into FlagReranker.
        """
        if not passages:
            return []
        self.load()
        truncated = [p[: self.max_length * 4] for p in passages]  # ~chars-per-token cap
        pairs = [[query, p] for p in truncated]
        scores = self._model.compute_score(pairs, normalize=True)
        if not isinstance(scores, list):
            scores = [scores]
        ranked = sorted(enumerate(scores), key=lambda kv: -kv[1])
        if top_k is not None:
            ranked = ranked[:top_k]
        return [(int(i), float(s)) for i, s in ranked]


_DEFAULT: Reranker | None = None


def get_default_reranker() -> Reranker:
    """Process-wide singleton, built on first call."""
    global _DEFAULT
    if _DEFAULT is None:
        s = get_settings()
        _DEFAULT = Reranker(
            model_name=s.retrieval.reranker_model,
            device=s.embedding.device,
        )
    return _DEFAULT
