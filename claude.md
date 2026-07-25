# KRUN Internal RAG Chatbot — Implementation Plan & Session Handoff

> **For a new Claude Code session:** start by reading this file end-to-end. The Status, Architecture, How to Resume, and Open TODOs sections give you everything you need to take over without re-asking the user. The original design notes follow afterward as historical context.

---

## 🟢 Current Status (last updated: 2026-07-19 — company_brief mode shipped)

**Index is current as of 2026-07-19: 12,625 chunks / 974 files.** The 2.5-month
backlog (856 files, untouched since 2026-05-02) was cleared this session.
`/companies` went from 9 → **81** companies. All vault folders are covered
(06_Resources 4,381 · 03_Companies 3,094 · 01_Daily 2,077 · 04_Meetings 1,466 ·
02_Persons 1,127 · 05_Projects 237 · 01a_Periodic 169 · 00_Inbox 74).

Note the vault was reorganized since May: `03_Companies/Antonio/검토단계/` is now
`Inbound deal / 검토중 / 검토종료 / 투자업체`.

### Reindex: always use `scripts/catchup_ingest.py`, not a bare `--incremental`

`index_files` embeds every pending chunk and writes **once at the end**, so one long
`--incremental` is all-or-nothing — a 2026-07-19 run was interrupted at 4% and lost
all 31 minutes of work. `scripts/catchup_ingest.py` slices the same backlog into
batches (default 40 files) that each persist on completion, and resumes automatically
because indexed files get a fresh `ingested_at`.

    uv run python scripts/catchup_ingest.py                 # whole backlog
    uv run python scripts/catchup_ingest.py --only 03_Companies
    uv run python scripts/catchup_ingest.py --dry-run       # report backlog size

Measured 2026-07-19 (16 CPU threads, no CUDA): **851 files / ~9,800 chunks in 164
minutes**, batches ranging 2.5–15 min. Do NOT trust the old "25 min for 4,487 chunks"
figure elsewhere in this doc, and do not extrapolate from a single folder —
03_Companies is unusually chunk-dense and produced a 3–5× overestimate.

Two things that actually move the needle:
- **Do not run the API server during ingest.** Measured: 110s/batch with the server up
  vs 63s with it down. Kill it with PowerShell (`Get-CimInstance Win32_Process | Where
  CommandLine -match uvicorn`) — `pkill` does not exist in this Git Bash.
- **Watch free RAM.** One batch took 47.6 min instead of the usual 5–15; free physical
  memory was 0.4 GB of 15.5 GB and the machine was paging. The ingest process alone
  holds ~5 GB.
- `detect_vault_changes` handles deletes correctly, so incremental is non-destructive;
  a `--full` re-embed is never needed for catch-up.

### Known vault data issues

**Fixed 2026-07-19** — 3 files whose frontmatter failed to parse. The body was indexed
but ALL metadata was lost, so they could never match a `company` / date filter, making
them invisible to company_brief and enumerate. Two distinct causes worth recognizing:

- `지엘켐.md` — `business_model: **① 이차전지…` — YAML reads a leading `*` as an alias
  reference (ScannerError). Fix: quote the value.
- `국토부 운용위.md`, `바이오SPC 기획위원회.md` — unrendered Templater placeholders
  (`created: {{DATE:YYYY-MM-DD}}`). YAML parses `{…}` as a flow mapping, making the key
  a dict (ConstructorError: unhashable key). Fix: fill the value or drop the
  placeholder. Both were empty index notes; real content lives in dated siblings, so
  `date` was left blank rather than invented.

**Also fixed 2026-07-19** — 지엘켐 was scattered across four locations with a typo'd
duplicate folder (`지엘캠`, ㅐ vs ㅔ) under `KRUN/검토중`. Consolidated into
`03_Companies/KRUN/검토중/지엘켐/` (4 notes), `review_scope` corrected to `krun`, and
`지엘캠 / GL Chem / glchem` registered in `config/aliases.yaml`. Junk moved to
`.trash/지엘켐_정리_<ts>/`, not hard-deleted. The `_dup_` 예비검토보고서 was verified
byte-identical after whitespace normalization (same md5) before being moved.
The 주간회의 STT transcript still says 지엘캠 — left alone deliberately (transcripts are
a record of what was said, and `08_회의록/` is excluded from indexing anyway).

**Still open:**

- **12 polluted `company` values** show up in `/companies` as if they were companies:
  `meeting_krun` (6 notes), `meeting_antonio` (4), `inbound_2026MMDD` (9 separate
  dates), `파트너회의_20260503`. Two distinct leaks — template placeholders and
  filenames landing in the `company` field. Root cause is upstream in whatever
  automation writes that frontmatter; fixing the notes alone will not stop it.
  `scripts/audit_frontmatter.py` already handles the related `company: [[meeting]]` case.
