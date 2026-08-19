"""grade_all.py — 병합본 numqa 전 골드셋 회귀 채점 (API 미사용, 순수 로컬).

choi `rag._store_answer()`의 store-우선 라우터를 그대로 재현한다(LLM narrative 폴백은 제외 —
폴백으로 샌 문항은 'narrative'로 카운트해서 라우팅 구멍이 숫자로 드러나게 한다).

슬라이스:
  - layerB 694 : numqa.grade() (fact_numeric / dual / compute)
  - layerC 259 : ratio 48 / major 135 / exchange 76 — store 라우터 채점
  - regression : SHLEE 05_TESTER gold_numeric 중 단일 intent로 기계채점 가능한 항목

출력: report/grade_all.json + report/GRADE_ALL.md
"""
import os
import sys
import json
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))

from ragrag.pipeline import numqa

REPORT = os.path.join(_HERE, "..", "report")
GOLD_C = os.path.join(_HERE, "..", "goldsets", "layerC", "goldC.jsonl")

# gold_numeric(23건) 중 단일 intent로 기계채점 가능한 항목만 명시적으로 고른다.
# 나머지(multi-step 서술형·합성 edge)는 자동채점 대상이 아님을 리포트에 명시.
REGRESSION = [
    ("대우_Q2_부채비율", "대우건설의 2025년 연결 기준 부채비율은 얼마인가?", ["284.5"]),
    ("삼성_Q2_부채비율", "삼성전자의 2025년 연결 기준 부채비율은 얼마인가?", ["29.9"]),
    ("삼성_Q2_자기자본비율", "삼성전자의 2025년 연결 기준 자기자본비율은 얼마인가?", ["77.0"]),
    ("대우_Q5_최대주주", "대우건설의 최대주주는 누구이고 지분율은 몇 %인가?", ["중흥토건", "40.60"]),
    ("삼성_Q4_지분율", "삼성전자의 최대주주 단독 지분율과 특수관계인 합산 지분율은 각각 몇 %인가?",
     ["삼성생명보험", "8.51", "19.84"]),
    ("EDGE-10_scope_별도", "대우건설의 2025년 별도 부채총계는 전년 대비 몇% 증가했는가?", ["별도"]),
    ("EDGE-05_sign_flip", "대우건설의 2025년 연결 영업이익은 전년 대비 몇% 증가했는가?", ["전환"]),
    ("EDGE-09_zero_denom_nci", "HD현대중공업의 2023년 연결 비지배지분은 전년 대비 몇% 증가했는가?",
     []),   # 크래시만 안 나면 통과(가드 확인) — 값 검증 아님
]


def _store_answer(q, store, sstore):
    """choi rag._store_answer 재현: ① XBRL 수치 → ② 비-XBRL 구조화. 둘 다 실패면 None(narrative)."""
    r = numqa.answer(q, store)
    if r["status"] == "ok" and r["text"]:
        return r, "xbrl"
    r2 = numqa.answer_struct(q, sstore)
    if r2["status"] in ("ok", "ambiguous") and r2["text"]:
        return r2, "struct"
    return None, "narrative"


def grade_layer_c(store, sstore):
    items = [json.loads(l) for l in open(GOLD_C, encoding="utf-8")]
    n, p = Counter(), Counter()
    via = Counter()
    fails = []
    for it in items:
        res, route = _store_answer(it["question"], store, sstore)
        text = res["text"] if res else ""
        ok = any(t in text for t in it["must_contain"])
        n[it["slice"]] += 1
        p[it["slice"]] += ok
        via[route] += 1
        if not ok:
            fails.append((it["qid"], it["slice"], it["question"], it["gold"], route, text[:70]))
    return {"by_slice": {s: f"{p[s]}/{n[s]}" for s in n},
            "total": f"{sum(p.values())}/{sum(n.values())}",
            "route": dict(via), "fails": fails}


