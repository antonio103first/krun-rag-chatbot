"""Build clickable Obsidian URIs for cited chunks.

`obsidian://open?vault=<vault>&file=<path>` opens the note in the user's
Obsidian app. We URL-encode the vault name and the .md-stripped file path so
Korean and spaces work.

Reference: https://help.obsidian.md/Concepts/Obsidian+URI
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from rag.config import get_settings


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
    """Turn LanceDB result rows into ordered Citation objects."""
    name = vault_name or vault_name_from_settings()
    out: list[Citation] = []
    for n, r in enumerate(rows, start=1):
        rel = r.get("vault_relative") or ""
        date_val = r.get("date")
        if hasattr(date_val, "isoformat"):
            date_str: str | None = date_val.isoformat()
        elif date_val:
            date_str = str(date_val)
        else:
            date_str = None
        text_full = (r.get("text") or "").strip()
        snippet = text_full.replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:240] + "…"
        out.append(
            Citation(
                n=n,
                chunk_id=r.get("chunk_id") or "",
                title=r.get("title") or Path(rel).stem,
                vault_relative=rel,
                header_path=r.get("header_path") or "",
                obsidian_uri=build_obsidian_uri(rel, vault_name=name),
                doc_type=r.get("doc_type") or "other",
                company=r.get("company"),
                person=r.get("person"),
                date=date_str,
                snippet=snippet,
                text_full=text_full,
            )
        )
    return out
