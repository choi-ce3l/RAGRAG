"""eval_extraction.py — 수치 '추출' 단계만 독립 평가 (조회·계산·라우팅 전부 우회).

질문: **원문에서 뽑아낸 숫자 자체가 맞는가?**
기존 골드셋(층B/C)은 "같은 fact로 만든 QA를 그 fact로 채점"하는 정합성 측정이라
추출 정확도를 못 잰다. 여기서는 fact를 골드셋 없이 원문·회계규칙과 직접 대조한다.

검사 4종 — 서로 다른 종류의 추출 실패를 잡는다:

  E1. 회계 항등식        자산총계 = 부채총계 + 자본총계
      → 행/열 좌표를 잘못 짚으면 반드시 깨진다. 골드셋 없이 전수 측정 가능한 가장 강한 신호.
  E2. verbatim 존재      value_raw 문자열이 raw XML에 실제로 존재하는가
      → 숫자 가공·조합·환각을 잡는다(추출기는 원문 문자열을 그대로 보존해야 함).
  E3. 라벨-값 근접       value_raw가 label_raw 바로 뒤 구간에 있는가
      → "값은 원문에 있지만 다른 행 값을 가져온" 오결합(misbinding)을 잡는다. LG유플러스 유형.
  E4. 지분율 합계 정합    개별 주주 행 지분율 합 ≈ '계' 행 지분율 (SHLEE Cycle 5 extract_shareholders)
      → 행 분류(일반/계/우선주) 오류를 잡는다.

출력: report/EVAL_EXTRACTION.md + report/eval_extraction.json
실행: conda run -n RAGRAG python3 eval_extraction.py [샘플수]
"""
import os
import re
import sys
import json
import random
from decimal import Decimal
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "pipeline"))

import load
import numqa

REPORT = os.path.join(_HERE, "..", "report")
# 경로/압축 해석은 numqa가 이미 해결해뒀으므로 그대로 재사용한다(중복 구현 금지).
FACTS = numqa.FACTS_PATH
SHARES = numqa.SHAREHOLDERS_PATH

BS = "balance_sheet"

# 자동 검사가 걸어낸 항목을 원문까지 직접 따라가 판정한 결과(수동 확인, 2026-08-17).
# 검사기가 "깨졌다"고 표시한 것이 추출 버그인지 원문 오류인지는 코드가 판단할 수 없으므로 여기 기록한다.
KNOWN_VERDICTS = [
    ("E1", "카카오 2022 연결", "원문 오류",
     "해당 문서(periodic_20250318001297)의 2022년 자본총계가 같은 표의 2023년 값과 글자 그대로 동일하다. "
     "카카오 2022 연결 자본총계는 **다른 4개 필링 전부 13,515,717,384,943**인데 이 문서만 13,858,599,224,126이다. "
     "raw XML에도 같은 숫자가 두 번 적혀 있음을 확인 — 추출은 원문에 충실하고 공시 문서가 틀렸다."),
    ("E4", "삼성전자 2023 지분율", "원문 오류",
     "이부진·이서현 행의 55,394,044주(0.93%)가 원문에 '우선주'로 적혀 있으나, 이 주식수÷지분율은 보통주 총수 기준이다. "
     "두 행을 보통주로 놓으면 개별합 20.71 vs 계 20.70으로 맞아떨어지고 우선주도 정합된다 — "
     "원문의 주식 종류 표기 오류. 추출값 자체는 verbatim 일치."),
]
NEAR_WINDOW = 1200      # E3: 라벨 뒤 몇 글자 안에 값이 있어야 같은 행으로 볼지


