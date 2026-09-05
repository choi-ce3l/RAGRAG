"""gen_goldB.py — 층 B 골드셋 자동생성 (facts.jsonl 기반).

설계: choi/md/16_검색_프레임워크.md(골드셋 계획) + 19/20(fact). fact를 QA로 변환한다.
정답이 XBRL 셀에 있으므로 수작업 없이 전 기업 fact_numeric 골드셋을 만든다.

intent별 md 슬라이스(하네스는 md 1개=테스트셋 1개):
  goldB_fact_numeric.md : 단일값(연결/별도 명시), tier1
  goldB_dual.md         : scope 미지정 이중값 함정, tier2
  goldB_compute.md      : 전년比 증가율(Decimal 계산), tier3
+ goldB_facts.jsonl     : 각 문항의 원천 fact_id·값(팩트경로/엄격채점용)
+ summary.json

하드네거티브(must_not_contain)는 자매 fact에서 자동: 연결↔별도, 당년↔전년.
must_contain은 OR 의미(eval) — 원시 숫자 + 조/억 환산 병기(단위표기 채점 대응).

실행: python gen_goldB.py     (순수 로컬, API 키 없음)
"""
import os
import json
import unicodedata
from decimal import Decimal

import load
import supersede
import facts as F

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(_HERE, "..", "goldset_layerB")

METRIC_KO = {"revenue": "매출액", "operating_income": "영업이익", "net_income": "당기순이익",
             "total_assets": "자산총계", "total_liabilities": "부채총계", "total_equity": "자본총계"}
METRIC_STMT = {"revenue": "income_statement", "operating_income": "income_statement",
               "net_income": "income_statement", "total_assets": "balance_sheet",
               "total_liabilities": "balance_sheet", "total_equity": "balance_sheet"}
SCOPE_KO = {"consolidated": "연결", "separate": "별도"}
CORE6 = ["revenue", "operating_income", "net_income", "total_assets", "total_liabilities", "total_equity"]


def _digit(value_raw):
    """'(815,431)'->'815,431', '333,605,938'->그대로. 부호/괄호 제거한 자릿수 문자열."""
    return value_raw.strip().strip("()").lstrip("-").strip()


def _won_form(value_decimal, scale):
    """조/억/만 단위 한국어 표기(단위표기 채점 대응). 없으면 None."""
    won = int(Decimal(value_decimal) * scale)
    a = abs(won)
    jo, eok, man = a // 10**12, (a % 10**12) // 10**8, (a % 10**8) // 10**4
    if jo:
        return f"{jo}조 {eok:,}억" if eok else f"{jo}조"
    if eok:
        return f"{eok:,}억"
    if man:
        return f"{man:,}만"
    return None


def _mc(fact):
    """must_contain 토큰: 원시 자릿수 + 조/억 환산(중복 제거)."""
    toks = [_digit(fact["value_raw"])]
    w = _won_form(fact["value_decimal"], fact["scale"])
    if w:
        toks.append(w)
    seen, out = set(), []
    for t in toks:
        if t and t not in seen:
            seen.add(t); out.append(t)
    return out


def _pick(index, metric, scope, year):
    return index.get((metric, scope, METRIC_STMT.get(metric), year))


def _index(fs):
    idx = {}
    for f in fs:
        if f["metric_key"]:
            idx[(f["metric_key"], f["scope"], f["statement"], f["base_year"])] = f
    return idx


def _stmt_ko(code):
    m = {"BS_C": "연결 재무상태표", "BS_S": "별도 재무상태표", "IS_C1": "연결 포괄손익계산서",
         "IS_C2": "연결 손익계산서", "IS_C3": "연결 포괄손익계산서", "IS_S1": "별도 포괄손익계산서",
         "IS_S2": "별도 손익계산서", "IS_S3": "별도 포괄손익계산서", "CF_C": "연결 현금흐름표", "CF_S": "별도 현금흐름표"}
    return m.get(code.replace("{XBRL}", ""), code)


def _q_block(qid, question, must_contain, must_not_contain, rcept, source_ko, notes):
    mc = ", ".join(f"`{t}`" for t in must_contain)
    mnc = ", ".join(f"`{t}`" for t in must_not_contain)
    lines = [f"## {qid}. {question}",
             f"- must_contain: {mc}",
             f"- must_not_contain: {mnc}" if mnc else "- must_not_contain: ",
             f"- **출처**: **{source_ko}**",
             f"접수번호 {rcept}",
             f"- notes: {notes}", ""]
    return "\n".join(lines)


