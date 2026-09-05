# 공시 QA 에이전트

공시 데이터에 자연어로 물으면 **답변과 근거 좌표**(접수번호·재무제표·셀)를 함께 돌려주는 에이전트.

**수치는 LLM이 만들지 않는다.** 조회는 XBRL fact store에서, 계산은 `Decimal`로 한다.
그래서 답변의 모든 숫자에 "어느 공시 어느 셀에서 왔는지"가 붙고, 응답이 0.13초로 빠르다.

## 저장소 구성

```
(레포 루트)
├── 공시_agent/                    (이 README가 있는 곳)
│   ├── qa/                       파이프라인 모듈
│   ├── server.py                 HTTP API
│   ├── data/                     런타임 캐시 — 없으면 자동 재생성 (아래 "데이터" 참고)
│   └── .env.example
└── code_chunkingandparsing/
    ├── src/numqa.py               공시_agent/qa가 import하는 파싱·팩트 추출 코드 — 실행 필수
    └── out/factstore.jsonl        원본 팩트 데이터 — 별도 다운로드 (아래 "데이터" 참고)
```

`공시_agent/`와 `code_chunkingandparsing/`은 **레포 루트 기준으로 형제 디렉토리여야 한다** —
`qa/*.py`가 `../code_chunkingandparsing/src`, `.../out`을 상대 경로로 참조하기 때문이다
(`qa/kg.py`의 `import numqa` 등).

## 빠른 시작

```bash
conda activate RAGRAG
pip install -r requirements.txt

# API 서버 (색인 로드에 약 12초)
uvicorn server:app --host 0.0.0.0 --port 8000

# 터미널에서 한 번만 물어볼 때
python scripts/ask.py "삼성전자의 2024년 연결 매출액은?"
```

| 주소 | 내용 |
|---|---|
| `POST /ask` | 질문 → 답변·근거·검증 (자체 데모·평가용, 풍부한 스키마) |
| `GET /answer` | 질문 → 답변 (**대회 제출 고정 스키마** — 아래 참고) |
| `GET /health` | 색인 로드 완료 여부 (`ready`) |
| `GET /docs` | 자동 생성된 API 명세서 + 브라우저 테스트 |
| `GET /` | 데모 웹 화면 |

## 환경 변수

```bash
cp .env.example .env
# .env를 열어 CLOVA_API_KEY 값을 채운다
```

`CLOVA_API_KEY`는 서술형 답변 경로(narrative)에서만 쓰인다 — `/ask`·`/answer` 기본 경로는
비어 있어도 정상 동작한다 (대회 규정상 LLM은 HyperCLOVA X만 허용).

## 대회 제출용 평가 API

**End-point URL**: `https://49-50-141-164.sslip.io/answer`

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

방화벽(NCP ACG)에서 TCP 80·443을 `0.0.0.0/0`으로 열어야 하고, [Caddy](https://caddyserver.com)가
`49-50-141-164.sslip.io`(무료 wildcard DNS — 이 IP로 자동 매핑됨) 앞단에서 Let's Encrypt 인증서로
자동 HTTPS를 붙여 8000번 포트(uvicorn)로 리버스 프록시한다. 설정은 서버의
`/etc/caddy/Caddyfile` 한 줄이다.

### 요청·응답

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

## 구조

```
질문 → [01] 온톨로지 매핑 → [02] 검색 → [03] 계산 → [04] 검증 → [05] 답변
```

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
| `server.py` | HTTP API |
| `mcp_server.py` | MCP 도구 (의존성 없이 stdio JSON-RPC) |

## 데이터

런타임에 필요한 파일은 **1.37 GB**다. 원본 corpus 전체(4.4GB)를 올릴 필요는 없다.

| 파일 | 크기 | 성격 |
|---|---|---|
| `code_chunkingandparsing/out/factstore.jsonl` | 1,087 MB | 원본 — 필수 |
| `data/corpus/manifest.jsonl` | 2 MB | 원본 — 필수 |
| `공시_agent/data/*.jsonl`, `*.json` | 285 MB | 캐시 — 없으면 원본에서 재생성(약 45초) |

캐시는 `.gitignore`에 있다. 원본이 있으면 첫 실행 때 자동으로 만들어진다.

**다운로드**: 위 원본 파일들을 압축한 `data.zip`을 받아 "저장소 구성"과 같은 경로에 풀어 넣는다.

- 다운로드 링크: `<TODO: 클라우드 스토리지 공유 링크>`
- 압축 해제 위치: `code_chunkingandparsing/out/factstore.jsonl`, `data/corpus/manifest.jsonl`

## Docker 실행

```bash
docker build -t gongsi-agent .

docker run -p 8000:8000 \
  -v /path/to/code_chunkingandparsing:/code_chunkingandparsing:ro \
  -v /path/to/data/corpus:/data/corpus:ro \
  -v /path/to/공시_agent/data:/app/data \
  gongsi-agent
```

데이터는 이미지에 넣지 않고 볼륨으로 마운트한다 (원본만 1.4GB라 이미지가 비대해진다).
위 "데이터"에서 받은 파일을 호스트에 풀어 넣고 그 경로를 `-v`로 연결하면 된다.
`code_chunkingandparsing`은 `src`(numqa 등)와 `out`(factstore.jsonl 등)을 **폴더째** 마운트해야
한다 — `qa/*.py`가 이 폴더를 컨테이너 루트의 형제 디렉토리로 상대 참조하기 때문에, `src`·`out`을
따로 쪼개서 마운트하면 경로를 못 찾는다.
색인 로드에 12초가 걸리는 동안 `/health`는 `ready:false`를 돌려주며, `HEALTHCHECK`가
`ready:true`가 될 때까지 컨테이너를 `starting` 상태로 유지한다.

## 평가 파이프라인

```bash
python evaluation/evaluate.py --dataset <골드셋 경로>
```

내용 정확도와 **행동 정확도**(답했어야 했는가)를 따로 잰다. 오류가 난 서브에이전트 단계,
근거 좌표 일치 여부, 진단 차트 4종이 리포트에 함께 나온다.

## 운영 메모

- **색인 로드 12초.** 서버 시작 때 미리 올리고, `/health`가 `ready:true`가 된 뒤 트래픽을 받는다.
- **워커당 메모리 764 MB.** 4GB 서버면 워커 1~2개.
- API 키 관련은 "환경 변수" 참고.