def grade_layer_a(store, sstore):
    """층 A 재작성 손검증 103건 — 병합 전에는 comparison intent가 없어 채점 자체가 불가했던 슬라이스.

    엄격 채점: gold_values(필링별 값) **전부**가 답변 텍스트에 있어야 통과.
    (must_contain any-match이 아니라 전부 요구 — 비교 질문은 한쪽만 맞으면 답이 아니다)
    """
    path = os.path.join(_HERE, "..", "goldsets", "layerA", "goldA_restatement.jsonl")
    items = [json.loads(l) for l in open(path, encoding="utf-8")]
    n = p = strict = 0
    via = Counter()
    fails = []
    for it in items:
        res, route = _store_answer(it["question"], store, sstore)
        text = res["text"] if res else ""
        gv = list(it.get("gold_values", {}).values())
        all_hit = bool(gv) and all(v in text for v in gv)
        any_hit = any(t in text for t in it.get("must_contain", []))
        # 재작성 여부 판정도 맞아야 함(gold의 restated 플래그와 대조)
        verdict_ok = (res or {}).get("restated") == it.get("restated") if res and "restated" in res else False
        n += 1
        p += any_hit
        strict += all_hit and verdict_ok
        via[route] += 1
        if not (all_hit and verdict_ok):
            fails.append((it["qid"], it["corp_name"], it["question"][:50], gv, route,
                          f"all_hit={all_hit} verdict_ok={verdict_ok}", text[:90]))
    return {"n": n, "pass": p, "strict": strict, "route": dict(via), "fails": fails}


def grade_regression(store, sstore):
    rows = []
    for qid, q, must in REGRESSION:
        try:
            res, route = _store_answer(q, store, sstore)
            text = res["text"] if res else ""
            status = res["status"] if res else "narrative"
            ok = all(t in text for t in must)
        except Exception as e:  # noqa: BLE001  — 크래시 자체가 실패
            rows.append((qid, False, "CRASH", f"{type(e).__name__}: {e}"))
            continue
        rows.append((qid, ok, status, text[:120]))
    return rows


def main():
    os.makedirs(REPORT, exist_ok=True)
    store = numqa.FactStore.load()
    sstore = numqa.StructStore.load()
    print(f"[load] xbrl fact {len(store.facts):,} · shareholders {len(store.shareholders):,}")

    b = numqa.grade(store=store)
    c = grade_layer_c(store, sstore)
    a = grade_layer_a(store, sstore)
    r = grade_regression(store, sstore)

    out = {"layerB": b["total"], "layerB_slices": b["slices"], "layerC": c,
           "layerA": a, "regression": r}
    with open(os.path.join(REPORT, "grade_all.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    L = ["# 병합본 numqa — 전 골드셋 회귀 채점\n",
         "> choi 라이브 + SHLEE Cycle 3~5 병합본. API 미사용(순수 로컬).\n",
         f"- fact {len(store.facts):,} · 지분율 record {len(store.shareholders):,}\n",
         "## 층 B (XBRL 자동생성 694)"]
    for k, v in b["slices"].items():
        L.append(f"- {k}: {v['pass']}/{v['n']} (엄격 {v['strict_pass']}/{v['n']})")
    L.append(f"- **합계 {b['total']['pass']}/{b['total']['n']}**\n")
    L.append("## 층 C (비-XBRL 자동생성 259)")
    for s, v in c["by_slice"].items():
        L.append(f"- {s}: {v}")
    L.append(f"- **합계 {c['total']}** · 라우팅 {c['route']}\n")
    if c["fails"]:
        L.append("### 실패")
        for qid, s, q, g, route, t in c["fails"][:20]:
            L.append(f"- [{qid}/{s}] gold={g} route={route}\n    q={q}\n    → {t}")
        L.append("")
    L.append("## 층 A (재작성 손검증 103) — 병합 전에는 채점 불가였던 슬라이스")
    L.append(f"- 느슨(must_contain any): {a['pass']}/{a['n']}")
    L.append(f"- **엄격(필링별 값 전부 + 재작성 판정 일치): {a['strict']}/{a['n']}** · 라우팅 {a['route']}\n")
    if a["fails"]:
        L.append("### 실패")
        for qid, corp, q, gv, route, why, t in a["fails"][:20]:
            L.append(f"- [{qid}] {corp} route={route} {why}\n    gold={gv}\n    → {t}")
        L.append("")
    L.append("## 손검증 회귀 (SHLEE gold_numeric 중 단일 intent 항목)")
    for qid, ok, st, t in r:
        L.append(f"- {'PASS' if ok else 'FAIL'} [{qid}] ({st}) {t}")
    L.append("\n> gold_numeric 23건 중 나머지는 multi-step 서술형·합성 edge라 자동채점 대상이 아님(수동 확인 항목).")
    with open(os.path.join(REPORT, "GRADE_ALL.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(json.dumps({"layerB": b["total"], "layerC": {k: v for k, v in c.items() if k != "fails"},
                      "regression_pass": sum(1 for _, ok, _, _ in r if ok)},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