- **7 body-less stub notes** (36–64 bytes, frontmatter only) produce zero chunks, so
  they never get an `ingested_at` and permanently show up in the catch-up backlog.
  Harmless, but the backlog never reads as truly empty.

### company_brief mode (2026-07-19) — ✅ validated end-to-end

Pre-meeting context restore for a single company. Bypasses the analyzer entirely
(a bare company name makes the analyzer return `companies=[]` by its bare-mention rule).

- `POST /ask` · `/ask/json` with `{"mode": "company_brief", "company": "…"}`
- `GET /companies` — company names in the index with note counts, powers the picker
- `company_brief_search()` — two stages: (A) `enumerate_search` fixes the complete
  file list, one head chunk per note, date-asc; (B) re-fetch **all** chunks of the
  5 most recent notes, spending a 90K-char budget newest-first. A note that doesn't
  fit degrades to its head chunk rather than disappearing.
- `build_user_message(mode="company_brief")` — 4 sections: 제목/현재단계 · 경위(시간순
  전체) · 핵심(2~3문단) · 이번 미팅 전 확인(미해결 요청사항).
- Plugin: command `회사 브리핑 (미팅 전 맥락 복원)` → `CompanyPickerModal`, pre-filtered
  by the active file's company folder.

Generation ceiling is raised to **8192 tokens for this mode only** (`_max_tokens_for`
in `apps/fastapi_server.py`). The 2048 default truncated a briefing mid-sentence at
exactly `output_tokens=2048`.

**Verified 2026-07-19** twice — first against 로드원오원 on the stale index (3 notes /
72 chunks, 34s), then against 레디로버스트머신 on the fresh index (5 notes / 115 chunks,
60K input tokens, 43s). The second produced a correct 4-section briefing: full timeline
IR → 예비검토 → 투심1차 → 투심2차, synthesis covering valuation (프리밸류 430억,
케이런 20억 RCPS) and the 매출 추정 dispute, plus 8 genuinely unresolved requests each
traced to the note that raised them. Failure paths also checked — unknown company
returns a fix-it message naming `config/aliases.yaml`; missing `company` returns 400.

### Cold-start fix + off switch (2026-07-23) — ✅ verified

Boot logs showed no error — just a `HF_TOKEN` warning and ~9s of HuggingFace Hub
HEAD/GET requests on the first `/ask`. Root cause: bge-m3 is already cached, but
`SentenceTransformer()` re-validates the model against the Hub online on every
process start, and the embedder loads lazily on the first query.

- **`scripts/run_api.bat`** now sets `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1`
  → cache-only load, no Hub round-trips, warning gone. (Comment out for one run
  when you need to re-download / update the model.)
- **`apps/fastapi_server.py`** — `lifespan` starts a background thread that calls
  `get_default_embedder(...).load()` at boot, so the first user query no longer
  eats the model-load latency. Verified: `embedder warmup complete (device=cpu)`,
  0 `huggingface.co` requests, 0 HF_TOKEN warnings.
- **`POST /shutdown`** (localhost) — refuses while a reindex is writing, else
  flushes the response and `os._exit(0)`. Wire-able to an Obsidian plugin button.
- **`scripts/stop_api.bat` + `scripts/stop_api.ps1`** (NEW) — the "off" switch.
  Detects first (honest "was not running"), tries graceful `/shutdown`, then
  force-kills the port listener **and** any `apps.fastapi_server` uvicorn process.
  The command-line sweep matters: the server runs as a **supervisor+worker pair**,
  so killing only the listening PID lets the parent respawn it (observed: kill
  2072 → 16564 reappears). Logic lives in the `.ps1` because cmd `for /f` mangles
  `^|` and unescaped `()` inside a `do ( )` block broke the bat twice.

`device=cpu` here (no CUDA on this machine); CUDA path unverified.

### Plugin server on/off menu (2026-07-25)

The sidebar status pill (`● N chunks` / `● offline`) is now clickable — it opens
a menu to **start** the server when offline, **shut it down** when online, or
refresh. The old standalone `⏻ 서버 종료` toolbar button is folded into this menu.

- Start launches the **venv Python directly** (desktop-only via Node
  `child_process`), then polls `/health` until it comes up. The bat path is
  configurable in settings (`serverScriptPath`); the interpreter and bind
  host/port are derived from it and the server URL.
