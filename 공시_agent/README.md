# 공시 QA 에이전트

## 소개

DART 공시(사업보고서 등)에 자연어로 물으면, **XBRL fact store에서 직접 조회한 값**과
**근거 좌표**(회사·보고서·접수번호·재무제표 목차 위치·셀)를 함께 돌려주는 QA 에이전트다.
미래에셋 AI Festival 예선 제출물이다.

일반적인 벡터 검색 기반 RAG 챗봇과는 결이 다르다. XBRL 공시는 "숫자가 어느 표 몇 행 몇 열에
있는가"가 명확한 구조화 데이터라서, 이 프로젝트는 **임베딩으로 비슷한 문장을 찾아 LLM이 값을
추정하게 하는 대신, 온톨로지로 질문을 분해해 구조화 저장소에서 값을 직접 조회**한다.

핵심 특징:

- **수치는 LLM이 만들지 않는다** — 조회는 XBRL fact store, 계산은 `Decimal`
- **모든 답변에 근거 좌표가 붙는다** — 회사·보고서·접수번호·셀 위치까지
- **응답이 빠르다** — 색인을 미리 올려둬 요청당 0.1~0.2초
- **모르면 지어내지 않는다** — 답을 못 내는 경우도 오류가 아니라 상태 코드(S1~S6)로 명시

## 주요 기능

- 자연어 질의 해석 — 기업·기간·지표·업종 추출 (`qa/ontology.py`, 개념 어휘 3,466개)
- XBRL fact 213,909건 조회 — 정정·폐기 공시 대신 유효 공시 자동 선별 (`qa/labelstore.py`)
- 파생지표 계산 9종(부채비율·영업이익률 등) — 공식을 공시 실린 값으로 자체 검증 (`qa/derived.py`)
- 답변 5중 교차검증 + 합계관계 검증(`qa/hierarchy.py`) — 근거 좌표 동봉
- (선택) HyperCLOVA X 기반 서술형 답변 생성 — 기본은 비활성, 필요 시 `CLOVA_API_KEY`로 활성화

## 시스템 아키텍처

```
질문 → [01] 온톨로지 매핑 → [02] 검색(fact store) → [03] 계산 → [04] 검증 → [05] 답변
```

| 단계 | 모듈 | 역할 |
|---|---|---|
| 01 | `qa/ontology.py` | 질문에서 기업·기간·지표·업종을 뽑는다 |
| 02 | `qa/labelstore.py` | XBRL fact 색인에서 값을 조회한다 |
| 03 | `qa/derived.py`, `qa/pipeline.py` | 파생지표 계산 (`Decimal`) |
| 04 | `qa/hierarchy.py`, `qa/applicability.py` | 합계 관계·업종 적합성 검증 |
| 05 | `server.py` / `qa/render.py` | JSON(API) 또는 사람이 읽는 화면으로 출력 |

배포 구조 (평가용 API 서버):

```
평가자 → HTTPS(443) → Caddy(자동 인증서) → 127.0.0.1:8000 → uvicorn(FastAPI) → qa.pipeline
```

Caddy가 `49-50-141-164.sslip.io`(무료 wildcard DNS, 이 IP로 자동 매핑) 앞단에서 Let's Encrypt
인증서로 자동 HTTPS를 붙여 8000번 포트(uvicorn)로 리버스 프록시한다. NCP(Naver Cloud Platform)
VPC 위에서 운영되며, 방화벽(ACG)에서 TCP 80·443만 전체 공개로 열려 있다.

## 기술 스택

| 구분 | 사용 기술 |
|---|---|
| Backend | Python, FastAPI, Uvicorn, Pydantic |
| 데이터 저장 | SQLite(`facts.db`, `tables.db`) + JSONL 색인(`factstore.jsonl` 등) — **벡터 DB 아님**, 구조화 XBRL 직접 조회 |
| 파싱 | lxml (XBRL 파싱) |
| LLM | HyperCLOVA X (CLOVA Studio, OpenAI 호환 API) — 서술형 답변 생성에만 선택적 사용, 수치 계산엔 미사용(대회 규정상 허용 LLM) |
| Frontend | 정적 HTML/바닐라 JS 데모 페이지 (`web/index.html`) |
| 배포 | Docker, Caddy(자동 HTTPS), NCP(Naver Cloud Platform) |

## 빠른 시작

```bash
conda activate RAGRAG
pip install -r requirements.txt

# API 서버 (색인 로드에 약 12초)
uvicorn server:app --host 0.0.0.0 --port 8000

# 터미널에서 한 번만 물어볼 때
python scripts/ask.py "삼성전자의 2024년 연결 매출액은?"
```

