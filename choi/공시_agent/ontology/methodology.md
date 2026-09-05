# 온톨로지 구축 방법론

METHONTOLOGY/NeOn류의 경량 절차(목적·범위 → 개념화 → 형식화 → 검증)를 따랐다. 아래는 각
단계에서 실제로 내린 결정과 근거다 — 나중에 "왜 이렇게 만들었나"를 설명할 때 이 문서를
그대로 인용하면 된다.

## 1. 목적과 범위

기존 `choi/공시_agent/qa/` 파이프라인은 이미 잘 동작하는 규칙 기반 QA 시스템이지만, 그
안의 "온톨로지"는 3곳(`facts.py`의 XBRL 지표 사전, `field_ontology.json`의 비-XBRL 필드
사전, `ontology.py`의 개념매핑 로직)에 흩어진 파이썬 dict/JSON이었다. 공식 스키마도, 표준
어휘(OWL/RDF)와의 접점도 없었다.

목적은 두 가지다.
1. 이 3곳을 하나의 OWL 스키마로 통합해 **재무 도메인 표준(FIBO)에 견줄 수 있는 형태**로
   만든다.
2. 실제 rdflib 산출물(TBox+ABox)로 만들어 SPARQL로 검증 가능하게 한다 — 개념 설명에
   그치지 않는다.

**하지 않은 것**: 런타임 QA 파이프라인을 SPARQL/OWL 추론으로 대체하는 것. `ontology.py`는
309문항 골드셋 기준으로 이미 검증된 시스템이라, 이 RDF 산출물은 그 옆에 서는 정식 문서화
산출물이지 대체재가 아니다.

## 2. 소스 매핑 (개념화의 근거)

TBox의 모든 클래스/속성은 다음 8개 원본 파일 중 하나에서 유도했다 (자세한 표는 계획서
§1). 어느 것도 추측으로 만들지 않았다 — 각 클래스의 `rdfs:comment`에 출처 파일을 인용해
뒀다(`tbox.ttl` 참고).

## 3. 개체(individual)로 둘 것 vs 클래스로 세분할 것

Metric(31)·StructuredField(387)·AccountItem(수백)·DerivedConcept(9)는 전부 개체로
모델링했다. 클래스로 세분하는 안도 검토했으나 기각했다:

- SPARQL 검색 난이도는 동일하다 — `?m a dart:Metric; dart:metricKey "revenue"`와
  `?m a dart:RevenueMetric`은 조회 편의성 차이가 없다.
- 클래스로 만들면 TBox가 20여 개에서 400개+로 부풀어, "체계적으로 설계된 스키마"라는
  발표 목적과 배치된다.
- `field_ontology.json`은 코퍼스에서 자동 추출되는 데이터다 — 새 공시가 나올 때마다 새
  필드가 발견되면 개체(ABox) 추가만으로 확장되지만, 클래스로 모델링하면 스키마(TBox)
  자체를 매번 고쳐야 한다.
- FIBO도 통화 코드·지수명처럼 열거 가능한 대규모 어휘는 클래스가 아니라 개체로 다룬다 —
  같은 관례를 따랐다.

## 4. SKOS 병용

`concepts.py`의 `surfaces`(표기변형)·`synonym_group`(동의어군)은 W3C SKOS(Simple
Knowledge Organization System)의 `skos:prefLabel`(대표 표기)/`skos:altLabel`(동의어)에
그대로 대응된다. 예: Metric `revenue` → prefLabel "매출액", altLabel "매출"·"영업수익"·
"수익(매출액)"·"수익". OWL(클래스/속성/추론)과 SKOS(어휘/동의어)를 병행하는 것은 W3C가
권장하는 표준 패턴이다.

## 5. n항관계 reification — 파생비율 공식

`derived.py`의 `RULES`는 `(연산자, (피연산자1, 피연산자2), 단위, 배수, 검증대상metric_key)`
튜플이다 — OWL 객체속성 하나로 표현할 수 없는 n항관계라서, 표준 W3C n-ary relation
패턴으로 `dart:DerivationRule` 클래스를 만들어 reify했다. `hasOperand1`/`hasOperand2`/
`multiplier`/`resultUnit`/`validatesAgainstMetric`을 개별 속성으로 갖는다.

검증 데이터(`reproductionRate`·`matchCount`·`mismatchCount`·`missingOperandCount`·
`validationStatus`)는 `derived_validation.json` 캐시에서 **그대로 옮겼다 — 재계산하지
않는다**. 예: 부채비율 98.3%(170/173, 실측), 유동비율 92.7%. 나머지 7개 규칙은
"불가/not_applicable"(코퍼스에 명시값 자체가 없어 검증 불가) — "실패"가 아니라는 점을
`validationStatus`의 3분류(`validated`/`failed`/`not_applicable`)로 구분했다.

## 6. FIBO 정합성 — 무엇을 참고했고 어디서 멈췄는가

