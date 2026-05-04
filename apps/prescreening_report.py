"""Generate a Korean VC 예비검토보고서 from a folder of IR materials.

Reads PDF / DOCX / PPTX / TXT / MD files in `--input`, extracts their text,
and asks Claude Opus 4.7 to write a markdown 예비검토보고서 (Pre-screening
Report) in the user's familiar 12-section structure. Saves to `--output`
as `{회사명}_{YYYYMMDD}_예비검토보고서.md` with a YAML metadata header.

This is a one-shot tool — it does NOT touch the LanceDB index or the
Obsidian vault. It is independent of the RAG chat pipeline; we only share
the Anthropic SDK setup and `.env` (`ANTHROPIC_API_KEY`, `ZDR_ENABLED`).

Run (Windows PowerShell example):
    uv sync --extra report
    uv run python -m apps.prescreening_report \
        --input  "C:\\Users\\anton\\Documents\\Claude AI_Personal\\Prescreening_Report\\검토 IR 자료" \
        --company 대우컴프레셔 \
        --output "C:\\Users\\anton\\Documents\\Claude AI_Personal\\Prescreening_Report\\output"

Notes:
- Filename filter: by default we pick files whose name contains the company
  string (case-insensitive). Use `--include-all` to feed every file in
  `--input` to the model regardless of filename.
- Per-file text is capped at ~30k chars to keep total prompt within the
  context window. Override with `--per-file-cap`.
- Pass an existing report as `--template` to mimic its structure / tone.
- Streaming output is on by default; use `--no-stream` to buffer instead.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

# Make `rag` importable when launched via `python -m apps.prescreening_report`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anthropic import Anthropic

from rag.config import get_settings


# --- System prompt --------------------------------------------------------
SYSTEM_PROMPT = """당신은 한국 벤처캐피탈(VC) 케이런(Krun Ventures)의 시니어 심사역입니다. 사용자가 제공하는 한 회사의 IR 자료(피치 덱, 회사 소개서, 재무 자료, 보도자료 등)를 종합 분석하여 케이런 내부에서 사용할 **예비검토보고서(Pre-screening Report)** 마크다운 초안을 작성합니다.

# 역할 맥락
- 케이런은 1인 운영 체제이며, 본 보고서는 1차 DD 진행 여부를 판단하기 위한 **초기 검토 단계** 산출물입니다.
- 당신의 출력은 그대로 케이런의 검토 단계 노트(03_Companies/Antonio/검토단계/{회사명}/{회사명}_{YYYYMMDD}_예비검토보고서.md)에 저장됩니다.

