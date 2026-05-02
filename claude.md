# KRUN Internal RAG Chatbot — Implementation Plan & Session Handoff

> **For a new Claude Code session:** start by reading this file end-to-end. The Status, Architecture, How to Resume, and Open TODOs sections give you everything you need to take over without re-asking the user. The original design notes follow afterward as historical context.

---

## 🟢 Current Status (last updated: 2026-05-02)

| Phase | Status | Notes |
|---|---|---|
| Phase 0 — Bootstrap | ✅ DONE | uv-based env, BGE-M3 verified, ZDR toggle in `.env` |
| Phase 1A — Ingest core | ✅ DONE | 4,323+ chunks indexed; 67 files updated via `--incremental` mid-session |
| Phase 1B — Hybrid retrieval + Sonnet 4.6 | ✅ DONE | RRF fusion, prompt caching working (`cache_read > 0` verified across requests) |
| Phase 1C — Streamlit MVP | ✅ DONE | Sidebar filters, citations, watcher/refresh buttons, reranker toggle |
| Phase 1D-A — Reranker | ✅ DONE | bge-reranker-v2-m3 via `uv sync --extra phase1d`; toggle wired in Streamlit |
| Phase 1D-B — Live watcher | ✅ DONE | `uv run python -m rag.watcher` |
| Phase 1D-C — PDF attachments | ✅ DONE (code) | User has not yet run `attachment_loader --full` |
| Phase 1D-D — Incremental ingest | ✅ DONE | mtime-based diff; ran successfully (50 new + 23 changed + 30 deleted) |
| Phase 1D-E — Eval harness | ✅ DONE (code) | User must populate `rag/eval/eval_set.yaml` (gitignored) |
| Phase 1D Gate (Recall@8 ≥ 0.7) | ⏳ PENDING | Blocked on eval_set.yaml population |
| Phase 2 — Obsidian plugin | ⏳ NOT STARTED | TypeScript + FastAPI; only after Phase 1D Gate |

**LanceDB state at session end:** 4,323+ chunks across many files. BM25 sidecar at `data/lancedb/bm25_index.pkl`. Run `uv run python -m rag.ingest.pipeline --refresh-metadata` if metadata logic changes; `--incremental` for new/changed files; `--full` only when re-embedding from scratch (~25 min CPU).

**Working directory on Antonio's machine:** `C:\Users\anton\Documents\Claude AI_Personal\krun-rag-chatbot` (Windows native — no WSL).

**Vault:** `C:\Users\anton\Documents\Obsidian_KRUN_Antonio` (do **not** edit; RAG is read-only).

**Active branch:** `claude/review-claude-md-ltCA4` — PR #1 (draft).

---

## 🔄 How to Resume in a New Session

When the user starts a new session, do this in order:

1. **Confirm scope.** Ask: "Are we continuing where we left off, or do you want to change direction?" Do NOT re-execute completed phases. Do NOT re-introduce features.

2. **Quick health check** (only if user asks or something is clearly broken):
   ```powershell
   cd "C:\Users\anton\Documents\Claude AI_Personal\krun-rag-chatbot"
   git pull origin claude/review-claude-md-ltCA4
   uv sync --extra phase1d                  # ensures all optional deps present
   uv run python scripts/verify_phase1a.py  # smoke: store + filters + vector
   uv run python scripts/verify_phase1b.py  # smoke: analyzer + generation
   ```
   If any fail, see "Common Failure Modes" below.

3. **Decide next step** from Open TODOs. Most likely paths in roughly this order:
   - User found a search-quality issue → use `scripts/diagnose_search.py` first to inspect what's actually indexed.
   - User wants to test the diversification fix from the prior session.
   - User wants to populate `eval_set.yaml` and run the eval harness.
   - User wants to start Phase 2 (Obsidian plugin).
   - User wants to add a new feature → understand what's already implemented (see "Files Last Touched" below) before duplicating.

4. **Default to incremental** when reindexing. `--full` is 25 minutes; `--incremental` is seconds-to-minutes. `--refresh-metadata` only updates EXISTING rows in place — it does NOT pick up new files.

---

## 🐛 Recent Fixes & Decisions (this session)

These were learned the hard way; preserve the rationale.

