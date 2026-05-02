"""Inventory all frontmatter fields across the vault.

Run BEFORE writing md_loader.py / metadata.py — this gives us the real
frontmatter schema (field names, value types, sample values, coverage)
so we don't guess.

Run:
    uv run python scripts/inventory_frontmatter.py

Output:
    - scripts/frontmatter_report.md    (human readable summary)
    - scripts/frontmatter_report.json  (machine readable, for downstream)

Both files are written to .gitignore'd locations so private values do not leak.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import frontmatter
except ImportError:
    print("[FAIL] python-frontmatter not installed. Run: uv sync")
    sys.exit(1)

from rag.config import get_settings


# --- Config ----------------------------------------------------------------
MAX_SAMPLES_PER_FIELD = 5
MAX_VALUE_DISPLAY = 80


# --- Helpers ---------------------------------------------------------------
def categorize_value(v: Any) -> str:
    """Return a coarse type label."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, (date, datetime)):
        return "date"
    if isinstance(v, list):
        if not v:
            return "list[empty]"
        inner = categorize_value(v[0])
        return f"list[{inner}]"
    if isinstance(v, dict):
        return "dict"
    if isinstance(v, str):
        return "str"
    return type(v).__name__


def format_value(v: Any) -> str:
    s = str(v)
    if len(s) > MAX_VALUE_DISPLAY:
        s = s[:MAX_VALUE_DISPLAY] + "…"
    return s


def is_excluded(rel_path: Path, exclude_patterns: list[str]) -> bool:
    s = rel_path.as_posix() + "/"
    for pat in exclude_patterns:
        # Treat trailing-slash patterns as folder prefixes.
        if pat.endswith("/"):
            if pat in s or s.startswith(pat):
                return True
        # Wildcard prefix folders (e.g. "_backup_*/").
        if pat.endswith("*/"):
            stem = pat.removesuffix("*/")
            if any(part.startswith(stem) for part in rel_path.parts):
                return True
        # Glob-like canvas exclusion is inert here (we only look at .md).
    return False


def list_md_files(vault_root: Path, include_dirs: list[str], exclude_patterns: list[str]) -> list[Path]:
    """Return all .md files under include_dirs, minus excluded paths."""
    results: list[Path] = []
    roots = [vault_root / d for d in include_dirs] if include_dirs else [vault_root]
    for root in roots:
        if not root.exists():
            print(f"  [warn] include_dir missing: {root}")
            continue
        for path in root.rglob("*.md"):
            rel = path.relative_to(vault_root)
            if is_excluded(rel, exclude_patterns):
                continue
            results.append(path)
    return results


