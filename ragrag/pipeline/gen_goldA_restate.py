"""gen_goldA_restate.py — 층 A 손검증 코어: 재작성(restatement) 슬라이스.

층 B(단일 필링 자동생성)와 달리, 층 A는 독립 정확도 측정용 하드네거티브를 손검증한다.
재작성 = 같은 (기업·지표·scope·회계연도) 값이 **필링(문서)에 따라 다른** 경우(numqa에서 발견한
doc_id 차원). 자동발견 후보를 **각 값이 해당 필링 raw XML에 실재하는지 grep 대조**해 확정한다(jin V1 방식).

comparison intent 골드: "값이 재작성되었는가?" — 두 필링 값 병기 + 재작성 인지가 정답.
출력: goldset_layerA/goldA_restatement.md · .jsonl · verify_report.md
실행: python gen_goldA_restate.py    (순수 로컬, API 키 없음)
"""
import os
import re
import json
import unicodedata
from collections import defaultdict
from decimal import Decimal

from . import load

_HERE = os.path.dirname(os.path.abspath(__file__))
FACTS = os.path.join(_HERE, "..", "out", "facts.jsonl")
OUT = os.path.join(_HERE, "..", "goldsets", "layerA")
CORE = ("revenue", "operating_income", "net_income", "total_assets", "total_liabilities", "total_equity")
METRIC_KO = {"revenue": "매출액", "operating_income": "영업이익", "net_income": "당기순이익",
             "total_assets": "자산총계", "total_liabilities": "부채총계", "total_equity": "자본총계"}
SCOPE_KO = {"consolidated": "연결", "separate": "별도"}
REL_MIN = 0.001   # 단위/반올림 아티팩트 제외 임계(상대차 0.1%)


def _won(f):
    return int(Decimal(f["value_decimal"]) * f["scale"])


def _digit(v):
    return v.strip().strip("()").lstrip("-").strip()


def _won_form(f):
    """원 정규화 조/억 표기(단위 상이 필링 비교용). 없으면 None."""
    a = abs(_won(f))
    jo, eok = a // 10**12, (a % 10**12) // 10**8
    if jo:
        return f"{jo}조 {eok:,}억" if eok else f"{jo}조"
    if eok:
        return f"{eok:,}억"
    return None


def _mc_tokens(f):
    """must_contain: 원시 자릿수 + 원정규화 조/억(단위 상이 대응, 중복 제거)."""
    toks, seen = [], set()
    for t in (_digit(f["value_raw"]), _won_form(f)):
        if t and t not in seen:
            seen.add(t); toks.append(t)
    return toks


def _grep_raw(entry, digit):
    """value_raw 자릿수 문자열이 해당 문서 raw XML에 실재하는지."""
    path = load.main_xml_path(entry)
    if not path:
        return False
    try:
        raw = load.read_text(path)
    except Exception:
        return False
    return digit in raw