- **Why not run the .bat?** Two failures, found in order. (1) The path has a
  space (`Claude AI_Personal`), so `cmd /c "<path>"` self-strips its quotes and
  splits at the space. (2) The deeper one: **Obsidian's process env usually
  lacks `~/.local/bin`, so the bat's `uv run uvicorn` fails silently** and the
  pill sticks on `starting…` — this bit a real user after the quoting was fixed.
  A git-bash test passed only because that shell had `uv` on PATH. Reproduced by
  scrubbing PATH: `[A]` bat → never starts, `[B]` venv python → online.
- **Working form:** `cp.spawn(python, ["-m","uvicorn","apps.fastapi_server:app",
  "--host",host,"--port",port], {cwd: root, env: {...process.env,
  HF_HUB_OFFLINE:"1", TRANSFORMERS_OFFLINE:"1"}, detached:true, stdio:"ignore"})`
  where `python = <root>/.venv/Scripts/python.exe` (derived from the bat path).
  Self-contained interpreter → no `uv`, no PATH, no cmd quote-strip. Falls back
  to the bat (shell:true + quoted path) only if the venv python is missing.
- The running `python.exe` **is** the server (background, no console window);
  `서버 종료` / `stop_api.bat` reap it. Touches `src/view.ts` (pill menu,
  `startServer`, `pollUntilOnline`), `src/api.ts` (`shutdown`), `src/settings.ts`
  (`serverScriptPath`). Rebuild `npm run build`, redeploy `main.js`. Verified at
  OS level in a scrubbed-PATH (Obsidian-like) env; the in-Obsidian click UI still
  relies on the user to confirm after reloading the plugin.

### Person search: title-insensitive + golf/travel scope (2026-07-25)

Person queries were too narrow on two axes: (1) indexed `person` carries the
title ("강규식 상무"), so a bare "강규식" matched nothing; (2) the `person IN (...)`
WHERE is a hard prefilter that only hits `02_Persons` notes.

- **Title-insensitive match.** `rag/aliases.py` adds `KOREAN_TITLES` +
  `strip_person_title()` ("최원석 전무" → "최원석"). `build_where_clause` now emits
  `(person = 'X' OR person LIKE 'X %')` per person, so "X" and "X 전무" resolve to
  the same person. Verified LanceDB/DataFusion supports `LIKE`; `person = '강규식'`
  → 0 rows, `person LIKE '강규식 %'` → the 강규식 상무 notes.
- **Golf/travel/meal scope.** No re-index needed: the person's master note
  (`02_Persons/{name}/{name}.md`) already aggregates *every* encounter type in
  its 만남 표 (daily_mention_sync pulls all 👥 만남 기록 lines — 골프/여행/식사/
  티타임/미팅). Once the title-insensitive filter reliably surfaces that master
  note, the golf/travel content comes with it. Verified: bare "김태환" → the
  "김태환 심사역" note, answer includes the 가평베네스트 golf round + score.
- **Limitation.** Standalone golf/travel *files* (`06_Resources/골프/…`, score/
  course; travel itineraries) have empty participant metadata (`players: []`), so
  they stay outside the person filter — only the encounter *context* (via the
  master note) is in scope. Tagging + re-indexing those files is a separate job.
- Touches `rag/aliases.py`, `rag/retrieval/hybrid_search.py`. Server restart
  applies it (running instance was restarted).

### Plugin: friendly offline error (2026-07-25)

A question asked while the server is down surfaced a cryptic
`오류: TypeError: Failed to fetch`. `src/view.ts` now detects connection errors
(`isConnectionError`: failed-to-fetch / fetch-failed / NetworkError / TypeError)
and shows "서버에 연결할 수 없습니다 — 상태 표시(●)를 클릭해 서버 시작하세요" plus
flips the pill to offline. Real server errors (502, quota) still pass through
verbatim. 5-case classification verified.

<details><summary>Previous status header (2026-05-02)</summary>

## Phase table (last updated: 2026-05-02 — Phase 2 session +1, post field-test bug fixes)