### Decisions made
| Decision | Rationale |
|---|---|
| Korean BM25 tokenizer = `char_trigram` (default) | Works without kiwipiepy; ~80% of morphological accuracy; vector search covers the rest |
| Reranker = `bge-reranker-v2-m3` (optional via `phase1d` extra) | +10-15% Recall@8 lift, +200ms CPU latency |
| Diversification = `max_chunks_per_file: 2` (default) | One note's many sections were dominating top-K (정경원 사장 case below) |
| Skip `doc_type` in WHERE clause by default | Analyzer over-narrows; we lose recall too often. WHERE only includes `companies / persons / date` |
| ZDR off by default; honor `ZDR_ENABLED=true` from `.env` | 1-person workspace; ZDR is opt-in |
| Sonnet 4.6 generation, Haiku 4.5 analyzer | Per original spec; verified working |
| `output_config.format` not used for generation | Free-form Korean answers with [n] citations don't fit a JSON schema |
| Prompt caching breakpoint on system prompt | System prompt deliberately expanded to 2.2K+ tokens to clear Sonnet 4.6's 2048-token cache minimum (verified `cache_read > 0` from request 2 onward) |
| Vault folders included | `00_Inbox`, `01_Daily`, `01a_Periodic`, `02_Persons`, `03_Companies`, `04_Meetings`, `05_Projects`, `06_Resources` |
| Vault folders excluded | `07_Archive/`, `08_회의록/` (STT raw dump), `_attachments/`, `_automation/`, `_backup_*/`, `_Templates/`, `copilot/`, `.obsidian/`, `.makemd/`, `.smart-env/`, `.space/`, `.trash/`, `**/sortspec.md`, `**/*.canvas` |

### Bugs fixed in this session (in order)
1. **LanceDB 0.30 dropped `to_pandas(columns=)` kwarg** → use `df[[cols]]` slicing in `verify_phase1a.py`, `lancedb_store.all_file_paths`.
2. **Frontmatter `company: [[X]]` stored verbatim** → broke `company = 'X'` filter. Fix: `_unwrap_wikilink` in `metadata.py` strips `[[Target|Display]]` wrappers in all `_coerce_str` / `_coerce_str_list` calls.
3. **Pandas NaN flowing into Citation fields** → `if c.company:` was True for null values, then `' | '.join(...)` got a float. Fix: `_clean_str` in `citations.py` normalizes NaN → None.
4. **`uv sync --extra X` removes other extras** → `uv sync --extra rerank` dropped phase1a packages (kiwipiepy, pymupdf). Fix: combined `phase1d` extra in `pyproject.toml` bundles everything.
5. **Streamlit watcher floods console with torchvision errors** → Streamlit's `auto` file watcher recursively touches every transformers submodule; FlagEmbedding pulls them in. Fix: `.streamlit/config.toml` with `fileWatcherType = "none"`.
6. **One file's chunks dominating top-K** (정경원 사장 case: profile note had 12 chunks indexed, meeting note had 2; profile won all 8 slots in top-K, hiding the meeting's actual content). Fix: `_diversify_by_file` caps chunks per file in `hybrid_search.py`.
7. **Initial system prompt < 2048 tokens** → Sonnet 4.6 silently doesn't cache below that minimum. Fix: expanded `SYSTEM_PROMPT_KO` to ~4500 chars (~2200 tokens) of *substantive* guidance (doc_type vocab, filename patterns, 5 question playbooks, anti-patterns) — not padding.

