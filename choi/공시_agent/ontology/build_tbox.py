#!/usr/bin/env python3
"""TBox 빌더 — 클래스/속성/domain·range·rdfs:comment를 정적으로 1회 구성한다.

코퍼스 데이터는 전혀 건드리지 않는다 (그건 build_abox_sample.py의 일). 여기서 만드는
클래스/속성 목록은 choi/공시_agent/qa/{concepts,derived,fields,sectors,ontology,kg}.py와
choi/code_chunkingandparsing/src/facts.py의 구조를 그대로 형식화한 것 — 근거는 각 항목의
rdfs:comment에 원본 파일:필드 형태로 인용해 둔다.

CLI: python build_tbox.py  ->  tbox.ttl
"""
import os

from rdflib import RDF, RDFS, XSD, BNode, Graph, Literal
from rdflib.namespace import OWL, SKOS

from namespaces import DART, DARTDATA, bind_all

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_HERE, "tbox.ttl")

# ---------------------------------------------------------------------------
# 클래스 정의: (이름, 부모(None이면 owl:Thing 직속), 한글 라벨, 출처 주석)
# ---------------------------------------------------------------------------
CLASSES = [
    ("FinancialConcept", None, "재무 개념",
     "추상 최상위 — '수치/텍스트 질문의 대상이 될 수 있는 것'. 하위 4개 클래스로 세분."),
    ("AccountItem", "FinancialConcept", "계정과목(정규형)",
     "choi/공시_agent/qa/concepts.py ConceptIndex — canonical(label_norm), surfaces, corps, synonym_group"),
    ("Metric", "FinancialConcept", "정규 재무지표",
     "choi/code_chunkingandparsing/src/facts.py _ONTOLOGY(10) + ragrag/pipeline/facts.py _ONTOLOGY_EXT(21) = 31개 metric_key"),
    ("DerivedConcept", "FinancialConcept", "파생 개념(비율)",
     "choi/공시_agent/qa/derived.py RULES — 9개 파생비율(부채비율 등)"),
    ("StructuredField", "FinancialConcept", "비-XBRL 구조화 필드",
     "choi/공시_agent/qa/fields.py + data/field_ontology.json — 387개 (major/exchange/holding 문서군)"),

    ("Company", None, "기업",
     "choi/code_chunkingandparsing/src/facts.py Fact.corp_code/corp_name"),

    ("Sector", None, "업종/산업",
     "choi/공시_agent/qa/sectors.py sector(20종)/industry(8종) 인덱스"),
    ("SectorClass", "Sector", "업종(sector, 20종)",
     "sectors.py load()['sector'] — 반도체·전자부품, 자동차·모빌리티 등"),
    ("IndustryClass", "Sector", "산업(industry, 8종)",
     "sectors.py load()['industry'] — IT, 금융, 소재 등 GICS류 대분류"),

    ("Document", None, "공시문서",
     "choi/code_chunkingandparsing/src/facts.py Fact.rcept_no/doc_id, fields.py doctypes"),
    ("PeriodicReport", "Document", "정기공시(사업보고서 등)",
     "facts.py entry['doc_group'] == 'periodic'"),
    ("MajorMattersReport", "Document", "주요사항보고서",
     "fields.py / field_ontology.json doc_group == 'major'"),
    ("ExchangeDisclosure", "Document", "거래소공시(단일판매·공급계약 등)",
     "fields.py / field_ontology.json doc_group == 'exchange'"),
    ("HoldingReport", "Document", "지분공시(대량보유상황보고서 등)",
     "fields.py / field_ontology.json doc_group == 'holding' "
     "(factx.jsonl 실측: holding 1,550,014건 — 3개 문서군 중 최다)"),

    ("Statement", None, "재무제표",
     "choi/code_chunkingandparsing/src/facts.py Fact.statement 필드 + derived.py 'ratio' 의사-제표"),
    ("BalanceSheet", "Statement", "재무상태표", "facts.py _STATEMENT['BS']"),
    ("IncomeStatement", "Statement", "손익계산서", "facts.py _STATEMENT['IS']"),
    ("CashFlowStatement", "Statement", "현금흐름표", "facts.py _STATEMENT['CF']"),
    ("EquityStatement", "Statement", "자본변동표",
     "facts.py _STATEMENT['EF'] — kg.py STATEMENT_KO에는 누락돼 있던 값을 여기서 보완"),
    ("RatioStatement", "Statement", "재무비율(공시 명시값)",
     "derived.py validate()가 참조하는 statement=='ratio' 레코드 — 코퍼스에 명시된 비율값"),

    ("Fact", None, "사실(관측값)",
     "choi/code_chunkingandparsing/src/facts.py Fact 레코드 / structstore 구조화 fact 레코드"),
    ("XBRLFact", "Fact", "XBRL 재무제표 사실",
     "facts.py extract_facts() 반환 레코드 — aclass_xbrl_code/fiscal_term/row_index/col_index/sign 보유"),
    ("StructuredFact", "Fact", "비-XBRL 구조화 사실",
     "factx.jsonl 레코드 — field_key/doc_group/kind 보유"),

    ("Scope", None, "연결/별도 기준",
     "facts.py Fact.scope ('consolidated'/'separate') — owl:oneOf로 정확히 두 개체만 허용"),
    ("FiscalYear", None, "회계연도",
     "facts.py Fact.base_year"),
    ("Period", None, "회계기간",
     "facts.py Fact.period_start/period_end + qa/period.py 파싱 결과(기수 포함)"),

    ("DerivationRule", None, "파생 공식(n항관계 reification)",
     "derived.py RULES 튜플(연산자·피연산자2개·단위·배수·검증대상metric_key)을 "
     "W3C n-ary relation 패턴으로 reify — OWL 객체속성 하나로는 표현 불가능한 n항관계이므로"),

    ("Event", "Document", "이벤트 공시",
     "qa/events_vocab.py event_types() — report_nm이 156종 이벤트유형 어휘에 매칭되는 "
     "문서(정기공시 제외). qa/eventspan.py가 이 문서의 접수일자를 시간축 기준점 삼아 "
     "전후 Fact를 대조한다."),
]

