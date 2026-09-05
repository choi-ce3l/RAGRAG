#!/usr/bin/env python3
"""검증 스크립트 — §8 (a) 구문 유효성 + (b) 원본 파이프라인 대비 정합성.

절대 하지 않는 것: 런타임 QA 파이프라인을 SPARQL로 대체하는 것. 여기서는 반대 방향 —
ontology.py/derived.py가 이미 내는 답을 정답지로 두고, RDF 그래프가 같은 답을 내는지만 본다.

CLI: python validate_graph.py  ->  validation_report.md
"""
import json
import sys
from decimal import Decimal
from pathlib import Path

from rdflib import Graph

from namespaces import DART, DARTDATA

_HERE = Path(__file__).resolve().parent
_AGENT = _HERE.parent
TBOX = _HERE / "tbox.ttl"
ABOX = _HERE / "abox_sample.ttl"
REPORT = _HERE / "validation_report.md"
DERIVED_VALIDATION_JSON = _AGENT / "data" / "derived_validation.json"

sys.path.insert(0, str(_AGENT))
from qa import derived as derived_mod        # noqa: E402


def load_graph():
    g = Graph()
    g.parse(str(TBOX), format="turtle")       # TBox 단독 파싱 — 먼저 성공해야 ABox를 얹는다
    n_tbox = len(g)
    g.parse(str(ABOX), format="turtle")
    return g, n_tbox, len(g) - n_tbox


def check_ask(g, name, query):
    ok = bool(g.query(query).askAnswer)
    return name, ok


def check_exact_cardinality(g, cls, prop):
    """cls의 모든 개체가 prop을 정확히 1개 갖는지 — GROUP BY/HAVING만 쓰는 표준 SPARQL로 확인.

    (스칼라 서브쿼리를 FILTER 안에 넣는 방식은 SPARQL 1.1 문법이 아니라 rdflib가 파싱하지
    못한다 — 그래서 GROUP BY 결과를 파이썬에서 집계하는 방식으로 우회한다.)
    """
    q = f"""
        PREFIX dart: <{DART}>
        SELECT ?f (COUNT(?x) AS ?c) WHERE {{
          ?f a dart:{cls} .
          OPTIONAL {{ ?f dart:{prop} ?x }}
        }} GROUP BY ?f
    """
    bad = [(str(r.f), int(r.c)) for r in g.query(q) if int(r.c) != 1]
    return not bad, bad


CARDINALITY_CHECKS = [
    ("XBRLFact", "of"), ("XBRLFact", "about"), ("XBRLFact", "hasYear"),
    ("XBRLFact", "hasScope"), ("XBRLFact", "citedIn"),
    ("DerivationRule", "hasOperand1"), ("DerivationRule", "hasOperand2"),
]

DANGLING_REF_CHECK = ("dart:about가 가리키는 대상은 전부 그 자리에서 rdf:type을 갖는다(매달린 참조 없음)", f"""
    PREFIX dart: <{DART}>
    ASK {{
      FILTER NOT EXISTS {{ ?f dart:about ?x . FILTER NOT EXISTS {{ ?x a ?t }} }}
    }}
""")


