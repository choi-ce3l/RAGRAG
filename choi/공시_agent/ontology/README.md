# DART 공시 온톨로지 (OWL/RDF)

`choi/공시_agent/` QA 파이프라인에 흩어져 있던 3곳의 온톨로지(재무지표 사전, 비-XBRL
구조화 필드 사전, 개념매핑 로직)를 하나의 OWL/RDF 스키마로 통합한 것. 공모전 제출물로
**실제 RDF/OWL 산출물**이 필요해서 시작했고, FIBO(금융산업 OWL 표준)를 참고점으로 삼되
K-IFRS 계정과목·한국 공시 특유 필드는 FIBO 범위 밖이라 대부분 자체 확장 스키마다.

설계 배경과 결정 근거는 [`methodology.md`](methodology.md), 검증용 질문은
[`competency_questions.md`](competency_questions.md)에 있다.

## 원본과의 관계 (읽기 전용)

이 폴더의 어떤 스크립트도 아래 파일을 수정하지 않는다 — 전부 import/읽기만 한다.

| 원본 | 내용 |
|---|---|
| `choi/code_chunkingandparsing/src/facts.py` | XBRL 핵심 지표 10개 (`_ONTOLOGY`) |
| `ragrag/pipeline/facts.py` | 확장 지표 21개 (`_ONTOLOGY_EXT`) — ABox는 이 31개 기준 |
| `choi/공시_agent/qa/concepts.py` | 계정과목 정규화·동의어그룹·커버리지 (`ConceptIndex`) |
| `choi/공시_agent/qa/derived.py` | 파생비율 9개 공식 + 재현율 캐시 |
| `choi/공시_agent/qa/fields.py`, `data/field_ontology.json` | 비-XBRL 구조화 필드 387개 |
| `choi/공시_agent/qa/sectors.py` | 업종(20)/산업(8) 소속 |
| `choi/공시_agent/qa/ontology.py`, `kg.py` | 기존 개념매핑 로직·mermaid 스키마 그래프 |

## 재생성 방법

```bash
pip install -r requirements.txt        # rdflib, networkx, matplotlib
python build_tbox.py                   # -> tbox.ttl
python build_abox_sample.py            # -> abox_sample.ttl (수 분 소요 — 전체 코퍼스 스캔)
python validate_graph.py               # -> validation_report.md
python viz/schema_graph_export.py      # -> viz/schema_graph.json, viz/schema_graph.html
python viz/render_static.py            # -> viz/schema_graph.svg, viz/schema_graph.png
```

빌드 순서가 중요하다 — `viz/schema_graph_export.py`는 `tbox.ttl`을 읽고, `validate_graph.py`는
`tbox.ttl`+`abox_sample.ttl`을 읽는다.

## 산출물

- `tbox.ttl` — 클래스 27개(+`docGroup`/`fieldKey`처럼 여러 클래스에 걸쳐 쓰이는 데이터속성의
  `owl:unionOf` 도메인을 표현하기 위한 익명 클래스 2개, +Scope 개체 2개), 객체속성 15개
  (+역속성 `memberOf` 1개), 데이터속성 41개. 코퍼스 데이터 없이 스키마만(351 트리플).
- `abox_sample.ttl` — 5개 표본기업(삼성전자 2022~2024 3개년, SK하이닉스·KB금융·대우건설·
  NAVER는 2024년만)의 실제 재무 fact + 어휘(Metric 31·DerivedConcept 9·StructuredField
  387·Sector 28) 전량. 전체 코퍼스(213,694 facts)가 아니라 표본이다.
- `sample_queries/*.rq` — competency question별 SPARQL.
- `viz/schema_graph.html` — 발표용 인터랙티브 스키마 뷰어(오프라인 단일 파일, 브라우저로 직접 열기).
- `viz/schema_graph.svg`, `viz/schema_graph.png` — 슬라이드 삽입용 정적 이미지.
- `validation_report.md` — 구문 유효성 + 원본 파이프라인 대비 정합성 검증 결과.

## 이 온톨로지가 하지 않는 것

- 런타임 QA 파이프라인(`ontology.py`/`derived.py`)을 SPARQL로 대체하지 않는다 — 파이프라인은
  그대로 두고, 이 RDF 산출물이 같은 답을 내는지 검증할 뿐이다.
- 전체 코퍼스를 인스턴스화하지 않는다 — ABox는 의도적으로 유계 표본이다(§6 in the plan).
- FIBO 클래스에 `owl:equivalentClass`를 함부로 걸지 않는다 — 확인되지 않은 FIBO IRI는
  `rdfs:seeAlso`조차 넣지 않고 모듈명만 텍스트로 인용한다(`methodology.md` 참고).
