"""Build clickable Obsidian URIs for cited chunks.

`obsidian://open?vault=<vault>&file=<path>` opens the note in the user's
Obsidian app. We URL-encode the vault name and the .md-stripped file path so
Korean and spaces work.

Reference: https://help.obsidian.md/Concepts/Obsidian+URI
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from rag.config import get_settings


def _clean_str(v: Any) -> str | None:
    """Coerce a pandas/LanceDB scalar to `str | None`, handling NaN.

    LanceDB nullable columns come back from `to_pandas()` as `float('nan')`
    when the value is null. NaN is truthy in Python (`if nan:` -> True), so
    callers that do `if c.company:` would silently flow a float into a join
    or f-string. Normalize to None here at the boundary.
    """
    if v is None:
        return None
    if isinstance(v, float) and v != v:  # NaN != NaN
        return None
    s = str(v).strip()
    return s or None


@dataclass
class Citation:
    """One numbered citation in an answer."""

    n: int
    chunk_id: str
    title: str
    vault_relative: str
    header_path: str
    obsidian_uri: str
    doc_type: str
    company: str | None = None
    person: str | None = None
    date: str | None = None
    snippet: str = ""       # truncated, for UI display
    text_full: str = ""     # full chunk text, for LLM context

    def display_label(self) -> str:
        """Compact human label, e.g. `Blueward > 투자 검토 요약 > ⚠️ 리스크 요인`."""
        if self.header_path:
            return f"{self.title} > {self.header_path}"
        return self.title

    def short_path(self) -> str:
        """Vault-relative path without the .md extension."""
        return self.vault_relative.removesuffix(".md")


def vault_name_from_settings() -> str:
    """Last component of the configured vault path."""
    s = get_settings()
    return s.vault.resolved_path.name or "vault"


def build_obsidian_uri(vault_relative: str, vault_name: str | None = None) -> str:
    name = vault_name or vault_name_from_settings()
    file_no_ext = vault_relative.removesuffix(".md")
    return (
        "obsidian://open"
        f"?vault={quote(name, safe='')}"
        f"&file={quote(file_no_ext, safe='/')}"
    )


def build_citations(rows: list[dict], vault_name: str | None = None) -> list[Citation]:
    """Turn LanceDB result rows into ordered Citation objects.

    Every nullable string field is funneled through `_clean_str` so NaN
    sentinels (from LanceDB null columns) become None instead of float NaN.
    """
    name = vault_name or vault_name_from_settings()
    out: list[Citation] = []
    for n, r in enumerate(rows, start=1):
        rel = _clean_str(r.get("vault_relative")) or ""
        date_val = r.get("date")
        if isinstance(date_val, float) and date_val != date_val:
            date_str: str | None = None
        elif hasattr(date_val, "isoformat"):
            date_str = date_val.isoformat()
        elif date_val:
            date_str = str(date_val)
        else:
            date_str = None
        text_full = (_clean_str(r.get("text")) or "").strip()
        snippet = text_full.replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:240] + "…"
        out.append(
            Citation(
                n=n,
                chunk_id=_clean_str(r.get("chunk_id")) or "",
                title=_clean_str(r.get("title")) or Path(rel).stem,
                vault_relative=rel,
                header_path=_clean_str(r.get("header_path")) or "",
                obsidian_uri=build_obsidian_uri(rel, vault_name=name),
                doc_type=_clean_str(r.get("doc_type")) or "other",
                company=_clean_str(r.get("company")),
                person=_clean_str(r.get("person")),
                date=date_str,
                snippet=snippet,
                text_full=text_full,
            )
        )
    return out