def fidelity_point_lookup(g):
    """(b-i) 삼성전자 2024 연결 매출액 — facts.jsonl 원본과 SPARQL 결과가 정확히 일치해야 함."""
    q = """
        PREFIX dart: <http://dart-ontology.example.org/schema#>
        PREFIX dart-data: <http://dart-ontology.example.org/data#>
        PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT ?value WHERE {
          ?f a dart:XBRLFact ; dart:of dart-data:company_삼성전자 ;
             dart:about dart-data:metric_revenue ;
             dart:hasScope dart-data:Consolidated ;
             dart:hasYear dart-data:year_2024 ;
             dart:valueDecimal ?value .
        }
    """
    rows = [Decimal(str(r.value)) for r in g.query(q)]
    ref = None
    with open(_AGENT.parent / "code_chunkingandparsing" / "out" / "facts.jsonl", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if (d.get("corp_name") == "삼성전자" and d.get("metric_key") == "revenue"
                    and d.get("scope") == "consolidated" and d.get("base_year") == 2024):
                ref = Decimal(d["value_decimal"])
                break
    ok = ref is not None and ref in rows
    return ("CQ1 point-lookup: SPARQL 결과에 원본 facts.jsonl 값이 포함됨", ok,
            f"원본={ref}, SPARQL 결과={rows}")


def fidelity_derivation_rule(g):
    """(b-iii) DerivationRule.reproductionRate가 derived_validation.json 캐시와 정확히 일치."""
    cache = json.loads(DERIVED_VALIDATION_JSON.read_text(encoding="utf-8"))
    all_ok = True
    detail = []
    for name, v in cache.items():
        if v.get("재현율") is None:
            continue
        q = f"""
            PREFIX dart: <http://dart-ontology.example.org/schema#>
            PREFIX dart-data: <http://dart-ontology.example.org/data#>
            SELECT ?rate WHERE {{
              dart-data:rule_{name} dart:reproductionRate ?rate .
            }}
        """
        rows = [float(r.rate) for r in g.query(q)]
        ok = rows and abs(rows[0] - v["재현율"]) < 1e-9
        all_ok = all_ok and bool(ok)
        detail.append(f"{name}: 캐시={v['재현율']}, RDF={rows}")
    return ("파생비율 reproductionRate가 derived_validation.json 캐시와 일치", all_ok,
            "; ".join(detail))


def fidelity_mapping_layer(g):
    """(b-ii) ontology.py.parse()가 잡는 concept과 SPARQL이 about으로 건 concept이 같은 축인지.

    비교 대상: '삼성전자의 2024년 연결 매출액은 얼마인가?' -> ontology.py가 metric='revenue'로
    잡는지, RDF 쪽 CQ1이 실제로 dart-data:metric/revenue를 조회하는지(위 SPARQL 쿼리 자체가
    이미 이를 하드코딩하고 있으므로, 여기서는 ontology.py가 같은 metric_key를 내는지만 재확인).
    """
    sys.path.insert(0, str(_AGENT / "qa"))
    from qa import ontology as ontology_mod, labelstore, pipeline   # noqa: E402
    ls = labelstore.get()
    store = pipeline.get_store()
    p = ontology_mod.parse("삼성전자의 2024년 연결 매출액은 얼마인가?", store, ls)
    ok = p.get("metric") == "revenue"
    return ("ontology.py.parse()가 CQ1과 같은 metric_key(revenue)를 resolve함", ok,
            f"ontology.py 결과: metric={p.get('metric')}, corp={p.get('corp')}, year={p.get('year')}")


def main():
    g, n_tbox, n_abox = load_graph()
    lines = ["# 검증 결과", "", f"- TBox: {n_tbox} triples (단독 파싱 성공)",
             f"- ABox: {n_abox} triples (TBox 위에 병합 파싱 성공)",
             f"- 합계: {len(g)} triples", "", "## (a) 구문 유효성 — SPARQL", ""]
    syntax_all_ok = True
    for cls, prop in CARDINALITY_CHECKS:
        ok, bad = check_exact_cardinality(g, cls, prop)
        syntax_all_ok = syntax_all_ok and ok
        note = "" if ok else f" — 위반 {len(bad)}건, 예: {bad[:3]}"
        lines.append(f"- [{'x' if ok else ' '}] 모든 {cls}는 {prop}을 정확히 1개 가진다{note}")
    dr_name, dr_query = DANGLING_REF_CHECK
    _, dr_ok = check_ask(g, dr_name, dr_query)
    syntax_all_ok = syntax_all_ok and dr_ok
    lines.append(f"- [{'x' if dr_ok else ' '}] {dr_name}")

    lines += ["", "## (b) 원본 파이프라인 대비 정합성", ""]
    fidelity_all_ok = True
    for fn in (fidelity_point_lookup, fidelity_derivation_rule, fidelity_mapping_layer):
        try:
            name, ok, detail = fn(g)
        except Exception as e:  # noqa: BLE001
            name, ok, detail = (fn.__name__, False, f"예외: {e}")
        fidelity_all_ok = fidelity_all_ok and ok
        lines.append(f"- [{'x' if ok else ' '}] {name}\n  - {detail}")

    lines += ["", "## 종합", "",
              f"- 구문 유효성: {'PASS' if syntax_all_ok else 'FAIL'}",
              f"- 정합성: {'PASS' if fidelity_all_ok else 'FAIL'}"]

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n-> {REPORT}")
    return 0 if (syntax_all_ok and fidelity_all_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