| Phase | Status | Notes |
|---|---|---|
| Phase 0 — Bootstrap | ✅ DONE | uv-based env, BGE-M3 verified, ZDR toggle in `.env` |
| Phase 1A — Ingest core | ✅ DONE | 4,487 chunks indexed |
| Phase 1B — Hybrid retrieval + Sonnet 4.6 | ✅ DONE | RRF fusion, prompt caching working |
| Phase 1C — Streamlit MVP | ✅ DONE | Sidebar filters, citations, watcher/refresh, reranker toggle |
| Phase 1D-A — Reranker | ✅ DONE | bge-reranker-v2-m3 via `uv sync --extra phase1d` |
| Phase 1D-B — Live watcher | ✅ DONE | `uv run python -m rag.watcher` |
| Phase 1D-C — PDF attachments | ✅ DONE (code) | User has not yet run `attachment_loader --full` |
| Phase 1D-D — Incremental ingest | ✅ DONE | mtime-based diff |
| Phase 1D-E — Eval harness | ✅ DONE (code) | User must populate `rag/eval/eval_set.yaml` (gitignored) |
| Phase 1D Gate (Recall@8 ≥ 0.7) | ⏳ PENDING | Blocked on eval_set.yaml population |
| Phase 2 — FastAPI server | ✅ DONE | `apps/fastapi_server.py` on 127.0.0.1:8765; SSE `/ask`, `/ask/json`, `/health`, `/reindex` |
| Phase 2 — Obsidian plugin | ✅ DONE (v0.1.0) | `obsidian-plugin/`; built `main.js` deployed to vault `.obsidian/plugins/krun-rag/`. Plugin auto-enabled in `community-plugins.json`. |
| **Enumerate mode** | ✅ DONE | Analyzer detects `intent=enumerate` for "list/all/count" queries with date range; server bypasses BM25/vector and runs metadata-only WHERE search (chunk_idx=0 dedupe). Returns up to 50 unique files. |
| **Category classification** | ✅ DONE | Citations get `category` field via path/filename heuristics: 회사미팅 / 인물미팅 / 사내회의 / 행사 / 통화 / 식사·친교 / 골프 / 기타. Enumerate prompt groups output by category with emoji headers. |

**LanceDB state at session end:** 4,487 chunks. BM25 sidecar at `data/lancedb/bm25_index.pkl`. Run `uv run python -m rag.ingest.pipeline --refresh-metadata` if metadata logic changes; `--incremental` for new/changed files; `--full` only when re-embedding from scratch (~25 min CPU).

> Superseded: the store held 2,826 chunks as of 2026-07-19 and the ~25 min figure did
> not reproduce. See the Reindex cost section at the top.

</details>


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

## 🆕 What changed in the Phase 2 session (2026-05-02)

**Goal**: ship FastAPI server + Obsidian plugin, then field-test against the user's vault.

### Built
1. **FastAPI server** (`apps/fastapi_server.py`)
   - `127.0.0.1:8765`. Endpoints: `GET /health`, `POST /ask` (SSE: `analysis`/`citations`/`delta`*N/`done`/`error`), `POST /ask/json`, `POST /reindex` (background thread; resets store/bm25 singletons after).
   - CORS for `app://obsidian.md` + localhost.
   - Launch: `scripts/run_api.bat` or `uv run uvicorn apps.fastapi_server:app --host 127.0.0.1 --port 8765`.
   - Deps: `uv sync --extra phase1d --extra api` (api extra adds fastapi/uvicorn/sse-starlette).

2. **Obsidian plugin** (`obsidian-plugin/`, TS+esbuild)
   - Right-leaf `ItemView` (`KrunRagView`): conversation log, streaming markdown, clickable `[n]` tokens, citation cards, health pill (rows count), Health/Reindex/Clear buttons. `Ctrl/⌘+Enter` to submit.
   - Commands: `Open KRUN RAG sidebar`, `Ask about current note` (prefills `[[stem]]에 대해 알려줘 …`), `Ask KRUN RAG…`, `Reindex vault (incremental)`.
   - SSE via `fetch().body.getReader()` (not EventSource — POST body required). `AbortController` for cancellation.
   - Settings: server URL, top-K, max chunks/file, reranker toggle, no-analyze toggle, send-active-note toggle (defaults OFF — search whole vault).
   - Build: `cd obsidian-plugin && npm install && npm run build`. Deploy: copy `main.js manifest.json styles.css` → `Obsidian_KRUN_Antonio/.obsidian/plugins/krun-rag/`.
   - Plugin auto-enabled by adding `"krun-rag"` to `Obsidian_KRUN_Antonio/.obsidian/community-plugins.json`. Sidebar leaf auto-mounted by editing `workspace.json` (`type: "krun-rag-view"`, currentTab set to it, right.collapsed=false, width=380).

3. **Enumerate mode** (the big late-session feature)
   - **Why**: user asked "4월에 미팅한 회사 모두" — RAG returned only 3 (top-K=8 with diversification). Truth: 47 meeting files. Semantic ranking is the wrong tool for "list everything matching".
   - `query_analyzer.py`: added `intent: "lookup" | "enumerate"` field. Updated SYSTEM_PROMPT with Korean examples ("4월 미팅 모두", "지난주 만난 회사", "올해 IR 받은 회사 몇 개" → enumerate).
   - `hybrid_search.py`: when `intent=enumerate AND where clause present`, bypass BM25/vector entirely. New `enumerate_search()` does pure metadata WHERE (with `chunk_idx = 0` to keep one head chunk per file), dedupe by file_path, cap at 50.
   - In enumerate mode, `doc_type` IS included in WHERE (unlike default hybrid path) so "4월 미팅" cleanly excludes daily/dashboard noise.
   - `prompts.py:build_user_message(mode="enumerate")`: swaps instruction to ask Claude for grouped output by `category=...` field, ordered 회사미팅 → 인물미팅 → 사내회의 → 행사 → 통화 → 식사·친교 → 골프 → 기타, with subtotal at bottom.

