"""Verify ANTHROPIC_API_KEY by making a tiny Haiku call.

Run:
    uv run python scripts/verify_anthropic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    print("=" * 60)
    print("KRUN RAG — Anthropic API key check")
    print("=" * 60)

    try:
        from anthropic import Anthropic
    except ImportError:
        print("\n[FAIL] anthropic SDK not installed. Run: uv sync")
        return 1

    from rag.config import get_settings

    s = get_settings()
    if not s.anthropic_api_key:
        print("\n[FAIL] ANTHROPIC_API_KEY missing in .env")
        return 1

    extra_headers: dict[str, str] = {}
    if s.generation.zdr_enabled:
        # Anthropic ZDR header (only effective once enrollment is approved).
        extra_headers["anthropic-zero-retention-window"] = "0"
        print("[info] ZDR header attached (enrollment must be approved server-side)")

    client = Anthropic(api_key=s.anthropic_api_key)

    print(f"\nCalling {s.generation.analyzer_model} with a 1-token ping...")
    try:
        resp = client.messages.create(
            model=s.generation.analyzer_model,
            max_tokens=16,
            messages=[{"role": "user", "content": "안녕"}],
            extra_headers=extra_headers or None,
        )
    except Exception as e:
        print(f"\n[FAIL] API error: {e}")
        return 1

    text = resp.content[0].text if resp.content else "<empty>"
    print(f"  response : {text!r}")
    print(f"  usage    : input={resp.usage.input_tokens}, output={resp.usage.output_tokens}")
    print("\n[PASS] API key valid, model reachable.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