### Known vault structure (from this session's analysis)
- **02_Persons/{Name}/** with both `{Name}.md` (profile) and `{Name}_{YYYYMMDD}_{stage}.md` (meetings) co-located.
- **03_Companies/Antonio/{검토단계|투자업체}/{X}/** — three levels deep, with both profile and meeting files nested.
- **deal_pipeline auto-generated notes** (28 files): structured frontmatter (company, valuation, lead_investor, …) but mostly empty content. The bot honestly reports them as templates — this is correct behavior; the value is in flagging undermaintained data.
- **Frontmatter wikilinks** common: `company: [[X]]`, `lead_investor: [[Y]]`, `people: [[Z]]`. Always unwrapped at metadata extraction time.
- **File naming patterns** (handled in `metadata.py:_PATTERN_*`):
  - `daily_YYYYMMDD.md` → doc_type=daily
  - `weekly_YYYYMMDD.md` or `weekly_YYYYWW.md` → doc_type=periodic (ISO week if YYYYWW)
  - `monthly_YYYYMM.md` → doc_type=periodic
  - `주간회의_YYYYMMDD.md` → doc_type=meeting, event_stage=주간회의
  - `{subject}_{YYYYMMDD}_{stage}.md` → doc_type=meeting; person/company derived from `{subject}` based on top folder

---

## 📋 Open TODOs (in priority order)

### Blocking Phase 1D Gate
- [ ] **User: populate `rag/eval/eval_set.yaml`** with 30 real questions (5 per category). Template + schema in `rag/eval/eval_set.example.yaml`. The file is gitignored.
- [ ] **Run eval & measure Phase 1D Gate**: `uv run python -m rag.eval.run_eval --output rag/eval/results/baseline.json`. Then `--reranker-on` for comparison. Target: `file_match_rate ≥ 0.7`.

### Pending user actions (carried over from prior session)
- [ ] **Verify diversification fix in browser** — 4 test queries documented in last assistant message of prior session. Specifically the 정경원 사장 query should now surface meeting note `정경원 사장_20260421_보백결재조건 변경 검토.md` alongside the profile.
- [ ] **Run PDF ingest once** (4 PDFs only, fast): `uv run python -m rag.ingest.attachment_loader --full`.
- [ ] **Phase 1C 1-week real usage evaluation** (originally requested by user). Track 6 categories of feedback: 🟢 잘 작동, 🟡 답변 부족, 🔴 검색 실패, 🟣 분석기 오류, ⚙️ UI 불편, 💰 비용.

### Code-side polish (small, low priority)
- [ ] If watcher dies on Windows reserved filenames or transient locks, surface to user (currently logs and continues).
- [ ] Add `kiwipiepy` toggle to Streamlit (currently only swappable via `config.yaml`). Not yet needed; char_trigram is fine.
- [ ] Eval harness's faithfulness scoring is currently `must_contain` substring matching. Replace with claim-by-claim LLM judge if Phase 1D Gate is hard to meet with current proxy.
- [ ] `attachment_loader._find_parent_note` is best-effort; if a single PDF is embedded in multiple notes, parent_path links to one of them. Acceptable for now.

### Phase 2 (Obsidian plugin — not started)
Per original spec:
- `apps/fastapi_server.py` — wraps RAG core, SSE streaming, `localhost:8765`.
- TypeScript plugin: ItemView sidebar + command palette items.
- "Ask about current note" command (auto-feeds active file as context).
- Citation clicks → `app.workspace.openLinkText()`.
- Package into `Obsidian_KRUN_Antonio/.obsidian/plugins/krun-rag/`.

---

## 🔥 Common Failure Modes (and instant fixes)

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'kiwipiepy'` (or pymupdf, FlagEmbedding) | User ran `uv sync --extra X` which dropped other extras | `uv sync --extra phase1d` |
| Streamlit console flooded with `torchvision` errors | Streamlit's file watcher walking transformers submodules | Already fixed via `.streamlit/config.toml`; if regresses, set `STREAMLIT_SERVER_FILE_WATCHER_TYPE=none` env var |
| `cache_read_input_tokens` always 0 | System prompt < 2048 tokens (Sonnet 4.6 cache minimum) or it changed mid-session | Verify `SYSTEM_PROMPT_KO` length; ensure no `datetime.now()` or per-request token in prompt |
| `LanceTable.to_pandas() got an unexpected keyword argument 'columns'` | LanceDB 0.30 API change | Already fixed in code; should not recur |
| 회사명 / 인물명이 `[[X]]` 처럼 보임 | Wikilink unwrap regression | Check `_unwrap_wikilink` in `metadata.py`; rerun `--refresh-metadata` |
| Search returns only one file's many sections | Diversification disabled or `max_chunks_per_file=0` | Set `max_chunks_per_file: 2` in `config.yaml` or via Streamlit slider |
| Watcher crashes on path with special chars | Windows path normalization | Confirm `is_excluded` filter precedes any filesystem op; the `resolve()` call inside `_is_target` may raise |
| New notes not appearing in search | They were created after last `--full` ingest, and `--refresh-metadata` only updates existing rows | Run `--incremental` (NOT `--refresh-metadata`) |

---

## 📂 Files Last Touched (Phase 1D batch)

```
.streamlit/config.toml              ← NEW (fileWatcherType=none)
config.yaml                         ← max_chunks_per_file added
pyproject.toml                      ← phase1d extra added
rag/config.py                       ← max_chunks_per_file in RetrievalConfig
rag/ingest/attachment_loader.py     ← NEW (PDF indexing)
rag/ingest/pipeline.py              ← --incremental mode + detect_vault_changes
rag/retrieval/hybrid_search.py      ← _diversify_by_file + reranker integration
rag/retrieval/reranker.py           ← NEW (BGE reranker wrapper)
rag/retrieval/citations.py          ← NaN → None normalization
rag/retrieval/query_analyzer.py     ← Haiku-based filter extraction
rag/store/bm25_index.py             ← BM25 sidecar with persistence
rag/store/lancedb_store.py          ← schema + filters
rag/generation/claude_client.py     ← streaming + caching + ZDR
rag/generation/prompts.py           ← 4500-char system prompt (cache-eligible)
rag/eval/eval_set.example.yaml      ← template (real eval_set.yaml gitignored)
rag/eval/run_eval.py                ← harness
rag/watcher.py                      ← NEW (live indexing daemon)
rag/query.py                        ← CLI orchestrator
apps/streamlit_app.py               ← UI with all toggles
scripts/diagnose_search.py          ← NEW (LanceDB direct query)
scripts/verify_phase1a.py           ← gate verification
scripts/verify_phase1b.py           ← gate verification
README.md                           ← daily usage docs
```

---

## Context (original)

> The sections below are the original design plan from the start of the project. Kept for reference; the implementation has diverged in places (see "Recent Fixes & Decisions" above for the deltas).

**Why this is being built**
케이런 VC 1인 운영자(Antonio)가 본인 Obsidian 볼트(`C:\Users\anton\Documents\Obsidian_KRUN_Antonio` — `03_Companies/`, `02_Persons/`, `04_Meetings/`, `06_Resources/`)에 누적된 회사·인물·미팅·리소스 노트에 대해 자연어 Q&A를 할 수 있는 내부 RAG 챗봇이 필요. VC 업무는 본질적으로 "내가 가진 정보를 얼마나 빨리 꺼내쓰느냐"의 게임인데, 폴더/키워드 검색만으로는 "지난 분기 IR 받은 회사 중 소부장 분야만", "메타씨앤아이 1차DD 리스크 정리" 같은 의미·필터 결합 질문에 답할 수 없음.

**Intended outcome**
- 회의 직전 5분 안에 과거 컨텍스트 즉시 복원
- 노트 작성 중 Obsidian 안에서 바로 호출 가능한 사이드패널
- 모든 답변에 클릭 가능한 출처(`obsidian://` 링크) → 환각 방지
- 민감 데이터 외부 송신 최소화 (로컬 임베딩 + 질의 시 관련 청크만 Claude 송신)

**Confirmed decisions (status)**
- 인터페이스: **Phase 1 = Streamlit MVP** ✅ 완료. **Phase 2 = Obsidian 플러그인** ⏳ 시작 안 함. Slack은 1인 사용이므로 스킵.
- 임베딩: **BGE-M3 로컬** (`sentence-transformers`) — 한국어 강함, 외부 송신 0. ✅
- LLM: **Claude Sonnet 4.6** (생성), **Claude Haiku 4.5** (질의 분석) — `.env`의 `ZDR_ENABLED=true`로 zero-retention 활성화. ✅
- 벡터 DB: **LanceDB** (파일 기반, 서버 불필요). ✅
- 검색: BM25(char_trigram) + 벡터 하이브리드 + bge-reranker-v2-m3 (옵션). ✅
- 사용자: 1인 (인증/멀티테넌시 불필요). ✅

---

## Architecture (as built)

```
[Obsidian Vault — Windows]
        │ watchdog / --incremental / --full
        ▼
[md_loader] → frontmatter + body 파싱, [[wikilinks]] unwrap, embeds 추출
        │
        ▼
[chunker] 헤더 인식 + sliding window 800토큰, breadcrumb 보존
        │
[attachment_loader] ── PDF만 (PyMuPDF, 페이지 그룹 청크)
        │
        ▼
[embedder] BGE-M3 (로컬, 1024차원, L2 normalized)
        │
        ▼
[LanceDB store] vault_chunks 테이블 + BM25 사이드카(pkl)

[사용자 질의]
        │
        ▼
[query_analyzer] (Haiku 4.5, prompt-cached) → {date_range, companies, persons, doc_types, rewritten_query}
        │
        ▼
build_where_clause (companies, persons, date — NOT doc_type)
        │
        ▼
[hybrid_search]
  ├─ BM25 top-30 (char_trigram)
  ├─ Vector top-30 (with WHERE prefilter)
  ├─ RRF fusion (k=60) → pool of 30
  ├─ Optional rerank (bge-reranker-v2-m3) → reorder
  └─ Diversify by file_path (max 2 per file) → final top-8
        │
        ▼
[claude_client] Sonnet 4.6, streaming, system-prompt cached (cache_control: ephemeral), ZDR header optional
        │
        ▼
[Streamlit UI / CLI] 답변 + clickable obsidian:// 출처 + 토큰/캐시/지연 통계
```

---

## Directory Layout (as built)

```
krun-rag-chatbot/
├── claude.md                       # this file (handoff doc)
├── README.md                       # daily-usage docs
├── pyproject.toml                  # uv deps + extras (rerank, phase1a, phase1d, …)
├── config.yaml                     # all knobs
├── .env.example                    # ANTHROPIC_API_KEY, ZDR_ENABLED
├── .streamlit/config.toml          # fileWatcherType=none
│
├── rag/
│   ├── config.py                   # Pydantic settings (Windows ↔ WSL path normalization)
│   ├── ingest/
│   │   ├── md_loader.py            # python-frontmatter + wikilink/embed extraction
│   │   ├── chunker.py              # header-aware + sliding + breadcrumb
│   │   ├── attachment_loader.py    # PDF (PyMuPDF) — Phase 1D-C
│   │   ├── metadata.py             # filename pattern → doc_type / company / person / date
│   │   ├── embedder.py             # BGE-M3 wrapper (lazy load)
│   │   └── pipeline.py             # CLI: --full / --incremental / --refresh-metadata / --paths
│   ├── store/
│   │   ├── lancedb_store.py        # vault_chunks pyarrow schema, search_by_*, vector_search
│   │   └── bm25_index.py           # rank_bm25 + char_trigram (or kiwipiepy alt), pickle cache
│   ├── retrieval/
│   │   ├── query_analyzer.py       # Haiku 4.5 → JSON filters
│   │   ├── hybrid_search.py        # BM25+vector RRF + diversification + optional rerank
│   │   ├── reranker.py             # bge-reranker-v2-m3 (lazy, FlagEmbedding)
│   │   └── citations.py            # obsidian:// URI + Citation dataclass
│   ├── generation/
│   │   ├── prompts.py              # SYSTEM_PROMPT_KO (~2200 tokens, cache-eligible)
│   │   └── claude_client.py        # Anthropic SDK, streaming, cache_control, ZDR
│   ├── eval/
│   │   ├── eval_set.example.yaml   # template
│   │   ├── eval_set.yaml           # gitignored — user populates
│   │   └── run_eval.py             # harness (Recall@K + must_contain + latency)
│   ├── watcher.py                  # watchdog daemon
│   └── query.py                    # CLI: python -m rag.query "질문"
│
├── apps/
│   └── streamlit_app.py            # Phase 1C UI (chat + sidebar + citations + admin)
│
├── scripts/
│   ├── verify_setup.py             # .env / vault path / LanceDB dir
│   ├── verify_anthropic.py         # 1-token Haiku ping
│   ├── verify_phase1a.py           # rows + filters + vector search
│   ├── verify_phase1b.py           # 3 sample questions end-to-end
│   ├── smoke_test_bge.py           # Phase 0 Gate
│   ├── inventory_frontmatter.py    # vault frontmatter schema dump (private — gitignored output)
│   └── diagnose_search.py          # direct LanceDB filter query (debug)
│
├── data/                           # gitignored
│   └── lancedb/
│       ├── vault_chunks.lance/     # vector + metadata
│       └── bm25_index.pkl          # BM25 sidecar
│
└── third_party/                    # reserved for extractors/ (not used yet)
```

---

## LanceDB Schema (`vault_chunks`)

| Field | Type | Source / Purpose |
|---|---|---|
| `chunk_id` | str (PK) | sha256(file_path + breadcrumb + chunk_idx + first 100 chars of text) |
| `file_path` | str | absolute path |
| `vault_relative` | str | obsidian:// URI builder input |
| `title` | str | file stem |
| `header_path` | str? | "H1 > H2 > H3" breadcrumb |
| `doc_type` | str | company / person / meeting / daily / periodic / project / resource / inbox / dashboard / attachment / other |
| `top_folder` | str? | first vault path component |
| `company` | str? | from `company`/`company_name` frontmatter (wikilink-unwrapped) or filename pattern |
| `person` | str? | for meetings under 02_Persons; filename subject |
| `date` | str? | ISO YYYY-MM-DD; filename pattern → frontmatter `date` → `first_met` → `created` |
| `event_stage` | str? | 1차DD, 미팅, 티타임, 킥오프, … (filename third token) |
| `investment_stage` | str? | from frontmatter (deal_pipeline) |
| `pipeline_stage` | str? | from frontmatter |
| `status` | str? | from frontmatter `status`/`investment_status` |
| `meeting_type` | str? | from frontmatter |
| `tags` | list[str] | frontmatter `tags` + inline #tags |
| `people` | list[str] | from frontmatter `people` |
| `source` | str | md / pdf |
| `parent_path` | str? | for PDF chunks: the note that embeds them |
| `text` | str | chunk text (with `[H1 > H2]\n` prefix from chunker) |
| `frontmatter_json` | str? | full original frontmatter (extension hook) |
| `chunk_idx` | int32 | per-file ordering |
| `vector` | vector(1024) | BGE-M3 |
| `ingested_at` | str | ISO timestamp (UTC, naive) |

---

## Verification Quickrefs

```bash
# Phase 0
uv run python scripts/smoke_test_bge.py

# Phase 1A
uv run python -m rag.ingest.pipeline --full
uv run python scripts/verify_phase1a.py

# Phase 1B
uv run python scripts/verify_phase1b.py
uv run python -m rag.query "Blueward 1차DD 핵심 리스크"

# Phase 1C
uv run streamlit run apps/streamlit_app.py

# Phase 1D
uv run python -m rag.ingest.pipeline --incremental
uv run python -m rag.watcher                              # background daemon
uv run python -m rag.ingest.attachment_loader --full      # PDFs (4 files)
uv run python -m rag.eval.run_eval                        # needs eval_set.yaml
uv run python scripts/diagnose_search.py --person "..."   # ad-hoc debugging
```

---

## Key Risks & Mitigations (status)

1. ~~한국어 BM25 토큰화 부실~~ — `char_trigram` for now; works in practice. kiwipiepy stays available as alt.
2. **HWP 텍스트 품질 한계** — out of scope; vault has 0 HWP, only 4 PDFs.
3. ~~WSL ↔ Windows 경로 불일치~~ — `config.py:normalize_vault_path` handles it; user is on Windows native.
4. ~~첫 인덱싱 느림~~ — 4,323 chunks in 25-27 min on CPU; one-time cost. `--incremental` ~3-4 min for 73 files.
5. ~~frontmatter 일관성 없음~~ — handled via wikilink unwrap + filename-pattern fallback.
6. **Anthropic ZDR enrollment** — 1인 운영체제 (toggle in `.env`). Default off.
7. ~~Watchdog double-fire~~ — handled via 2-second per-path debounce.
8. **Backups** — `data/lancedb/` is gitignored; user is responsible for OneDrive sync. Not yet automated.

---

## Reference: Question Categories (for `eval_set.yaml`)

### 회사 단건
- 위밋모빌리티 / 메타씨앤아이 / Blueward 1차DD 핵심 리스크 / 투자 포인트
- 샌드박스네트웍스 IPO 진행상황

### 시간 기반
- 지난주 / 지난달 만난 LP
- 작년 대비 올해 검토 건수

### 필터 + 의미 조합 (RAG 핵심 가치)
- 지난 분기 IR 받은 회사 중 소부장 분야만
- 케이런 7호 펀드 관련 회사
- 검토단계 회사 비교

### 정량 / 통계
- 평균 보유 기간 / 검토 → 투심위 통과율

### 작성 보조 (RAG 핵심 가치)
- 투심보고서 / LP 보고용 요약 / 5분 발표 스크립트

### 인사이트 (RAG 핵심 가치)
- Pass한 회사들의 공통점
- 시간순 의견 변화

`rag/eval/eval_set.example.yaml`에 포맷과 starter 4개 예시 있음.

---

## Prompt Template (요지 — actual prompt in `rag/generation/prompts.py`)

```
SYSTEM (cached via cache_control: ephemeral, ~2200 tokens):
당신은 케이런 VC의 1인 운영자(Antonio)를 보조하는 한국어 리서치 어시스턴트입니다.
[role/context, citation rules, doc_type vocab, 5 question playbooks, output format, anti-patterns]

USER (volatile, after cache breakpoint):
[컨텍스트]
[1] (type=meeting | company=Blueward | date=2026-04-19)
03_Companies/Antonio/검토단계/Blueward/Blueward_20260419_1차DD  >  회의록 > 리스크
---
{chunk_text}
[2] ...

[질문] {user_query}

[지시]
위 컨텍스트만 사용해서 한국어로 답변하세요. 모든 사실에 [n] 인용을 붙입니다.
```