실행 후 접속:

| 주소 | 내용 |
|---|---|
| `POST /ask` | 질문 → 답변·근거·검증 (자체 데모·평가용, 풍부한 스키마) |
| `GET /answer` | 질문 → 답변 (**대회 제출 고정 스키마** — 아래 "API 문서" 참고) |
| `GET /health` | 색인 로드 완료 여부 (`ready`) |
| `GET /docs` | 자동 생성된 API 명세서 + 브라우저 테스트 |
| `GET /` | 데모 웹 화면 |

필요한 데이터는 "프로젝트 구조" 아래 "데이터"에서 받아 배치해야 한다 — 없으면 서버가 뜨지 않는다.

## 환경 변수 설정

```bash
cp .env.example .env
# .env를 열어 CLOVA_API_KEY 값을 채운다
```

| 변수명 | 용도 | 필수 여부 |
|---|---|---|
| `CLOVA_API_KEY` | HyperCLOVA X(CLOVA Studio) 호출 — 서술형 답변 경로(narrative)에서만 쓰인다 | 선택 — 비어 있어도 `/ask`·`/answer` 기본 경로는 정상 동작 |

`.env` 파일 자체는 `.gitignore`에 있어 커밋되지 않는다. **API 키 값은 이 저장소 어디에도 포함돼
있지 않다** — `.env.example`은 키 이름만 담은 템플릿이다.

## 사용 방법

```bash
curl -X POST http://<HOST>:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"삼성전자의 2024년 연결 매출액은?"}'
```

```json
{
  "question": "삼성전자의 2024년 연결 매출액은?",
  "state": "S0",
  "answer": "삼성전자의 2024년 연결 기준 매출액은 300,870,903백만원 (300조 8,709억원)입니다.",
  "evidence": [{
    "corp": "삼성전자", "report": "사업보고서 (2024.12)",
    "path": "손익계산서(연결) > 매출액 (주29)", "cell": "r1:c56",
    "rcept_no": "20250311001085",
    "fact_id": "periodic_20250311001085:IS_C2:r1:c56", "flags": []
  }],
  "elapsed_ms": 169
}
```

`{"verbose": true}`를 주면 `detail`에 계산 과정·파이프라인 단계·검증 항목·해석 결과가 함께 온다.
브라우저에서 데모를 보려면 서버 실행 후 `http://<HOST>:8000/`을 열면 된다 (질문 입력창 + 답변·근거
표시 화면).

### 상태 코드

답을 못 내는 경우에도 **HTTP 200**이며 `state`로 구분한다. 데이터가 없는 것과 질문이 모호한 것은
서로 다른 상황이고 오류가 아니기 때문이다.

| `state` | 뜻 |
|---|---|
| `S0` | 정상 답변 |
| `S1` | corpus에 데이터가 없음 (지어내지 않음) |
| `S2` | 답변하되 검증 미흡 — 신뢰도 낮음 |
| `S3` | 질문이 모호해 되물음 |
| `S6` | 아직 지원하지 않는 질문 유형 |

## API 문서

`GET /docs`에서 자동 생성된 명세서(Swagger UI)로 브라우저에서 바로 테스트할 수 있다.

**대회 제출용 평가 API — End-point URL**: `https://49-50-141-164.sslip.io/answer`

미래에셋 AI Festival 예선 평가 스키마에 맞춘 고정 포맷 엔드포인트다. 새 파이프라인이 아니라
`/ask`와 완전히 같은 `qa.pipeline.run()` 결과를 대회가 요구하는 필드 이름으로 재포장한 것뿐이다
(`server.py`의 `answer_eval()` 참고).

```bash
curl -G "https://49-50-141-164.sslip.io/answer" \
  --data-urlencode "question_id=Q-001" \
  --data-urlencode "question=삼성전자의 2024년 연결 매출액은?"
```

```json
{
  "question_id": "Q-001",
  "question": "삼성전자의 2024년 연결 매출액은?",
  "retrieved_context": "[삼성전자 · 사업보고서 (2025.12) · 접수번호 20260310002820] III. 재무에 관한 사항 > 2-2. 연결 손익계산서 > 매출액 (주30): 300,870,903백만원",
  "think_trace": "[01] 온톨로지/개념 매핑: 완료 — 삼성전자 · 2024년 · 매출액 (metric) · intent=fact_numeric\n[02] 검색(RAG): 완료 — fact 1건\n[03] 계산/추론: 완료\n[04] 도메인 전문가 검증: 완료 — 모든 규칙 통과\n[05] 답변 생성: 완료\n계산 과정:\n  단순 조회 — 계산 없음 (store의 fact 값을 그대로 사용)",
  "answer": "삼성전자의 2024년 매출액은 연결 300,870,903백만원"
}
```