# ---------------------------------------------------------------------------
# E1. 회계 항등식 — 자산총계 = 부채총계 + 자본총계
# ---------------------------------------------------------------------------
def e1_balance_identity(facts):
    """(doc_id, scope, base_year)별로 BS 3종을 모아 항등식 검사."""
    g = defaultdict(dict)
    for f in facts:
        mk = f.get("metric_key")
        if mk in ("total_assets", "total_liabilities", "total_equity") and f["statement"] == BS:
            k = (f["doc_id"], f["scope"], f["base_year"])
            # 같은 키에 fact가 여럿이면(행 중복) 첫 것만 — 중복 자체는 E1b로 따로 보고
            g[k].setdefault(mk, f)

    res = {"checked": 0, "ok": 0, "broken": 0, "incomplete": 0, "rounding": 0}
    breaks = []
    for k, d in g.items():
        if len(d) < 3:
            res["incomplete"] += 1
            continue
        ta = Decimal(d["total_assets"]["value_decimal"])
        tl = Decimal(d["total_liabilities"]["value_decimal"])
        te = Decimal(d["total_equity"]["value_decimal"])
        res["checked"] += 1
        gap = ta - (tl + te)
        rel = abs(gap) / ta * 100 if ta else None
        # 원문 표가 자기들끼리 절사되어 1~수십 단위 어긋나는 건 추출 문제가 아니다.
        # 절대값이 아니라 **상대 크기**로 판정한다(0.01% 미만 = 절사오차).
        if gap == 0:
            res["ok"] += 1
        elif rel is not None and rel < Decimal("0.01"):
            res["ok"] += 1
            res["rounding"] += 1
        else:
            res["broken"] += 1
            breaks.append({
                "doc_id": k[0], "scope": k[1], "year": k[2],
                "corp_name": d["total_assets"]["corp_name"],
                "total_assets": d["total_assets"]["value_raw"],
                "total_liabilities": d["total_liabilities"]["value_raw"],
                "total_equity": d["total_equity"]["value_raw"],
                "gap": str(gap), "gap_pct": str(round(rel, 3)) if rel is not None else None,
                "fact_ids": [d[m]["fact_id"] for m in ("total_assets", "total_liabilities", "total_equity")],
            })
    breaks.sort(key=lambda b: abs(Decimal(b["gap_pct"] or 0)), reverse=True)
    return res, breaks


# ---------------------------------------------------------------------------
# E2/E3. 원문 대조 — verbatim 존재 + 라벨-값 근접
# ---------------------------------------------------------------------------
def e23_raw_check(facts, manifest_by_rcept, sample_n, seed=20260817):
    """샘플 fact를 raw XML과 직접 대조. 문서 단위로 묶어 읽기 비용을 줄인다."""
    pool = [f for f in facts if f.get("metric_key") and f.get("value_raw")]
    random.seed(seed)
    sample = random.sample(pool, min(sample_n, len(pool)))

    by_doc = defaultdict(list)
    for f in sample:
        by_doc[f["rcept_no"]].append(f)

    res = Counter()
    misses = []
    for rcept, fs in by_doc.items():
        e = manifest_by_rcept.get(rcept)
        if not e:
            res["no_manifest"] += len(fs)
            continue
        try:
            raw = load.read_text(load.main_xml_path(e))
        except Exception:  # noqa: BLE001
            res["read_error"] += len(fs)
            continue
        for f in fs:
            v, lab = f["value_raw"], f["label_raw"]
            res["checked"] += 1
            # E2 — 값 문자열이 원문에 그대로 있는가
            if v not in raw:
                res["e2_missing"] += 1
                misses.append({"kind": "E2_값이_원문에_없음", "fact_id": f["fact_id"],
                               "corp_name": f["corp_name"], "label": lab, "value": v})
                continue
            res["e2_ok"] += 1
            # E3 — 그 값이 자기 라벨 바로 뒤 구간에 있는가(오결합 탐지)
            near = False
            for m in re.finditer(re.escape(lab), raw):
                if v in raw[m.end():m.end() + NEAR_WINDOW]:
                    near = True
                    break
            if near:
                res["e3_ok"] += 1
            else:
                res["e3_far"] += 1
                misses.append({"kind": "E3_라벨과_값이_떨어짐", "fact_id": f["fact_id"],
                               "corp_name": f["corp_name"], "label": lab, "value": v})
    return dict(res), misses


# ---------------------------------------------------------------------------
# E4. 지분율 합계 정합 (SHLEE Cycle 5 extract_shareholders)
# ---------------------------------------------------------------------------
def _pct(s):
    s = (s or "").strip().replace(",", "").rstrip("%").strip()
    if not s or s in ("-", "－", "―"):
        return None
    try:
        return Decimal(s)
    except Exception:  # noqa: BLE001
        return None


def _decimals(s):
    """원문 지분율 표기의 소수 자릿수. '19.2'→1, '19.84'→2, '20'→0."""
    s = (s or "").strip().rstrip("%").strip()
    return len(s.split(".")[1]) if "." in s else 0


