#!/usr/bin/env python3
"""온톨로지 knowledge graph를 시각화용 JSON으로 내보낸다.

fact 노드는 21만 개라 그대로 그리면 의미 없는 덩어리가 된다. 그래서 fact는 세어서
가중치로만 쓰고, 구조를 만드는 층(업종·기업·재무제표·개념·정규지표·파생)을 노드로 낸다.

    python scripts/export_graph.py [--min-coverage 20] [-o graph.json]
"""

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qa import concepts, derived, kg, labelstore, sectors   # noqa: E402

STATEMENT_KO = kg.STATEMENT_KO


def build(min_coverage=20):
    ls = labelstore.get()
    ci = concepts.get(ls.facts)
    sec = sectors.load()["sector"]

    nodes, edges = [], []

    # ── 업종 ────────────────────────────────────────────────
    corp_sector = {}
    for name, members in sec.items():
        nodes.append({"id": f"S:{name}", "type": "sector", "label": name,
                      "weight": len(members)})
        for m in members:
            corp_sector[m] = name

    # ── 기업 ────────────────────────────────────────────────
    corp_facts = collections.Counter()
    corp_stmt = collections.defaultdict(collections.Counter)
    for f in ls.facts:
        c = f.get("corp_name")
        if c:
            corp_facts[c] += 1
            if f.get("statement"):
                corp_stmt[c][f["statement"]] += 1
    for corp, n in corp_facts.items():
        nodes.append({"id": f"C:{corp}", "type": "company", "label": corp,
                      "weight": n, "sector": corp_sector.get(corp)})
        s = corp_sector.get(corp)
        if s:
            edges.append({"s": f"S:{s}", "t": f"C:{corp}", "rel": "HAS_MEMBER"})

    # ── 재무제표 ────────────────────────────────────────────
    stmt_total = collections.Counter(f["statement"] for f in ls.facts if f.get("statement"))
    for st, n in stmt_total.items():
        nodes.append({"id": f"T:{st}", "type": "statement",
                      "label": STATEMENT_KO.get(st, st), "weight": n})
    for corp, counts in corp_stmt.items():
        for st, n in counts.items():
            edges.append({"s": f"C:{corp}", "t": f"T:{st}", "rel": "REPORTS", "w": n})

    # ── 개념 ────────────────────────────────────────────────
    kept = []
    for c in ci.surfaces:
        cov = ci.coverage(c)
        if cov < min_coverage:
            continue
        st = ci.main_statement(c)
        if not st:
            continue
        kept.append(c)
        nodes.append({"id": f"K:{c}", "type": "concept", "label": c,
                      "weight": cov, "statement": st,
                      "surfaces": len(ci.surfaces[c])})
        edges.append({"s": f"T:{st}", "t": f"K:{c}", "rel": "HAS_ITEM"})

    # ── 정규지표 ────────────────────────────────────────────
    metrics = {ci.metric_of[c] for c in kept if ci.metric_of.get(c)}
    for m in sorted(metrics):
        nodes.append({"id": f"M:{m}", "type": "metric", "label": m, "weight": 1})
    for c in kept:
        m = ci.metric_of.get(c)
        if m:
            edges.append({"s": f"K:{c}", "t": f"M:{m}", "rel": "NORMALIZES_TO"})

    # ── 파생 개념 ───────────────────────────────────────────
    kept_set = set(kept)
    val = {}
    try:
        from qa import pipeline
        val = derived.validate(pipeline.get_store(), ls)
    except Exception:                                    # noqa: BLE001
        pass
    for name, (op, operands, unit, mult, _) in derived.RULES.items():
        v = val.get(name, {})
        nodes.append({"id": f"D:{name}", "type": "derived", "label": name,
                      "weight": 1, "unit": unit,
                      "validated": v.get("검증") == "됨",
                      "recall": v.get("재현율")})
        for oper in operands:
            canon, _, _ = ci.match(oper)
            canon = canon or oper
            tid = f"K:{canon}"
            if canon not in kept_set:                    # 커버리지 밖이면 노드를 살려둔다
                nodes.append({"id": tid, "type": "concept", "label": canon,
                              "weight": ci.coverage(canon),
                              "statement": ci.main_statement(canon), "surfaces": 1})
                kept_set.add(canon)
                st = ci.main_statement(canon)
                if st:
                    edges.append({"s": f"T:{st}", "t": tid, "rel": "HAS_ITEM"})
            edges.append({"s": f"D:{name}", "t": tid, "rel": "DERIVES_FROM"})

    # 중복 노드 정리 (id 기준 첫 것 유지)
    seen, uniq = set(), []
    for n in nodes:
        if n["id"] in seen:
            continue
        seen.add(n["id"])
        uniq.append(n)
    ids = {n["id"] for n in uniq}
    edges = [e for e in edges if e["s"] in ids and e["t"] in ids]

    totals = kg.stats(ls)
    totals["ConceptShown"] = sum(1 for n in uniq if n["type"] == "concept")
    return {"nodes": uniq, "edges": edges, "totals": totals,
            "minCoverage": min_coverage}


def main():
    ap = argparse.ArgumentParser(description="knowledge graph JSON 내보내기")
    ap.add_argument("--min-coverage", type=int, default=20,
                    help="이 기업 수 이상에서 쓰이는 개념만 (기본 20)")
    ap.add_argument("-o", "--out", default="data/graph.json")
    a = ap.parse_args()
    g = build(a.min_coverage)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    kinds = collections.Counter(n["type"] for n in g["nodes"])
    rels = collections.Counter(e["rel"] for e in g["edges"])
    print(f"노드 {len(g['nodes'])} {dict(kinds)}")
    print(f"엣지 {len(g['edges'])} {dict(rels)}")
    print(f"→ {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
