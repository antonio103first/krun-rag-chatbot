# KRUN RAG Chatbot

케이런 VC 1인 운영자(Antonio)의 Obsidian 볼트(`Obsidian_KRUN_Antonio`)에 대한 한국어 자연어 Q&A RAG 챗봇.

> 전체 설계와 단계별 일정은 [`claude.md`](./claude.md) 참고.

---

## Phase 0 Bootstrap (현재 단계)

이 단계에서는 프로젝트 골격, 설정 시스템, 그리고 BGE-M3 임베딩 모델이 로컬에서 정상 동작하는지 확인합니다.

### 1. 사전 요구사항

- **Python 3.11 이상**
- **`uv`** (https://github.com/astral-sh/uv)
  ```powershell
  # PowerShell에서
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
- **Anthropic API key** — https://console.anthropic.com/settings/keys
- (옵션) **GPU** — BGE-M3 임베딩 가속 (CPU도 동작)

### 2. 의존성 설치 (uv)

프로젝트 폴더에서:

```powershell
cd "C:\Users\anton\Documents\Claude AI_Personal\krun-rag-chatbot"

# 가상환경 + 핵심 의존성 설치
uv sync

# Phase 1A에 필요한 추가 패키지 (PDF 추출 + Korean tokenizer 실험용)
uv sync --extra phase1a
```

`uv sync`는 자동으로 `.venv/`를 만들고 `pyproject.toml`의 의존성을 설치합니다.

### 3. 환경변수 설정

```powershell
# 템플릿을 복사
Copy-Item .env.example .env

# 메모장으로 열어서 ANTHROPIC_API_KEY 채우기
notepad .env
```

`.env`는 `.gitignore`에 포함되어 있어 절대 커밋되지 않습니다.

### 4. 설정 검증

```powershell
# (a) 설정 파일 + 볼트 경로 확인
uv run python scripts/verify_setup.py

# (b) Anthropic API key 동작 확인
uv run python scripts/verify_anthropic.py

# (c) BGE-M3 임베딩 smoke test (Phase 0 Gate)
uv run python scripts/smoke_test_bge.py
```

**Phase 0 Gate 통과 기준** (`smoke_test_bge.py` 출력):
- `[PASS] Single-sentence embedding in <1000 ms`
- 첫 실행 시 약 2.3 GB BGE-M3 모델 다운로드 (이후 캐시됨)

---

## 폴더 구조

```
krun-rag-chatbot/
├── claude.md              # 전체 설계 + 단계별 일정 (Single source of truth)
├── README.md              # 이 파일
├── pyproject.toml         # uv 의존성
├── config.yaml            # 모델/검색/청킹 설정
├── .env.example           # 환경변수 템플릿
│
├── rag/                   # 코어 라이브러리
│   ├── config.py          # Pydantic settings (Phase 0)
│   ├── ingest/            # md_loader, chunker, embedder, pipeline (Phase 1A)
│   ├── store/             # lancedb_store, bm25_index (Phase 1A)
│   ├── retrieval/         # hybrid_search, query_analyzer, reranker (Phase 1B/D)
│   ├── generation/        # claude_client, prompts (Phase 1B)
│   └── eval/              # eval_set, run_eval (Phase 1D)
│
├── apps/
│   ├── streamlit_app.py   # Phase 1C UI
│   └── fastapi_server.py  # Phase 2 백엔드
│
├── scripts/
│   ├── verify_setup.py    # .env / vault 경로 점검
│   ├── verify_anthropic.py# API key 동작 확인
│   └── smoke_test_bge.py  # Phase 0 Gate
│
└── third_party/
    └── extractors/        # contract-subattach-documents 재활용 (Phase 1D)
```

## 다음 단계

Phase 0/1A/1B Gate 통과 후 → **Phase 1C** 진입 (Streamlit MVP).

---

## 일상 사용법 (Phase 1B 이상)

### CLI로 한 번 질문 (빠름)

```powershell
uv run python -m rag.query "Blueward 1차DD 핵심 리스크"
uv run python -m rag.query "지난주 미팅 액션아이템"
uv run python -m rag.query "강규식 상무 미팅 이력"

# 분석기 OFF (Haiku 호출 생략, ~1초 빨라짐)
uv run python -m rag.query --no-analyze "Blueward 핵심 리스크"

# JSON 출력 (다른 툴과 연동)
uv run python -m rag.query --json "케이런 7호 펀드 관련 회사"
```

### Streamlit 채팅 UI (Phase 1C)

```powershell
uv run streamlit run apps/streamlit_app.py
```

브라우저가 자동으로 열리거나 http://localhost:8501 로 접속.

기능:
- 채팅 히스토리 + 스트리밍 답변
- 사이드바 필터: 문서 유형 / 회사 / 인물 / 날짜 범위 / top-K
- 인용 expander: 청크 미리보기 + `obsidian://` "노트 열기" 버튼
- 메타데이터 / BM25 재인덱싱 버튼
- 세션 누적 토큰 + 예상 비용 ($)
- 캐시 적중률 표시 (90%+ 면 정상)

### 노트가 추가되거나 수정되면

```powershell
# 옵션 1: 메타데이터만 새로고침 (임베딩 재사용, ~1분)
uv run python -m rag.ingest.pipeline --refresh-metadata

# 옵션 2: 특정 파일만 다시 인덱싱
uv run python -m rag.ingest.pipeline --paths "C:\Users\anton\Documents\Obsidian_KRUN_Antonio\03_Companies\X\X.md"

# 옵션 3: 전체 재인덱싱 (BGE-M3 CPU에서 ~25분)
uv run python -m rag.ingest.pipeline --full
```

또는 Streamlit 사이드바의 **📥 메타데이터 새로고침** 버튼 사용.

---

## 보안 / 프라이버시

- 모든 임베딩은 **로컬 BGE-M3** — 볼트 콘텐츠가 외부로 송신되지 않음
- LLM 호출은 **질의에 매칭된 청크만** Claude API로 송신
- ZDR(Zero Data Retention) — Anthropic 콘솔 enrollment 후 `.env`에서 `ZDR_ENABLED=true`로 활성화
- `.env`, `data/lancedb/`, 볼트 경로는 모두 `.gitignore` 처리됨