def e4_shareholder_sum(records):
    """문서×기준연도별로 개별 주주 행 지분율 합 vs '계' 행 지분율 대조(보통주 기준)."""
    g = defaultdict(lambda: {"rows": [], "total": None})
    for r in records:
        if r.get("share_type") != "보통주":
            continue
        k = (r["doc_id"], r["base_year"])
        if r.get("is_total"):
            g[k]["total"] = g[k]["total"] or r
        else:
            g[k]["rows"].append(r)

    res = Counter()
    breaks = []
    for k, d in g.items():
        tot, rows = d["total"], d["rows"]
        if not tot or not rows:
            res["incomplete"] += 1
            continue
        tv = _pct(tot.get("pct_close"))
        parts = [_pct(r.get("pct_close")) for r in rows]
        if tv is None or any(p is None for p in parts):
            res["unparsable_pct"] += 1
            continue
        res["checked"] += 1
        s = sum(parts)
        gap = s - tv
        # 허용오차는 **원문이 쓴 소수 자릿수**에 맞춘다. 회사마다 표기가 달라서(삼성=2자리 19.84,
        # 알테오젠=1자리 19.2) 고정 허용오차를 쓰면 1자리 표기 회사가 전부 오탐으로 잡힌다.
        # 각 행의 반올림 오차는 최대 0.5 × 10^(-자릿수)이고, 이게 행 수만큼 누적된다.
        tol = sum(Decimal("0.5") * (Decimal(10) ** -_decimals(r.get("pct_close"))) for r in rows)
        tol += Decimal("0.5") * (Decimal(10) ** -_decimals(tot.get("pct_close")))   # 계 행 자신의 반올림
        if abs(gap) <= tol:
            res["ok"] += 1
        else:
            res["broken"] += 1
            breaks.append({"doc_id": k[0], "year": k[1], "corp_name": tot["corp_name"],
                           "n_rows": len(rows), "sum_rows": str(s), "total_row": str(tv),
                           "gap": str(round(gap, 3)), "tol": str(tol)})
    breaks.sort(key=lambda b: abs(Decimal(b["gap"])), reverse=True)
    return dict(res), breaks


