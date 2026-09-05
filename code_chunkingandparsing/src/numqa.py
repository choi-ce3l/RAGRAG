"""numqa.py — 숫자 QA 팩트경로 (Phase B 오케스트레이션).

16번 프레임워크의 함수 오케스트레이션 구현: LLM은 쓰지 않고(가드레일), 규칙기반 intent 파서 +
facts.lookup_fact/compute로 답한다. 답변의 모든 숫자는 fact/계산 함수 출력에서만 나온다.

경로:
  질문 → parse_intent(규칙) → 라우팅(fact_numeric | dual | fact_compute)
       → lookup_fact / compute → compose(결정론적 문장화) → 답변 + 근거(rcept·fact_id·scope·단위)

CLI:
  python numqa.py "삼성전자의 2025년 연결 기준 매출액은?"   # 단일 질문
  python numqa.py grade                                     # goldset_layerB 팩트경로 채점 리포트
순수 로컬(API 키 없음).
"""
import os
import re
import json
import unicodedata
from decimal import Decimal

import facts as FA
import load

_HERE = os.path.dirname(os.path.abspath(__file__))
GOLD_DIR = os.path.join(_HERE, "..", "goldset_layerB")
FACTS_PATH = os.path.join(_HERE, "..", "out", "facts.jsonl")
FACTX_PATH = os.path.join(_HERE, "..", "out", "factx.jsonl")
STORE_PATH = os.path.join(_HERE, "..", "out", "factstore.jsonl")

METRIC_KO = {"revenue": "매출액", "operating_income": "영업이익", "net_income": "당기순이익",
             "total_assets": "자산총계", "total_liabilities": "부채총계", "total_equity": "자본총계",
             "debt_ratio": "부채비율", "current_ratio": "유동비율"}
_KO2METRIC = {v: k for k, v in METRIC_KO.items()}
METRIC_STMT = {"revenue": "income_statement", "operating_income": "income_statement",
               "net_income": "income_statement", "total_assets": "balance_sheet",
               "total_liabilities": "balance_sheet", "total_equity": "balance_sheet",
               "debt_ratio": "ratio", "current_ratio": "ratio"}
SCOPE_KO = {"consolidated": "연결", "separate": "별도"}


def _digit(value_raw):
    return value_raw.strip().strip("()").lstrip("-").strip()


def _won_form(value_decimal, scale):
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


# ---------------------------------------------------------------------------
# fact 인덱스
# ---------------------------------------------------------------------------
class FactStore:
    def __init__(self, facts_list):
        self.facts = facts_list
        self.by_id = {f["fact_id"]: f for f in facts_list}
        self.corp_names = sorted({f["corp_name"] for f in facts_list}, key=len, reverse=True)
        self.corp_code = {}
        for f in facts_list:
            self.corp_code.setdefault(f["corp_name"], f["corp_code"])
        # (corp_code, metric, scope, statement, year) -> fact (동일값 중복은 첫 것)
        # 주의: 같은 (corp,metric,scope,year)라도 문서가 다르면 값이 다를 수 있다(재작성/단위상이).
        # 그래서 문서단위 인덱스도 둔다 — 증가율은 반드시 같은 문서의 당기·전기로 계산.
        self.idx = {}
        self.idx_doc = {}
        for f in facts_list:
            if not f.get("metric_key"):        # 구조화 fact(비-XBRL)는 metric_key 없음 → xbrl 인덱스 skip
                continue
            k = (f["metric_key"], f["scope"], f["statement"], f["base_year"])
            self.idx.setdefault((f["corp_code"],) + k, f)
            self.idx_doc.setdefault((f["doc_id"],) + k, f)

    @classmethod
    def load(cls, path=None):
        path = path or (STORE_PATH if os.path.exists(STORE_PATH) else FACTS_PATH)
        # XBRL 수치 fact(+비율)만 필요. 통합 store에서 xbrl_fact + metric_key만 걸러 로드.
        recs = []
        for line in open(path, encoding="utf-8"):
            d = json.loads(line)
            if d.get("store_kind") in (None, "xbrl_fact") and d.get("metric_key"):
                recs.append(d)
        return cls(recs)

    def lookup(self, corp_code, metric, scope, year, statement=None):
        statement = statement or METRIC_STMT.get(metric)
        return self.idx.get((corp_code, metric, scope, statement, year))

    def lookup_in_doc(self, doc_id, metric, scope, year, statement=None):
        """같은 문서 내 조회 — 증가율의 전기값은 당기 fact와 같은 보고서에서 가져와야 정합(재작성·단위)."""
        statement = statement or METRIC_STMT.get(metric)
        return self.idx_doc.get((doc_id, metric, scope, statement, year))