4. **Category classification** (`rag/retrieval/citations.py:classify_category`)
   - Path-prefix heuristics (most specific) → filename suffix → doc_type fallback.
   - Categories: 골프 / 식사·친교 / 통화 / 사내회의 / 행사 / 회사미팅 / 인물미팅 / 기타.
   - Filename suffix table: `_티타임 _석식 _조식 _중식 _저녁 _점심 _식사 _와인 _커피 _석식모임` → 식사·친교; `_통화` → 통화.
   - Path rules: `06_Resources/골프/` → 골프; `04_Meetings/사내회의/` → 사내회의; `04_Meetings/행사/` → 행사; `03_Companies/` default → 회사미팅; `02_Persons/` default → 인물미팅.
   - Field added to `Citation` dataclass and to `CitationOut` SSE payload (plugin can show category badge — not yet rendered in UI but field is on the wire).

### Bugs fixed (in order encountered)
1. **Wrong: `run_pipeline()` import in fastapi_server.py** — there's no such function. Use `index_full_vault / index_incremental / refresh_metadata` from `rag.ingest.pipeline`. Fixed.
2. **Active-note hint biased every query toward the open file** — sidebar settings now default `sendActiveNote: false`. Toggle exists for users who want the hint.
3. **Enumerate returned only 36/47 April meetings** — `enumerate_search` was pulling `limit*8 = 400` chunks from the table, but April meeting cluster has 719 chunks (avg 15 chunks/file). Random subset missed 11 files. Fix: query with `WHERE ... AND chunk_idx = 0` (1 row per file), `limit*4 = 200` is plenty. Verified all 47/47 unique files now returned.
4. **Category classification mis-bucketed everything as 회사미팅** — path checks used leading `/` (`/04_Meetings/사내회의/`) but vault_relative paths don't start with `/`. Fix: `lstrip("/")` + `path.startswith(...)` instead of `in`.

### Decisions made
| Decision | Rationale |
|---|---|
| Enumerate mode triggers on `intent=enumerate` AND any structured filter | Without a filter (date/company/person), enumerate would dump 50 random files. Filter is required. |
| `chunk_idx=0` for enumerate retrieval | Head chunk of meeting note has breadcrumb + key summary. One row per file is enough for a list answer. |
| Default `sendActiveNote: false` | User wants whole-vault search by default. Active-note hint biased retrieval and was confusing. |
| Plugin enabled via direct `community-plugins.json` edit | Faster than asking the user to toggle it in the GUI. Sidebar leaf in `workspace.json` similarly auto-mounted. |
| Category classification at citation-build time, not at ingest | Cheap, deterministic, works on existing index. No reindex needed if rules change. |
| Group by category inside the LLM, not in code | Claude already merges duplicates (e.g. VC협회 표지 + dated note for the same event) intelligently. Letting code group would lose that. |

### Known data quality issues (Claude flagged in answers — worth fixing in vault)
- `시너지_20260429_1차DD.md` frontmatter `company: [[meeting]]` — wrong, should be `[[시너지]]`.
- `투자팀회의_20260420.md` frontmatter `date: 2026-04-19` — filename says 0420.
- VC협회 조찬세미나, 스케일업 팁스 장관간담회 each have **two** notes per event (one with date suffix, one without) — frontmatter `date` differs between them. User likely wants to dedupe.
- 4월 47개 파일 중 골프 3건·티타임 1건·석식 1건은 비즈니스 미팅 아님 → category 분리됨 (의도). 사용자가 "미팅"에서 빼고 싶다면 enumerate 시 `category != 골프 AND category != 식사·친교` 옵션 추가 가능.

---

## 🆕 Field-test fixes (2026-05-02, post-plugin-deploy)

User started using the plugin in real work and hit two issues on a single query: **"전체 노트에서 7호조합 주목적중 남부권에 해당하는 검토업체 리스트 정리"**.