| 필드 | 내용 |
|---|---|
| `question_id` | 요청에 실어 보낸 값을 그대로 되돌려준다 |
| `question` | 요청 원문을 그대로 되돌려준다 |
| `retrieved_context` | 답변 생성에 쓴 근거 — 회사·보고서명·접수번호·재무제표 목차 위치·값 |
| `think_trace` | [01]~[05] 파이프라인 단계별 진행상황 + 계산 과정 (사고·추론·도구 사용 과정) |
| `answer` | 최종 생성 답변 (`/ask`의 `answer`와 동일한 문장) |

## 프로젝트 구조

이 저장소는 `공시_agent/`(이 README) 외에 **형제 디렉토리 `code_chunkingandparsing/`이 반드시
같은 부모 디렉토리 아래 있어야** 동작한다 — `qa/*.py`가 그 폴더를 상대 경로로 참조한다
(예: `qa/kg.py`의 `import numqa`).

```
(레포 루트)
├── 공시_agent/                    ← 이 README가 있는 곳. 배포되는 QA 서비스 본체
│   ├── qa/                       파이프라인 모듈 (01~05 단계, 아래 표 참고)
│   ├── evaluation/                내용/행동 정확도 평가 스크립트
│   ├── scripts/                   CLI 도구 (ask.py 등)
│   ├── tests/                     회귀 테스트
│   ├── web/                       데모 웹 화면 (index.html)
│   ├── server.py                  HTTP API (FastAPI)
│   ├── mcp_server.py              MCP 도구 (stdio JSON-RPC)
│   ├── data/                      런타임 캐시 — 없으면 자동 재생성 (아래 "데이터" 참고)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── .env.example
└── code_chunkingandparsing/       ← 공시_agent의 필수 의존성 (numqa 등 파싱·팩트 추출 코드)
    ├── src/                       공시_agent/qa가 import — 실행 필수
    └── out/                       원본 팩트 데이터(factstore.jsonl 등) — 별도 다운로드
```

`qa/` 안의 핵심 모듈:

| 모듈 | 역할 |
|---|---|
| `qa/ontology.py` | 질문에서 기업·기간·지표·업종을 뽑는다 |
| `qa/concepts.py` | 개념 어휘 3,466개 — 표기변형·동의어·부분어·모호성 |
| `qa/labelstore.py` | XBRL fact 213,909건 색인. 폐기된 공시 대신 유효 공시를 고른다 |
| `qa/derived.py` | 파생 지표 9종 (부채비율·영업이익률 등). 공식을 공시 실린 값으로 검증 |
| `qa/hierarchy.py` | 합계 관계 58건을 corpus에서 유도 → 04의 합계 검증 |
| `qa/applicability.py` | 업종별 지표 적합성 (은행에 재고자산회전율은 부적합) |
| `qa/period.py` | 기간 — 범위·추이·CAGR·"가장 최근"·기수 |
| `qa/pipeline.py` | 01~05 오케스트레이션 |

### 데이터

런타임에 필요한 파일은 **1.37 GB**다. 원본 corpus 전체(4.4GB)를 올릴 필요는 없다.

| 파일 | 크기 | 성격 |
|---|---|---|
| `code_chunkingandparsing/out/factstore.jsonl` | 1,087 MB | 원본 — 필수 |
| `data/corpus/manifest.jsonl` | 2 MB | 원본 — 필수 |
| `공시_agent/data/*.jsonl`, `*.json` | 285 MB | 캐시 — 없으면 원본에서 재생성(약 45초) |

캐시는 `.gitignore`에 있다. 원본이 있으면 첫 실행 때 자동으로 만들어진다.

**선택 사항**: `data/corpus/raw/`(DART 공시목록 원자료, 5GB+)는 없어도 대부분의 질문에
영향이 없다 — `qa/boolean.py`의 "이 문서가 그 뒤로 또 정정됐는가" 같은 일부 문서-존재
확인 질의에서만 쓰이고, 없으면 그 부분만 조용히 빈 결과로 넘어간다(`qa/rawcorpus.py`).
용량 때문에 이번 데이터 패키지엔 포함하지 않았다.

**다운로드**: 위 원본 파일들을 압축한 `data.zip`을 받아 위 트리와 같은 경로에 풀어 넣는다.

