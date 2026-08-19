"""crossval_ratio.py — 부채비율 이중경로 교차검증 (병합으로 처음 가능해진 측정).

두 팀이 같은 지표를 서로 독립된 경로로 구현해놨다:
  A) choi factx Phase 1b : 사업보고서 MD&A 표에 회사가 **보고한** 부채비율을 그대로 추출 (debt_ratio fact)
  B) SHLEE Cycle 3       : XBRL 재무제표의 부채총계 ÷ 자본총계 × 100 으로 **재계산**

지금까지 두 경로는 각각 1~2건(대우 Q2, 삼성 Q2)만 맞춰봤다. 여기서 전수 대조한다.
층B/층C 골드셋은 둘 다 "같은 fact에서 생성된 QA를 그 fact로 채점"하는 정합성 측정이라
독립 정확도를 못 잰다(choi 노트 22·29가 명시한 한계). 이 대조는 그 한계를 우회한다 —
두 경로가 공유하는 것은 원본 공시 문서뿐이고, 추출 코드도 계산 근거도 완전히 다르기 때문이다.

방법(중요): **같은 문서(doc_id) 안에서만** 비교한다.
  MD&A 비율과 재무제표는 같은 사업보고서에 실려 있으므로, 재작성/정정으로 인한 필링 간 값 차이를
  섞지 않으려면 doc 단위로 짝지어야 한다(choi 노트 22의 lookup_in_doc 교훈과 같은 이유).

출력: report/crossval_ratio.json + report/CROSSVAL_RATIO.md
"""
import os
import sys
import json
from decimal import Decimal

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))

from ragrag.pipeline import numqa

REPORT = os.path.join(_HERE, "..", "report")
# 경로·압축 해석은 numqa가 이미 하므로 재사용한다(배포 패키지는 .gz만 담김).
STORE_PATH = numqa.STORE_PATH

TOL_PP = Decimal("0.1")     # 반올림 표기(소수 1자리) 차이를 흡수하는 허용오차, %p


def load_reported():
    """factstore에서 원문 추출 비율 fact(debt_ratio/current_ratio)만."""
    out = []
    for line in numqa._open(STORE_PATH):
        if '"debt_ratio"' not in line and '"current_ratio"' not in line:
            continue
        d = json.loads(line)
        if d.get("metric_key") in ("debt_ratio", "current_ratio"):
            out.append(d)
    return out