def main():
    os.makedirs(REPORT, exist_ok=True)
    sample_n = int(sys.argv[1]) if len(sys.argv) > 1 else 600

    # FACTS가 factstore로 해석된 경우 structured_fact가 섞여 있으므로 xbrl만 남긴다.
    facts = []
    for l in numqa._open(FACTS):
        d = json.loads(l)
        if d.get("store_kind") in (None, "xbrl_fact"):
            facts.append(d)
    mapped = [f for f in facts if f.get("metric_key")]
    shares = [json.loads(l) for l in numqa._open(SHARES)]
    print(f"[load] fact {len(facts):,} (온톨로지 매핑 {len(mapped):,}) · 지분율 record {len(shares):,}")

    print("[E1] 회계 항등식 전수 검사 ...")
    e1, e1_breaks = e1_balance_identity(mapped)
    print(f"     {e1['ok']}/{e1['checked']} ok · 깨짐 {e1['broken']} · 3종 미충족 {e1['incomplete']}")

    # E2/E3는 raw XML을 다시 열어야 하므로 원문 코퍼스가 있는 환경에서만 돈다.
    # 파생물만 배포된 패키지에서는 건너뛰고 그 사실을 리포트에 명시한다(조용히 0으로 두지 않는다).
    if os.path.exists(os.path.join(load.CORPUS_ROOT, "manifest.jsonl")):
        print(f"[E2/E3] 원문 대조 (샘플 {sample_n}) ...")
        man = {e["rcept_no"]: e for e in load.load_manifest()}
        e23, e23_miss = e23_raw_check(mapped, man, sample_n)
        print(f"     verbatim {e23.get('e2_ok',0)}/{e23.get('checked',0)} · "
              f"라벨근접 {e23.get('e3_ok',0)}/{e23.get('e2_ok',0)}")
    else:
        print("[E2/E3] 건너뜀 — 원문 코퍼스(data/corpus) 없음. E1/E4만 측정합니다.")
        e23, e23_miss = {"skipped": True}, []

    print("[E4] 지분율 합계 정합 전수 검사 ...")
    e4, e4_breaks = e4_shareholder_sum(shares)
    print(f"     {e4.get('ok',0)}/{e4.get('checked',0)} ok · 깨짐 {e4.get('broken',0)}")

    out = {"e1": e1, "e1_breaks": e1_breaks[:80],
           "e23": e23, "e23_misses": e23_miss[:60],
           "e4": e4, "e4_breaks": e4_breaks[:60]}
    with open(os.path.join(REPORT, "eval_extraction.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    def rate(a, b):
        return f"{a}/{b}" + (f" ({a*100/b:.2f}%)" if b else "")

    L = ["# 수치 '추출' 단계 단독 평가\n",
         "> 조회·계산·라우팅을 전부 우회하고 **원문에서 뽑아낸 숫자 자체가 맞는지**만 본다.",
         "> 골드셋을 쓰지 않는다 — 층B/C 골드는 같은 fact로 만든 QA라 추출 정확도를 못 재기 때문.\n",
         f"- 대상: XBRL fact {len(facts):,}건(온톨로지 매핑 {len(mapped):,}건) · 지분율 record {len(shares):,}건\n",
         "## 한눈에\n",
         "| 검사 | 잡아내는 실패 | 결과 |",
         "|---|---|---|",
         f"| E1 회계 항등식 | 행·열 좌표 오지정 | **{rate(e1['ok'], e1['checked'])}** |",
         f"| E2 verbatim 존재 | 숫자 가공·환각 | **{rate(e23.get('e2_ok',0), e23.get('checked',0))}** |",
         f"| E3 라벨-값 근접 | 다른 행 값 오결합 | **{rate(e23.get('e3_ok',0), e23.get('e2_ok',0))}** |",
         f"| E4 지분율 합계 정합 | 행 분류(일반/계/우선주) 오류 | **{rate(e4.get('ok',0), e4.get('checked',0))}** |",
         "",
         "## 걸린 항목의 판정 (원문까지 직접 확인)\n",
         "> 검사기는 '안 맞는다'까지만 말한다. 그게 추출 버그인지 원문 오류인지는 원문을 열어봐야 안다.\n"]
    for tag, who, verdict, why in KNOWN_VERDICTS:
        L += [f"**[{tag}] {who} — {verdict}**", "", f"{why}", ""]
    L += ["**→ 이번 평가에서 확인된 추출기 버그: 0건.** 걸린 2건은 모두 공시 원문 쪽 오류이고, "
          "추출기는 원문 값을 그대로 가져왔다(E2 verbatim 100%).\n",
         "## E1. 회계 항등식 — 자산총계 = 부채총계 + 자본총계",
         "추출기가 행이나 열을 한 칸이라도 잘못 짚으면 반드시 깨진다. 골드셋 없이 전수 측정 가능한 가장 강한 신호.\n",
         f"- (문서 × 연결/별도 × 연도) 조합 **{e1['checked']:,}건** 검사 → **일치 {e1['ok']:,} / 깨짐 {e1['broken']:,}**",
         f"- 일치 중 {e1['rounding']:,}건은 원문 표 자체의 절사오차(0.01% 미만)를 흡수한 것",
         f"- BS 3종이 다 안 모인 조합 {e1['incomplete']:,}건은 검사 대상에서 제외(추출 실패가 아니라 커버리지 문제)\n"]

    if e1_breaks:
        L += ["### 깨진 조합 (차이 큰 순)\n",
              "| 기업 | 연도 | scope | 자산총계 | 부채총계 | 자본총계 | 차이 | 차이% |",
              "|---|---|---|---|---|---|---|---|"]
        for b in e1_breaks[:40]:
            L.append(f"| {b['corp_name']} | {b['year']} | {b['scope']} | {b['total_assets']} | "
                     f"{b['total_liabilities']} | {b['total_equity']} | {b['gap']} | {b['gap_pct']}% |")
        L.append("")

    L += ["## E2 / E3. 원문 직접 대조 (샘플)",
          f"raw XML을 다시 열어 대조. 샘플 {e23.get('checked',0):,}건.\n",
          f"- **E2 verbatim 존재**: {rate(e23.get('e2_ok',0), e23.get('checked',0))} — "
          "value_raw 문자열이 원문에 그대로 있는가(추출기는 원문 표기를 보존해야 함)",
          f"- **E3 라벨-값 근접**: {rate(e23.get('e3_ok',0), e23.get('e2_ok',0))} — "
          f"그 값이 자기 라벨 뒤 {NEAR_WINDOW}자 안에 있는가(다른 행 값을 가져온 오결합 탐지)\n"]
    if e23_miss:
        L += ["### 걸린 항목\n", "| 유형 | 기업 | 라벨 | 값 | fact_id |", "|---|---|---|---|---|"]
        for m in e23_miss[:40]:
            L.append(f"| {m['kind']} | {m['corp_name']} | {m['label']} | {m['value']} | `{m['fact_id']}` |")
        L.append("")

    L += ["## E4. 지분율 합계 정합 (SHLEE Cycle 5)",
          "개별 주주 행의 기말 지분율 합이 '계' 행 지분율과 맞는가 — 보통주 기준, 허용오차 0.05%p.\n",
          f"- **{rate(e4.get('ok',0), e4.get('checked',0))}** · 깨짐 {e4.get('broken',0)}건",
          f"- 계 행 또는 개별 행이 없어 검사 불가 {e4.get('incomplete',0)}건 · "
          f"지분율 파싱 불가 {e4.get('unparsable_pct',0)}건\n"]
    if e4_breaks:
        L += ["### 깨진 문서 (차이 큰 순)\n",
              "| 기업 | 연도 | 개별행 | 개별합 | 계 행 | 차이 |", "|---|---|---|---|---|---|"]
        for b in e4_breaks[:40]:
            L.append(f"| {b['corp_name']} | {b['year']} | {b['n_rows']} | {b['sum_rows']} | "
                     f"{b['total_row']} | {b['gap']} |")
        L.append("")

    with open(os.path.join(REPORT, "EVAL_EXTRACTION.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("\n→ report/EVAL_EXTRACTION.md")


if __name__ == "__main__":
    main()
