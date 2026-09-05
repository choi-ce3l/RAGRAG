#!/usr/bin/env python3
"""schema_graph.json -> schema_graph.svg / schema_graph.png — 슬라이드 삽입용 정적 이미지.

networkx의 범용 레이아웃(kamada_kawai 등)은 노드 27개+한글 라벨 조합에서 라벨끼리 겹쳐
읽을 수 없는 그림이 나왔다(1차 시도 확인됨). 그래서 범용 물리 레이아웃 대신, 온톨로지가
실제로 말하는 흐름 — "기업이 문서를 낸다 → 문서는 재무제표를 담는다 → 재무제표는 사실을
갖는다 → 사실은 개념을 가리킨다 → 일부 개념은 파생 규칙으로 계산된다" — 을 그대로 6개 행에
배치하는 수작업 레이아웃을 쓴다. 발표에서 "체계적으로 설계됐다"는 이야기를 그림 자체가
하게 만드는 것이 목적이라, 억지로 자동 레이아웃을 고집할 이유가 없다.

CLI: python viz/render_static.py  ->  viz/schema_graph.svg, viz/schema_graph.png
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch

# 기본 폰트(DejaVu Sans)엔 한글 글리프가 없어 라벨이 네모(tofu)로 깨진다 — 시스템에 설치된
# 나눔고딕으로 강제 지정한다. 다른 환경에서 돌릴 땐 fc-list :lang=ko로 대체 폰트를 확인할 것.
matplotlib.rcParams["font.family"] = "NanumGothic"
matplotlib.rcParams["axes.unicode_minus"] = False

_HERE = Path(__file__).resolve().parent
DATA_JSON = _HERE / "schema_graph.json"
ABOX_JSON = _HERE / "knowledge_graph.json"          # 실사례 데이터 — 스키마 노드 아래 실제 예시를 붙이는 데만 쓴다
SVG_OUT = _HERE / "schema_graph.svg"
PNG_OUT = _HERE / "schema_graph.png"

# TBox 클래스 이름 → ABox statement id. 이 클래스들 아래에 실제 커버리지 상위
# 개념 3개를 작은 주석으로 붙인다 — "재무제표"가 스키마 도형으로만 존재하는 게
# 아니라 실제 어떤 개념들을 담고 있는지 한눈에 보이게 하기 위함(사용자 요청).
ABOX_STATEMENT_ID = {
    "BalanceSheet": "T:balance_sheet",
    "IncomeStatement": "T:income_statement",
    "CashFlowStatement": "T:cashflow",
}


def top_concepts(abox, statement_id, top=3):
    """statement_id에 HAS_ITEM으로 연결된 개념 중 커버리지(기업 수) 상위 top개 라벨."""
    node_by_id = {n["id"]: n for n in abox["nodes"]}
    neigh = []
    for e in abox["edges"]:
        if e["rel"] != "HAS_ITEM":
            continue
        if e["s"] == statement_id or e["t"] == statement_id:
            other = e["t"] if e["s"] == statement_id else e["s"]
            n = node_by_id.get(other)
            if n:
                neigh.append(n)
    neigh.sort(key=lambda n: -n.get("weight", 0))
    return [n["label"] for n in neigh[:top]]

CATEGORY_COLOR = {
    "concept": "#f2b93d", "entity": "#52d17c", "document": "#6aa8ff",
    "fact": "#ef7f6b", "rule": "#c98cf0", "event": "#ff6b6b",
}
CATEGORY_KO = {"entity": "기업/업종", "document": "공시문서", "document2": "재무제표",
               "fact": "사실/차원", "concept": "재무 개념", "rule": "파생 규칙",
               "event": "이벤트·정정 (오늘 추가)"}
BG, TEXT, EDGE, EDGE_HIER = "#0b1220", "#e7ecf5", "#5b6f96", "#2c3b58"

# 행 배치 — 온톨로지의 서사 순서. (row, x위치는 행 안에서 등간격 자동 배분)
ROWS = [
    ["Sector", "SectorClass", "IndustryClass", "Company"],
    ["Document", "PeriodicReport", "MajorMattersReport", "ExchangeDisclosure", "HoldingReport", "Event"],
    ["Statement", "BalanceSheet", "IncomeStatement", "CashFlowStatement", "EquityStatement", "RatioStatement"],
    ["Fact", "XBRLFact", "StructuredFact", "Scope", "FiscalYear", "Period"],
    ["FinancialConcept", "AccountItem", "Metric", "DerivedConcept", "StructuredField"],
    ["DerivationRule"],
]
ROW_LABEL = ["누가", "어떤 문서", "어느 표", "무슨 사실", "무슨 개념", "어떻게 계산되는가"]

ROW_Y = [5, 4, 3, 2, 1, 0]
ROW_H = 1.0


def layout_positions():
    pos = {}
    for row_idx, names in enumerate(ROWS):
        y = ROW_Y[row_idx] * 2.4
        n = len(names)
        width = max(n - 1, 1) * 3.6
        for i, name in enumerate(names):
            x = -width / 2 + i * 3.6 if n > 1 else 0.0
            pos[name] = (x, y)
    return pos


def draw_edge(ax, pos, src, dst, label, hierarchy, color, rad):
    x1, y1 = pos[src]
    x2, y2 = pos[dst]
    style = "-" if not hierarchy else (0, (2, 2))
    if src == dst:
        # 자기참조 관계(예: Document supersedes Document, 정정본이 이전 문서를 대체) —
        # 시작점=끝점이면 arc3가 길이 0인 선을 그려 안 보인다. 노드 위에 작은 loop를 낸다.
        lx1, ly1 = x1 - 0.16, y1 + 0.42
        lx2, ly2 = x1 + 0.16, y1 + 0.42
        arrow = FancyArrowPatch((lx1, ly1), (lx2, ly2), connectionstyle="arc3,rad=2.2",
                                 arrowstyle="-|>" if not hierarchy else "-", mutation_scale=9,
                                 linewidth=1.1 if not hierarchy else 0.8,
                                 linestyle=style, color=color, alpha=0.85 if not hierarchy else 0.55,
                                 shrinkA=3, shrinkB=3, zorder=1)
        ax.add_patch(arrow)
        if label and not hierarchy:
            ax.text(x1, y1 + 1.05, label, fontsize=6.3, color="#c7cfe0", ha="center", va="center",
                    zorder=3, bbox=dict(boxstyle="round,pad=0.08", fc=BG, ec="none", alpha=0.75))
        return
    arrow = FancyArrowPatch((x1, y1), (x2, y2), connectionstyle=f"arc3,rad={rad}",
                             arrowstyle="-|>" if not hierarchy else "-", mutation_scale=10,
                             linewidth=1.1 if not hierarchy else 0.8,
                             linestyle=style, color=color, alpha=0.85 if not hierarchy else 0.55,
                             shrinkA=14, shrinkB=14, zorder=1)
    ax.add_patch(arrow)
    if label and not hierarchy:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        # rad 방향으로 살짝 밀어서 선 위에 라벨이 겹치지 않게 한다
        dx, dy = x2 - x1, y2 - y1
        nx_, ny_ = -dy, dx
        norm = (nx_ ** 2 + ny_ ** 2) ** 0.5 or 1
        off = rad * 1.6
        lx, ly = mx + nx_ / norm * off, my + ny_ / norm * off
        ax.text(lx, ly, label, fontsize=6.3, color="#c7cfe0", ha="center", va="center",
                zorder=3, bbox=dict(boxstyle="round,pad=0.08", fc=BG, ec="none", alpha=0.75))


def render():
    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    node_by_id = {n["id"]: n for n in data["nodes"]}
    pos = layout_positions()

    concept_examples = {}
    if ABOX_JSON.exists():
        abox = json.loads(ABOX_JSON.read_text(encoding="utf-8"))
        for cls_name, stmt_id in ABOX_STATEMENT_ID.items():
            top3 = top_concepts(abox, stmt_id)
            if top3:
                concept_examples[cls_name] = top3

    fig, ax = plt.subplots(figsize=(20, 14), dpi=200)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    # 행 구분 안내선 + 행 이름표
    xmin = min(x for x, y in pos.values()) - 3.4
    xmax = max(x for x, y in pos.values()) + 2.2
    for row_idx, y in enumerate(ROW_Y):
        yy = y * 2.4
        ax.text(xmin, yy, ROW_LABEL[row_idx], fontsize=11, color="#8b96b3", ha="left", va="center",
                style="italic", fontweight="bold")

    # 엣지: 계층(subClassOf) 먼저, 그 다음 객체 속성
    for e in data["edges"]:
        if e.get("hierarchy"):
            draw_edge(ax, pos, e["source"], e["target"], None, True, EDGE_HIER, rad=0.15)
    for e in data["edges"]:
        if not e.get("hierarchy"):
            src_row = next(i for i, r in enumerate(ROWS) if e["source"] in r)
            dst_row = next(i for i, r in enumerate(ROWS) if e["target"] in r)
            rad = 0.12 if src_row == dst_row else 0.22
            draw_edge(ax, pos, e["source"], e["target"], e["label"], False, EDGE, rad=rad)

    # 노드
    for name, (x, y) in pos.items():
        n = node_by_id[name]
        color = CATEGORY_COLOR[n["category"]]
        ax.add_patch(Circle((x, y), 0.42, facecolor=color, edgecolor=color, alpha=0.92, zorder=4))
        ax.text(x, y - 0.72, n["label"], fontsize=8.6, color=TEXT, ha="center", va="top", zorder=5,
                fontweight="medium")
        examples = concept_examples.get(name)
        if examples:
            ax.text(x, y - 0.98, "예: " + "·".join(examples), fontsize=6.4, color="#8b96b3",
                    ha="center", va="top", zorder=5, style="italic")

    ax.set_xlim(xmin - 0.5, xmax + 0.5)
    ax.set_ylim(-1.6, max(ROW_Y) * 2.4 + 1.6)
    ax.set_title("DART 공시 온톨로지 — TBox 스키마 (choi/공시_agent/ontology/tbox.ttl)",
                 color=TEXT, fontsize=16, pad=18)
    ax.axis("off")

    handles = [plt.Line2D([0], [0], marker='o', color='none', markerfacecolor=c,
                           markersize=11, label=CATEGORY_KO.get(cat, cat))
               for cat, c in CATEGORY_COLOR.items()]
    ax.legend(handles=handles, loc="upper right", bbox_to_anchor=(1.0, 1.02), frameon=True,
              fontsize=9.5, facecolor="#121a2b", edgecolor="#24314d", labelcolor=TEXT)

    fig.tight_layout()
    fig.savefig(SVG_OUT, facecolor=BG)
    fig.savefig(PNG_OUT, facecolor=BG)
    print(f"-> {SVG_OUT}\n-> {PNG_OUT}")


if __name__ == "__main__":
    render()
