"""Markdown loader: parse frontmatter, body, and Obsidian-style links.

Design notes
------------
- We use `python-frontmatter` for YAML frontmatter parsing because that is what
  Obsidian writes.
- Wikilinks (`[[Target]]` or `[[Target|Display]]`) and embeds (`![[file.pdf]]`)
  are extracted into separate lists so we can surface them as metadata without
  polluting the chunk text. The display form is preserved inline so the reader
  still sees a sensible sentence.
- We also normalize CRLF -> LF so chunker offsets are stable across OSes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter

# `[[Target]]`, `[[Target|Display]]`, `[[Target#Heading|Display]]`
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\|\n]+?)(?:#([^\[\]\|\n]+?))?(?:\|([^\[\]\n]+?))?\]\]")
# `![[file.pdf]]` / `![[image.png|alt]]`
_EMBED_RE = re.compile(r"!\[\[([^\[\]\|\n]+?)(?:\|([^\[\]\n]+?))?\]\]")
# Inline `#tag` (not at column 0 to avoid headings)
_INLINE_TAG_RE = re.compile(r"(?<![#\w])#([\w가-힣\-_/]+)")


@dataclass
class LoadedNote:
    path: Path
    relative_path: str
    title: str
    frontmatter: dict[str, Any]
    body: str
    wikilinks: list[str] = field(default_factory=list)
    embeds: list[str] = field(default_factory=list)
    inline_tags: list[str] = field(default_factory=list)
    raw_size: int = 0
    parse_error: str | None = None


def _replace_wikilinks_with_display(text: str) -> str:
    """Replace `[[Target|Display]]` with `Display`, `[[Target]]` with `Target`."""
    def sub(m: re.Match[str]) -> str:
        target, _heading, display = m.group(1), m.group(2), m.group(3)
        return (display or target).strip()
    return _WIKILINK_RE.sub(sub, text)


def _strip_embeds(text: str) -> str:
    """Remove `![[...]]` blocks from the body but keep a sentence cue.

    We replace each embed with `[첨부: filename]` so the reader knows
    something was there.
    """
    def sub(m: re.Match[str]) -> str:
        target = m.group(1).strip()
        return f"[첨부: {target}]"
    return _EMBED_RE.sub(sub, text)


def extract_wikilinks(body: str) -> list[str]:
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(body)]


def extract_embeds(body: str) -> list[str]:
    return [m.group(1).strip() for m in _EMBED_RE.finditer(body)]


def extract_inline_tags(body: str) -> list[str]:
    """Find inline `#tag` markers, skipping markdown headings."""
    tags: set[str] = set()
    for line in body.splitlines():
        # Strip leading whitespace then skip headings.
        stripped = line.lstrip()
        if stripped.startswith("#") and (
            stripped.startswith("# ") or stripped.startswith("## ")
            or stripped.startswith("### ") or stripped.startswith("#### ")
            or stripped.startswith("##### ") or stripped.startswith("###### ")
        ):
            continue
        for m in _INLINE_TAG_RE.finditer(line):
            tags.add(m.group(1))
    return sorted(tags)


def load_note(path: Path, vault_root: Path) -> LoadedNote:
    """Read one .md file and return a LoadedNote.

    On parse failure the body falls back to raw text so we still index the
    note (with `parse_error` populated for the caller).
    """
    raw_bytes = path.read_bytes()
    raw_size = len(raw_bytes)

    try:
        post = frontmatter.loads(raw_bytes.decode("utf-8", errors="replace"))
        meta = dict(post.metadata or {})
        body_raw = post.content or ""
        parse_err: str | None = None
    except Exception as e:  # malformed YAML, mojibake, etc.
        meta = {}
        body_raw = raw_bytes.decode("utf-8", errors="replace")
        parse_err = f"frontmatter parse error: {e!r}"

    body_normalized = body_raw.replace("\r\n", "\n").replace("\r", "\n")
    wikilinks = extract_wikilinks(body_normalized)
    embeds = extract_embeds(body_normalized)
    inline_tags = extract_inline_tags(body_normalized)

    body_clean = _strip_embeds(_replace_wikilinks_with_display(body_normalized))

    rel = path.relative_to(vault_root).as_posix()
    title = path.stem

    return LoadedNote(
        path=path,
        relative_path=rel,
        title=title,
        frontmatter=meta,
        body=body_clean,
        wikilinks=wikilinks,
        embeds=embeds,
        inline_tags=inline_tags,
        raw_size=raw_size,
        parse_error=parse_err,
    )


def is_excluded(rel_path: Path, exclude_patterns: list[str]) -> bool:
    """Check whether a path matches an exclude pattern from config.yaml.

    Trailing-slash patterns are folder prefixes (`07_Archive/`).
    `*` wildcards in folder prefixes are supported (`_backup_*/`).
    Glob patterns starting with `**/` match any descendant (`**/sortspec.md`).
    """
    rel_posix = rel_path.as_posix()
    for pat in exclude_patterns:
        if pat.startswith("**/"):
            tail = pat[3:]
            if rel_path.name == tail or rel_path.match(pat):
                return True
            continue
        if pat.endswith("*/"):
            stem = pat[:-2]
            if any(part.startswith(stem) for part in rel_path.parts):
                return True
            continue
        if pat.endswith("/"):
            if rel_posix.startswith(pat) or f"/{pat}" in f"/{rel_posix}":
                return True
            continue
        # Plain glob.
        if rel_path.match(pat):
            return True
    return False


def list_vault_md(
    vault_root: Path,
    include_dirs: list[str],
    exclude_patterns: list[str],
) -> list[Path]:
    """Walk the vault and return all .md files matching the include/exclude rules."""
    results: list[Path] = []
    roots = [vault_root / d for d in include_dirs] if include_dirs else [vault_root]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            rel = path.relative_to(vault_root)
            if is_excluded(rel, exclude_patterns):
                continue
            results.append(path)
    return results