# --- Main ------------------------------------------------------------------
def main() -> int:
    print("=" * 60)
    print("KRUN RAG — Frontmatter inventory")
    print("=" * 60)

    settings = get_settings()
    vault_root = settings.vault.resolved_path
    if not vault_root.exists():
        print(f"[FAIL] Vault not reachable: {vault_root}")
        return 1

    print(f"\nVault: {vault_root}")
    print(f"Include dirs: {settings.vault.include_dirs}")
    print(f"Exclude patterns: {settings.vault.exclude_patterns}")

    md_files = list_md_files(vault_root, settings.vault.include_dirs, settings.vault.exclude_patterns)
    print(f"\nFound {len(md_files)} .md files to scan.")

    # Stats per field.
    field_count: Counter[str] = Counter()
    field_types: dict[str, Counter[str]] = defaultdict(Counter)
    field_samples: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    seen_fields_per_doctype: dict[str, Counter[str]] = defaultdict(Counter)
    files_per_doctype: Counter[str] = Counter()

    parse_errors: list[tuple[str, str]] = []
    no_frontmatter: list[str] = []

    for path in md_files:
        rel = path.relative_to(vault_root).as_posix()
        # Top-level folder used as a coarse doc_type proxy.
        top = rel.split("/", 1)[0]
        files_per_doctype[top] += 1
        try:
            post = frontmatter.load(path)
        except Exception as e:
            parse_errors.append((rel, str(e)))
            continue

        meta = post.metadata or {}
        if not meta:
            no_frontmatter.append(rel)
            continue

        for k, v in meta.items():
            field_count[k] += 1
            t = categorize_value(v)
            field_types[k][t] += 1
            seen_fields_per_doctype[top][k] += 1
            if len(field_samples[k]) < MAX_SAMPLES_PER_FIELD:
                field_samples[k].append((rel, v))

    # --- Render report -----------------------------------------------------
    out_md_lines: list[str] = []
    out_md_lines.append("# Vault Frontmatter Inventory\n")
    out_md_lines.append(f"- Scanned: **{len(md_files)}** `.md` files")
    out_md_lines.append(f"- With frontmatter: **{len(md_files) - len(no_frontmatter) - len(parse_errors)}**")
    out_md_lines.append(f"- Without frontmatter: **{len(no_frontmatter)}**")
    out_md_lines.append(f"- Parse errors: **{len(parse_errors)}**\n")

    out_md_lines.append("## Files per top-level folder\n")
    out_md_lines.append("| folder | count |")
    out_md_lines.append("|---|---|")
    for folder, count in files_per_doctype.most_common():
        out_md_lines.append(f"| `{folder}` | {count} |")
    out_md_lines.append("")

    out_md_lines.append("## All frontmatter fields\n")
    out_md_lines.append("| field | coverage | types | samples |")
    out_md_lines.append("|---|---|---|---|")
    for field, count in field_count.most_common():
        types_str = ", ".join(f"{t}({c})" for t, c in field_types[field].most_common())
        sample_strs = []
        for path, val in field_samples[field][:3]:
            sample_strs.append(f"`{format_value(val)}`")
        samples = " · ".join(sample_strs)
        coverage = f"{count}/{len(md_files)} ({count * 100 // max(len(md_files), 1)}%)"
        out_md_lines.append(f"| `{field}` | {coverage} | {types_str} | {samples} |")
    out_md_lines.append("")

    out_md_lines.append("## Field coverage by folder (top fields)\n")
    top_fields = [f for f, _ in field_count.most_common(15)]
    out_md_lines.append("| folder | " + " | ".join(f"`{f}`" for f in top_fields) + " |")
    out_md_lines.append("|---" * (len(top_fields) + 1) + "|")
    for folder, _ in files_per_doctype.most_common():
        cells = [folder]
        total = files_per_doctype[folder]
        for f in top_fields:
            seen = seen_fields_per_doctype[folder].get(f, 0)
            cells.append(f"{seen}/{total}" if seen else "-")
        out_md_lines.append("| " + " | ".join(cells) + " |")
    out_md_lines.append("")

    if no_frontmatter:
        out_md_lines.append(f"## Files without frontmatter ({len(no_frontmatter)})\n")
        out_md_lines.append("First 10 examples:\n")
        for rel in no_frontmatter[:10]:
            out_md_lines.append(f"- `{rel}`")
        out_md_lines.append("")

    if parse_errors:
        out_md_lines.append(f"## Parse errors ({len(parse_errors)})\n")
        for rel, err in parse_errors[:10]:
            out_md_lines.append(f"- `{rel}` → {err}")
        out_md_lines.append("")

    # --- Write outputs -----------------------------------------------------
    out_dir = Path(__file__).resolve().parent
    md_path = out_dir / "frontmatter_report.md"
    json_path = out_dir / "frontmatter_report.json"

    md_path.write_text("\n".join(out_md_lines), encoding="utf-8")

    json_payload = {
        "vault_root": str(vault_root),
        "files_scanned": len(md_files),
        "files_without_frontmatter": len(no_frontmatter),
        "parse_errors": len(parse_errors),
        "files_per_folder": dict(files_per_doctype),
        "fields": {
            field: {
                "coverage": field_count[field],
                "types": dict(field_types[field]),
                "samples": [
                    {"path": p, "value": (v.isoformat() if isinstance(v, (date, datetime)) else v)}
                    for p, v in field_samples[field]
                ],
            }
            for field in field_count
        },
    }

    def _default(o: Any) -> Any:
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        return str(o)

    json_path.write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2, default=_default),
        encoding="utf-8",
    )

    print(f"\nReport written:")
    print(f"  - {md_path}")
    print(f"  - {json_path}")
    print("\nTop 10 fields:")
    for field, count in field_count.most_common(10):
        types_str = ", ".join(f"{t}({c})" for t, c in field_types[field].most_common())
        print(f"  {count:5d}  {field:25s}  {types_str}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