def main():
    os.makedirs(REPORT, exist_ok=True)
    store = numqa.FactStore.load()
    reported = load_reported()
    rep_debt = [d for d in reported if d["metric_key"] == "debt_ratio"]
    rep_curr = [d for d in reported if d["metric_key"] == "current_ratio"]

    rows = []
    for d in rep_debt:
        doc_id, scope, year = d["doc_id"], d["scope"], d["base_year"]
        # 같은 문서 안의 XBRL 부채총계·자본총계
        tl = store.lookup_in_doc(doc_id, "total_liabilities", scope, year)
        te = store.lookup_in_doc(doc_id, "total_equity", scope, year)
        rec = {"corp_name": d["corp_name"], "corp_code": d["corp_code"], "doc_id": doc_id,
               "rcept_no": d["rcept_no"], "scope": scope, "year": year,
               "reported": d["value_raw"], "fact_id": d["fact_id"]}
        if not (tl and te):
            rec["verdict"] = "no_xbrl"
            rec["missing"] = ("total_liabilities" if not tl else "") + ("," if not tl and not te else "") + \
                             ("total_equity" if not te else "")
            rows.append(rec)
            continue
        den = Decimal(te["value_decimal"])
        if den == 0:
            rec["verdict"] = "zero_denominator"
            rows.append(rec)
            continue
        computed = Decimal(tl["value_decimal"]) / den * 100
        rep_val = Decimal(d["value_decimal"])
        diff = computed - rep_val
        rec.update(computed=str(round(computed, 2)), reported_val=str(rep_val),
                   diff_pp=str(round(diff, 2)),
                   num_fact=tl["fact_id"], den_fact=te["fact_id"],
                   num_raw=tl["value_raw"], den_raw=te["value_raw"])
        rec["verdict"] = "match" if abs(diff) <= TOL_PP else "mismatch"
        rows.append(rec)

    from collections import Counter
    verdicts = Counter(r["verdict"] for r in rows)
    matched = [r for r in rows if r["verdict"] in ("match", "mismatch")]
    mism = sorted([r for r in rows if r["verdict"] == "mismatch"],
                  key=lambda r: abs(Decimal(r["diff_pp"])), reverse=True)

    summary = {"n_reported_debt_ratio": len(rep_debt), "n_reported_current_ratio": len(rep_curr),
               "verdicts": dict(verdicts), "tolerance_pp": str(TOL_PP),
               "n_compared": len(matched),
               "match_rate": f"{verdicts['match']}/{len(matched)}" if matched else "0/0"}
    with open(os.path.join(REPORT, "crossval_ratio.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=2)

    L = ["# 부채비율 이중경로 교차검증 — 원문 보고값 vs XBRL 재계산\n",
         "> A) choi factx: 사업보고서 MD&A에 회사가 보고한 부채비율 (추출)",
         "> B) SHLEE Cycle 3: 같은 문서의 XBRL 부채총계 ÷ 자본총계 × 100 (재계산)",
         "> 두 경로가 공유하는 것은 원본 공시뿐 — 추출 코드·계산 근거가 완전히 다르다.",
         f"> 같은 doc_id 안에서만 대조. 허용오차 ±{TOL_PP}%p (소수 1자리 표기 반올림 흡수).\n",
         "## 요약",
         f"- 원문 보고 부채비율 fact: **{len(rep_debt)}건** (유동비율 {len(rep_curr)}건은 XBRL 재계산 경로 없음 — 유동자산/유동부채 미온톨로지)",
         f"- 실제 대조 가능: **{len(matched)}건**",
         f"- **일치 {verdicts['match']} / 불일치 {verdicts['mismatch']}**"
         + (f" (일치율 {verdicts['match']}/{len(matched)} = {verdicts['match']*100//len(matched)}%)" if matched else ""),
         f"- XBRL 구성요소 없음: {verdicts['no_xbrl']}건 · 분모 0: {verdicts['zero_denominator']}건\n"]

    if mism:
        L.append(f"## 불일치 {len(mism)}건 (차이 큰 순)\n")
        L.append("| 기업 | 연도 | scope | 보고값 | 재계산 | 차이(%p) | rcept |")
        L.append("|---|---|---|---|---|---|---|")
        for r in mism[:60]:
            L.append(f"| {r['corp_name']} | {r['year']} | {r['scope']} | {r['reported']} | "
                     f"{r['computed']} | {r['diff_pp']} | {r['rcept_no']} |")
        L.append("")
        L.append("### 근거 fact (상위 10건)")
        for r in mism[:10]:
            L.append(f"- **{r['corp_name']} {r['year']} {r['scope']}**: 보고 {r['reported']} vs 재계산 {r['computed']}%")
            L.append(f"  - 부채총계 {r['num_raw']} (`{r['num_fact']}`)")
            L.append(f"  - 자본총계 {r['den_raw']} (`{r['den_fact']}`)")
        L.append("")

    no_x = [r for r in rows if r["verdict"] == "no_xbrl"]
    if no_x:
        L.append(f"## XBRL 구성요소 결측 {len(no_x)}건 (재계산 불가 — 커버리지 갭)\n")
        for r in no_x[:30]:
            L.append(f"- {r['corp_name']} {r['year']} {r['scope']} (보고 {r['reported']}, 결측: {r.get('missing')})")
        L.append("")

    with open(os.path.join(REPORT, "CROSSVAL_RATIO.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for r in mism[:15]:
        print(f"  MISMATCH {r['corp_name']} {r['year']} {r['scope']}: "
              f"보고 {r['reported']} vs 재계산 {r['computed']}% (차 {r['diff_pp']}%p)")


if __name__ == "__main__":
    main()
