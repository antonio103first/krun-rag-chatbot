"""Repoint eval_set.yaml `expected_files` at the vault's current paths.

The eval set records full vault-relative paths, and the vault gets reorganized
(2026-07: `03_Companies/Antonio/검토단계/` split into `검토중` / `검토종료`;
company notes moved into per-company subfolders). Every move turns a passing
item into a false failure — on 2026-07-20 this dragged file_match_rate to 0.294
and looked exactly like a retrieval regression.

Matching is by filename stem, which is stable across the moves that actually
happen here (folders change, note names don't). Ambiguous stems and files that
no longer exist are reported, never guessed at.

    uv run python scripts/refresh_eval_paths.py --dry-run
    uv run python scripts/refresh_eval_paths.py
"""

from __future__ import annotations

import argparse
import io
from collections import defaultdict
from pathlib import Path

import yaml

from rag.store.lancedb_store import open_store

EVAL_SET = Path("rag/eval/eval_set.yaml")


def build_stem_map() -> tuple[dict[str, str], set[str]]:
    """stem -> vault-relative path (no .md). Second value: ambiguous stems."""
    df = open_store().table.to_pandas()
    by_stem: dict[str, set[str]] = defaultdict(set)
    for vr in df["vault_relative"].dropna().astype(str).unique():
        rel = vr.replace("\\", "/")
        if rel.endswith(".md"):
            rel = rel[:-3]
        by_stem[rel.split("/")[-1]].add(rel)
    ambiguous = {s for s, paths in by_stem.items() if len(paths) > 1}
    return {s: next(iter(p)) for s, p in by_stem.items() if len(p) == 1}, ambiguous


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
    args = ap.parse_args()

    if not EVAL_SET.exists():
        print(f"missing {EVAL_SET}")
        return 1

    raw = io.open(EVAL_SET, encoding="utf-8").read()
    data = yaml.safe_load(raw)
    items = data if isinstance(data, list) else data.get("items", [])

    stem_map, ambiguous = build_stem_map()
    updated = missing = ambig = unchanged = 0

    for it in items:
        files = it.get("expected_files") or []
        for i, f in enumerate(files):
            stem = str(f).replace("\\", "/").split("/")[-1]
            if stem in ambiguous:
                print(f"  [ambiguous] {it.get('id')}: {stem} — multiple files share this name")
                ambig += 1
                continue
            cur = stem_map.get(stem)
            if cur is None:
                print(f"  [gone] {it.get('id')}: {stem} — not in the index")
                missing += 1
                continue
            if cur == str(f):
                unchanged += 1
                continue
            print(f"  [move] {it.get('id')}: {f}  ->  {cur}")
            files[i] = cur
            updated += 1

    print(
        f"\nunchanged {unchanged} | repointed {updated} | "
        f"gone {missing} | ambiguous {ambig}"
    )
    if missing:
        print(
            "Files reported 'gone' were renamed or deleted in the vault. Fix those "
            "entries by hand — picking a replacement automatically would silently "
            "change what the eval measures."
        )

    if args.dry_run:
        print("--dry-run: eval_set.yaml not written")
        return 0
    if not updated:
        return 0

    with io.open(EVAL_SET, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False, width=200)
    print(f"wrote {EVAL_SET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
