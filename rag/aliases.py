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


# Korean position/title tokens that trail a personal name in the vault
# (folder/file names are like "강규식 상무", "강진영 변호사"). Stripping these
# lets "최원석" and "최원석 전무" resolve to the same person. Mirrors the
# automation's KOREAN_TITLES (person_property_sync).
KOREAN_TITLES: frozenset[str] = frozenset({
    "회장", "부회장", "사장", "부사장", "대표", "대표이사", "총괄", "사업부장",
    "전무", "상무", "이사", "사외이사", "감사", "고문", "자문", "본부장", "실장",
    "센터장", "소장", "원장", "부장", "차장", "과장", "팀장", "파트장", "대리",
    "주임", "사원", "선임", "책임", "수석", "위원", "위원장", "심사역", "매니저",
    "프로", "변호사", "회계사", "변리사", "세무사", "박사", "교수", "연구원",
    "기자", "국장", "실장", "처장", "청장", "차관", "장관", "의원", "지사장",
    "지점장", "본부장", "CEO", "CFO", "CTO", "COO", "CMO", "CIO",
})


def strip_person_title(name: str) -> str:
    """Drop trailing Korean title tokens: "최원석 전무" → "최원석".

    Leaves single-token names and title-less names unchanged. Only trailing
    tokens that are known titles are removed (so "김 민수" is not touched).
    """
    if not name:
        return name
    toks = name.split()
    while len(toks) > 1 and toks[-1] in KOREAN_TITLES:
        toks.pop()
    return " ".join(toks)


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