# ---------------------------------------------------------------------------
# ① intent 파서 (규칙기반 — API 키 없음)
# ---------------------------------------------------------------------------
def parse_intent(q, store):
    # jin CHOI_상태파악.md 결함 수정: 기업은 prefix가 아닌 부분일치(긴 이름 우선), scope는 compute에도 반영,
    # comparison/existence intent 추가.
    corp = next((n for n in store.corp_names if n in q), None)   # corp_names는 길이 desc 정렬
    corp_code = store.corp_code.get(corp) if corp else None
    ym = re.search(r"(\d{4})\s*년", q)
    year = int(ym.group(1)) if ym else None
    scope = "consolidated" if "연결" in q else ("separate" if "별도" in q else None)
    metric = next((mk for ko, mk in _KO2METRIC.items() if ko in q), None)
    # "재작성치 대비 ~% 증가했는가"처럼 증가율(fact_compute) 신호가 함께 있으면
    # comparison보다 먼저 본다 — "재작성"이라는 낱말만으로 comparison(정정 전/후
    # 문서 대조)으로 몰아가면, 실제로는 계산 가능한 증가율 질문까지 route_narrative로
    # 새 나간다(SEM-NUM-12 등 최대 11건 실측 확인). comparison 전용 신호(정정/전후/
    # 일치하는가/바뀌)만 있고 증가율 신호가 없을 때만 comparison으로 본다.
    if re.search(r"증가|감소|증감|몇\s*%|증가율", q):
        intent = "fact_compute"
    elif re.search(r"정정|전후|전/후|일치하는가|재작성|바뀌", q):
        intent = "comparison"
    elif re.search(r"기재(되어 있지|하지) 않|미기재|없는가|부재|확인 불가", q):
        intent = "existence"
    elif scope is None:
        intent = "dual"          # scope 미지정 = 연결/별도 이중값 함정
    else:
        intent = "fact_numeric"
    return {"intent": intent, "corp": corp, "corp_code": corp_code,
            "year": year, "scope": scope, "metric": metric}


# ---------------------------------------------------------------------------
# ②③⑤⑥ 라우팅 + 조회/계산 + 결정론적 문장화 (숫자는 fact/함수 출력만)
# ---------------------------------------------------------------------------
def _fact_phrase(f):
    if f.get("unit_kr") == "%":
        return f["value_raw"]                       # 이미 %가 포함된 비율값
    w = _won_form(f["value_decimal"], f["scale"])
    return f"{f['value_raw']}{f['unit_kr']}" + (f" ({w}원)" if w else "")


def answer(q, store):
    p = parse_intent(q, store)
    corp, cc, year, scope, metric = p["corp"], p["corp_code"], p["year"], p["scope"], p["metric"]
    out = {"question": q, "parsed": p, "text": "", "numbers": [], "sources": [], "status": "ok"}
    # comparison(정정 전/후)·existence(부재)는 XBRL 수치경로가 답할 수 없음 → rag narrative로 폴백.
    if p["intent"] in ("comparison", "existence"):
        out.update(status="route_narrative", text="")
        return out
    if not (corp and metric and year):
        out.update(status="unparsed", text="질문에서 기업/지표/연도를 특정하지 못했습니다.")
        return out

    if p["intent"] == "fact_compute":
        sc = scope or "consolidated"      # jin 결함수정: compute도 scope 반영(별도 증가율 등)
        cur = store.lookup(cc, metric, sc, year)
        prev = store.lookup_in_doc(cur["doc_id"], metric, sc, year - 1) if cur else None
        if not (cur and prev):
            out.update(status="no_fact", text="계산에 필요한 fact를 찾지 못했습니다.")
            return out
        r = FA.compute("growth", [cur, prev])
        pct = round(float(r["value"]), 1)
        out["numbers"] = [f"{pct}%"]
        out["sources"] = [{"fact_id": fid} for fid in r["inputs"]] + \
                         [{"rcept_no": cur["rcept_no"]}]
        out["text"] = (f"{corp}의 {year}년 {SCOPE_KO[sc]} {METRIC_KO[metric]}은 전년({year-1}년) 대비 "
                       f"{pct}% {'증가' if pct >= 0 else '감소'}했습니다 "
                       f"({cur['value_raw']} vs {prev['value_raw']}, 단위 {cur['unit_kr']}).")
        return out

    if p["intent"] == "dual":
        cf = store.lookup(cc, metric, "consolidated", year)
        sf = store.lookup(cc, metric, "separate", year)
        found = [(SCOPE_KO[s], f) for s, f in (("consolidated", cf), ("separate", sf)) if f]
        if not found:
            out.update(status="no_fact", text="해당 지표 fact를 찾지 못했습니다.")
            return out
        parts = [f"{sk} {_fact_phrase(f)}" for sk, f in found]
        out["numbers"] = [_digit(f["value_raw"]) for _, f in found]
        out["sources"] = [{"scope": sk, "fact_id": f["fact_id"], "rcept_no": f["rcept_no"],
                           "aclass": f["aclass_xbrl_code"]} for sk, f in found]
        note = " — 연결/별도 기준이 다르므로 구분이 필요합니다." if len(found) == 2 else ""
        out["text"] = f"{corp}의 {year}년 {METRIC_KO[metric]}은 " + " / ".join(parts) + note
        return out

    # fact_numeric (scope 명시)
    f = store.lookup(cc, metric, scope, year)
    if not f:
        out.update(status="no_fact", text="해당 fact를 찾지 못했습니다.")
        return out
    out["numbers"] = [_digit(f["value_raw"])]
    out["sources"] = [{"scope": scope, "fact_id": f["fact_id"], "rcept_no": f["rcept_no"],
                       "aclass": f["aclass_xbrl_code"], "row": f["label_raw"]}]
    out["text"] = f"{corp}의 {year}년 {SCOPE_KO[scope]} 기준 {METRIC_KO[metric]}은 {_fact_phrase(f)}입니다."
    return out


