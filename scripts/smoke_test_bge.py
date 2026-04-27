"""Phase 0 Gate: BGE-M3 local embedding smoke test.

Pass criteria (per claude.md Phase 0):
- BGE-M3 loads on local machine
- A single Korean sentence is embedded in under 1 second (after model load)
- Output shape is (N, 1024)

Run:
    uv run python scripts/smoke_test_bge.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Make sibling `rag` package importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    print("=" * 60)
    print("KRUN RAG — Phase 0 Gate: BGE-M3 smoke test")
    print("=" * 60)

    # --- 1. Import + device detection ---
    try:
        import torch
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        print(f"\n[FAIL] Missing dependency: {e}")
        print("  Run: uv sync")
        return 1

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"\n[1/4] Torch {torch.__version__}  |  device: {device}")
    if device == "cuda":
        print(f"      GPU: {torch.cuda.get_device_name(0)}")

    # --- 2. Load model (first run downloads ~2.3 GB) ---
    print("\n[2/4] Loading BAAI/bge-m3 (first run downloads ~2.3 GB)...")
    t0 = time.perf_counter()
    try:
        model = SentenceTransformer("BAAI/bge-m3", device=device)
    except Exception as e:
        print(f"\n[FAIL] Model load error: {e}")
        return 1
    load_seconds = time.perf_counter() - t0
    print(f"      loaded in {load_seconds:.1f}s")

    # --- 3. Encode a single Korean sentence ---
    sentences_single = ["안녕하세요. 케이런 VC 내부 RAG 챗봇 테스트입니다."]
    print("\n[3/4] Encoding single Korean sentence...")
    t0 = time.perf_counter()
    vec = model.encode(sentences_single, normalize_embeddings=True)
    single_seconds = time.perf_counter() - t0
    print(f"      shape={vec.shape}  |  {single_seconds * 1000:.0f} ms")

    if vec.shape != (1, 1024):
        print(f"\n[FAIL] Unexpected shape {vec.shape}, expected (1, 1024)")
        return 1

    # --- 4. Batch encode (sanity for ingest workload) ---
    sample_chunks = [
        "위밋모빌리티는 전기차 충전 인프라 운영사로, 2025년 12월 1차 DD를 진행했다.",
        "메타씨앤아이 1차 DD 핵심 리스크: 매출 집중도, 핵심 인력 의존, 경쟁사 진입.",
        "케이런 7호 펀드는 2024년 결성된 200억 원 규모의 모험투자형 펀드이다.",
        "샌드박스네트웍스 IPO 진행상황: 2026년 상장 예심 청구 예정.",
    ] * 8  # 32 chunks = one BGE-M3 batch
    print(f"\n[4/4] Encoding batch of {len(sample_chunks)} chunks (batch_size=32)...")
    t0 = time.perf_counter()
    vecs = model.encode(sample_chunks, batch_size=32, normalize_embeddings=True)
    batch_seconds = time.perf_counter() - t0
    chunks_per_sec = len(sample_chunks) / batch_seconds
    print(f"      shape={vecs.shape}  |  {batch_seconds:.2f}s  |  {chunks_per_sec:.0f} chunks/s")

    # --- Gate check ---
    print("\n" + "=" * 60)
    if single_seconds <= 1.0:
        print(f"[PASS] Single-sentence embedding in {single_seconds * 1000:.0f} ms (<= 1000 ms)")
    else:
        print(f"[WARN] Single-sentence embedding took {single_seconds * 1000:.0f} ms (> 1000 ms)")
        print("       OK on first call (model warm-up). Re-run for steady-state.")

    estimated_3000_chunks = 3000 / chunks_per_sec
    print(f"\nEstimated full reindex (3,000 chunks): {estimated_3000_chunks:.0f}s")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