def generate():
    man = load.load_manifest()
    smap = supersede.build_supersede_map(man)
    # 기업별 최신(비-superseded) 사업보고서 XML
    best = {}
    for e in man:
        if e["doc_group"] != "periodic" or "사업보고서" not in (e.get("report_nm") or "") \
           or e.get("file_format") != "xml":
            continue
        if smap.get(f"periodic_{e['rcept_no']}", {}).get("is_superseded"):
            continue
        c = e["corp_code"]
        if c not in best or (e.get("base_year") or 0) > (best[c].get("base_year") or 0):
            best[c] = e

    slices = {"fact_numeric": [], "dual": [], "compute": []}   # 각 원소 = (question, mc, mnc, rcept, src, notes, rich)
    for c, e in sorted(best.items(), key=lambda kv: unicodedata.normalize("NFC", kv[1]["corp_name"])):
        corp = unicodedata.normalize("NFC", e["corp_name"])
        rcept = e["rcept_no"]
        year = e.get("base_year")
        fs = F.extract_facts(e, smap.get(f"periodic_{rcept}"))
        idx = _index(fs)

        # --- tier1 fact_numeric: 연결 6지표 + 별도 2지표 ---
        for scope in ("consolidated", "separate"):
            metrics = CORE6 if scope == "consolidated" else ["revenue", "total_assets"]
            for m in metrics:
                f = _pick(idx, m, scope, year)
                if not f:
                    continue
                sib = _pick(idx, m, "separate" if scope == "consolidated" else "consolidated", year)
                prev = _pick(idx, m, scope, year - 1) if year else None
                mnc = []
                for s in (sib, prev):
                    if s and _digit(s["value_raw"]) != _digit(f["value_raw"]):
                        mnc.append(_digit(s["value_raw"]))
                q = f"{corp}의 {year}년 {SCOPE_KO[scope]} 기준 {METRIC_KO[m]}은 얼마인가?"
                slices["fact_numeric"].append((
                    q, _mc(f), mnc, rcept, _stmt_ko(f["aclass_xbrl_code"]),
                    f"metric={m} scope={scope} unit={f['unit_kr']} fact={f['fact_id']}",
                    {"intent": "fact_numeric", "answer_type": "value", "tier": 1,
                     "corp_name": corp, "corp_code": c, "metric_key": m, "scope": scope,
                     "base_year": year, "unit_kr": f["unit_kr"], "gold_value": f["value_raw"],
                     "source": {"rcept_no": rcept, "fact_id": f["fact_id"],
                                "aclass_xbrl_code": f["aclass_xbrl_code"], "row_label": f["label_raw"]}}))

        # --- tier2 dual: 자산총계 scope 미지정(연결/별도 모두 있어야) ---
        cf = _pick(idx, "total_assets", "consolidated", year)
        sf = _pick(idx, "total_assets", "separate", year)
        if cf and sf:
            q = f"{corp}의 {year}년 자산총계는 얼마인가?"
            mc = _mc(cf) + _mc(sf)
            slices["dual"].append((
                q, mc, [], rcept, "연결/별도 재무상태표",
                f"scope 미지정 함정 — 연결={cf['value_raw']} 별도={sf['value_raw']} 모두 제시 필요(엄격채점은 goldB_facts.jsonl)",
                {"intent": "fact_numeric", "answer_type": "dual", "tier": 2,
                 "corp_name": corp, "corp_code": c, "metric_key": "total_assets", "base_year": year,
                 "unit_kr": cf["unit_kr"],
                 "gold_values": {"consolidated": cf["value_raw"], "separate": sf["value_raw"]},
                 "source": {"rcept_no": rcept,
                            "fact_id": {"consolidated": cf["fact_id"], "separate": sf["fact_id"]}}}))

        # --- tier3 compute: 영업이익·당기순이익 전년比 증가율(양수 전년만) ---
        for m in ("operating_income", "net_income"):
            cur = _pick(idx, m, "consolidated", year)
            prv = _pick(idx, m, "consolidated", year - 1) if year else None
            if not (cur and prv):
                continue
            if Decimal(prv["value_decimal"]) <= 0 or Decimal(cur["value_decimal"]) <= 0:
                continue   # 적자/부호변화는 증가율 무의미 → 별도 intent
            r = F.compute("growth", [cur, prv])
            pct = round(float(r["value"]), 1)
            q = f"{corp}의 {year}년 연결 {METRIC_KO[m]}은 전년({year-1}년) 대비 몇 % 증가했는가?"
            slices["compute"].append((
                q, [f"{pct}%", f"{pct:g}"], [], rcept, f"연결 {'손익계산서'}",
                f"compute=growth {cur['value_raw']}→ 기준 {prv['value_raw']} = {pct}% 원천={r['inputs']}",
                {"intent": "fact_compute", "answer_type": "compute", "tier": 3,
                 "corp_name": corp, "corp_code": c, "metric_key": m, "scope": "consolidated",
                 "base_year": year, "gold_value": f"{pct}%", "formula": "growth",
                 "source": {"rcept_no": rcept, "fact_ids": r["inputs"], "expr": r["expr"]}}))

    # --- 파일 출력 ---
    os.makedirs(OUT, exist_ok=True)
    titles = {"fact_numeric": "층 B 골드셋 — fact_numeric (단일값, tier1)",
              "dual": "층 B 골드셋 — dual (scope 이중값 함정, tier2)",
              "compute": "층 B 골드셋 — fact_compute (전년比 증가율, tier3)"}
    rich_all = []
    counts = {}
    for name, items in slices.items():
        md = [f"# {titles[name]}", "",
              f"> facts.jsonl 자동생성(gen_goldB.py). 문항 {len(items)}개. 정답 원천 fact_id는 goldB_facts.jsonl.", ""]
        with open(os.path.join(OUT, f"goldB_{name}.jsonl"), "w", encoding="utf-8") as fj:
            for i, (q, mc, mnc, rcept, src, notes, rich) in enumerate(items, 1):
                qid = f"Q{i}"
                md.append(_q_block(qid, q, mc, mnc, rcept, src, notes))
                rec = {"qid": qid, "slice": name, "question": q,
                       "must_contain": mc, "must_not_contain": mnc, "rcepts": [rcept], **rich}
                fj.write(json.dumps(rec, ensure_ascii=False) + "\n")
                rich_all.append(rec)
        with open(os.path.join(OUT, f"goldB_{name}.md"), "w", encoding="utf-8") as fm:
            fm.write("\n".join(md))
        counts[name] = len(items)

    with open(os.path.join(OUT, "goldB_facts.jsonl"), "w", encoding="utf-8") as f:
        for r in rich_all:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summary = {"n_companies": len(best), "counts": counts, "n_total": sum(counts.values()),
               "out_dir": OUT}
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary


if __name__ == "__main__":
    print(json.dumps(generate(), ensure_ascii=False, indent=2))