# ---------------------------------------------------------------------------
# 비-XBRL 구조화(doc-type) 조회 — Phase 2b
# ---------------------------------------------------------------------------
# 필드 온톨로지: (질문 키워드 all-match, doc_group, field_key, 표현명). 첫 매칭 채택.
# 순서 중요(첫 매칭 채택): 구체 필드(금액/목적/방법)를 먼저, 주식수(count)는 일반 폴백.
# 주의: "주식" 키워드는 "자기주식"에 substring 매칭되므로 count 판정에 쓰지 않는다.
_DOCTYPE_FIELDS = [
    (["자기주식", "취득", "금액"], "major", "ACQ_OSTK_PRC", "취득 예정 보통주식 금액"),
    (["자기주식", "취득", "목적"], "major", "ACQ_PPS", "취득 목적"),
    (["자기주식", "취득", "방법"], "major", "ACQ_MTH", "취득 방법"),
    (["자기주식", "취득"], "major", "ACQ_OSTK", "취득 예정 보통주식수"),
    (["공급계약", "금액"], "exchange", "계약금액(원)", "계약금액"),
    (["공급계약", "상대"], "exchange", "계약상대", "계약상대"),
    (["공급계약", "매출액대비"], "exchange", "매출액대비(%)", "매출액대비"),
    (["대량보유", "비율"], "holding", "SUM_TMT_RT", "이번 보고서 보유비율"),
]
_STRUCT_KEYS = {(g, fk) for _, g, fk, _ in _DOCTYPE_FIELDS}


class StructStore:
    """비-XBRL 구조화 fact(factx) 조회. (corp, doc_group, field_key) 인덱스 + 필링 메타(rcept_dt/report_nm)."""
    def __init__(self, recs, manifest):
        self.corp_names = sorted({f["corp_name"] for f in recs}, key=len, reverse=True)
        self.corp_code = {}
        self.idx = {}
        for f in recs:
            self.corp_code.setdefault(f["corp_name"], f["corp_code"])
            self.idx.setdefault((f["corp_code"], f["doc_group"], f["field_key"]), []).append(f)
        self.rcept_dt = {e["rcept_no"]: e.get("rcept_dt", "") for e in manifest}
        self.report_nm = {e["rcept_no"]: e.get("report_nm", "") for e in manifest}

    @classmethod
    def load(cls, path=FACTX_PATH):
        recs = []
        for line in open(path, encoding="utf-8"):
            d = json.loads(line)
            if (d.get("doc_group"), d.get("field_key")) in _STRUCT_KEYS:
                recs.append(d)
        return cls(recs, load.load_manifest())


def _q_date(q):
    m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", q)
    return f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}" if m else None


