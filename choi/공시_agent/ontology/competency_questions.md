# Competency Questions

309문항 골드셋(`choi/공시_agent/`)의 실제 질문 패턴에서 뽑았다. CQ1~9는 `sample_queries/`에
실행 가능한 SPARQL로 만들어 `abox_sample.ttl`에 직접 실행·검증했다(아래 "실행 결과"는
2026-08-28 실측). CQ10만 서술로 남겼다 — 스키마가 못 다뤄서가 아니라, 비교 대상인
한화솔루션이 표본 5개사에 없어서다(표본을 넓히면 CQ2와 같은 패턴 그대로 동작한다).

## CQ1 — 단일조회
**"삼성전자의 2025년 연결 매출액은 얼마인가?"** (`01_ontology_mapper.md`의 실행 예시와 동일 패턴, 실제로는 2024년으로 테스트)
→ [`cq1_point_lookup.rq`](sample_queries/cq1_point_lookup.rq)
**실행 결과**: 300,870,903 백만원 — `facts.jsonl` 원본과 정확히 일치(`validate_graph.py` 확인).

## CQ2 — 순위
**"2024년 연결 자산총계 기준 상위 3개 기업은?"** (`ontology.py`의 `_RANKING`/`_TOPN` 패턴)
→ [`cq2_ranking.rq`](sample_queries/cq2_ranking.rq)
**실행 결과**: KB금융(757.8조원) > 삼성전자(514.5조원) > SK하이닉스(119.9조원).
**주의사항 하나 발견**: `valueDecimal`은 원문 표 셀 숫자 그대로라(16번 함수 계약), 회사마다
표기 단위가 다르면(백만원 vs 원) 그냥 비교하면 순위가 틀어진다 — `scale`을 곱해 원(KRW)
단위로 맞춰야 한다. 처음 쿼리를 안 그렇게 짰다가 NAVER가 1등으로 나오는 오답을 봤다
(`validation_report.md`가 아니라 이 문서를 만드는 과정에서 직접 잡음).

## CQ3 — 업종 합계
**"IT 업종 기업들의 2024년 연결 매출액 합계는?"**
→ [`cq3_sector_aggregate.rq`](sample_queries/cq3_sector_aggregate.rq)
**실행 결과**: 367,063,863,000,000원(삼성전자+SK하이닉스). `sector_index.json` 실측 결과
표본 5개사 중 industry="IT" 소속은 이 둘뿐이다 — NAVER는 "커뮤니케이션서비스"로 분류된다.

## CQ4 — 파생비율
**"삼성전자의 2024년 부채비율은?"**
→ [`cq4_derived_ratio.rq`](sample_queries/cq4_derived_ratio.rq)
**실행 결과**: 27.93%, 이 규칙 자체의 코퍼스 재현율(reproductionRate) 98.3%도 같이 조회됨.
"부채비율"이라는 이름이나 공식을 쿼리에 하드코딩하지 않고, `DerivationRule` reification이
그래프 안에 들고 있는 연산자·피연산자·배수를 그대로 따라간 결과다.

## CQ5 — 비-XBRL 구조화 필드
**"대우건설의 행당제7구역 주택재개발정비사업 공급계약, 2024-06-25 변경계약과 2025-07-29
변경계약의 계약금액 차이는?"** (gold qid `SEM-NUM-03` 실문항)
→ [`cq5_structured_field.rq`](sample_queries/cq5_structured_field.rq)
**실행 결과**: 250,969,804,000원 → 255,316,876,300원, 차이 4,347,072,300원.
**데이터 품질 이슈 하나 발견**: 원본 `factx.jsonl`에 같은 `field_key`("계약금액(원)")로
두 값이 잡히는데, 하나는 "계약금액(원)" 단독 셀, 하나는 "2. 계약내역 - 계약금액(원) -
매출액대비(%)" 병합 셀이 이어붙은 오염된 값이다(예: "219,229,000,000 2.69"). 정규식으로
순수 숫자만 걸러 우회했다 — 이 온톨로지가 원본 데이터 품질 문제를 고치는 것은 아니고,
쿼리 작성 시 알고 있어야 할 함정으로 `cq5_structured_field.rq`에 남겨뒀다.

## CQ6 — Scope 비교
**"삼성전자의 2024년 연결과 별도 매출액 차이는?"**
→ [`cq6_scope_comparison.rq`](sample_queries/cq6_scope_comparison.rq)
**실행 결과**: 연결 300,870,903 백만원 − 별도 209,052,241 백만원 = 91,818,662 백만원
(같은 회사·같은 표라 scale 보정 불필요 — CQ4와 같은 이유).

## CQ7 — 커버리지
**"재고자산 계정을 보고한 기업은 이 샘플에서 몇 개인가?"**
→ [`cq7_coverage.rq`](sample_queries/cq7_coverage.rq)
**실행 결과**: 51개 기업. `coverageCount`는 표본이 아니라 전체 코퍼스(213,694 facts) 기준
실측치다(§6 설계 의도 — `ConceptIndex`를 전체 코퍼스로 만들었기 때문).

## CQ8 — 추이
**"삼성전자의 2022~2024년 연결 매출액 추이는?"**
→ [`cq8_trend.rq`](sample_queries/cq8_trend.rq)
**실행 결과**: 2022년 302,231,360 → 2023년 258,935,494 → 2024년 300,870,903 (백만원).
ABox에 삼성전자만 3개년(2022~2024)을 넣어둔 이유가 이 질문 때문이다(§6 바운딩 설계).

## CQ9 — 동의어
**"NAVER의 영업수익(매출액)은 얼마인가?"**
→ [`cq9_synonym.rq`](sample_queries/cq9_synonym.rq)
**실행 결과**: 10,737,719,264,647원. NAVER는 "매출액"이 아니라 "영업수익"으로 신고하는데,
`dart:Metric` `revenue` 개체의 `skos:altLabel`에 "영업수익"이 이미 들어 있어(facts.py
`_ONTOLOGY`의 표기변형 목록) 표기가 달라도 같은 `normalizesTo` 경로로 resolve된다.

## CQ10 — 기업간 비교 (서술)
**"2024년 말 연결 자산총계가 더 큰 곳은 대우건설과 한화솔루션 중 어디인가?"** (gold qid
`SEM-NUM-15` 실문항) — CQ2와 같은 패턴(scale 보정 포함)으로 두 회사만 필터링하면 된다.
한화솔루션은 표본 5개사에 없어 이 문서에서는 서술로만 남긴다 — 표본을 넓히면 그대로
동작한다.
