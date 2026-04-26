# KRUN Internal RAG Chatbot — Implementation Plan & Session Handoff

> 이 파일은 새 Claude Code 세션이 컨텍스트 없이 곧바로 구현을 이어받을 수 있도록 작성된 핸드오프 문서입니다.
> 새 세션에서 working directory를 이 파일이 있는 폴더(`C:\Users\anton\Documents\Claude AI_Personal\krun-rag-chatbot\`)로 시작하면 자동으로 로드됩니다.

---

## 새 세션 시작 가이드

1. **Windows 측 준비**:
   - `C:\Users\anton\Documents\Claude AI_Personal\krun-rag-chatbot\` 폴더 생성 (이미 있으면 패스)
   - 이 CLAUDE.md를 그 폴더에 복사
2. **Claude Code 시작**: 해당 폴더에서 `claude` 실행 (또는 Claude Code 앱에서 working dir 지정)
3. **Phase 1A부터 구현 시작**: 아래 "Phased Schedule" 참고
4. **사전 확인 필요 사항**:
   - `ANTHROPIC_API_KEY` (Anthropic 콘솔에서 발급)
   - Obsidian 볼트 경로 접근 가능 여부 (`C:\Users\anton\Documents\Obsidian_KRUN_Antonio`)
   - Python 3.11+ 설치 여부
   - GPU 유무 (BGE-M3 임베딩 속도에 영향, 없어도 동작)

---

## Context

**Why this is being built**
케이런 VC 1인 운영자(Antonio)가 본인 Obsidian 볼트(`C:\Users\anton\Documents\Obsidian_KRUN_Antonio` — `03_Companies/`, `02_Persons/`, `04_Meetings/`, `06_Resources/`)에 누적된 회사·인물·미팅·리소스 노트에 대해 자연어 Q&A를 할 수 있는 내부 RAG 챗봇이 필요. VC 업무는 본질적으로 "내가 가진 정보를 얼마나 빨리 꺼내쓰느냐"의 게임인데, 폴더/키워드 검색만으로는 "지난 분기 IR 받은 회사 중 소부장 분야만", "메타씨앤아이 1차DD 리스크 정리" 같은 의미·필터 결합 질문에 답할 수 없음.

**Intended outcome**
- 회의 직전 5분 안에 과거 컨텍스트 즉시 복원
- 노트 작성 중 Obsidian 안에서 바로 호출 가능한 사이드패널
- 모든 답변에 클릭 가능한 출처(`obsidian://` 링크) → 환각 방지
- 민감 데이터 외부 송신 최소화 (로컬 임베딩 + 질의 시 관련 청크만 Claude 송신)

**Confirmed decisions**
- 인터페이스: **Phase 1 = Streamlit MVP (단독 검증)**, **Phase 2 = Obsidian 플러그인** — Slack은 1인 사용이므로 스킵
- 임베딩: **BGE-M3 로컬** (`sentence-transformers`) — 한국어 강함, 외부 송신 0
- LLM: **Claude Sonnet 4.6** (생성), **Claude Haiku 4.5** (질문 분석) — zero-retention 헤더
- 벡터 DB: **LanceDB** (파일 기반, 서버 불필요)
- 검색: BM25 + 벡터 하이브리드 + bge-reranker-v2-m3
- 사용자: 1인 (인증/멀티테넌시 불필요)
- 기존 레포 `contract-subattach-documents/`의 `extractors/*` 모듈 재활용 (DOCX/HWP/PDF 파서 — 첨부 인덱싱용)

---

## Architecture

```
[Obsidian Vault (Windows)]
        │ watchdog / cron
        ▼
[md_loader] → frontmatter + body 파싱, [[wikilinks]] 해석
        │
        ▼
[chunker] 헤더 단위 + 800토큰 슬라이딩, breadcrumb 보존
        │
[attachment_loader] ── 기존 extractors/ 재활용 (DOCX/HWP/PDF)
        │
        ▼
[embedder] BGE-M3 (로컬, 1024차원)
        │
        ▼
[LanceDB store] vault_chunks 테이블 + BM25 사이드카 인덱스

[사용자 질의]
        │
        ▼
[query_analyzer] (Haiku) → {date_range, companies, tags, doc_types, rewritten_query}
        │
        ▼
메타데이터 필터 → [hybrid_search] BM25+벡터 RRF top-30 → [reranker] top-8
        │
        ▼
[claude_client] Sonnet 4.6, zero-retention, 스트리밍, 인용 [n] 강제
        │
        ▼
[Streamlit UI / Obsidian Plugin] 답변 + 클릭 가능한 obsidian:// 출처
```

---

## Directory Layout

이 프로젝트는 **독립 폴더**로 운영하되, 기존 레포의 extractors는 git submodule 또는 코드 복사로 가져옴.

```
krun-rag-chatbot/                 # 이 폴더 (Windows: C:\Users\anton\Documents\Claude AI_Personal\krun-rag-chatbot\)
├── CLAUDE.md                     # 이 문서
├── README.md                     # 사용자 가이드
├── requirements.txt
├── .env.example                  # ANTHROPIC_API_KEY
├── config.yaml                   # vault_path, 모델명, top_k 등
│
├── rag/
│   ├── config.py                 # Pydantic settings, 경로 자동 변환
│   ├── ingest/
│   │   ├── md_loader.py          # python-frontmatter
│   │   ├── chunker.py            # 헤더 인식 + 슬라이딩 윈도우
│   │   ├── attachment_loader.py  # ![[file.pdf]] → extractors.* 호출
│   │   ├── metadata.py           # 폴더 경로 → doc_type 매핑
│   │   ├── embedder.py           # BGE-M3 wrapper, 배치 32
│   │   └── pipeline.py           # 전체/증분 인덱싱
│   ├── store/
│   │   ├── lancedb_store.py      # 단일 테이블 vault_chunks
│   │   └── bm25_index.py         # rank-bm25 + kiwipiepy
│   ├── retrieval/
│   │   ├── query_analyzer.py     # Haiku JSON 출력
│   │   ├── hybrid_search.py      # 병렬 BM25+ANN, RRF k=60
│   │   ├── reranker.py           # bge-reranker-v2-m3
│   │   └── citations.py          # obsidian:// URI 빌더
│   ├── generation/
│   │   ├── prompts.py            # 한국어 시스템 프롬프트
│   │   └── claude_client.py      # Anthropic SDK, 스트리밍
│   ├── watcher.py                # watchdog, 2초 debounce
│   └── eval/
│       ├── eval_set.yaml         # 30개 한국어 QA 페어
│       └── run_eval.py           # Recall@8, faithfulness
│
├── apps/
│   ├── streamlit_app.py          # Phase 1 UI
│   └── fastapi_server.py         # Phase 2 백엔드
│
├── obsidian-plugin/              # Phase 2 (TypeScript)
│   ├── manifest.json
│   ├── main.ts
│   ├── settings.ts
│   └── package.json
│
└── third_party/
    └── extractors/               # contract-subattach-documents/extractors 복사 또는 submodule
```

**New dependencies** (`requirements.txt`):
```
lancedb>=0.13
sentence-transformers>=3.0
anthropic>=0.40
rank-bm25>=0.2.2
kiwipiepy>=0.18
streamlit>=1.40
watchdog>=5.0
python-frontmatter>=1.1
pydantic-settings>=2.6
fastapi>=0.115           # Phase 2
uvicorn[standard]>=0.32  # Phase 2
# 기존 extractors 재사용을 위해
python-docx>=1.2
docxtpl>=0.20
pymupdf>=1.27
pytesseract>=0.3.10
olefile>=0.47
lxml>=6.0
```

---

## LanceDB Schema (`vault_chunks`)

| Field | Type | Purpose |
|---|---|---|
| `chunk_id` | str (PK) | sha256(file_path + header_path + chunk_idx) |
| `file_path` | str | 절대 Windows 경로 |
| `vault_relative` | str | obsidian:// URI 빌드용 |
| `title` | str | 파일명 stem |
| `header_path` | str | "H1 > H2 > H3" breadcrumb |
| `doc_type` | enum | company / person / meeting / resource / attachment |
| `company` | str? | `03_Companies/<X>/` 에서 추출 |
| `tags` | list[str] | frontmatter + #inline |
| `date` | date? | frontmatter 또는 파일명 YYYYMMDD |
| `source` | enum | md / attachment |
| `parent_path` | str? | attachment의 부모 노트 |
| `text` | str | 청크 원문 |
| `vector` | vector(1024) | BGE-M3 |
| `ingested_at` | datetime | |

---

## Critical Files to Create (작성 순서)

1. `rag/config.py` — 경로/키 설정 (가장 먼저)
2. `rag/ingest/md_loader.py` + `rag/ingest/metadata.py`
3. `rag/ingest/chunker.py` — 청킹 품질이 답변 품질의 80%
4. `rag/ingest/embedder.py`
5. `rag/store/lancedb_store.py`
6. `rag/ingest/pipeline.py` — 전체 흐름 오케스트레이터
7. `rag/store/bm25_index.py`
8. `rag/retrieval/hybrid_search.py` — RRF 융합
9. `rag/retrieval/query_analyzer.py`
10. `rag/generation/prompts.py` + `rag/generation/claude_client.py` — 인용 강제 + zero-retention
11. `apps/streamlit_app.py` — MVP UI
12. `rag/eval/eval_set.yaml` — 검증 기준
13. `rag/retrieval/reranker.py` — 선택 (Phase 1D)
14. `rag/ingest/attachment_loader.py` — 선택 (Phase 1D)
15. `rag/watcher.py` — 선택 (Phase 1D)

## Reused Existing Code (from contract-subattach-documents)

- `extractors/contract_extractor.py` — DOCX/HWP/PDF 키워드 파싱 → `attachment_loader.py`에서 호출
- `extractors/pdf_extractor.py` — PyMuPDF + OCR → 스캔 PDF 인덱싱
- `extractors/report_extractor.py` — dataclass 정규화 패턴 참고

→ `third_party/extractors/`로 복사하거나 git submodule로 연결.

---

## Phased Schedule (Go/No-Go gates)

### Phase 0 — Bootstrap (Day 1)
- 프로젝트 폴더 초기화, `requirements.txt` 설치
- BGE-M3 로컬 로드 smoke test
- `rag/config.py`: Windows 경로 처리 (필요시 WSL 자동 변환)
- `.env` 작성, `ANTHROPIC_API_KEY` 검증
- **Gate**: BGE-M3 로컬에서 한국어 문장 임베딩 1초 내 생성

### Phase 1A — Ingest Core (Day 2-4)
- `md_loader` + `chunker` + `metadata` (헤더 단위 청킹, frontmatter 추출)
- `embedder` + `lancedb_store` (배치 임베딩, upsert)
- 전체 볼트 1회 인덱싱 (1000+ 노트 가정)
- **Gate**: `lancedb`에서 회사명 메타필터로 정확히 검색됨

### Phase 1B — Retrieval + Generation (Day 5-7)
- `hybrid_search` (BM25+벡터 RRF) — 리랭커는 일단 OFF
- `query_analyzer` (Haiku) + `claude_client` (Sonnet 4.6, 스트리밍, zero-retention)
- 인용 [n] 강제 프롬프트
- CLI 테스트 (`python -m rag.query "위밋모빌리티 리스크"`)
- **Gate**: 사용자 질문 5개에 대해 출처 클릭 가능한 정답 반환

### Phase 1C — Streamlit MVP (Day 8-9)
- 채팅 UI + 사이드바 필터 + 인용 expander + obsidian:// 링크
- "Reindex now" 버튼
- **Gate**: 본인 일상 워크플로우에서 1주일 사용 → 만족도 평가

### Phase 1D — Polish (Day 10-11) — *조건부*
- 리랭커 ON, attachment_loader (DOCX/HWP/PDF 첨부 인덱싱)
- watchdog 라이브 인덱싱
- `eval_set.yaml` 30문항 작성 + 베이스라인 측정
- **Gate**: Recall@8 ≥ 0.7, faithfulness ≥ 0.85

### Phase 2 — Obsidian Plugin (Day 12-16) — *Phase 1 만족 시에만*
- `fastapi_server.py` (RAG 코어 래핑, SSE 스트리밍, localhost:8765)
- TypeScript 플러그인: ItemView 사이드패널 + 명령 팔레트 항목
- "Ask about current note" 명령 (현재 파일을 컨텍스트로 자동 추가)
- 인용 클릭 → `app.workspace.openLinkText()`
- 플러그인 패키징 → `Obsidian_KRUN_Antonio/.obsidian/plugins/krun-rag/`

**Realistic total**: Streamlit MVP **9-11일**, Obsidian 플러그인까지 **14-16일**.
원래 사용자 추정 4-5일은 Phase 1A+1B 코어만 가능할 정도이며, UI/평가/튜닝까지 포함하면 부족함.

---

## Prompt Template (요지)

```
SYSTEM (cached via cache_control: ephemeral):
당신은 케이런 VC 내부 자료에 대한 한국어 리서치 어시스턴트입니다.
규칙:
1. 반드시 제공된 컨텍스트에서만 답변. 모르면 "제공된 자료에서 찾을 수 없습니다".
2. 모든 사실 주장에 [n] 형식 인용. 회사명/금액/일자/조건은 원문 그대로.
3. 한국어로 답변. 약어는 한 번 풀어서 명시.

USER:
[컨텍스트]
[1] 04_Meetings/2025-12-08_위밋.md > 후속투자 논의
{chunk_text}
[2] ...

[질문] {user_query}
```

---

## Key Risks & Mitigations

1. **한국어 BM25 토큰화 부실** → `kiwipiepy` 형태소 분석기로 사전 토큰화. 실패 시 char-trigram fallback
2. **HWP 텍스트 품질 한계** (`olefile`은 PrvText 미리보기만) → DOCX 형제 파일 우선, HWP는 인용에 low-confidence 표시
3. **WSL ↔ Windows 경로 불일치** → `vault_relative` 항상 별도 저장, URI는 그것만 사용
4. **첫 인덱싱 느림** (1000+ 노트) → 배치 32 + tqdm + 중단점 재개
5. **frontmatter 일관성 없음** → 폴더별 fallback 규칙, 누락 필드는 인덱싱 실패시키지 않음
6. **Anthropic zero-retention 헤더가 조직 enrollment 필요할 수 있음** → 콘솔에서 확인, 안 되면 ZDR 정책 문서로 대체
7. **Watchdog Obsidian 저장 시 더블파이어** → 경로별 2초 debounce, `.obsidian/` 무시
8. **단일 머신 = 백업 없음** → LanceDB 폴더 야간 OneDrive 복사

---

## Verification

### Phase 0 검증
```bash
python -c "from sentence_transformers import SentenceTransformer; \
           m = SentenceTransformer('BAAI/bge-m3'); \
           print(m.encode(['안녕하세요']).shape)"
# 기대: (1, 1024)
```

### Phase 1A 검증
```bash
python -m rag.ingest.pipeline --full
python -c "from rag.store.lancedb_store import open_store; \
           s = open_store(); print(s.count(), 'chunks'); \
           print(s.search_by_company('위밋모빌리티')[:3])"
```
기대: 청크 수 출력, 위밋모빌리티 관련 청크 3개 정확히 반환

### Phase 1B 검증 (CLI)
```bash
python -m rag.query "위밋모빌리티 1차DD 리스크 정리"
python -m rag.query "최근 3개월 만난 LP"
python -m rag.query "케이런 7호 펀드 LPA 조건과 충돌하는 검토건"
```
기대: 답변 + [n] 인용 + 클릭 가능한 obsidian:// 링크

### Phase 1C 검증 (Streamlit)
```bash
streamlit run apps/streamlit_app.py
```
- 본인 작성 질문 30개 (5개 카테고리)
- 출처 클릭 → Obsidian에서 노트 열림 확인
- 사이드바 필터 (날짜/회사/doc_type) 동작 확인

### Phase 1D 평가
```bash
python -m rag.eval.run_eval
```
기대: Recall@8 ≥ 0.7, faithfulness ≥ 0.85. 미달 시 청크 크기/리랭커/프롬프트 튜닝.

### Phase 2 검증 (Obsidian 플러그인)
1. `apps/fastapi_server.py` 백그라운드 실행
2. Obsidian 플러그인 활성화 → 우측 사이드패널에 "KRUN RAG" 표시
3. 명령 팔레트 "RAG: Ask about current note" → 현재 파일 컨텍스트로 미리 채워짐
4. 인용 클릭 → 동일 창에서 노트 열림 (`openLinkText`)

---

## Reference: 예상 질문 카테고리 (50개 — eval_set.yaml 작성 참고)

### 회사 단건 조회
- 위밋모빌리티 투자 단계와 집행일?
- 메타씨앤아이 1차DD에서 나온 핵심 리스크 3가지?
- 샌드박스네트웍스 IPO 진행상황 정리

### 시간 기반
- 최근 3개월 만난 LP 누구누구?
- 지난주 미팅 액션아이템 전부
- 작년 대비 올해 검토 건수 비교

### 필터 + 의미 조합 (RAG 핵심 가치 영역)
- 지난 분기 IR 받은 회사 중 소부장 분야만
- 최근 6개월 검토한 모빌리티 회사 비교
- 케이런 7호 포트폴리오 중 후속 라운드 임박 회사

### 정량/통계
- 우리 포트폴리오 평균 보유 기간
- 검토 → 투심위 통과율

### 작성 보조 (RAG 핵심 가치 영역)
- 위밋모빌리티 투심보고서 초안
- LP 보고용 1페이지 요약
- 투심위 발표 5분 스크립트

### 인사이트 (RAG 핵심 가치 영역)
- Pass한 회사들의 공통점 — 우리 투자 기준 역추출
- 비슷한 회사 검토 사례 찾기
- 한 회사에 대해 시간순으로 우리 의견이 어떻게 바뀌었는지
