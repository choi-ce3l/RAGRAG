"""온톨로지 knowledge graph — 01 온톨로지 매핑이 무엇을 아는지, 그리고 질문이
그래프의 어느 경로를 타고 수치에 도달했는지를 그린다.

노드/엣지는 factstore의 실제 데이터에서 나온다. 꾸며낸 도식이 아니다.

  (Company)  ─FILED→      (Document)  ─CONTAINS→  (Statement)
                                                      │ HAS_ITEM
                                                      ▼
  (Metric) ←─NORMALIZES─  (Concept) ←─ABOUT─  (Fact)  ─SCOPE→ (Scope)
                                                      └─YEAR→  (Year)
"""

import collections

import numqa

from . import derived, sectors

NODE_TYPES = ["Sector", "Company", "Document", "Statement", "Concept",
              "Derived", "Metric", "Scope", "Year", "Fact"]

STATEMENT_KO = {
    "balance_sheet": "재무상태표", "income_statement": "손익계산서",
    "cashflow": "현금흐름표", "ratio": "재무비율",
}


def stats(labels):
    """실데이터 기준 노드/엣지 규모."""
    f = labels.facts
    idx = sectors.load()
    return {
        "Sector": len(idx["sector"]),
        "Derived": len(derived.RULES),
        "Company": len({x["corp_code"] for x in f}),
        "Document": len({x["rcept_no"] for x in f}),
        "Statement": len({x["statement"] for x in f if x["statement"]}),
        "Concept": len(labels.vocab),
        "Metric": len(numqa.METRIC_KO),
        "Scope": len({x["scope"] for x in f if x["scope"]}),
        "Year": len({x["base_year"] for x in f if x["base_year"]}),
        "Fact": len(f),
    }


def concept_stats(labels, top=12):
    """가장 많이 등장하는 개념 노드."""
    return collections.Counter(labels.vocab_count).most_common(top)


def statement_concepts(labels, top=6):
    """재무제표별 대표 개념 — HAS_ITEM 엣지의 실제 분포."""
    by = collections.defaultdict(collections.Counter)
    for x in labels.facts:
        lab = x.get("label_norm") or x.get("label_raw")
        if x.get("statement") and lab:
            by[x["statement"]][lab] += 1
    return {s: c.most_common(top) for s, c in by.items()}


def _mm(s):
    """mermaid 라벨에서 문제되는 문자를 정리한다."""
    return str(s).replace('"', "'").replace("|", "/").replace("\n", " ")


def schema_mermaid(labels=None):
    """온톨로지 스키마 그래프. labels를 주면 노드에 실제 규모를 붙인다."""
    n = stats(labels) if labels else {}

    def lbl(t, ko):
        return f"{t}<br/>{ko}" + (f"<br/><small>{n[t]:,}</small>" if t in n else "")

    return "\n".join([
        "graph LR",
        f'  G["{lbl("Sector", "업종")}"]',
        f'  C["{lbl("Company", "기업")}"]',
        f'  D["{lbl("Document", "공시문서")}"]',
        f'  S["{lbl("Statement", "재무제표")}"]',
        f'  K["{lbl("Concept", "개념·계정")}"]',
        f'  M["{lbl("Metric", "정규지표")}"]',
        f'  V["{lbl("Derived", "파생개념")}"]',
        f'  F["{lbl("Fact", "수치")}"]',
        f'  P["{lbl("Scope", "기준")}"]',
        f'  Y["{lbl("Year", "회계연도")}"]',
        "  G -->|HAS_MEMBER| C",
        "  C -->|FILED| D",
        "  D -->|CONTAINS| S",
        "  S -->|HAS_ITEM| K",
        "  K -->|NORMALIZES_TO| M",
        "  V -->|DERIVES_FROM| K",
        "  F -->|ABOUT| K",
        "  F -->|OF| C",
        "  F -->|CITED_IN| D",
        "  F -->|SCOPE| P",
        "  F -->|YEAR| Y",
    ])


