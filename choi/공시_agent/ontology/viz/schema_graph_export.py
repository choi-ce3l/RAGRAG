#!/usr/bin/env python3
"""tbox.ttl -> schema_graph.json — 시각화 노드/엣지 데이터를 실제 TBox에서 유도한다.

손으로 스키마를 다시 베끼지 않는 이유: 시각화가 tbox.ttl과 어긋나면(클래스 추가/삭제 시
그림만 안 고쳐지는 등) 발표 자료가 거짓말을 하게 된다. 항상 이 스크립트로 재생성한다.

CLI: python viz/schema_graph_export.py  ->  viz/schema_graph.json
"""
import json
import sys
from pathlib import Path

from rdflib import RDF, RDFS, Graph
from rdflib.namespace import OWL

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from namespaces import DART  # noqa: E402

_HERE = Path(__file__).resolve().parent
TBOX = _HERE.parent / "tbox.ttl"
OUT = _HERE / "schema_graph.json"
HTML_TEMPLATE = _HERE / "schema_graph_template.html"
HTML_OUT = _HERE / "schema_graph.html"

# 카테고리(색상 구분용) — 클래스 이름이 어느 계열인지로 분류한다.
CATEGORY = {
    "FinancialConcept": "concept", "AccountItem": "concept", "Metric": "concept",
    "DerivedConcept": "concept", "StructuredField": "concept",
    "Company": "entity",
    "Sector": "entity", "SectorClass": "entity", "IndustryClass": "entity",
    "Document": "document", "PeriodicReport": "document", "MajorMattersReport": "document",
    "ExchangeDisclosure": "document", "HoldingReport": "document",
    "Statement": "document", "BalanceSheet": "document", "IncomeStatement": "document",
    "CashFlowStatement": "document", "EquityStatement": "document", "RatioStatement": "document",
    "Fact": "fact", "XBRLFact": "fact", "StructuredFact": "fact",
    "Scope": "fact", "FiscalYear": "fact", "Period": "fact",
    "DerivationRule": "rule",
    "Event": "event",
}
CATEGORY_KO = {"concept": "재무 개념", "entity": "기업/업종", "document": "문서/재무제표",
               "fact": "사실/차원", "rule": "파생 규칙", "event": "이벤트·정정"}


def local(uri):
    return str(uri).rsplit("#", 1)[-1]


def export():
    g = Graph()
    g.parse(str(TBOX), format="turtle")

    nodes = {}
    for cls in g.subjects(RDF.type, OWL.Class):
        if not str(cls).startswith(str(DART)):
            continue
        name = local(cls)
        label = g.value(cls, RDFS.label)
        comment = g.value(cls, RDFS.comment)
        parent = g.value(cls, RDFS.subClassOf)
        nodes[name] = {
            "id": name,
            "label": str(label) if label else name,
            "comment": str(comment) if comment else "",
            "category": CATEGORY.get(name, "concept"),
            "parent": local(parent) if parent and str(parent).startswith(str(DART)) else None,
        }

    edges = []
    seen = set()
    for prop in g.subjects(RDF.type, OWL.ObjectProperty):
        name = local(prop)
        if g.value(prop, OWL.inverseOf):
            continue  # 역속성은 별도 엣지로 그리지 않는다 (원 속성과 시각적으로 중복)
        domain = g.value(prop, RDFS.domain)
        rng = g.value(prop, RDFS.range)
        if domain is None or rng is None:
            continue
        d, r = local(domain), local(rng)
        if d not in nodes or r not in nodes:
            continue
        key = (d, r, name)
        if key in seen:
            continue
        seen.add(key)
        edges.append({"source": d, "target": r, "label": name})

    # subClassOf 계층도 옅은 엣지로 함께 그린다 (분류를 시각적으로 보여주기 위함)
    for name, n in nodes.items():
        if n["parent"]:
            edges.append({"source": name, "target": n["parent"], "label": "subClassOf",
                          "hierarchy": True})

    payload = {
        "nodes": list(nodes.values()),
        "edges": edges,
        "categories": CATEGORY_KO,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"schema_graph.json: {len(nodes)} nodes, {len(edges)} edges -> {OUT}")
    return payload


def export_html(payload):
    """schema_graph.json을 HTML 템플릿에 그대로 박아 단일 오프라인 파일로 만든다.

    file:// 로 열었을 때 fetch()가 CORS로 막히는 브라우저가 있어, 별도 JSON을 AJAX로
    불러오지 않고 생성 시점에 인라인한다 — 그래서 schema_graph.json이 바뀌면 반드시
    이 스크립트를 다시 돌려야 html도 갱신된다(하나의 소스에서 함께 나온다는 원칙 유지).
    """
    template = HTML_TEMPLATE.read_text(encoding="utf-8")
    html = template.replace("__GRAPH_DATA_JSON__", json.dumps(payload, ensure_ascii=False))
    HTML_OUT.write_text(html, encoding="utf-8")
    print(f"schema_graph.html -> {HTML_OUT}")


if __name__ == "__main__":
    data = export()
    export_html(data)