### Bug 1: enumerate sort crash on NaN dates
- **Symptom**: `TypeError: '<' not supported between instances of 'float' and 'str'` in `enumerate_search` sort key.
- **Cause**: pandas leaks `float('nan')` for missing string columns. NaN is truthy in Python, so `r.get("date") or ""` doesn't catch it; the sort then compared NaN (float) to "" (str) and raised.
- **Fix**: added `_safe_str()` / `_safe_int()` NaN-safe helpers in `hybrid_search.py` and used them for all enumerate-mode sort/dedupe keys. Same pattern as the `--refresh-metadata` fix (commit `64b9de6`); generalize this when touching pandas-derived dicts.

### Bug 2: enumerate misroutes content-conditional listings
- **Symptom**: query returned a generic dump of `project` + `company` head chunks. Actual answer was in meeting notes (예비검토보고서, 1차DD, IR), which were excluded.
- **Cause #1**: analyzer classified `intent=enumerate` because of "리스트" + "전체 노트" keywords, even though the listing predicate ("남부권에 해당") is a **semantic content condition**, not a structural metadata filter. enumerate then bypassed BM25/vector entirely.
- **Cause #2**: analyzer emitted `doc_types=['project','company']` with `companies=[]` and no date — so the WHERE clause was `None`. The previous behavior (commit `7b96cd6`) was to fire enumerate from doc_types-only when no other filter is set; that's fine for "Antonio가 투자한 업체들 모두" but breaks for content-conditional queries.
- **Fix A** (`hybrid_search.py`): enumerate now requires a non-None `where` (i.e., date_range / companies / persons must narrow the set). doc_types-only enumeration falls back to hybrid. Simplification: removed the doc_types-only branch in enum_where construction.
- **Fix B** (`query_analyzer.py` SYSTEM_PROMPT): added a Rules clause that **content-conditional listings stay in lookup** — when the predicate is geography ("남부권/수도권"), sector ("소부장/바이오"), fund-purpose ("주목적/주요투자분야"), or qualitative judgment, use `intent="lookup"`. Added two new examples ("7호조합 주목적중 남부권 검토업체", "소부장 분야 검토 회사").

### Decisions made
| Decision | Rationale |
|---|---|
| enumerate requires a structural WHERE (not just doc_types) | doc_types alone is too broad. "전체 회사 리스트" by metadata is rarely what the user wants — they almost always have a content predicate in mind. Force those queries to use semantic search. |
| Two-pronged fix (analyzer prompt + retrieval guardrail) | Belt-and-suspenders. The analyzer should ideally route correctly, but if it slips up on edge cases, the retrieval-side guardrail still produces a reasonable answer instead of a metadata dump. |
| NaN-safe helpers as named functions, not inlined | Will recur. Anywhere we sort/compare values from pandas-derived dicts (LanceDB → pandas → dict), wrap with `_safe_str` / `_safe_int`. Future refactor: store-level cleanup so dict consumers never see NaN. |

### Verified working
After both fixes + server restart, the same query routed to `intent=lookup`, BM25/vector returned the right notes (시너지_예비검토보고서, 블루타일랩_예비검토보고서, 7호.md, 정기조합원총회 등), and the LLM answered with the expected southern-region candidates.

### Files touched this session
- `rag/retrieval/hybrid_search.py` — `_safe_str` / `_safe_int` helpers; enumerate trigger requires `where`
- `rag/retrieval/query_analyzer.py` — content-conditional rule + 2 new example Q/A pairs

---

## 🐛 Recent Fixes & Decisions (Phase 1 session, kept for context)

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

### Most likely next steps (resume here)
- [ ] **Plugin field-test (in progress)** — user is using the Obsidian sidebar in real work. Collect failures: bad citations, wrong category bucket, missing files in enumerate, slow first-query (BGE-M3 model load takes ~10s). When the user reports an issue, FIRST hit `/ask/json` with the same query to inspect raw citations before debugging the LLM output.
- [x] **Vault data hygiene** — `scripts/audit_frontmatter.py` added (2026-05-02). Detects 3 issue classes (suspicious_link, date_mismatch, possible_duplicate) and supports `--fix-dates` / `--fix-suspicious` for safe auto-fixes. Initial run fixed 4 date mismatches + 2 `[[meeting]]` wikilinks. VC협회/스케일업 팁스 "duplicates" turned out to be intentional event dashboards (`type: event`); audit now skips index-typed undated siblings. Re-run anytime; should stay at 0 findings unless new bad data appears.
- [ ] **More enumerate-mode validation** — try variations and watch for misses:
  - "지난주 만난 사람들" (인물 + 짧은 date range)
  - "올해 IR 받은 회사" (date range + tag-ish: IR)
  - "3월 미팅 모두" (different month)
  - "이번 주 일정" (future dates — current data may not have any)
  - Edge case: enumerate with no filter → currently falls through to hybrid; verify behavior is reasonable.