# ---------------------------------------------------------------------------
# 객체 속성: (이름, domain, range, 역속성 이름 또는 None, 주석)
# ---------------------------------------------------------------------------
OBJECT_PROPERTIES = [
    ("hasMember", "Sector", "Company", "memberOf",
     "sectors.py HAS_MEMBER 엣지 (kg.py schema_mermaid)"),
    ("filed", "Company", "Document", None,
     "facts.py corp_code + rcept_no 조인 (kg.py FILED 엣지)"),
    ("contains", "Document", "Statement", None,
     "facts.py TABLE-GROUP → statement per 문서 (kg.py CONTAINS 엣지)"),
    ("hasItem", "Statement", "AccountItem", None,
     "concepts.py ConceptIndex.statements Counter (kg.py HAS_ITEM 엣지)"),
    ("normalizesTo", "AccountItem", "Metric", None,
     "concepts.py ConceptIndex.metric_of (kg.py NORMALIZES_TO 엣지)"),
    ("about", "Fact", "FinancialConcept", None,
     "facts.py metric_key/label_norm, factx field_key (kg.py ABOUT 엣지)"),
    ("of", "Fact", "Company", None,
     "facts.py corp_code (kg.py OF 엣지)"),
    ("citedIn", "Fact", "Document", None,
     "facts.py rcept_no/doc_id (kg.py CITED_IN 엣지)"),
    ("hasScope", "Fact", "Scope", None,
     "facts.py scope (kg.py SCOPE 엣지)"),
    ("hasYear", "Fact", "FiscalYear", None,
     "facts.py base_year (kg.py YEAR 엣지)"),
    ("hasPeriod", "Fact", "Period", None,
     "facts.py period_start/period_end/fiscal_term"),
    ("hasRule", "DerivedConcept", "DerivationRule", None,
     "derived.py RULES — DerivedConcept 1개당 DerivationRule 1개(현재 스키마 기준 functional)"),
    ("hasOperand1", "DerivationRule", "AccountItem", None,
     "derived.py RULES[name][1][0] (예: 부채비율의 '부채총계')"),
    ("hasOperand2", "DerivationRule", "AccountItem", None,
     "derived.py RULES[name][1][1] (예: 부채비율의 '자본총계')"),
    ("validatesAgainstMetric", "DerivationRule", "Metric", None,
     "derived.py RULES[name][4] — non-null인 규칙에만 부여(부채비율→debt_ratio, 유동비율→current_ratio)"),
    ("anchorsPeriod", "Event", "Period", None,
     "qa/eventspan.py — 이벤트 접수일자(filings.py meta[rn]['rcept_dt'])를 전/후 비교의 "
     "시간 기준점으로 삼는다"),
    ("supersedes", "Document", "Document", "supersededBy",
     "qa/filings.py chain()/diff() — 정정본이 이전 문서를 대체한다. Fact.isSuperseded는 "
     "이 관계의 결과로 개별 수치에 찍히는 플래그다"),
]

