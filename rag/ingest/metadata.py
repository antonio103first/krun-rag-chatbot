"""Derive structured metadata from a loaded note.

Priority chain (most to least specific):
1. Filename pattern (e.g. `Blueward_20260419_1차DD.md` → meeting + company + date + stage)
2. Frontmatter fields (`type`, `fileClass`, `company`, `date`, …)
3. Folder path (`03_Companies/Blueward/...` → company)

This works because the vault has strong filename conventions but the
frontmatter is sometimes stale or empty (49 of 402 files had no frontmatter).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from rag.ingest.md_loader import LoadedNote


# --- Doc type vocabulary ---------------------------------------------------
# Keep this stable; LanceDB filters and prompts depend on it.
DOC_TYPES = (
    "company",     # company profile (e.g. Blueward.md in 03_Companies)
    "person",      # person profile (e.g. 강규식 상무.md in 02_Persons)
    "meeting",     # meeting / event / call / DD note
    "daily",       # daily_YYYYMMDD
    "periodic",    # weekly_/monthly_
    "project",     # 05_Projects
    "resource",    # 06_Resources
    "inbox",       # 00_Inbox (when not a meeting)
    "dashboard",   # auto-generated (e.g. _전체현황, deal_pipeline dashboards)
    "other",
)


# --- Filename pattern matchers ---------------------------------------------
# {subject}_{YYYYMMDD}_{stage}    — most specific
_PATTERN_SUBJECT_DATE_STAGE = re.compile(
    r"^(?P<subject>.+?)_(?P<date>\d{8})_(?P<stage>[^_]+)$"
)
# daily_{YYYYMMDD}
_PATTERN_DAILY = re.compile(r"^daily_(?P<date>\d{8})$")
# weekly_{YYYYMMDD or YYYYWW (4 digits + 2 digits)}
_PATTERN_WEEKLY = re.compile(r"^weekly_(?P<token>\d{6,8})$")
# monthly_{YYYYMM}
_PATTERN_MONTHLY = re.compile(r"^monthly_(?P<yyyymm>\d{6})$")
# 주간회의_{YYYYMMDD}
_PATTERN_WEEKLY_MEETING = re.compile(r"^주간회의_(?P<date>\d{8})$")


def _parse_yyyymmdd(s: str) -> date | None:
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def _parse_yyyymm(s: str) -> date | None:
    if len(s) != 6 or not s.isdigit():
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), 1)
    except ValueError:
        return None


def _parse_yyyy_ww(s: str) -> date | None:
    """Convert YYYYWW to the Monday of that ISO week."""
    if len(s) != 6 or not s.isdigit():
        return None
    try:
        year = int(s[:4])
        week = int(s[4:6])
        if not 1 <= week <= 53:
            return None
        return datetime.strptime(f"{year}-W{week:02d}-1", "%G-W%V-%u").date()
    except ValueError:
        return None


def _coerce_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        # Try YYYY-MM-DD first.
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            pass
        # Strip dashes/slashes and try YYYYMMDD.
        cleaned = re.sub(r"[^\d]", "", v)
        return _parse_yyyymmdd(cleaned[:8]) if len(cleaned) >= 8 else None
    return None


def _coerce_str(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return str(v).strip() or None


def _coerce_str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if x is not None and str(x).strip()]
    return [str(v).strip()]


# --- Output dataclass ------------------------------------------------------
@dataclass
class NoteMetadata:
    title: str
    relative_path: str
    file_path: str
    top_folder: str
    doc_type: str
    company: str | None = None
    person: str | None = None          # subject of person notes / meetings under 02_Persons
    date: date | None = None
    event_stage: str | None = None     # 1차DD, 미팅, 티타임, 킥오프, etc.
    investment_stage: str | None = None
    pipeline_stage: str | None = None
    status: str | None = None
    meeting_type: str | None = None
    tags: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    role: str | None = None
    organization: str | None = None
    raw_frontmatter: dict[str, Any] = field(default_factory=dict)

    def as_lance_row_partial(self) -> dict[str, Any]:
        """Return the metadata fields as plain Python types (LanceDB-friendly)."""
        return {
            "file_path": self.file_path,
            "vault_relative": self.relative_path,
            "title": self.title,
            "doc_type": self.doc_type,
            "company": self.company,
            "person": self.person,
            "date": self.date,
            "event_stage": self.event_stage,
            "investment_stage": self.investment_stage,
            "pipeline_stage": self.pipeline_stage,
            "status": self.status,
            "meeting_type": self.meeting_type,
            "tags": self.tags,
            "people": self.people,
            "top_folder": self.top_folder,
        }


# --- Public API ------------------------------------------------------------
def derive_metadata(note: LoadedNote) -> NoteMetadata:
    """Compute structured metadata for a note using filename + frontmatter + folder."""
    fm = note.frontmatter or {}
    rel = Path(note.relative_path)
    top_folder = rel.parts[0] if rel.parts else ""
    title = note.title

    # --- 1. Filename pattern detection -----------------------------------
    fn_doc_type: str | None = None
    fn_subject: str | None = None
    fn_date: date | None = None
    fn_stage: str | None = None

    if m := _PATTERN_DAILY.match(title):
        fn_doc_type = "daily"
        fn_date = _parse_yyyymmdd(m["date"])
    elif m := _PATTERN_WEEKLY.match(title):
        token = m["token"]
        fn_doc_type = "periodic"
        if len(token) == 8:
            fn_date = _parse_yyyymmdd(token)
        elif len(token) == 6:
            # weekly_YYYYWW = ISO week number, not YYYYMM.
            fn_date = _parse_yyyy_ww(token)
    elif m := _PATTERN_MONTHLY.match(title):
        fn_doc_type = "periodic"
        fn_date = _parse_yyyymm(m["yyyymm"])
    elif m := _PATTERN_WEEKLY_MEETING.match(title):
        fn_doc_type = "meeting"
        fn_subject = "주간회의"
        fn_date = _parse_yyyymmdd(m["date"])
        fn_stage = "주간회의"
    elif m := _PATTERN_SUBJECT_DATE_STAGE.match(title):
        # Has a YYYYMMDD in the middle → meeting/event note.
        fn_doc_type = "meeting"
        fn_subject = m["subject"].strip()
        fn_date = _parse_yyyymmdd(m["date"])
        fn_stage = m["stage"].strip()

    # --- 2. Folder-based default doc_type --------------------------------
    folder_doc_type = {
        "00_Inbox": "inbox",
        "01_Daily": "daily",
        "01a_Periodic": "periodic",
        "02_Persons": "person",
        "03_Companies": "company",
        "04_Meetings": "meeting",
        "05_Projects": "project",
        "06_Resources": "resource",
    }.get(top_folder, "other")

    # --- 3. Frontmatter explicit doc_type (highest authority for type) ---
    explicit_type = _coerce_str(fm.get("type")) or _coerce_str(fm.get("fileClass"))
    if explicit_type:
        explicit_type = explicit_type.lower()
        # Normalize a few known synonyms.
        if explicit_type in {"call", "interview"}:
            explicit_type = "meeting"
        if explicit_type == "people":
            explicit_type = "person"

    # --- 4. Choose final doc_type ---------------------------------------
    # filename pattern beats folder default; explicit frontmatter beats both
    # except when frontmatter says `dashboard`, which we trust unconditionally.
    if explicit_type == "dashboard":
        doc_type = "dashboard"
    elif fn_doc_type:
        doc_type = fn_doc_type
    elif explicit_type in DOC_TYPES:
        doc_type = explicit_type
    else:
        doc_type = folder_doc_type

    # --- 5. Company / person derivation ---------------------------------
    company = _coerce_str(fm.get("company")) or _coerce_str(fm.get("company_name"))
    person = None

    # If the file is a meeting under 02_Persons, the filename subject is the person.
    if top_folder == "02_Persons" and fn_subject and doc_type == "meeting":
        person = fn_subject
    # If the file is a meeting under 03_Companies, the filename subject is the company.
    elif top_folder == "03_Companies" and fn_subject and doc_type == "meeting":
        company = company or fn_subject
    # If it is a profile note (no date pattern) under those folders, the title is the entity.
    elif fn_doc_type is None and doc_type == "company":
        company = company or title.lstrip("_")
    elif fn_doc_type is None and doc_type == "person":
        person = title

    # Fallback: use parent folder name under 03_Companies for nested files.
    if doc_type in {"company", "meeting"} and not company:
        # 03_Companies/<X>/something.md → company = X
        if len(rel.parts) >= 3 and rel.parts[0] == "03_Companies":
            company = rel.parts[1]

    # --- 6. Date derivation ---------------------------------------------
    date_val = (
        fn_date
        or _coerce_date(fm.get("date"))
        or _coerce_date(fm.get("first_met"))
        or _coerce_date(fm.get("created"))
        or _coerce_date(fm.get("modified"))
    )

    # --- 7. Other fields -----------------------------------------------
    tags = _coerce_str_list(fm.get("tags")) + (note.inline_tags or [])
    # Dedup while preserving order.
    seen: set[str] = set()
    tags_unique: list[str] = []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            tags_unique.append(t)

    return NoteMetadata(
        title=title,
        relative_path=note.relative_path,
        file_path=str(note.path),
        top_folder=top_folder,
        doc_type=doc_type,
        company=company,
        person=person,
        date=date_val,
        event_stage=fn_stage or _coerce_str(fm.get("event_stage")),
        investment_stage=_coerce_str(fm.get("investment_stage")),
        pipeline_stage=_coerce_str(fm.get("pipeline_stage")),
        status=_coerce_str(fm.get("status")) or _coerce_str(fm.get("investment_status")),
        meeting_type=_coerce_str(fm.get("meeting_type")),
        tags=tags_unique,
        people=_coerce_str_list(fm.get("people")),
        aliases=_coerce_str_list(fm.get("aliases")),
        role=_coerce_str(fm.get("role")),
        organization=_coerce_str(fm.get("organization")),
        raw_frontmatter={k: v for k, v in fm.items() if v is not None},
    )
