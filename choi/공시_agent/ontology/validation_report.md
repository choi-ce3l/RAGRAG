# 검증 결과

- TBox: 351 triples (단독 파싱 성공)
- ABox: 22420 triples (TBox 위에 병합 파싱 성공)
- 합계: 22771 triples

## (a) 구문 유효성 — SPARQL

- [x] 모든 XBRLFact는 of을 정확히 1개 가진다
- [x] 모든 XBRLFact는 about을 정확히 1개 가진다
- [x] 모든 XBRLFact는 hasYear을 정확히 1개 가진다
- [x] 모든 XBRLFact는 hasScope을 정확히 1개 가진다
- [x] 모든 XBRLFact는 citedIn을 정확히 1개 가진다
- [x] 모든 DerivationRule는 hasOperand1을 정확히 1개 가진다
- [x] 모든 DerivationRule는 hasOperand2을 정확히 1개 가진다
- [x] dart:about가 가리키는 대상은 전부 그 자리에서 rdf:type을 갖는다(매달린 참조 없음)

## (b) 원본 파이프라인 대비 정합성

- [x] CQ1 point-lookup: SPARQL 결과에 원본 facts.jsonl 값이 포함됨
  - 원본=300870903, SPARQL 결과=[Decimal('300870903.0'), Decimal('300870903.0')]
- [x] 파생비율 reproductionRate가 derived_validation.json 캐시와 일치
  - 부채비율: 캐시=98.3, RDF=[98.3]; 유동비율: 캐시=92.7, RDF=[92.7]
- [x] ontology.py.parse()가 CQ1과 같은 metric_key(revenue)를 resolve함
  - ontology.py 결과: metric=revenue, corp=삼성전자, year=2024

## 종합

- 구문 유효성: PASS
- 정합성: PASS
