# RAGRAG

공시(DART) 데이터를 근거로 자연어 질문에 답하는 RAG 기반 QA 시스템 개발 레포.

## 구성

| 폴더 | 내용 |
|---|---|
| [`공시_agent/`](공시_agent/README.md) | **공시 QA 에이전트** — 실제 배포되는 QA 서비스. 미래에셋 AI Festival 제출물이자 이 레포의 메인 산출물. 실행 방법·API 명세·평가 파이프라인은 해당 README 참고 |
| [`code_chunkingandparsing/`](code_chunkingandparsing) | 공시 원문 파싱·청킹·팩트 추출 — `공시_agent/qa`가 직접 import해서 쓰는 필수 의존성 (`numqa` 등) |
| [`ragrag/`](ragrag) | 검색 랭킹·지표 온톨로지 확장 파이프라인 — `공시_agent`가 쓰는 metric 사전을 10개에서 31개로 확장하는 등, 위 두 폴더의 이식·회귀 검증 작업 트랙 |

## 빠른 시작

실제 서비스를 띄우거나 API를 호출하려면 [`공시_agent/README.md`](공시_agent/README.md)부터 보면 된다 —
환경 구성, `docker build`/`docker run`, 평가용 API 엔드포인트가 전부 거기 있다.