def question_mermaid(result):
    """질문 → 파이프라인이 실제로 밟은 그래프 경로.

    01이 어떤 노드로 매핑했고 02가 어떤 fact를 짚었는지를 그대로 그린다.
    """
    p = result.parsed or {}
    lines = ["graph LR", f'  Q["질문<br/>{_mm(result.question[:40])}"]']
    edges = []

    if p.get("sector"):
        lines.append(f'  G["Sector<br/>{_mm(p["sector"])}"]')
        edges.append("  Q -->|업종 인식| G")
    if p.get("corps"):
        shown = ", ".join(p["corps"][:3]) + ("…" if len(p["corps"]) > 3 else "")
        lines.append(f'  C["Company<br/>{_mm(shown)}"]')
        edges.append("  G -->|HAS_MEMBER| C" if p.get("sector") else "  Q -->|기업 인식| C")
    if p.get("year"):
        lines.append(f'  Y["Year<br/>{p["year"]}"]')
        edges.append("  Q -->|연도 인식| Y")
    if p.get("scope"):
        lines.append(f'  P["Scope<br/>{"연결" if p["scope"] == "consolidated" else "별도"}"]')
        edges.append("  Q -->|기준 인식| P")

    concept = p.get("concept")
    if concept:
        src = "METRIC_KO" if p.get("metric") else "label 색인"
        lines.append(f'  K["Concept<br/>{_mm(concept)}<br/><small>{src}</small>"]')
        edges.append("  Q -->|지표 인식| K")
        if p.get("metric"):
            lines.append(f'  M["Metric<br/>{_mm(p["metric"])}"]')
            edges.append("  K -->|NORMALIZES_TO| M")

    for i, ev in enumerate(result.evidence[:3], 1):
        lines.append(f'  F{i}["Fact<br/>{_mm(ev["path"])}<br/><small>{_mm(ev["ref_id"])}</small>"]')
        lines.append(f'  D{i}["Document<br/>{_mm(ev["report_nm"])}<br/><small>rcept {ev["rcept_no"]}</small>"]')
        edges.append(f"  F{i} -->|CITED_IN| D{i}")
        if concept:
            edges.append(f"  K -->|조회| F{i}")
        if p.get("corps"):
            edges.append(f"  F{i} -->|OF| C")

    if p.get("unsupported"):
        lines.append(f'  X["미지원<br/>{_mm(p["unsupported"])}"]')
        edges.append("  Q -.->|중단| X")

    lines.append(f'  R["상태 {result.state}"]')
    edges.append(f"  {'X' if p.get('unsupported') else ('F1' if result.evidence else 'Q')} --> R")
    return "\n".join(lines + edges)


def derived_mermaid(store=None, labels=None):
    """파생 개념 그래프 — 어떤 개념을 어떤 개념들로 만드는가, 그리고 검증 상태."""
    val = derived.validate(store, labels) if (store and labels) else {}
    lines, edges, seen = ["graph LR"], [], set()
    for i, (name, (op, operands, unit, mult, _)) in enumerate(derived.RULES.items(), 1):
        v = val.get(name, {})
        tag = (f"검증 {v['재현율']}%" if v.get("검증") == "됨" else "미검증")
        lines.append(f'  D{i}["{_mm(name)}<br/><small>{tag}</small>"]')
        for oper in operands:
            nid = f"K{abs(hash(oper)) % 10000}"
            if nid not in seen:
                seen.add(nid)
                lines.append(f'  {nid}["{_mm(oper)}"]')
            edges.append(f"  D{i} -->|DERIVES_FROM| {nid}")
    return "\n".join(lines + edges)


def instance_mermaid(labels, corp_name, year, scope="consolidated", top=6):
    """한 기업·연도의 실제 인스턴스 서브그래프 — 어떤 개념들이 달려 있는지."""
    facts = [f for f in labels.facts
             if f.get("corp_name") == corp_name and f.get("base_year") == year
             and f.get("scope") == scope]
    if not facts:
        return f"graph LR\n  E[\"{_mm(corp_name)} {year} 데이터 없음\"]"
    lines = ["graph LR", f'  C["Company<br/>{_mm(corp_name)}"]']
    edges = []
    by_stmt = collections.defaultdict(list)
    for f in facts:
        by_stmt[f["statement"]].append(f)
    for si, (stmt, fs) in enumerate(sorted(by_stmt.items()), 1):
        lines.append(f'  S{si}["Statement<br/>{STATEMENT_KO.get(stmt, stmt)}"]')
        edges.append(f"  C -->|FILED/CONTAINS| S{si}")
        seen = []
        for f in fs:
            lab = f.get("label_norm") or f.get("label_raw")
            if lab in seen:
                continue
            seen.append(lab)
            if len(seen) > top:
                break
            nid = f"K{si}_{len(seen)}"
            lines.append(f'  {nid}["{_mm(lab)}<br/><small>{_mm(f["value_raw"])} {_mm(f["unit_kr"])}</small>"]')
            edges.append(f"  S{si} -->|HAS_ITEM| {nid}")
    return "\n".join(lines + edges)
