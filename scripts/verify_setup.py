"""Verify that .env, config.yaml, and the vault path are all reachable.

Run:
    uv run python scripts/verify_setup.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    print("=" * 60)
    print("KRUN RAG — Setup verification")
    print("=" * 60)

    try:
        from rag.config import get_settings
    except Exception as e:
        print(f"\n[FAIL] Could not import rag.config: {e}")
        return 1

    try:
        s = get_settings()
    except Exception as e:
        print(f"\n[FAIL] Settings load error: {e}")
        return 1

    print("\n[1] .env / config.yaml")
    print(f"    ANTHROPIC_API_KEY present : {'yes' if s.anthropic_api_key else 'NO'}")
    print(f"    ZDR enabled               : {s.generation.zdr_enabled}")
    print(f"    Generation model          : {s.generation.gen_model}")
    print(f"    Analyzer model            : {s.generation.analyzer_model}")

    print("\n[2] Vault path")
    print(f"    raw   : {s.vault.path}")
    resolved = s.vault.resolved_path
    print(f"    resolved: {resolved}")
    print(f"    exists  : {resolved.exists()}")
    if resolved.exists():
        md_count = sum(1 for _ in resolved.rglob("*.md"))
        print(f"    *.md count under vault: {md_count}")

    print("\n[3] LanceDB path")
    print(f"    {s.storage.resolved_path}")
    s.ensure_runtime_dirs()
    print(f"    created: {s.storage.resolved_path.exists()}")

    print("\n[4] Embedding config")
    print(f"    model   : {s.embedding.model_name}")
    print(f"    device  : {s.embedding.device}")
    print(f"    batch   : {s.embedding.batch_size}")

    # --- Gate check ---
    print("\n" + "=" * 60)
    issues: list[str] = []
    if not s.anthropic_api_key:
        issues.append("ANTHROPIC_API_KEY missing in .env")
    if not resolved.exists():
        issues.append(f"Vault path not reachable: {resolved}")

    if issues:
        print("[FAIL] Issues:")
        for i in issues:
            print(f"  - {i}")
        return 1

    print("[PASS] Settings load OK, vault reachable.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
