"""Entity alias resolution for vault content.

Loads `config/aliases.yaml` and provides lookups: given any variant spelling,
return the canonical name plus all known aliases. Used by:

- `scripts/audit_frontmatter.py` to check that a meeting note's body actually
  mentions the company/person its filename claims (or any alias thereof)
- retrieval (potential future use): expand a single-company filter into an
  IN-clause across all aliases so a query like "ISTN 미팅" surfaces files
  filed under 아이에스티엠

The file is small and rarely changes, so we cache it at module load.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALIASES_PATH = PROJECT_ROOT / "config" / "aliases.yaml"


@lru_cache(maxsize=1)
def _load(path: Path = DEFAULT_ALIASES_PATH) -> dict:
    if not path.exists():
        return {"companies": {}, "persons": {}}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        "companies": data.get("companies") or {},
        "persons": data.get("persons") or {},
    }


def reload_aliases() -> None:
    """Drop the LRU cache (useful in tests / interactive sessions)."""
    _load.cache_clear()


def _build_inverse(canonical_map: dict[str, list[str]]) -> dict[str, str]:
    """{ variant_lower → canonical } including the canonical itself."""
    out: dict[str, str] = {}
    for canonical, variants in canonical_map.items():
        out[canonical.strip().lower()] = canonical
        for v in (variants or []):
            out[str(v).strip().lower()] = canonical
    return out


def canonical_name(name: str, *, kind: str = "companies") -> str:
    """Return the canonical name for `name`. If unknown, return `name` unchanged."""
    if not name:
        return name
    inv = _build_inverse(_load().get(kind, {}))
    return inv.get(name.strip().lower(), name)


def all_variants(name: str, *, kind: str = "companies") -> list[str]:
    """Return [canonical, *aliases] for `name`. If `name` is unknown, return [name]."""
    if not name:
        return []
    canon = canonical_name(name, kind=kind)
    bucket = _load().get(kind, {}).get(canon)
    if bucket is None:
        return [name]
    return [canon, *bucket]


def mention_count(text: str, names: Iterable[str]) -> int:
    """Total case-insensitive substring count of any of `names` in `text`."""
    if not text or not names:
        return 0
    lowered = text.lower()
    total = 0
    for n in names:
        if not n:
            continue
        needle = n.strip().lower()
        if not needle:
            continue
        # Disjoint counting: scan-and-skip avoids double-counting overlapping
        # matches (e.g. searching for both "ISTN" and "ISTM" in "ISTN").
        i = 0
        while True:
            j = lowered.find(needle, i)
            if j < 0:
                break
            total += 1
            i = j + len(needle)
    return total