# ---------------------------------------------------------------------------
# 데이터 속성: (이름, [domain들], xsd 타입, 주석)
# ---------------------------------------------------------------------------
DATA_PROPERTIES = [
    ("factId", ["Fact"], XSD.string, "facts.py fact_id"),
    ("valueRaw", ["Fact"], XSD.string, "facts.py value_raw (원문 표기)"),
    ("valueDecimal", ["Fact"], XSD.decimal, "facts.py value_decimal (Decimal 보존값)"),
    ("sign", ["Fact"], XSD.integer, "facts.py sign (+1/-1)"),
    ("unit", ["Fact"], XSD.string, "facts.py unit ('KRW')"),
    ("unitKr", ["Fact"], XSD.string, "facts.py unit_kr ('백만원' 등)"),
    ("scale", ["Fact"], XSD.integer, "facts.py scale (단위 배수)"),
    ("isSuperseded", ["Fact"], XSD.boolean, "facts.py is_superseded"),
    ("parserConfidence", ["Fact"], XSD.float, "facts.py parser_confidence"),
    ("rowIndex", ["XBRLFact"], XSD.integer, "facts.py row_index"),
    ("colIndex", ["XBRLFact"], XSD.integer, "facts.py col_index"),
    ("aclassXbrlCode", ["XBRLFact"], XSD.string, "facts.py aclass_xbrl_code"),
    ("fieldKey", ["StructuredFact", "StructuredField"], XSD.string, "factx.jsonl field_key"),
    ("docGroup", ["StructuredFact", "StructuredField", "Document"], XSD.string,
     "factx.jsonl / field_ontology.json doc_group ('major'/'exchange'/'holding'/'periodic')"),
    ("corpCode", ["Company"], XSD.string, "facts.py corp_code"),
    ("corpName", ["Company"], XSD.string, "facts.py corp_name (rdfs:label과 별개로 원값 보존)"),
    ("rceptNo", ["Document"], XSD.string, "facts.py rcept_no"),
    ("docId", ["Document"], XSD.string, "facts.py doc_id"),
    ("reportNm", ["Document"], XSD.string, "manifest report_nm"),
    ("docSubtype", ["Document"], XSD.string, "manifest doc_subtype"),
    ("canonicalLabel", ["AccountItem"], XSD.string, "concepts.py canonical()"),
    ("coverageCount", ["AccountItem"], XSD.integer, "concepts.py ConceptIndex.corps[c] 크기(기업 수)"),
    ("metricKey", ["Metric"], XSD.string, "facts.py _ONTOLOGY/_ONTOLOGY_EXT 키"),
    ("kind", ["StructuredField"], XSD.string, "field_ontology.json kind ('numeric'/'text')"),
    ("shape", ["StructuredField"], XSD.string, "field_ontology.json shape ('ratio'/'count'/null)"),
    ("corpCoverage", ["StructuredField"], XSD.integer, "field_ontology.json corps"),
    ("occurrenceCount", ["StructuredField"], XSD.integer, "field_ontology.json n"),
    ("sectorName", ["Sector"], XSD.string, "sectors.py sector/industry 키"),
    ("memberCount", ["Sector"], XSD.integer, "sectors.py members() 전체 크기(샘플로 축소되기 전 원본 규모)"),
    ("yearValue", ["FiscalYear"], XSD.gYear, "facts.py base_year"),
    ("periodStart", ["Period"], XSD.date, "facts.py period_start"),
    ("periodEnd", ["Period"], XSD.date, "facts.py period_end"),
    ("fiscalTerm", ["Period"], XSD.integer, "facts.py fiscal_term (제N기)"),
    ("operator", ["DerivationRule"], XSD.string, "derived.py RULES[name][0] ('÷')"),
    ("resultUnit", ["DerivationRule"], XSD.string, "derived.py RULES[name][2] ('%'/'회')"),
    ("multiplier", ["DerivationRule"], XSD.decimal, "derived.py RULES[name][3]"),
    ("reproductionRate", ["DerivationRule"], XSD.decimal,
     "derived_validation.json 캐시값의 '재현율' — 재계산하지 않고 그대로 옮김"),
    ("matchCount", ["DerivationRule"], XSD.integer, "derived_validation.json '일치'"),
    ("mismatchCount", ["DerivationRule"], XSD.integer, "derived_validation.json '불일치'"),
    ("missingOperandCount", ["DerivationRule"], XSD.integer, "derived_validation.json '피연산자없음'"),
    ("validationStatus", ["DerivationRule"], XSD.string,
     "derived_validation.json '검증' 필드를 영문 enum(validated/failed/not_applicable)으로 정규화"
     " — 한글 원값(됨/실패/불가)은 rdfs:comment로 보존"),
    ("eventTypeLabel", ["Event"], XSD.string,
     "qa/events_vocab.py event_types() 156종 통제 어휘 중 하나(정규화된 report_nm)"),
]


