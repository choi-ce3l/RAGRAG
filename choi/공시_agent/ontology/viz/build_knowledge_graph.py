#!/usr/bin/env python3
"""전체 코퍼스 규모의 인스턴스 knowledge graph 뷰어를 만든다.

`schema_graph.html`(TBox 27개 클래스만)과는 목적이 다르다 — 이건 "온톨로지 구조가 실제
데이터에서 어떻게 채워지는가"를 보여주는 인스턴스 레벨 그래프다. 데이터는 기존
`choi/공시_agent/scripts/export_graph.py`(읽기 전용 — 손대지 않음)의 `build()`를 그대로
불러 쓴다. 그 스크립트가 만드는 `data/graph.json`은 기본 min_coverage=20(261노드)인데,
여기서는 좀 더 풍성하게 보이도록 min_coverage=10(389노드)을 기본값으로 쓴다.

CLI: python viz/build_knowledge_graph.py [--min-coverage 10]
  -> viz/knowledge_graph.json, viz/knowledge_graph.html
"""
import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_AGENT = _HERE.parent.parent                      # choi/공시_agent
sys.path.insert(0, str(_AGENT))
sys.path.insert(0, str(_AGENT / "scripts"))
import export_graph                                # noqa: E402  (읽기 전용 — choi 원본, 수정 안 함)

JSON_OUT = _HERE / "knowledge_graph.json"
TEMPLATE = _HERE / "knowledge_graph_template.html"
HTML_OUT = _HERE / "knowledge_graph.html"

TYPE_KO = {"sector": "업종", "company": "기업", "statement": "재무제표",
           "concept": "개념", "metric": "정규지표", "derived": "파생개념"}
TYPE_ORDER = ["sector", "company", "statement", "concept", "metric", "derived"]


def build(min_coverage=3):
    g = export_graph.build(min_coverage)
    for n in g["nodes"]:
        n["typeKo"] = TYPE_KO.get(n["type"], n["type"])
    g["typeOrder"] = TYPE_ORDER
    g["typeKo"] = TYPE_KO
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-coverage", type=int, default=3)
    a = ap.parse_args()

    data = build(a.min_coverage)
    JSON_OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"knowledge_graph.json: {len(data['nodes'])} nodes, {len(data['edges'])} edges -> {JSON_OUT}")

    template = TEMPLATE.read_text(encoding="utf-8")
    html = template.replace("__GRAPH_DATA_JSON__", json.dumps(data, ensure_ascii=False))
    HTML_OUT.write_text(html, encoding="utf-8")
    print(f"knowledge_graph.html -> {HTML_OUT}")


if __name__ == "__main__":
    main()