def answer_struct(q, sstore):
    """doc-type 질문 → 단서(날짜>연도>키워드)로 필링을 찾아 exact 값 반환. '최신 default' 안 함."""
    out = {"question": q, "text": "", "numbers": [], "sources": [], "status": "route_narrative"}
    field = next(((g, fk, lab) for kws, g, fk, lab in _DOCTYPE_FIELDS if all(k in q for k in kws)), None)
    if not field:
        return out
    corp = next((n for n in sstore.corp_names if n in q), None)
    if not corp:
        out["status"] = "unparsed"; return out
    cc = sstore.corp_code[corp]
    g, fk, lab = field
    cands = [f for f in sstore.idx.get((cc, g, fk), []) if not f.get("is_superseded")]
    if not cands:
        out["status"] = "no_fact"; return out
    # 같은 필링 내 중복 field_key(예: 정정전/후 계약상대)는 마지막(실효값) 채택
    by_rcept = {}
    for f in cands:
        by_rcept[f["rcept_no"]] = f
    cands = list(by_rcept.values())
    # 단서로 필링 찾기 (최신 default 금지)
    qd = _q_date(q)
    ym = re.search(r"(\d{4})\s*년", q)
    year = ym.group(1) if ym else None
    sel = None
    if qd:
        sel = ([f for f in cands if sstore.rcept_dt.get(f["rcept_no"], "") == qd]
               or [f for f in cands if sstore.rcept_dt.get(f["rcept_no"], "")[:6] == qd[:6]])
    if not sel and year:
        sel = [f for f in cands if sstore.rcept_dt.get(f["rcept_no"], "")[:4] == year]
    if not sel:
        sel = cands
    if len(sel) > 1 and len({f["value_raw"] for f in sel}) > 1:
        # 단서로 못 좁힘 → 회차를 나열(임의 선택 안 함)
        opts = sorted(sel, key=lambda f: sstore.rcept_dt.get(f["rcept_no"], ""))
        out.update(status="ambiguous",
                   text=f"{corp} {lab}은(는) 여러 회차가 있어 특정이 필요합니다: " +
                        "; ".join(f"{sstore.report_nm.get(f['rcept_no'], '')}={f['value_raw']}" for f in opts[:6]),
                   sources=[{"rcept_no": f["rcept_no"], "fact_id": f["fact_id"]} for f in opts])
        return out
    f = sel[0]
    rn = sstore.report_nm.get(f["rcept_no"], "")
    out.update(status="ok", numbers=[_digit(f["value_raw"])],
               sources=[{"rcept_no": f["rcept_no"], "fact_id": f["fact_id"], "field": fk, "report_nm": rn}],
               text=f"{corp} {rn} {lab}: {f['value_raw']}")
    return out


# ---------------------------------------------------------------------------
# 골드셋 팩트경로 채점
# ---------------------------------------------------------------------------
def _tok_in(tok, text):
    return tok in text


def grade(gold_dir=GOLD_DIR, store=None):
    store = store or FactStore.load()
    report = {"slices": {}, "total": {"n": 0, "pass": 0, "strict_pass": 0, "unparsed": 0, "no_fact": 0}}
    lines = ["# 층 B 골드셋 — 팩트경로 채점 (numqa, LLM 없음)\n"]
    for name in ("fact_numeric", "dual", "compute"):
        path = os.path.join(gold_dir, f"goldB_{name}.jsonl")
        if not os.path.exists(path):
            continue
        items = [json.loads(l) for l in open(path, encoding="utf-8")]
        n = p = sp = unp = nof = 0
        fails = []
        for it in items:
            res = answer(it["question"], store)
            text = res["text"]
            if res["status"] == "unparsed":
                unp += 1
            if res["status"] == "no_fact":
                nof += 1
            mc = it.get("must_contain", [])
            mnc = it.get("must_not_contain", [])
            mc_hit = any(_tok_in(t, text) for t in mc)
            mnc_hit = any(_tok_in(t, text) for t in mnc)
            ok = (mc_hit or not mc) and not mnc_hit
            # 엄격: dual은 두 gold값 모두, 그 외는 표준 채점
            if name == "dual":
                gv = it.get("gold_values", {})
                strict = all(_digit(v) in text for v in gv.values()) and not mnc_hit
            else:
                strict = ok
            n += 1
            p += ok
            sp += strict
            if not strict:
                fails.append((it["qid"], it["question"], res["status"], text[:80]))
        report["slices"][name] = {"n": n, "pass": p, "strict_pass": sp, "unparsed": unp, "no_fact": nof}
        for k in ("n", "pass", "strict_pass", "unparsed", "no_fact"):
            report["total"][k] += report["slices"][name][k]
        lines.append(f"## {name}: {p}/{n} pass (엄격 {sp}/{n}) · unparsed {unp} · no_fact {nof}")
        for qid, q, st, t in fails[:8]:
            lines.append(f"  - [{qid}] {st}: {q}\n      → {t}")
        lines.append("")
    t = report["total"]
    lines.insert(1, f"- 전체 {t['pass']}/{t['n']} pass · 엄격 {t['strict_pass']}/{t['n']} · "
                    f"unparsed {t['unparsed']} · no_fact {t['no_fact']}\n")
    with open(os.path.join(gold_dir, "factpath_eval.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return report


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "grade":
        rep = grade()
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1:
        st = FactStore.load()
        res = answer(" ".join(sys.argv[1:]), st)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(__doc__)