def build():
    g = Graph()
    bind_all(g)

    for name, parent, ko, comment in CLASSES:
        cls = DART[name]
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.label, Literal(ko, lang="ko")))
        g.add((cls, RDFS.comment, Literal(comment, lang="ko")))
        if parent:
            g.add((cls, RDFS.subClassOf, DART[parent]))

    # Scope는 열린 클래스가 아니라 정확히 두 개체만 허용 — owl:oneOf.
    consolidated, separate = DARTDATA.Consolidated, DARTDATA.Separate
    for ind, ko in ((consolidated, "연결"), (separate, "별도")):
        g.add((ind, RDF.type, DART.Scope))
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, SKOS.prefLabel, Literal(ko, lang="ko")))
    one_of_list_head = BNode()
    g.add((DART.Scope, OWL.oneOf, one_of_list_head))
    from rdflib.collection import Collection
    Collection(g, one_of_list_head, [consolidated, separate])

    for name, domain, rng, inverse, comment in OBJECT_PROPERTIES:
        prop = DART[name]
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.domain, DART[domain]))
        g.add((prop, RDFS.range, DART[rng]))
        g.add((prop, RDFS.comment, Literal(comment, lang="ko")))
        if inverse:
            inv = DART[inverse]
            g.add((inv, RDF.type, OWL.ObjectProperty))
            g.add((inv, OWL.inverseOf, prop))
            g.add((inv, RDFS.domain, DART[rng]))
            g.add((inv, RDFS.range, DART[domain]))

    for name, domains, xsd_type, comment in DATA_PROPERTIES:
        prop = DART[name]
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        if len(domains) == 1:
            g.add((prop, RDFS.domain, DART[domains[0]]))
        else:
            # 서로 겹치지 않는 클래스 여럿에 쓰이는 속성(예: docGroup은 Document/
            # StructuredFact/StructuredField 전부에 쓰임)은 rdfs:domain을 여러 번
            # 찍으면 안 된다 — RDFS 의미론상 "각 도메인 클래스의 교집합"으로 해석돼
            # 하나의 개체가 세 클래스 전부에 속한다고 잘못 추론된다. owl:unionOf로
            # "이 중 하나"를 명시한다.
            union_node = BNode()
            g.add((prop, RDFS.domain, union_node))
            g.add((union_node, RDF.type, OWL.Class))
            list_head = BNode()
            g.add((union_node, OWL.unionOf, list_head))
            from rdflib.collection import Collection as _Collection
            _Collection(g, list_head, [DART[d] for d in domains])
        g.add((prop, RDFS.range, xsd_type))
        g.add((prop, RDFS.comment, Literal(comment, lang="ko")))

    return g


if __name__ == "__main__":
    graph = build()
    graph.serialize(destination=OUT_PATH, format="turtle")
    print(f"TBox: {len(graph)} triples -> {OUT_PATH}")
    reloaded = Graph().parse(OUT_PATH, format="turtle")
    assert len(reloaded) == len(graph), "재파싱 트리플 수 불일치"
    print("파싱 검증 통과")