- 다운로드 링크: `<TODO: 클라우드 스토리지 공유 링크>`
- 압축 해제 위치: `code_chunkingandparsing/out/factstore.jsonl`, `data/corpus/manifest.jsonl`

## 배포 및 운영

```bash
docker build -t gongsi-agent .

docker run -p 8000:8000 \
  -v /path/to/code_chunkingandparsing:/code_chunkingandparsing:ro \
  -v /path/to/data/corpus:/data/corpus:ro \
  -v /path/to/공시_agent/data:/app/data \
  gongsi-agent
```

데이터는 이미지에 넣지 않고 볼륨으로 마운트한다 (원본만 1.4GB라 이미지가 비대해진다).
`code_chunkingandparsing`은 `src`(numqa 등)와 `out`(factstore.jsonl 등)을 **폴더째** 마운트해야
한다 — `qa/*.py`가 이 폴더를 컨테이너 루트의 형제 디렉토리로 상대 참조하기 때문에, `src`·`out`을
따로 쪼개서 마운트하면 경로를 못 찾는다.

**헬스체크**: `GET /health` → `{"status":"ok","ready":true,"facts":213694,"corps":70,"load_ms":...}`.
색인 로드에 12초가 걸리는 동안 `ready:false`를 돌려주며, Dockerfile의 `HEALTHCHECK`가
`ready:true`가 될 때까지 컨테이너를 `starting` 상태로 유지한다(`--start-period=90s`).

**장애 시 확인할 것**:
- `/health`가 계속 `ready:false`거나 응답이 없으면 → 색인 로드 중이거나 데이터 파일 경로가
  잘못 마운트된 것. 컨테이너 로그(`docker logs`) 또는 `data/server_errors.jsonl`(개별 요청
  예외 기록, `server.py` 참고) 확인.
- 재시작: `docker run` 재실행. 색인은 메모리에만 있어 별도 정리 없이 재기동하면 된다.

**운영 메모**:
- 색인 로드 12초 — 서버 시작 때 미리 올리고, `/health`가 `ready:true`가 된 뒤 트래픽을 받는다.
- 워커당 메모리 764 MB — 4GB 서버면 워커 1~2개.
- 평가 파이프라인: `python evaluation/evaluate.py --dataset <골드셋 경로>` — 내용 정확도와
  **행동 정확도**(답했어야 했는가)를 따로 재고, 오류가 난 서브에이전트 단계·근거 좌표 일치
  여부·진단 차트 4종이 리포트에 함께 나온다.

## 보안 유의사항

- **API 키는 절대 커밋하지 않는다.** `CLOVA_API_KEY`는 `.env`(gitignore 처리됨)로만 관리하고,
  이 저장소에는 `.env.example`(키 이름만 있는 템플릿)만 포함돼 있다.
- **SSH(22)·RDP(3389)는 특정 IP만 허용**한다 — NCP ACG(Access Control Group)에서 인바운드
  규칙을 팀원 공인 IP로 좁혀뒀다. 대회 요건상 API 트래픽용 80·443만 `0.0.0.0/0`으로 열려 있다.
- **평가는 HTTPS 경로로만 받는다.** `https://49-50-141-164.sslip.io/answer`가 정식 엔드포인트고,
  uvicorn이 직접 듣는 8000번 포트는 HTTP·인증서 없음 상태라 팀 내부 점검용으로만 쓴다.

## 제한 사항 및 향후 계획

- 서술형(narrative) 답변 경로는 기본 비활성 — `CLOVA_API_KEY` 설정 시에만 동작한다.
- `data/corpus/raw/`(원문 raw 코퍼스) 없이는 일부 "문서 존재/정정 이력 확인" 질의(`qa/boolean.py`)에
  한해 답을 못 낼 수 있다 — 그 외 수치 조회 경로엔 영향 없다.
- `scripts/holdout.py`, `scripts/regression_check.py`는 로컬 개발용 회귀테스트 도구로, 이번
  제출 데이터 패키지에 포함되지 않은 별도 골드셋이 있어야 동작한다 (필수 실행 경로 아님).
- 향후 계획: 서브에이전트 단계별 오류 추적 세분화(`scripts/evaluate.py`가 뼈대만 있는 상태),
  업종·지표 온톨로지 확장(`ragrag/` 트랙에서 진행 중).

## License

이 저장소는 제10회 미래에셋증권 AI Festival 예선 제출용으로 작성됐다. 별도의 오픈소스
라이선스를 지정하지 않았으며, 재사용·배포 관련 문의는 팀에 직접 연락 바란다.
