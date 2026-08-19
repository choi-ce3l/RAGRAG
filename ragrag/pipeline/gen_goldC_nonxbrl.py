"""gen_goldC_nonxbrl.py — 비-XBRL 골드 슬라이스(layer C) 자동생성 + agent 채점.

factstore의 비-XBRL fact(비율·doc-type)를 QA로 만들고, rag.answer(store-우선 라우터)로 채점한다.
정답은 fact value_raw(원문 그대로)에서 나오므로 이 채점은 "라우팅·추출 정합성 + 커버리지" 측정이다
(독립 정확도는 jin/대우 손검증 문항이 담당).

슬라이스:
- ratio: {corp} {year} 부채비율/유동비율   (factx_periodic)
- major: {corp} {날짜} 자기주식 취득 주식수/금액/목적   (factx, 자기주식취득결정 필링, 날짜 단서로 특정)
- exchange: {corp} {year} 공급계약 계약금액/계약상대   (factx)

출력: goldset_layerC/goldC.jsonl · factpath_eval_C.md
실행: python gen_goldC_nonxbrl.py       (store 로드로 다소 느림, API 미사용)
"""
import os
import re
import json

from . import load

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(_HERE, "..", "goldsets", "layerC")
STORE = os.path.join(_HERE, "..", "out", "factstore.jsonl")
FACTX = os.path.join(_HERE, "..", "out", "factx.jsonl")


def _digit(v):
    return v.strip().strip("()").lstrip("-").strip()


def generate():
    man = {e["rcept_no"]: e for e in load.load_manifest()}
    qs = []

    # --- ratio: factstore의 debt_ratio/current_ratio, 기업별 최신연도 ---
    latest = {}
    for line in open(STORE, encoding="utf-8"):
        if '"debt_ratio"' not in line and '"current_ratio"' not in line:
            continue
        d = json.loads(line)
        if d.get("metric_key") not in ("debt_ratio", "current_ratio") or d.get("is_superseded"):
            continue
        key = (d["corp_name"], d["metric_key"])
        if key not in latest or (d["base_year"] or 0) > (latest[key]["base_year"] or 0):
            latest[key] = d
    for (corp, mk), d in latest.items():
        ko = "부채비율" if mk == "debt_ratio" else "유동비율"
        qs.append({"qid": f"R{len(qs)+1}", "slice": "ratio",
                   "question": f"{corp}의 {d['base_year']}년 {ko}은 얼마인가?",
                   "must_contain": [d["value_raw"], _digit(d["value_raw"])], "gold": d["value_raw"]})

    # --- doc-type: factx에서 자기주식취득/공급계약 필드, 날짜 단서 포함 ---
    WANT = {("major", "ACQ_OSTK"): ("자기주식 취득 예정 보통주식은 몇 주인가", "주"),
            ("major", "ACQ_OSTK_PRC"): ("자기주식 취득 예정 보통주식 금액은", "원"),
            ("major", "ACQ_PPS"): ("자기주식 취득 목적은", "text"),
            ("exchange", "계약금액(원)"): ("공급계약의 계약금액은", "원"),
            ("exchange", "계약상대"): ("공급계약의 계약상대는 누구인가", "text")}
    seen = set()
    picked = {}   # (corp,g,fk) -> {rcept: fact}  (last per rcept)
    for line in open(FACTX, encoding="utf-8"):
        d = json.loads(line)
        k = (d.get("doc_group"), d.get("field_key"))
        if k not in WANT or d.get("is_superseded"):
            continue
        picked.setdefault((d["corp_name"],) + k, {})[d["rcept_no"]] = d
    for (corp, g, fk), by_r in picked.items():
        phrase, kind = WANT[(g, fk)]
        for rc, d in list(by_r.items())[:3]:      # 기업·필드당 최대 3개 필링
            e = man.get(rc, {})
            dt = e.get("rcept_dt", "")
            if g == "major" and dt and len(dt) == 8:
                when = f"{dt[:4]}년 {int(dt[4:6])}월 {int(dt[6:8])}일 "
            else:
                when = f"{(e.get('base_year') or dt[:4])}년 " if (e.get("base_year") or dt) else ""
            q = f"{when}{corp} {phrase}?"
            if q in seen:
                continue
            seen.add(q)
            mc = [d["value_raw"]] if kind == "text" else [d["value_raw"], _digit(d["value_raw"])]
            qs.append({"qid": f"D{len([x for x in qs if x['qid'].startswith('D')])+1}",
                       "slice": g, "question": q, "must_contain": mc, "gold": d["value_raw"],
                       "rcept": rc})
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "goldC.jsonl"), "w", encoding="utf-8") as f:
        for q in qs:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    return qs


def grade(qs):
    from . import rag
    from collections import Counter
    by_slice = Counter()
    by_slice_pass = Counter()
    fails = []
    for q in qs:
        ans, blocks = rag.answer(q["question"], verbose=False)
        via = blocks and blocks[0].get("source") == "factstore"
        ok = any(t in ans for t in q["must_contain"])
        by_slice[q["slice"]] += 1
        by_slice_pass[q["slice"]] += ok
        if not ok:
            fails.append((q["qid"], q["slice"], q["question"], q["gold"], ans[:70], via))
    lines = ["# 층C 비-XBRL 팩트경로 채점 (agent rag.answer, API 미사용)\n"]
    tot = sum(by_slice.values()); tp = sum(by_slice_pass.values())
    lines.append(f"- 전체 {tp}/{tot} pass\n")
    for s in by_slice:
        lines.append(f"## {s}: {by_slice_pass[s]}/{by_slice[s]}")
    lines.append("\n## 실패 샘플")
    for qid, s, q, g, a, via in fails[:20]:
        lines.append(f"- [{qid}/{s}] gold={g}  q={q}\n    → ({'store' if via else 'narr'}) {a}")
    with open(os.path.join(OUT, "factpath_eval_C.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return {"total": tot, "pass": tp,
            "by_slice": {s: f"{by_slice_pass[s]}/{by_slice[s]}" for s in by_slice}}


if __name__ == "__main__":
    qs = generate()
    print(f"생성 {len(qs)}문항")
    print(json.dumps(grade(qs), ensure_ascii=False, indent=2))