def generate():
    man = {e["rcept_no"]: e for e in load.load_manifest()}
    # 그룹: (corp_code, metric, scope, statement, 회계연도) -> facts[](비-superseded)
    groups = defaultdict(list)
    corp_of = {}
    for line in open(FACTS, encoding="utf-8"):
        d = json.loads(line)
        if not d["metric_key"] or d["is_superseded"] or d["metric_key"] not in CORE:
            continue
        d["_report_year"] = (man.get(d["rcept_no"], {}) or {}).get("base_year")
        groups[(d["corp_code"], d["metric_key"], d["scope"], d["statement"], d["base_year"])].append(d)
        corp_of[d["corp_code"]] = d["corp_name"]

    cand = []       # (key, early, late)
    for key, fs in groups.items():
        byval = {}
        for f in fs:
            byval.setdefault(_won(f), f)
        if len(byval) < 2:
            continue
        lo, hi = min(byval), max(byval)
        if lo == 0 or (hi - lo) / abs(lo) <= REL_MIN:
            continue
        reps = sorted(byval.values(), key=lambda f: (f["_report_year"] or 0))
        cand.append((key, reps[0], reps[-1]))     # 최초 필링값 vs 최신 필링값

    # 손검증: 두 값이 각 필링 raw XML에 실재하는지 grep
    verified, dropped = [], []
    for key, early, late in cand:
        e_ok = _grep_raw(man.get(early["rcept_no"], {}), _digit(early["value_raw"]))
        l_ok = _grep_raw(man.get(late["rcept_no"], {}), _digit(late["value_raw"]))
        (verified if (e_ok and l_ok) else dropped).append((key, early, late, e_ok, l_ok))

    os.makedirs(OUT, exist_ok=True)
    md = ["# 층 A 손검증 — 재작성(restatement) 슬라이스", "",
          f"> gen_goldA_restate.py 자동발견 + raw 2필링 grep 대조 검증. 확정 {len(verified)}건 "
          f"(후보 {len(cand)} / grep 탈락 {len(dropped)}).",
          "> comparison intent: 두 필링 값 병기 + 재작성 인지가 정답. 엄격채점은 goldA_restatement.jsonl.", ""]
    recs = []
    for i, (key, early, late, _e, _l) in enumerate(
            sorted(verified, key=lambda x: (unicodedata.normalize("NFC", corp_of[x[0][0]]), x[0][4])), 1):
        cc, metric, scope, stmt, year = key
        corp = unicodedata.normalize("NFC", corp_of[cc])
        mk = METRIC_KO[metric]
        ev, lv = _digit(early["value_raw"]), _digit(late["value_raw"])
        e_toks, l_toks = _mc_tokens(early), _mc_tokens(late)
        ew, lw = _won_form(early) or ev, _won_form(late) or lv
        ey, ly = early["_report_year"], late["_report_year"]
        qid = f"Q{i}"
        q = (f"{corp}의 {year}년 {SCOPE_KO[scope]} {mk}을 {ey}년 사업보고서와 {ly}년 사업보고서"
             f"(비교표시)에서 각각 확인하면 값이 일치하는가?")
        # must_contain OR: 각 값의 원시+조/억(단위상이 대응). 엄격(둘 다)은 rich. "일치"는 '불일치'와 substring 충돌 → mnc 미사용.
        mc = ", ".join(f"`{t}`" for t in e_toks + l_toks)
        md += [f"## {qid}. {q}",
               f"- must_contain: {mc}",
               f"- must_not_contain: ",
               f"- **출처**: **{SCOPE_KO[scope]} {'재무상태표' if stmt=='balance_sheet' else '손익계산서'}**",
               f"접수번호 {early['rcept_no']} 접수번호 {late['rcept_no']}",
               f"- notes: 재작성 — {ey}년보고서 {ew}({early['unit_kr']}) → {ly}년보고서(비교표시) {lw}({late['unit_kr']}). "
               f"metric={metric} scope={scope}", ""]
        recs.append({
            "qid": qid, "slice": "restatement", "intent": "comparison", "answer_type": "restated",
            "tier": 3, "question": q, "corp_name": corp, "corp_code": cc,
            "metric_key": metric, "scope": scope, "fiscal_year": year,
            "must_contain": e_toks + l_toks, "must_not_contain": [],
            "rcepts": [early["rcept_no"], late["rcept_no"]],
            "won_normalized": {f"report_{ey}": ew, f"report_{ly}": lw},
            "gold_values": {f"report_{ey}": early["value_raw"], f"report_{ly}": late["value_raw"]},
            "restated": True,
            "source": {"early": {"rcept_no": early["rcept_no"], "fact_id": early["fact_id"],
                                 "report_year": ey, "value_raw": early["value_raw"], "unit": early["unit_kr"]},
                       "late": {"rcept_no": late["rcept_no"], "fact_id": late["fact_id"],
                                "report_year": ly, "value_raw": late["value_raw"], "unit": late["unit_kr"]}}})

    with open(os.path.join(OUT, "goldA_restatement.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    with open(os.path.join(OUT, "goldA_restatement.jsonl"), "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # 검증 리포트
    vr = [f"# 재작성 검증 리포트", "",
          f"- 후보 {len(cand)} · 확정(양쪽 grep OK) {len(verified)} · 탈락 {len(dropped)}", "",
          "## grep 탈락(추출 아티팩트 의심)"]
    for key, early, late, e_ok, l_ok in dropped[:30]:
        corp = unicodedata.normalize("NFC", corp_of[key[0]])
        vr.append(f"- {corp} {key[1]} {key[4]}: early_ok={e_ok}({_digit(early['value_raw'])}) "
                  f"late_ok={l_ok}({_digit(late['value_raw'])})")
    with open(os.path.join(OUT, "verify_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(vr))

    from collections import Counter
    return {"candidates": len(cand), "verified": len(verified), "dropped": len(dropped),
            "by_corp": Counter(unicodedata.normalize("NFC", corp_of[k[0]]) for k, *_ in verified).most_common(),
            "by_metric": dict(Counter(k[1] for k, *_ in verified)), "out": OUT}


if __name__ == "__main__":
    print(json.dumps(generate(), ensure_ascii=False, indent=2))
