# Re:Work

완성된 보고서(PDF)와 원천 데이터(CSV)를 넣으면, 담당자가 **어떤 데이터를 어떤 기준으로 보고 그 보고서와 제안을 썼는지** 되찾아 인수인계 매뉴얼과 재실행 가능한 규칙으로 남기는 Agent.

- 개념: `핵심개념_판단근거추론.md`
- 실행 흐름·구성 요소·도구 설명: `실행흐름.md`
- 기획: `기획서.md` (일부 내용은 구현 과정에서 바뀌었다. 최신 구조는 `실행흐름.md`를 따른다)

## 실행 방법

준비: Docker Desktop 실행, 루트 `.env`에 OpenAI 키 (`OPENAI_API_KEY`, 또는 `OPEN_API_KEY`).

```bash
uv sync                                   # 의존성 설치 (버전은 uv.lock에 고정)
docker compose up -d                      # PostgreSQL(5433) + RabbitMQ(5672, 관리 화면 15672)
uv run python scripts/seed_demo.py        # 데모 회사·사용자·업무 지식 등록

# 터미널 1: API + 화면
uv run uvicorn rework.api.main:app --port 8000
# 터미널 2: Worker
uv run python -m rework.workers.worker
```

브라우저에서 http://localhost:8000 → `demo` / `demo1234` 로그인

1. 대시보드에서 원천 CSV(`data/agent_판매분석가_일별상세.csv`)와 보고서 PDF(`reports/2026-06_ST001_월간판매실적보고서.pdf`)를 올리고 **분석 시작**을 누른다. "장애 시연"을 켜면 첫 도구 호출이 실패하고 10초 뒤 큐 재시도로 이어서 진행된다.
2. 작업 화면에서 진행 이벤트를 본다. 약 1~2분 뒤 **담당자 확인 질문**이 뜨면 답한다.
3. 완료되면 계산 규칙·판단 근거·매뉴얼 탭과 결과물을 확인한다.
4. "결과물 · 다음 기간" 탭에서 같은 CSV와 `2026-07`을 넣으면 7월(부분월) 보고서 초안이 만들어진다.

## 개발용 명령

```bash
uv run pytest                                   # 단위 + API 통합 테스트 (통합 테스트는 DB 필요)
uv run python scripts/run_local.py --auto-answer  # 큐·API 없이 Agent만 실행 (질문에 자동 응답)
uv run python scripts/eval_run.py <job_id>      # 정답표와 비교
```

실제 모델 검증과 모의 테스트는 구분된다. `tests/`는 모델을 부르지 않고, `run_local.py`·`eval_run.py`·화면 실행은 실제 OpenAI 모델을 부른다.

## 폴더

```text
src/rework/
├── api/            FastAPI (인증·파일·작업·질문·결과·화면)
├── auth/           JWT·비밀번호
├── queue/          RabbitMQ 큐 구성·outbox 발행기
├── workers/        Worker (중복 방지·임대·재시도)
├── agent/          LangGraph 그래프·노드·프롬프트·MCP 클라이언트·실행 진입점
├── business_mcp/   MCP 서버 + 서비스 (데이터·PDF 파서·계산·분석·결과물)
├── rag_mcp/        MCP 서버 + 색인·검색
├── contracts/      규칙·판단 기준 스키마
├── db/             SQLAlchemy 모델·세션
└── web/            템플릿·CSS·JS
data/               원천 데이터
reports/            완성 보고서 PDF (Agent 입력)
docs/knowledge/     시연용 업무 지침 (RAG)
tests/fixtures/ground_truth/   정답표·보고서 원본 HTML (Agent 입력 아님)
scripts/            시드·로컬 실행·평가
var/                업로드·결과물 저장소 (Git 제외)
```