# 절대 규칙
1. **IR 자료에 명시된 사실만 사용합니다.** 자료에 없는 매출 수치·시장 규모·경쟁사 정보·임원 이력을 만들어내지 마십시오.
2. **누락·불명확 항목은 명시적으로 표기**합니다: `[자료 미기재]`, `[추가 확인 필요]`, `[수치 명시 안 됨]`.
3. **회사명·금액·일자·계약 조건은 IR 자료의 표기 그대로** 사용합니다 (의역·추측·반올림 금지). 한자/영문/한글 표기가 자료에 같이 있으면 자료의 우선 표기를 따릅니다.
4. **결론을 강제하지 않습니다.** "투자 추천" 같은 결정은 §12 추천 결론 섹션에서만, 명확한 근거가 있을 때만. 근거가 부족하면 "보류 — [추가 확인 필요 항목]"으로 답합니다.
5. 출력은 **순수 한국어 마크다운**. 코드 펜스(```)·메타 코멘트·서론 인사말 없이 첫 줄부터 보고서 본문.

# 인용 규칙
- 사실 진술 뒤에 자료 출처를 `[파일명, p.N]` 형식으로 인용합니다. 페이지가 없는 자료는 `[파일명]`만.
- 한 문장에 여러 자료 근거가 있으면 `…확인됨[A.pdf, p.3][B.docx]`처럼 나열.
- 자료가 모호하면 인용 없이 단정하지 말고 `[자료 미기재]` 표기.

# 출력 구조 (이 순서대로, 모든 섹션 헤더 그대로)

```
# 예비검토보고서 — {회사명}

**작성일**: {YYYY-MM-DD}
**검토 단계**: 예비검토 (Pre-screening)
**작성자**: 케이런 VC (AI 초안 — Antonio 검수 필요)
**참고 자료**: {제공된 IR 파일 목록}

---

## 1. 보고서 개요
검토 목적, 회사 한 줄 소개, 자료 출처 요약 (3-5문장).

## 2. 회사 기본 정보
| 항목 | 내용 |
|---|---|
| 회사명 (한/영) | … |
| 설립연도 | … |
| 소재지 | … |
| 대표이사 | … |
| 임직원 수 | … |
| 사업 영역 | … |
| 자본금 / 누적 투자 | … |
| 현재 단계 (Seed/Series A/...) | … |

## 3. 사업 모델 및 제품/서비스
- **핵심 가치 제안 (Value Proposition)**: …
- **수익 모델**: B2B/B2C/B2G, 구독/판매/라이선스 등
- **주요 제품·서비스 라인업**: …
- **핵심 기술 / IP**: 특허, 인증, 기술 차별화 요소

## 4. 시장 분석
- **타겟 시장 정의**: TAM/SAM/SOM (자료 있을 경우)
- **시장 규모 / 성장률**: …
- **시장 동향 및 트렌드**: …

## 5. 경쟁 환경
- **주요 경쟁사**: 자료에 명시된 회사만
- **차별화 포인트 / 해자 (Moat)**: …
- **진입 장벽**: …

## 6. 재무 현황
연도별 매출·영업이익·순이익을 표 형식으로. 자료에 없으면 행을 비우지 말고 `[자료 미기재]`로 표기.
| 연도 | 매출 | 영업이익 | 순이익 |
|---|---|---|---|
| … | … | … | … |

- **주요 KPI**: ARR, MAU, 거래액 등 자료에 있는 것
- **자금 조달 이력**: …
- **현재 캐시 런웨이**: …

## 7. 경영진 및 조직
- **대표이사 이력**: 자료에 있는 만큼만
- **핵심 인력**: CTO/CFO/COO 등
- **조직 구조 / 인원**: …
- **채용 계획**: …

## 8. 투자 포인트 (Investment Highlights)
3-5개의 핵심 강점을 각 1-2문단으로. 정량 지표가 있으면 반드시 인용.
1. **[강점 1]** — …
2. **[강점 2]** — …
3. **[강점 3]** — …

## 9. 리스크 요인
3-5개. 시장·경쟁·재무·운영·규제·인력 카테고리 표시.
1. **[리스크 1] (카테고리)** — …
2. **[리스크 2] (카테고리)** — …
3. **[리스크 3] (카테고리)** — …

## 10. 평가 및 종합 의견
3-5문장. 케이런 투자 관점(7호 펀드 / 소부장2호 등 자료에 명시된 펀드와 적합성)에서 회사를 어떻게 보는지.

## 11. 추가 검토 필요 사항 (1차 DD 시 확인)
- [ ] 항목 1
- [ ] 항목 2
- [ ] 항목 3
…

## 12. 추천 결론
**한 가지 선택**: ✅ 진행 / 🟡 조건부 진행 / ⏸ 보류 / ❌ Pass
**사유** (2-3문장): …
**다음 단계 제안**: …
```

# 분량
- 전체 1500-3000자(한국어 기준). 자료가 빈약하면 짧게 끝내고 §11에 부족분을 명시.
- 표·리스트는 의미가 있을 때만 사용. 재무·경영진·경쟁사 비교는 표가 자연스러움.

# 문체
- 평어/존댓말 X — 보고서체(개조식 + 평서문 혼합).
- "···한다" / "···로 보임" / "···로 추정됨" 같은 단정/추정의 구분을 분명히.
"""


# --- File extraction ------------------------------------------------------
def _extract_pdf(path: Path) -> str:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "PyMuPDF is required. Install with: uv sync --extra report"
        ) from e
    parts: list[str] = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            try:
                t = (page.get_text("text") or "").strip()
            except Exception:
                t = ""
            if t:
                parts.append(f"[p.{i}]\n{t}")
    return "\n\n".join(parts)


def _extract_docx(path: Path) -> str:
    try:
        from docx import Document  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "python-docx is required. Install with: uv sync --extra report"
        ) from e
    doc = Document(str(path))
    parts: list[str] = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            parts.append(text)
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_pptx(path: Path) -> str:
    try:
        from pptx import Presentation  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "python-pptx is required. Install with: uv sync --extra report"
        ) from e
    prs = Presentation(str(path))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        slide_parts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs).strip()
                    if line:
                        slide_parts.append(line)
            if getattr(shape, "has_table", False):
                table = shape.table
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        slide_parts.append(" | ".join(cells))
        if slide_parts:
            parts.append(f"[slide {i}]\n" + "\n".join(slide_parts))
    return "\n\n".join(parts)


_EXTRACTORS: dict[str, callable] = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".pptx": _extract_pptx,
}


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _EXTRACTORS:
        return _EXTRACTORS[suffix](path)
    if suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    raise ValueError(f"Unsupported file type: {suffix} ({path.name})")


_SUPPORTED_SUFFIXES = (".pdf", ".docx", ".pptx", ".txt", ".md")


def find_company_files(input_dir: Path, company: str, include_all: bool) -> list[Path]:
    files: list[Path] = []
    for ext in _SUPPORTED_SUFFIXES:
        files.extend(input_dir.rglob(f"*{ext}"))
    files = [f for f in files if not f.name.startswith("~$")]  # Office lock files

    if include_all:
        return sorted(files)

    needle = company.lower()
    matched = [f for f in files if needle in f.name.lower()]
    return sorted(matched)


# --- Prompt assembly ------------------------------------------------------
def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n\n[...자료가 길어 {len(text) - cap:,}자 생략됨...]"


def build_user_message(
    company: str,
    source_files: list[Path],
    extracts: list[str],
    *,
    per_file_cap: int,
    template_text: str | None = None,
) -> str:
    today_iso = date.today().isoformat()
    file_list_md = "\n".join(f"- `{p.name}` ({p.stat().st_size // 1024} KB)" for p in source_files)

    parts: list[str] = []
    for path, text in zip(source_files, extracts):
        truncated = _truncate(text, per_file_cap)
        parts.append(f"## 📄 `{path.name}`\n\n{truncated}")
    materials_block = "\n\n---\n\n".join(parts)

    template_block = ""
    if template_text:
        template_block = (
            "\n\n[참고 — 기존 보고서 스타일 (구조와 어조만 참고; 사실 인용 금지)]\n"
            f"```markdown\n{_truncate(template_text, 8000)}\n```\n"
        )

    return (
        f"[메타]\n"
        f"- 회사명: {company}\n"
        f"- 작성일: {today_iso}\n"
        f"- IR 자료 ({len(source_files)}개):\n{file_list_md}\n"
        f"\n[IR 자료 본문]\n\n{materials_block}\n"
        f"{template_block}"
        f"\n[지시]\n"
        f"위 자료만을 근거로 시스템 프롬프트의 12개 섹션 구조를 그대로 사용하여 "
        f"`{company}` 예비검토보고서를 작성하세요. 첫 줄은 정확히 "
        f"`# 예비검토보고서 — {company}` 입니다. "
        f"코드 펜스로 감싸지 말고 마크다운 본문을 그대로 출력하세요."
    )