### Phase 1D Gate ✅ MET
- [x] **`eval_set.yaml` v2** (2026-05-02, 20 items: 회사 5/인물 3/시간 3/필터+의미 4/작성 2/skip-placeholders 3 for 정량+인사이트 needing user judgment). Auto-verifiable `expected_files`/companies/persons; `must_contain` set only on `메타씨앤아이_1차dd` where chunk content was inspected directly.
- [x] **Retrieval bug fix landed** (commit `f61868a`): hybrid_search now augments the candidate pool with head chunks (`chunk_idx=0`) of WHERE-matching files that didn't make RRF, so sibling meeting notes can reach top-K via diversification. Also re-indexed 25 dated meeting files whose `company` field was stored as raw `[[X]]` (pre-wikilink-unwrap legacy rows).
- [x] **Baseline → after-fix**: `file_match_rate 0.615 → 0.882` (gate ≥ 0.7 cleared). Saved `rag/eval/results/v2_baseline.json`. Reranker still no help (the win is metadata-side breadth, not ranking).
- [x] **Analyzer prompt fixes** (commit `7b96cd6`): "Antonio" / "내가" / "본인" excluded from `persons`; fund references ("케이런 N호 펀드") routed to `doc_types=["project"]` not `companies`. enumerate path now also fires from doc_types-only when no other WHERE filter is set. → Antonio_투자업체_리스트 case 0/4 → 4/4 ✅
- [x] **`--refresh-metadata` fixed** (commit `64b9de6`): pandas NaN was leaking through `row.get(x) or default` because NaN is truthy in Python. NaN-safe coercion added; full vault refresh now runs in 1.9s for 4,487 chunks.
- [x] **Final eval (full LLM-generation run)**: file_match_rate=0.941 (16/17), must_contain_rate=0.941, avg_latency=12.86s. Saved `rag/eval/results/v4_full.json`. Last file miss is 펀드7호_정기조합원총회 (hybrid path doesn't apply doc_type filter so the right file gets buried under 5 daily/주간회의 chunks). Last must_contain miss is 메타씨앤아이_1차dd looking for "마이크로" + "디스플레이" — answer used "OLEDos" / "OLED" so substring matcher missed; lesson: must_contain on Korean technical terms is brittle, prefer multiple OR-able alternatives.

### Open follow-ups (post-Gate)
- [x] **Blueward 1차DD diagnosis CORRECTED**: my earlier "content is not Blueward" claim was wrong — I read only the first 800 chars (opening attendee list mentions visiting executives from predecessor entities ISTN/INF컨설팅) and jumped to a wrong conclusion. Body actually mentions "Blueward" 14 times. **Blueward = ISTN + INF컨설팅 합병 후 신규 사명 (2025-10)**. Recorded in `config/aliases.yaml`.
- [x] **Alias system added** (commit `17ca8e1`): `config/aliases.yaml` + `rag/aliases.py`. Used by audit script's new `--check-content` mode and ready for retrieval-side query expansion. Add new entries when you discover variants/typos.
- [x] **Audit content_filename_mismatch check** (commit `17ca8e1`): opt-in via `--check-content`. Restricted to `03_Companies/.../*meeting.md` (skips 02_Persons where bodies usually summarize what was said, not who said it). High-recall low-precision: 9 hits on current vault, mostly false positives where bodies use brand short-names or product details. Review each manually.
- [x] **`must_contain_any` matcher** (commit `17ca8e1`): OR-semantics per group, e.g. `must_contain_any: [["마이크로","Micro","OLEDos"], ["디스플레이","display"]]` passes if at least one variant per group is in the answer.
- [x] **Hybrid doc_type-WHERE fallback experiment**: tried, reverted. Analyzer too often emits doc_types alone for queries that should keyword-match (e.g. "시너지 검토" → analyzer reads 시너지 as "synergy" → doc_types=[project]). doc_type-only WHERE then drops the actual company files. Rolled back.
- [ ] **펀드7호 retrieval still misses**: `펀드LP관리_20260323_정기조합원총회.md` exists but BM25/vector rank lower than 7호.md profile + 주간회의 chunks. Genuine retrieval-burying, not WHERE issue. Future fix: kiwipiepy tokenizer (better Korean morphology) or query rewrite that boosts unique compound nouns like "정기조합원총회".
- [ ] **Use aliases.yaml in retrieval**: `aliases.canonical_name()` could expand `companies=['ISTN']` from analyzer into `companies IN ('Blueward')` for the WHERE clause. Not yet wired.
- [ ] **Audit content check produces false positives**: 9 hits on the current vault are mostly false (bodies use brand short-names, product details, or person names instead of the full company name). Improvements: extract additional aliases from frontmatter `aliases:` field, add more brand-shortname suffixes, or cross-reference with vault-wide entity list.

### Pending user actions (carried over from earlier sessions)
- [ ] **Run PDF ingest once** (4 PDFs only, fast): `uv run python -m rag.ingest.attachment_loader --full`.
- [ ] **Phase 1C 1-week real usage evaluation** — now subsumed by the plugin field-test above.

### Plugin polish (low priority, only if user asks)
- [ ] Render `category` badge on each citation card in the sidebar (field already arrives in SSE payload). Add `.krun-rag-citation-cat` style.
- [ ] Citation card sort by date ascending in the sidebar (currently follows server order, which is also date-asc for enumerate).
- [ ] Settings toggle: include/exclude `골프 / 식사·친교` from enumerate result. Server-side WHERE clause: `category` is computed at citation-build time, not stored, so this'd need to push the filter into the frontend (filter citations after receipt) or precompute at ingest.
- [ ] Plugin "Reload server" button when health is offline (currently must restart `run_api.bat`).

### Code-side polish (small, low priority)
- [ ] If watcher dies on Windows reserved filenames or transient locks, surface to user (currently logs and continues).
- [ ] Add `kiwipiepy` toggle to Streamlit (currently only swappable via `config.yaml`). Not yet needed; char_trigram is fine.
- [ ] Eval harness's faithfulness scoring is currently `must_contain` substring matching. Replace with claim-by-claim LLM judge if Phase 1D Gate is hard to meet with current proxy.
- [ ] `attachment_loader._find_parent_note` is best-effort; if a single PDF is embedded in multiple notes, parent_path links to one of them. Acceptable for now.

### Phase 2 (Obsidian plugin — DONE 2026-05-02)

**Server** (`apps/fastapi_server.py`):
- `GET  /health` — rows count + vault/model info + reindex status
- `POST /ask` — SSE stream: events `analysis` → `citations` → `delta`*N → `done` | `error`
- `POST /ask/json` — non-streaming JSON (analysis + citations + answer + usage)
- `POST /reindex` — body `{mode: "incremental"|"full"|"refresh-metadata"}`; runs in a daemon thread; resets BM25/store singletons after.
- Singletons: store/bm25/Claude client are lazy + reset on reindex finish.
- CORS: `app://obsidian.md` + localhost.
- Launch: `scripts/run_api.bat` or `uv run uvicorn apps.fastapi_server:app --host 127.0.0.1 --port 8765`.
- Install deps: `uv sync --extra phase1d --extra api` (api extra adds fastapi/uvicorn/sse-starlette).

**Plugin** (`obsidian-plugin/`):
- TypeScript + esbuild, vanilla `fetch()` SSE reader (no EventSource — POST body needed).
- `src/main.ts` — plugin entry, ribbon icon, 4 commands:
  - `Open KRUN RAG sidebar`
  - `Ask about current note` — checkCallback gated on active file; prefills `[[stem]]에 대해 알려줘 …`
  - `Ask KRUN RAG…` — opens sidebar, focuses input
  - `Reindex vault (incremental)`
- `src/view.ts` — `KrunRagView` (right-leaf ItemView): conversation log, streaming markdown render, clickable `[n]` tokens, citation cards, header with health pill (rows count), Reindex/Health/Clear buttons. `Ctrl/⌘+Enter` to submit.
- `src/api.ts` — `KrunRagApi`: `health()`, `ask()` returns `AbortController`, `reindex()`.
- `src/settings.ts` — server URL, top-K, max chunks/file, reranker toggle, no-analyze toggle, send-active-note toggle.
- Citation click → `app.vault.getAbstractFileByPath(vault_relative)` → `getLeaf(false).openFile()`; falls back to `workspace.openLinkText()`.
- Active note hint: when enabled, plugin sends `active_note` in body; server prepends `[현재 노트: stem]` to the query for retrieval (analyzer/Claude still see the original question).

**Build & deploy**:
```bash
cd obsidian-plugin
npm install
npm run build
# Deploy
cp main.js manifest.json styles.css \
   /c/Users/anton/Documents/Obsidian_KRUN_Antonio/.obsidian/plugins/krun-rag/
```

**To use**:
1. Start API server: `scripts/run_api.bat` (or run on boot via Task Scheduler if desired).
2. In Obsidian: Settings → Community plugins → Enable "KRUN RAG".
3. Click ribbon icon (messages-square) or run command `Open KRUN RAG sidebar`.
4. Health pill should turn green and show chunk count.

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