FIBO(Financial Industry Business Ontology)는 EDM Council이 주관하고 OMG(Object
Management Group)가 표준화한 금융 도메인 OWL 온톨로지다. Citigroup·Goldman Sachs·Wells
Fargo 등 88개 금융기관이 검토에 참여했고, 2020년부터 오픈 커뮤니티 프로세스로 개발되며
GitHub(edmcouncil)에 공개돼 있다 — 신빙성 있는 산업 표준이다(2026-08-28 공식 사이트
spec.edmcouncil.org/fibo/ 확인).

다만 FIBO는 국제 도매금융(법인·증권·파생상품·시장데이터) 중심이라 K-IFRS 계정과목이나
한국 DART 공시 특유 필드(공급계약금액, 대량보유 지분율)는 다루지 않는다. 그래서:

| 클래스 | FIBO 대응 | 판정 |
|---|---|---|
| `dart:Company` | BE(Business Entities) 모듈 `LegalEntity`/`FormalOrganization` | 개연성 있음 — `rdfs:seeAlso`만 |
| `dart:Document` | FND "Documents and Communications" `Document`/`Report` | 개연성 있음 — `rdfs:seeAlso` |
| `dart:Statement` 계열 | FND Accounting에 근접 개념 있으나 공개 IRI 불확실 | 불확실 — 링크 보류 |
| `dart:Metric`/`AccountItem`/`DerivedConcept`/`StructuredField` | 없음 | **완전 커스텀** |

**중요한 원칙**: FIBO IRI를 기억에 의존해 지어내지 않는다. 이번 구현에서는 확인되지 않은
IRI를 `owl:equivalentClass`는커녕 `rdfs:seeAlso`로도 걸지 않았다(`tbox.ttl`에 FIBO 링크
없음) — 실제 FIBO 스펙에서 정확한 IRI를 확인하기 전까지는 텍스트 인용(이 문서)에만
머무르는 것이 근거 없는 매핑보다 정직하다.

## 7. ABox 표본 바운딩 — 계획 대비 실측 조정

최초 계획(§6)은 "3,000~6,000 트리플"을 목표로 했으나, 실제로 만들어보니 두 가지가
계획보다 훨씬 컸다.

1. **XBRL facts**: 표본 5개사·해당 연도의 `facts.jsonl` 원본 행은 4,497건인데, 그중
   metric_key가 잡히는(=31개 정규지표 중 하나에 해당하는) 것은 처음엔 367건으로 보였다.
   원인을 보니 `facts.jsonl`의 `metric_key` 필드는 **원본 choi facts.py(10개 지표)로 추출
   시점에 이미 구워진 값**이라, 확장 21개 지표(유동자산·재고자산·영업활동현금흐름 등)는
   실제로 라벨이 일치해도 파일에는 `null`로 남아 있었다(ragrag 확장은 "재파싱 없이
   라벨→키 매핑만 넓히는" 방식이라 파일 자체는 그대로다). 그래서 파일의 `metric_key`를
   신뢰하지 않고 31개 지표 전체 라벨→키 매핑으로 **재계산**했고, 최종 956건이 됐다.
2. **비-XBRL structured facts**: `holding`(대량보유상황보고서) 문서군은 표본 5개사만으로도
   366,231건이었다 — 대주주 지분이 바뀔 때마다 새 신고서가 나오는 문서군 특성상 "표본을
   줄인다"가 아니라 애초에 개체 수가 사실상 무계에 가깝다. 그래서 `holding`은 어휘
   (`StructuredField`)로만 남기고 인스턴스(`StructuredFact`)는 만들지 않았다. `major`/
   `exchange`는 회사당 문서군당 최대 8건으로 자르되, competency question CQ5가 참조하는
   대우건설의 두 정정 필체(2024-06-25, 2025-07-29)는 캡과 무관하게 강제 포함했다.

최종 ABox는 XBRLFact 956건 + StructuredFact 48건(major/exchange만) + 전량 어휘
(Metric 31·DerivedConcept 9·StructuredField 387·Sector 28) = 약 2만 트리플 수준이다.
계획의 원래 추정치보다 크지만, 대부분(387개 필드 어휘)이 "전량 포함하기로 이미 정한"
부분이라 바운딩 원칙 자체는 지켜졌다 — 코퍼스 전체(213,694 facts, 190만+ structured
facts)에 비하면 여전히 작은 표본이다.

## 8. 검증

`validate_graph.py`가 두 층위를 검증한다.

- **(a) 구문 유효성**: TBox/ABox 각각 단독 파싱 성공, 병합 후 카디널리티 제약(모든
  XBRLFact가 of/about/hasYear/hasScope/citedIn을 정확히 1개씩, 모든 DerivationRule이
  hasOperand1/2를 정확히 1개씩) + 매달린 참조 없음을 SPARQL로 확인.
- **(b) 원본 파이프라인 대비 정합성**: (i) SPARQL 결과가 `facts.jsonl` 원본 값과 정확히
  일치(같은 출처이므로 오차 0), (ii) `ontology.py.parse()`가 같은 질문에서 같은
  metric_key를 resolve하는지(매핑 레이어 검증), (iii) `DerivationRule.reproductionRate`가
  `derived_validation.json` 캐시와 정확히 일치.

결과는 `validation_report.md`에 있다.
