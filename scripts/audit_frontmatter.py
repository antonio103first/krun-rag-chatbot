"""Audit vault frontmatter for issues that confuse the RAG pipeline.

Reports four classes of problem:

1. **suspicious_link**   `company:` / `person:` / `lead_investor:` value unwraps to a
   generic word (`meeting`, `company`, `person`, …) — almost always a bad wikilink
   target left over from copy/paste or a template glitch.
2. **date_mismatch**     Filename matches `*_YYYYMMDD*` but frontmatter `date`
   disagrees (off by even one day → likely a typo).
3. **possible_duplicate** A file with no `_YYYYMMDD` suffix in its stem but with
   a frontmatter `date` set, sharing a root stem with at least one sibling that
   DOES have a `_YYYYMMDD` suffix (e.g. `VC협회_조찬세미나` next to
   `VC협회_조찬세미나_20260420`). The undated-stem file usually represents the
   same event written twice. Profile/meeting pairs (`정경원 사장` +
   `정경원 사장_20260313_석식`) don't trigger because profiles have no `date` field.
4. **content_filename_mismatch** Meeting note's body never mentions the entity
   its filename names (or any registered alias from `config/aliases.yaml`). This
   catches cases like `Blueward_20260419_1차DD.md` whose body is actually about
   another company. Skips known sub-meeting subjects (주간회의, 투자팀회의, …)
   and event-name subjects (신한OI, KDB, …) where the literal subject string
   wouldn't be expected in the body.

Default behavior is read-only. Pass `--fix-dates` to rewrite the frontmatter
`date` field so it matches the filename (filename is authoritative per the
project's metadata derivation rules — see `rag/ingest/metadata.py`).

Run:
    uv run python scripts/audit_frontmatter.py
    uv run python scripts/audit_frontmatter.py --fix-dates
    uv run python scripts/audit_frontmatter.py --json > audit.json
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys

# Windows consoles default to cp949 — force UTF-8 so Korean prints correctly.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)
from collections import defaultdict
from datetime import date as Date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import frontmatter  # noqa: E402

from rag.aliases import all_variants, mention_count  # noqa: E402
from rag.config import get_settings  # noqa: E402
from rag.ingest.md_loader import is_excluded, list_vault_md, load_note  # noqa: E402
from rag.ingest.metadata import (  # noqa: E402
    _PATTERN_SUBJECT_DATE_STAGE,
    _PATTERN_WEEKLY_MEETING,
    _coerce_date,
    _parse_yyyymmdd,
    _unwrap_wikilink,
)

# Subjects whose name is a category, not an entity that would be mentioned in
# the body (skip the content_filename_mismatch check for these).
_CATEGORY_SUBJECTS = {
    "주간회의", "투자팀회의", "관리팀회의", "파트너회의", "사내회의",
    "신한OI", "KDB", "V런치", "OI",
    "AX", "영업보고", "Catholic_전례", "Catholic_복사", "Catholic_상임위",
    "Catholic_봉사", "Catholic_교육", "골프", "여행", "회사",
}

# Korean person-title suffixes — for person notes like "강규식 상무" we also
# search for the bare name "강규식" in case the body uses just that.
_TITLE_SUFFIXES = (
    " 상무", " 대표", " 부사장", " 사장", " 회장", " 이사", " 전무",
    " 본부장", " 팀장", " 부장", " 차장", " 과장", " 매니저", " 원장",
    " 1차관", " 차관", " 장관", " 기자", " 박사", " 변호사", " 감사",
    " 안드레아", " 베드로", " 프란치스코",  # Catholic given names
)


def _person_short(name: str) -> str | None:
    for suf in _TITLE_SUFFIXES:
        if name.endswith(suf):
            return name[: -len(suf)].strip() or None
    return None

# Generic tokens that should never appear as company/person/investor names.
# If we see `company: [[meeting]]`, it's a stale template value.
SUSPICIOUS_TOKENS = {
    "meeting", "company", "person", "people", "daily", "weekly", "monthly",
    "dashboard", "untitled", "template", "n/a", "na", "tbd", "todo",
    "회의", "미팅", "회사", "인물", "템플릿",
}

# Frontmatter fields that should resolve to a real entity.
ENTITY_FIELDS = ("company", "company_name", "person", "lead_investor", "people")

# Stem-tail patterns that distinguish two near-duplicate filenames.
_DATE_TAIL_RE = re.compile(r"_(\d{8})(?:_[^_]+)?$")


def _filename_date(stem: str) -> Date | None:
    if m := _PATTERN_WEEKLY_MEETING.match(stem):
        return _parse_yyyymmdd(m["date"])
    if m := _PATTERN_SUBJECT_DATE_STAGE.match(stem):
        return _parse_yyyymmdd(m["date"])
    return None


def _is_suspicious(value: object) -> str | None:
    """If `value` (string or list) contains a suspicious wrapped token, return it."""
    if value is None:
        return None
    candidates: list[str] = []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple)):
        candidates = [str(x) for x in value if x]
    for raw in candidates:
        unwrapped = _unwrap_wikilink(str(raw).strip())
        if unwrapped and unwrapped.lower() in SUSPICIOUS_TOKENS:
            return unwrapped
    return None


def _stem_root(stem: str) -> str:
    """Strip a trailing `_YYYYMMDD` (and optional `_stage`) for duplicate detection."""
    return _DATE_TAIL_RE.sub("", stem)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable output")
    p.add_argument("--fix-dates", action="store_true",
                   help="Rewrite frontmatter `date` to match the filename (irreversible — review first)")
    p.add_argument("--fix-suspicious", action="store_true",
                   help="For suspicious company values under 03_Companies/{Antonio|KRUN}/<stage>/<X>/, "
                        "rewrite to [[X]] using the folder name (irreversible)")
    p.add_argument("--check-content", action="store_true",
                   help="Also check that company-meeting body text mentions the company filename "
                        "subject (or any alias from config/aliases.yaml). High-recall low-precision: "
                        "flags ~5-10%% of meetings; review each manually. Skips 02_Persons (body "
                        "usually talks ABOUT the meeting topic, not the person).")
    p.add_argument("--limit", type=int, default=200, help="Cap printed entries per category")
    args = p.parse_args()

    s = get_settings()
    vault_root = s.vault.resolved_path
    files = list_vault_md(vault_root, s.vault.include_dirs, s.vault.exclude_patterns)

    findings: dict[str, list[dict]] = defaultdict(list)
    file_records: list[dict] = []
    AUTO_ROOTS = {"daily", "weekly", "monthly", "주간회의", "_전체현황"}

    for path in files:
        rel = path.relative_to(vault_root)
        if is_excluded(rel, s.vault.exclude_patterns):
            continue
        try:
            note = load_note(path, vault_root)
        except Exception as e:
            findings["load_error"].append({"file": rel.as_posix(), "error": repr(e)})
            continue

        fm = note.frontmatter or {}

        # 1. Suspicious entity values
        for field in ENTITY_FIELDS:
            if field in fm:
                if bad := _is_suspicious(fm[field]):
                    findings["suspicious_link"].append({
                        "file": rel.as_posix(),
                        "field": field,
                        "value": bad,
                        "raw": fm[field] if isinstance(fm[field], str) else list(fm[field]),
                    })

        # 2. Filename ↔ frontmatter date mismatch
        fn_date = _filename_date(note.title)
        fm_date = _coerce_date(fm.get("date"))
        if fn_date and fm_date and fn_date != fm_date:
            findings["date_mismatch"].append({
                "file": rel.as_posix(),
                "filename_date": fn_date.isoformat(),
                "frontmatter_date": fm_date.isoformat(),
            })

        file_records.append({
            "rel": rel.as_posix(),
            "stem": note.title,
            "root": _stem_root(note.title),
            "fn_date": fn_date,
            "fm_date": fm_date,
            "has_date_suffix": _DATE_TAIL_RE.search(note.title) is not None,
            "fm_type": (str(fm.get("type") or fm.get("fileClass") or "")).strip().lower(),
        })

        # 4. content_filename_mismatch — opt-in via --check-content.
        # Restrict to 03_Companies/.. meeting notes (high-signal scope).
        # Person notes (02_Persons/..) are skipped — bodies usually summarize
        # the meeting topic, not the person's name. Event notes are also out
        # of scope (subject is event-name, not entity-mentioned-in-body).
        m = _PATTERN_SUBJECT_DATE_STAGE.match(note.title) if args.check_content else None
        if m and rel.parts and rel.parts[0] == "03_Companies":
            subject = m["subject"].strip()
            body = note.body or ""
            if subject and subject not in _CATEGORY_SUBJECTS and len(body) >= 400:
                # Build candidate strings: subject + aliases (companies +
                # persons buckets) + bare-name variant for titled persons.
                candidates: set[str] = {subject}
                candidates.update(all_variants(subject, kind="companies"))
                candidates.update(all_variants(subject, kind="persons"))
                if (short := _person_short(subject)) and len(short) >= 2:
                    candidates.add(short)
                # Cheap brand-shortname heuristic: for company names ending in
                # "로보틱스/시스템즈/테크놀로지스" or English suffixes, also try
                # the prefix (디든로보틱스 → 디든; LinkedIn → Link).
                for suf in ("로보틱스", "시스템즈", "테크놀로지스", "테크놀로지",
                            "솔루션", "네트웍스", "네트워크", "코리아"):
                    if subject.endswith(suf) and len(subject) > len(suf) + 1:
                        candidates.add(subject[: -len(suf)].rstrip())
                hits = mention_count(body, candidates)
                if hits == 0:
                    body_preview = body.strip().replace("\n", " ")[:160]
                    findings["content_filename_mismatch"].append({
                        "file": rel.as_posix(),
                        "subject": subject,
                        "checked": sorted(c for c in candidates if c),
                        "body_preview": body_preview,
                    })

    # 3. Duplicate detection (tight rule):
    # Group by root stem. Skip auto-generated roots. A group is flagged ONLY when
    # it contains both (a) ≥1 sibling with `_YYYYMMDD` suffix AND (b) ≥1 file with
    # NO suffix but a frontmatter `date` set. The undated-stem-with-date case is
    # the smoking gun for an event written twice. Profile/meeting pairs don't
    # trigger because profile notes have no `date` frontmatter.
    by_root: dict[str, list[dict]] = defaultdict(list)
    for r in file_records:
        if r["root"] in AUTO_ROOTS:
            continue
        by_root[r["root"]].append(r)

    for root, group in by_root.items():
        if len(group) < 2:
            continue
        suffixed = [r for r in group if r["has_date_suffix"]]
        # An undated sibling explicitly tagged as event/dashboard/index/profile is
        # a deliberate index note, not a duplicate of the dated meeting record.
        INDEX_TYPES = {"event", "dashboard", "index", "profile", "company", "person"}
        undated_with_fmdate = [
            r for r in group
            if not r["has_date_suffix"] and r["fm_date"] and r["fm_type"] not in INDEX_TYPES
        ]
        if not suffixed or not undated_with_fmdate:
            continue
        findings["possible_duplicate"].append({
            "root": root,
            "files": [
                {
                    "file": r["rel"],
                    "date": (r["fm_date"] or r["fn_date"]).isoformat()
                            if (r["fm_date"] or r["fn_date"]) else None,
                }
                for r in group
            ],
        })

    # ---- Optional auto-fix for date_mismatch ----
    fixed: list[dict] = []
    if args.fix_dates and findings["date_mismatch"]:
        for entry in findings["date_mismatch"]:
            target = vault_root / entry["file"]
            try:
                post = frontmatter.loads(target.read_text(encoding="utf-8"))
                post["date"] = entry["filename_date"]  # ISO string — Obsidian-friendly
                target.write_text(frontmatter.dumps(post), encoding="utf-8")
                fixed.append({"file": entry["file"], "from": entry["frontmatter_date"], "to": entry["filename_date"]})
            except Exception as e:
                fixed.append({"file": entry["file"], "error": repr(e)})

    # ---- Optional auto-fix for suspicious_link (folder-name-derived) ----
    fixed_susp: list[dict] = []
    if args.fix_suspicious and findings["suspicious_link"]:
        for entry in findings["suspicious_link"]:
            if entry["field"] not in {"company", "company_name"}:
                continue  # only auto-fix company; person/lead_investor are ambiguous
            parts = Path(entry["file"]).parts
            # Expect 03_Companies/<Antonio|KRUN>/<stage>/<X>/file.md  → company is parts[3]
            if len(parts) < 5 or parts[0] != "03_Companies" or parts[1] not in {"Antonio", "KRUN"}:
                fixed_susp.append({"file": entry["file"], "skipped": "not in 03_Companies/{Antonio|KRUN}/<stage>/<X>/"})
                continue
            new_value = f"[[{parts[3]}]]"
            target = vault_root / entry["file"]
            try:
                post = frontmatter.loads(target.read_text(encoding="utf-8"))
                old_value = post.get(entry["field"])
                post[entry["field"]] = new_value
                target.write_text(frontmatter.dumps(post), encoding="utf-8")
                fixed_susp.append({"file": entry["file"], "field": entry["field"],
                                   "from": str(old_value), "to": new_value})
            except Exception as e:
                fixed_susp.append({"file": entry["file"], "error": repr(e)})

    # ---- Output ----
    if args.json:
        print(json.dumps({"scanned": len(files), "findings": findings,
                          "fixed_dates": fixed, "fixed_suspicious": fixed_susp},
                         ensure_ascii=False, indent=2))
        return 0

    print("=" * 72)
    print(f"Vault: {vault_root}")
    print(f"Scanned: {len(files)} markdown files")
    print("=" * 72)

    for category in ("load_error", "suspicious_link", "date_mismatch",
                     "possible_duplicate", "content_filename_mismatch"):
        items = findings.get(category, [])
        print(f"\n## {category}  ({len(items)})")
        if not items:
            print("  (none)")
            continue
        for entry in items[: args.limit]:
            if category == "suspicious_link":
                print(f"  {entry['file']}")
                print(f"      {entry['field']}: {entry['raw']!r}  →  unwrap=‘{entry['value']}’")
            elif category == "date_mismatch":
                print(f"  {entry['file']}")
                print(f"      filename={entry['filename_date']}   frontmatter={entry['frontmatter_date']}")
            elif category == "possible_duplicate":
                print(f"  root: {entry['root']}")
                for f in entry["files"]:
                    print(f"      - {f['file']}   (date={f['date']})")
            elif category == "content_filename_mismatch":
                print(f"  {entry['file']}")
                print(f"      subject: {entry['subject']!r}  (also tried: {entry['checked']})")
                print(f"      body[:160]: {entry['body_preview']}")
            else:
                print(f"  {entry}")
        if len(items) > args.limit:
            print(f"  … {len(items) - args.limit} more (use --limit N)")

    if args.fix_dates:
        print(f"\n## fixed_dates  ({len(fixed)})")
        for f in fixed:
            if "error" in f:
                print(f"  ! {f['file']}  → {f['error']}")
            else:
                print(f"  ✓ {f['file']}   {f['from']} → {f['to']}")
    if args.fix_suspicious:
        print(f"\n## fixed_suspicious  ({len(fixed_susp)})")
        for f in fixed_susp:
            if "error" in f:
                print(f"  ! {f['file']}  → {f['error']}")
            elif "skipped" in f:
                print(f"  - {f['file']}  ({f['skipped']})")
            else:
                print(f"  ✓ {f['file']}  {f['field']}: {f['from']} → {f['to']}")

    print("\n" + "=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