# --- Generation -----------------------------------------------------------
def generate_report(
    *,
    company: str,
    source_files: list[Path],
    extracts: list[str],
    template_text: str | None,
    per_file_cap: int,
    model: str,
    max_tokens: int,
    show_progress: bool,
) -> tuple[str, dict]:
    s = get_settings()
    if not s.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY missing. Add it to .env or the environment."
        )

    client = Anthropic(api_key=s.anthropic_api_key)

    extra_headers: dict[str, str] = {}
    if s.generation.zdr_enabled:
        extra_headers["anthropic-zero-retention-window"] = "0"

    user_msg = build_user_message(
        company=company,
        source_files=source_files,
        extracts=extracts,
        per_file_cap=per_file_cap,
        template_text=template_text,
    )

    # Opus 4.7 specifics: adaptive thinking only, no temperature/top_p/top_k.
    create_kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": user_msg}],
    }
    if extra_headers:
        create_kwargs["extra_headers"] = extra_headers
    if model.startswith("claude-opus-4-7"):
        create_kwargs["thinking"] = {"type": "adaptive"}
        create_kwargs["output_config"] = {"effort": "high"}

    chunks: list[str] = []
    with client.messages.stream(**create_kwargs) as stream:
        for text in stream.text_stream:
            chunks.append(text)
            if show_progress:
                sys.stdout.write(text)
                sys.stdout.flush()
        final = stream.get_final_message()

    usage = {
        "input_tokens": getattr(final.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(final.usage, "output_tokens", 0) or 0,
        "cache_creation_tokens": getattr(final.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_tokens": getattr(final.usage, "cache_read_input_tokens", 0) or 0,
        "stop_reason": final.stop_reason or "",
    }
    return "".join(chunks), usage


# --- Output ---------------------------------------------------------------
def _yaml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def write_report(
    *,
    output_dir: Path,
    company: str,
    source_files: list[Path],
    body_md: str,
    usage: dict,
    model: str,
) -> Path:
    today = date.today().strftime("%Y%m%d")
    target = output_dir / f"{company}_{today}_예비검토보고서.md"

    files_yaml = "\n".join(f'  - "{_yaml_escape(f.name)}"' for f in source_files) or "  []"
    front = (
        "---\n"
        "type: prescreening_report\n"
        f"company: \"{_yaml_escape(company)}\"\n"
        "generated_by: krun-rag-chatbot/apps/prescreening_report\n"
        f"generated_at: {date.today().isoformat()}\n"
        f"model: {model}\n"
        "source_files:\n"
        f"{files_yaml}\n"
        "usage:\n"
        f"  input_tokens: {usage['input_tokens']}\n"
        f"  output_tokens: {usage['output_tokens']}\n"
        f"  cache_creation_tokens: {usage['cache_creation_tokens']}\n"
        f"  cache_read_tokens: {usage['cache_read_tokens']}\n"
        "---\n\n"
    )
    target.write_text(front + body_md.rstrip() + "\n", encoding="utf-8")
    return target


# --- CLI ------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a Korean VC 예비검토보고서 from IR materials in a folder.",
    )
    parser.add_argument("--input", required=True, help="Folder containing IR materials")
    parser.add_argument("--company", required=True, help="Company name (e.g., 대우컴프레셔)")
    parser.add_argument("--output", required=True, help="Output folder for the report")
    parser.add_argument(
        "--include-all",
        action="store_true",
        help="Use ALL files in --input (default: filter by company name in filename)",
    )
    parser.add_argument(
        "--template",
        default=None,
        help="Path to an existing 예비검토보고서.md to use as style reference (structure/tone only)",
    )
    parser.add_argument("--model", default="claude-opus-4-7", help="Claude model")
    parser.add_argument(
        "--max-tokens", type=int, default=8000, help="Max output tokens (default 8000)"
    )
    parser.add_argument(
        "--per-file-cap",
        type=int,
        default=30000,
        help="Max chars per IR file fed to the model (default 30000)",
    )
    parser.add_argument("--no-stream", action="store_true", help="Buffer instead of streaming")
    args = parser.parse_args(argv)

    input_dir = Path(args.input).expanduser()
    output_dir = Path(args.output).expanduser()
    template_path = Path(args.template).expanduser() if args.template else None

    print("=" * 70)
    print("KRUN — 예비검토보고서 생성기")
    print("=" * 70)
    print(f"  Input    : {input_dir}")
    print(f"  Company  : {args.company}")
    print(f"  Output   : {output_dir}")
    print(f"  Model    : {args.model}")
    if template_path:
        print(f"  Template : {template_path}")

    if not input_dir.exists():
        print(f"\n[FAIL] Input folder not found: {input_dir}", file=sys.stderr)
        return 1
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Discover files
    print("\n[1/4] Scanning input folder...")
    source_files = find_company_files(input_dir, args.company, include_all=args.include_all)
    if not source_files:
        print(
            f"[FAIL] No files matching '{args.company}' in {input_dir}.\n"
            f"       (Looked for *.pdf, *.docx, *.pptx, *.txt, *.md whose name contains the company string.)\n"
            f"       Try --include-all to feed every file in the folder regardless of name.",
            file=sys.stderr,
        )
        return 1
    print(f"      Found {len(source_files)} files:")
    for f in source_files:
        print(f"      - {f.name} ({f.stat().st_size // 1024} KB)")

    # 2. Extract text
    print("\n[2/4] Extracting text...")
    extracts: list[str] = []
    total_chars = 0
    for f in source_files:
        try:
            text = extract_text(f)
        except Exception as e:
            print(f"      ✗ {f.name}: {e!r}")
            extracts.append(f"[추출 실패: {e!r}]")
            continue
        extracts.append(text)
        total_chars += len(text)
        print(f"      ✓ {f.name}: {len(text):,} chars")
    print(f"      Total: {total_chars:,} chars (~{total_chars // 2:,} tokens estimated)")

    # 3. Optional template
    template_text = None
    if template_path:
        if not template_path.exists():
            print(f"[FAIL] Template not found: {template_path}", file=sys.stderr)
            return 1
        template_text = template_path.read_text(encoding="utf-8", errors="replace")
        print(f"      Template: {len(template_text):,} chars loaded")

    # 4. Generate
    print(f"\n[3/4] Generating report with {args.model}...")
    print("-" * 70)
    started = time.perf_counter()
    try:
        body_md, usage = generate_report(
            company=args.company,
            source_files=source_files,
            extracts=extracts,
            template_text=template_text,
            per_file_cap=args.per_file_cap,
            model=args.model,
            max_tokens=args.max_tokens,
            show_progress=not args.no_stream,
        )
    except Exception as e:
        print(f"\n[FAIL] Generation error: {e!r}", file=sys.stderr)
        return 1
    elapsed = time.perf_counter() - started
    print("\n" + "-" * 70)
    print(
        f"      Done in {elapsed:.1f}s — "
        f"in={usage['input_tokens']} out={usage['output_tokens']} "
        f"cache_w={usage['cache_creation_tokens']} cache_r={usage['cache_read_tokens']} "
        f"stop={usage['stop_reason']}"
    )

    # 5. Write
    print("\n[4/4] Writing report...")
    target = write_report(
        output_dir=output_dir,
        company=args.company,
        source_files=source_files,
        body_md=body_md,
        usage=usage,
        model=args.model,
    )
    print(f"      ✓ {target}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
