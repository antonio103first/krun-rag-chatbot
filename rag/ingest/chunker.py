"""Header-aware Markdown chunker.

Approach:
1. Parse the body into a tree of (header_path, content) blocks.
2. For each leaf block, if it fits within `max_tokens`, emit one chunk.
3. If it overflows, split into sliding sub-chunks that respect paragraph
   boundaries when possible.
4. Always prefix the chunk text with `H1 > H2 > H3` breadcrumb so retrieval
   has a structural cue even after the chunk is isolated.

Token estimate
--------------
We approximate tokens as `max(len_chars, 2 * len_words)` // 2 to stay close to
both Claude's tokenizer (English-leaning) and Korean character density. For
Korean-heavy content roughly 1 token ≈ 2 characters; we err on the safe side.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# `# Heading`, `## Heading`, … up to 6 levels.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# Code fence delimiters that must not be split.
_FENCE_RE = re.compile(r"^(```|~~~)")


@dataclass
class Chunk:
    chunk_id: str
    chunk_idx: int
    header_path: str  # "H1 > H2 > H3"
    text: str
    char_count: int
    approx_tokens: int


@dataclass
class _Block:
    header_path: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


# --- Token estimation ------------------------------------------------------
def estimate_tokens(text: str) -> int:
    """Cheap estimate that works for mixed Korean/English."""
    char_estimate = len(text) / 2.0
    word_estimate = len(text.split())
    return int(max(char_estimate, word_estimate))


# --- Block parsing ---------------------------------------------------------
def parse_blocks(body: str) -> list[_Block]:
    """Split body into blocks separated by markdown headings.

    Inside a fenced code block we never start a new block, even if a `#` line
    appears (it would actually be a comment in the code).
    """
    blocks: list[_Block] = []
    current = _Block()
    header_stack: list[tuple[int, str]] = []  # (level, text)
    in_fence = False

    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            current.lines.append(line)
            continue

        if not in_fence and (m := _HEADING_RE.match(line)):
            # Flush current block before starting a new one.
            if current.lines or current.header_path:
                blocks.append(current)
            level = len(m.group(1))
            text = m.group(2).strip()
            # Pop deeper-or-equal levels.
            while header_stack and header_stack[-1][0] >= level:
                header_stack.pop()
            header_stack.append((level, text))
            current = _Block(header_path=[t for _, t in header_stack])
            continue

        current.lines.append(line)

    if current.lines or current.header_path:
        blocks.append(current)

    # Drop blocks that are pure whitespace AND have no header.
    return [b for b in blocks if b.header_path or any(s.strip() for s in b.lines)]


# --- Splitting overflow blocks --------------------------------------------
def _split_long_text(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Split a long text into overlapping sub-chunks on paragraph boundaries."""
    if estimate_tokens(text) <= max_tokens:
        return [text]

    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    sub_chunks: list[str] = []
    buf: list[str] = []
    buf_tokens = 0

    for p in paragraphs:
        p_tokens = estimate_tokens(p)
        if buf_tokens + p_tokens > max_tokens and buf:
            sub_chunks.append("\n\n".join(buf))
            # Keep tail for overlap.
            tail: list[str] = []
            tail_tokens = 0
            for prev in reversed(buf):
                t = estimate_tokens(prev)
                if tail_tokens + t > overlap_tokens:
                    break
                tail.insert(0, prev)
                tail_tokens += t
            buf = tail
            buf_tokens = tail_tokens
        # If a single paragraph itself exceeds max_tokens, split by sentences.
        if p_tokens > max_tokens:
            sentences = re.split(r"(?<=[\.\?\!。！？])\s+|\n", p)
            sent_buf: list[str] = []
            sent_tokens = 0
            for s in sentences:
                s_tok = estimate_tokens(s)
                if sent_tokens + s_tok > max_tokens and sent_buf:
                    sub_chunks.append(" ".join(sent_buf))
                    sent_buf = []
                    sent_tokens = 0
                sent_buf.append(s)
                sent_tokens += s_tok
            if sent_buf:
                buf.append(" ".join(sent_buf))
                buf_tokens += sent_tokens
        else:
            buf.append(p)
            buf_tokens += p_tokens

    if buf:
        sub_chunks.append("\n\n".join(buf))
    return sub_chunks


# --- Public API ------------------------------------------------------------
def chunk_note(
    file_path: str,
    body: str,
    max_tokens: int = 800,
    overlap_tokens: int = 80,
    min_tokens: int = 50,
    preserve_breadcrumb: bool = True,
) -> list[Chunk]:
    """Split a note body into Chunk objects."""
    blocks = parse_blocks(body)
    chunks: list[Chunk] = []
    chunk_idx = 0

    for block in blocks:
        text = "\n".join(block.lines).strip()
        if not text and not block.header_path:
            continue
        breadcrumb = " > ".join(block.header_path) if block.header_path else ""

        # Tiny blocks are merged with the previous chunk (same breadcrumb if possible).
        if estimate_tokens(text) < min_tokens and chunks and chunks[-1].header_path == breadcrumb:
            prev = chunks[-1]
            merged_text = (prev.text + "\n\n" + text).strip()
            chunks[-1] = Chunk(
                chunk_id=prev.chunk_id,
                chunk_idx=prev.chunk_idx,
                header_path=prev.header_path,
                text=merged_text,
                char_count=len(merged_text),
                approx_tokens=estimate_tokens(merged_text),
            )
            continue

        sub_texts = _split_long_text(text, max_tokens=max_tokens, overlap_tokens=overlap_tokens)

        for st in sub_texts:
            display = st.strip()
            if preserve_breadcrumb and breadcrumb:
                display = f"[{breadcrumb}]\n{display}"
            chunk_id = hashlib.sha256(
                f"{file_path}|{breadcrumb}|{chunk_idx}|{display[:100]}".encode("utf-8")
            ).hexdigest()
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    chunk_idx=chunk_idx,
                    header_path=breadcrumb,
                    text=display,
                    char_count=len(display),
                    approx_tokens=estimate_tokens(display),
                )
            )
            chunk_idx += 1

    return chunks
