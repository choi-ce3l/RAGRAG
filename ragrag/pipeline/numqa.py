"""numqa.py — 숫자 QA 팩트경로 (병합본: choi 라이브 + SHLEE Cycle 3~5).

병합 기준(2026-08-17):
  base = choi/code_chunkingandparsing/src/numqa.py (10:22 스냅샷)
       — 통합 store(factstore 1.84M) 로드, StructStore(비-XBRL doc-type 조회),
         comparison/existence intent, 기업명 부분일치, debt_ratio/current_ratio 원문추출 fact.
  merge in = SHLEE/AGENT/04_FUNCTION_DESIGNER/numqa_local/src/numqa.py (09:49 스냅샷)
       — FactStore 재작성 다건보존 + latest-valid(Cycle 4), ratio 계산경로(Cycle 3),
         sign_flip 문구화, zero-division/missing-fact 가드, shareholder_lookup(Cycle 5).

병합 시 새로 정한 것 하나: **비율(ratio) intent 통합**.
  choi는 부채비율/유동비율을 MD&A 원문에서 뽑은 fact로 "조회"하고,
  SHLEE는 XBRL 부채총계/자본총계로 "재계산"한다 — 서로 독립된 두 경로다.
  여기서는 원문 보고값을 우선 채택(공시 이용자가 실제로 보는 값)하고, 계산 가능한 경우
  계산값을 `cross_check`에 함께 실어 두 경로가 일치하는지 항상 드러나게 한다.
  (이 필드가 crossval_ratio.py의 174건 전수 대조 근거가 된다.)

경로:
  질문 → parse_intent(규칙)
       → 라우팅(shareholder_lookup | comparison/existence | ratio | fact_compute | dual | fact_numeric)
       → lookup / compute → compose(결정론적 문장화) → 답변 + 근거(rcept·fact_id·scope·단위)

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

from . import facts as FA
from . import load

_HERE = os.path.dirname(os.path.abspath(__file__))
GOLD_DIR = os.path.join(_HERE, "..", "goldsets", "layerB")
_OUT = os.path.join(_HERE, "..", "out")


def _resolve(*names):
    """out/ 아래에서 이름을 순서대로 찾되 .gz 변형도 함께 본다. 없으면 첫 후보 경로 반환."""
    for n in names:
        for cand in (os.path.join(_OUT, n), os.path.join(_OUT, n + ".gz")):
            if os.path.exists(cand):
                return cand
    return os.path.join(_OUT, names[0])


def _open(path):
    """.gz면 투명하게 풀어서 연다 — 배포 패키지는 압축본만 담기 때문."""
    if path.endswith(".gz"):
        import gzip
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


# factstore 하나에 xbrl_fact + structured_fact가 다 들어 있다(필드 손실 없음).
# 배포 패키지는 factstore만 담고, 개발 환경에 facts/factx가 따로 있으면 그것도 인정한다.
STORE_PATH = _resolve("factstore.jsonl")
FACTS_PATH = _resolve("facts.jsonl", "factstore.jsonl")
FACTX_PATH = _resolve("factx.jsonl", "factstore.jsonl")
# 필링 메타(rcept_dt/report_nm). 원문 코퍼스 없이 동작하도록 패키지에 동봉한다.
FILINGS_PATH = _resolve("filings.jsonl")
# SHLEE Cycle 5 — 지분율(BSH_SPCL) record. metric-value 기반 idx와 구조가 달라 별도 파일.
SHAREHOLDERS_PATH = _resolve("shareholders.jsonl")

METRIC_KO = {"revenue": "매출액", "operating_income": "영업이익", "net_income": "당기순이익",
             "total_assets": "자산총계", "total_liabilities": "부채총계", "total_equity": "자본총계",
             "debt_ratio": "부채비율", "current_ratio": "유동비율"}
_KO2METRIC = {v: k for k, v in METRIC_KO.items()}
METRIC_STMT = {"revenue": "income_statement", "operating_income": "income_statement",
               "net_income": "income_statement", "total_assets": "balance_sheet",
               "total_liabilities": "balance_sheet", "total_equity": "balance_sheet",
               "debt_ratio": "ratio", "current_ratio": "ratio"}
SCOPE_KO = {"consolidated": "연결", "separate": "별도"}

# SHLEE Cycle 3(A) — 비율형 지표의 XBRL 재계산 경로: (분자metric, 분모metric).
# facts.compute("ratio",...)는 원래 있었으나 answer()가 호출하지 않았음(FINDINGS F-004).
RATIO_KO2METRICS = {"부채비율": ("total_liabilities", "total_equity"),
                    "자기자본비율": ("total_equity", "total_assets")}
# 원문(MD&A) 추출 fact가 존재하는 비율 — choi factx Phase 1b 산출.
RATIO_KO2REPORTED = {"부채비율": "debt_ratio", "유동비율": "current_ratio"}
_RATIO_KOS = sorted(set(RATIO_KO2METRICS) | set(RATIO_KO2REPORTED), key=len, reverse=True)


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
    def __init__(self, facts_list, shareholders_list=None):
        self.facts = facts_list
        # SHLEE Cycle 5 — 지분율 record는 (성명/관계/지분율) 구조라 idx 키와 안 맞음. 별도 리스트로 보관.
        self.shareholders = shareholders_list or []
        self.by_id = {f["fact_id"]: f for f in facts_list}
        self.corp_names = sorted({f["corp_name"] for f in facts_list}, key=len, reverse=True)
        self.corp_code = {}
        for f in facts_list:
            self.corp_code.setdefault(f["corp_name"], f["corp_code"])
        # (corp_code, metric, scope, statement, year) -> list[fact], rcept_no 오름차순(문서 작성 순).
        # SHLEE Cycle 4(restatement_policy.md §3/§7): 예전엔 setdefault로 첫 값만 유지해 정정/재작성본을
        # 조용히 버렸다(F-002-ROOT). 이제 같은 키의 모든 문서를 보존하고 lookup()이 latest-valid로 고른다.
        # 문서단위 인덱스(idx_doc)는 그대로 — 증가율은 반드시 같은 문서의 당기·전기로 계산해야 정합.
        self.idx = {}
        self.idx_doc = {}
        for f in facts_list:
            if not f.get("metric_key"):    # 구조화 fact(비-XBRL)는 metric_key 없음 → xbrl 인덱스 skip
                continue
            k = (f["corp_code"], f["metric_key"], f["scope"], f["statement"], f["base_year"])
            self.idx.setdefault(k, []).append(f)
            self.idx_doc.setdefault(
                (f["doc_id"], f["metric_key"], f["scope"], f["statement"], f["base_year"]), f)
        for k, lst in self.idx.items():
            by_doc = {}
            for f in lst:
                by_doc.setdefault(f["doc_id"], f)
            self.idx[k] = sorted(by_doc.values(), key=lambda f: f["rcept_no"])

    @classmethod
    def load(cls, path=None, shareholders_path=SHAREHOLDERS_PATH):
        path = path or (STORE_PATH if os.path.exists(STORE_PATH) else FACTS_PATH)
        # XBRL 수치 fact(+비율)만 필요. 통합 store에서 xbrl_fact + metric_key만 걸러 로드.
        recs = []
        for line in _open(path):
            d = json.loads(line)
            if d.get("store_kind") in (None, "xbrl_fact") and d.get("metric_key"):
                recs.append(d)
        sh_list = []
        if os.path.exists(shareholders_path):
            sh_list = [json.loads(l) for l in _open(shareholders_path)]
        return cls(recs, sh_list)

    def lookup(self, corp_code, metric, scope, year, statement=None, policy="latest_valid"):
        """policy: "latest_valid"(기본, 최신 유효값) | "as_filed"(최초 유효값).
        is_superseded==False로 먼저 거른 뒤 rcept_no 순서로 고른다 — KB금융 실사례에서 rcept_no가
        더 큰 문서가 오히려 is_superseded=True인 경우가 있어 순서가 중요(restatement_policy.md §4).
        """
        statement = statement or METRIC_STMT.get(metric)
        candidates = self.idx.get((corp_code, metric, scope, statement, year), [])
        if not candidates:
            return None
        valid = [f for f in candidates if not f.get("is_superseded")] or candidates
        if policy == "latest_valid":
            return valid[-1]
        if policy == "as_filed":
            return valid[0]
        raise ValueError(f"unknown policy: {policy}")

    def lookup_all(self, corp_code, metric, scope, year, statement=None, include_superseded=True):
        """rcept_no 오름차순(과거→최신) 전체 반환 — lookup()의 latest_valid가 항상
        lookup_all()[-1]과 같아지도록 일관성 유지(restatement_policy.md §5)."""
        statement = statement or METRIC_STMT.get(metric)
        candidates = self.idx.get((corp_code, metric, scope, statement, year), [])
        if not include_superseded:
            candidates = [f for f in candidates if not f.get("is_superseded")]
        return candidates

    def lookup_in_doc(self, doc_id, metric, scope, year, statement=None):
        """같은 문서 내 조회 — 증가율의 전기값은 당기 fact와 같은 보고서에서 가져와야 정합(재작성·단위)."""
        statement = statement or METRIC_STMT.get(metric)
        return self.idx_doc.get((doc_id, metric, scope, statement, year))


# ---------------------------------------------------------------------------
# ① intent 파서 (규칙기반 — API 키 없음)
# ---------------------------------------------------------------------------
def parse_intent(q, store):
    # 기업은 prefix가 아닌 부분일치(긴 이름 우선) — jin CHOI_상태파악.md 결함 수정(choi).
    corp = next((n for n in store.corp_names if n in q), None)
    corp_code = store.corp_code.get(corp) if corp else None
    ym = re.search(r"(\d{4})\s*년", q)
    year = int(ym.group(1)) if ym else None
    scope = "consolidated" if "연결" in q else ("separate" if "별도" in q else None)
    ratio_ko = next((ko for ko in _RATIO_KOS if ko in q), None)
    # 지표 추출은 intent 분기 **밖**에서 한 번만 — 분기 안으로 넣으면 comparison/existence 같은
    # 분기에서 metric이 누락돼 조용히 narrative로 새어나간다(병합 중 실제로 밟은 함정).
    metric = next((mk for ko, mk in _KO2METRIC.items() if ko in q), None)
    ratio_pair = ratio_metric = sh_kinds = None

    if "최대주주" in q or "지분율" in q:
        # SHLEE Cycle 5 — 지분율 record는 metric-value 구조가 아니라 가장 먼저 분리 판정.
        # "지분율은 몇 %"처럼 growth 정규식과 겹쳐도 이 분기가 먼저라 fact_compute로 새지 않음.
        intent = "shareholder_lookup"
        metric = None                 # 지분율 record는 metric-value 구조가 아님
        sh_kinds = []
        if "단독" in q:
            sh_kinds.append("major")
        if "합산" in q or "특수관계인" in q:
            sh_kinds.append("total")
        if not sh_kinds:
            sh_kinds = ["major"]
    elif re.search(r"정정|전후|전/후|일치하는가|재작성|바뀌", q):
        intent = "comparison"
    elif re.search(r"기재(되어 있지|하지) 않|미기재|없는가|부재|확인 불가", q):
        intent = "existence"
    elif ratio_ko:
        # 비율 어휘가 "몇 %"를 포함해도 growth 정규식보다 먼저 판정 — F-TEST-003(unparsed/오라우팅) 수정.
        intent = "ratio"
        ratio_pair = RATIO_KO2METRICS.get(ratio_ko)         # XBRL 재계산 경로(없을 수 있음)
        ratio_metric = RATIO_KO2REPORTED.get(ratio_ko)      # 원문 추출 fact 경로(없을 수 있음)
        metric = ratio_metric
    else:
        if re.search(r"증가|감소|증감|몇\s*%|증가율", q):
            intent = "fact_compute"
        elif scope is None:
            intent = "dual"          # scope 미지정 = 연결/별도 이중값 함정
        else:
            intent = "fact_numeric"
    return {"intent": intent, "corp": corp, "corp_code": corp_code,
            "year": year, "scope": scope, "metric": metric,
            "ratio_ko": ratio_ko, "ratio_pair": ratio_pair, "ratio_metric": ratio_metric,
            "sh_kinds": sh_kinds}


# ---------------------------------------------------------------------------
# ②③⑤⑥ 라우팅 + 조회/계산 + 결정론적 문장화 (숫자는 fact/함수 출력만)
# ---------------------------------------------------------------------------
def _fact_phrase(f):
    if f.get("unit_kr") == "%":
        return f["value_raw"]                       # 이미 %가 포함된 비율값
    w = _won_form(f["value_decimal"], f["scale"])
    return f"{f['value_raw']}{f['unit_kr']}" + (f" ({w}원)" if w else "")


def _ratio_computed(store, cc, scope, year, ratio_pair):
    """XBRL 구성요소로 비율을 재계산 — (값, 분자fact, 분모fact) 또는 (None, ...) 반환."""
    if not ratio_pair:
        return None, None, None
    num_key, den_key = ratio_pair
    num_f = store.lookup(cc, num_key, scope, year)
    den_f = store.lookup(cc, den_key, scope, year)
    if not (num_f and den_f):
        return None, num_f, den_f
    try:
        r = FA.compute("ratio", [num_f, den_f])
    except (ZeroDivisionError, TypeError):        # F-TEST-004/F-FUNC-002 가드
        return None, num_f, den_f
    return round(float(r["value"]), 1), num_f, den_f


def answer(q, store):
    p = parse_intent(q, store)
    corp, cc, year, scope, metric = p["corp"], p["corp_code"], p["year"], p["scope"], p["metric"]
    out = {"question": q, "parsed": p, "text": "", "numbers": [], "sources": [], "status": "ok"}

    # --- 지분율 (SHLEE Cycle 5) -------------------------------------------------
    if p["intent"] == "shareholder_lookup":
        if not corp:
            out.update(status="unparsed", text="질문에서 기업을 특정하지 못했습니다.")
            return out
        eff_year = year
        if eff_year is None:
            years = [r["base_year"] for r in store.shareholders if r["corp_code"] == cc]
            eff_year = max(years) if years else None
        if eff_year is None:
            out.update(status="no_fact", text="지분율 관련 fact를 찾지 못했습니다.")
            return out
        parts, numbers, sources = [], [], []
        for kind in p["sh_kinds"]:
            rec = FA.lookup_shareholder(store.shareholders, cc, eff_year, kind=kind)
            if not rec:
                continue
            pct = rec["pct_close"]
            numbers.append(f"{pct}%")
            sources.append({"fact_id": rec["fact_id"], "rcept_no": rec["rcept_no"]})
            if kind == "major":
                parts.append(f"최대주주는 {rec['holder_name']}이며 단독 지분율은 {pct}%")
            else:
                parts.append(f"특수관계인 합산 지분율은 {pct}%")
        if not parts:
            out.update(status="no_fact", text="지분율 관련 fact를 찾지 못했습니다.")
            return out
        out["numbers"] = numbers
        out["sources"] = sources
        out["text"] = f"{corp}의 {eff_year}년 기준 " + ", ".join(parts) + "입니다."
        return out

    # --- 재작성/정정 전후 비교 (병합으로 처음 가능해진 경로) ------------------
    # choi 라이브는 comparison 어휘를 인식만 하고 narrative로 흘려보냈고(수치경로가 답할 수 없었음),
    # SHLEE는 lookup_all()로 필링별 다건을 보존해뒀지만 호출할 intent가 없었다(F-TEST-012).
    # 둘을 합치면 "같은 (기업·지표·scope·연도)를 여러 필링이 각각 얼마로 보고했나"를 그대로 답할 수 있다.
    if p["intent"] == "comparison":
        if not (corp and metric and year):
            out.update(status="route_narrative", text="")
            return out
        sc = scope or "consolidated"
        recs = store.lookup_all(cc, metric, sc, year, include_superseded=False)
        if not recs:
            out.update(status="no_fact", text="해당 fact를 찾지 못했습니다.")
            return out
        if len(recs) == 1:
            f = recs[0]
            out["numbers"] = [_digit(f["value_raw"])]
            out["sources"] = [{"fact_id": f["fact_id"], "rcept_no": f["rcept_no"]}]
            out["text"] = (f"{corp}의 {year}년 {SCOPE_KO[sc]} {METRIC_KO[metric]}은 "
                           f"보고서 1건({f['rcept_no']})에서만 확인되며 값은 {_fact_phrase(f)}입니다 "
                           f"— 필링 간 비교 대상이 없습니다.")
            return out
        # 필링별 값 나열. 단위(scale)가 다르면 원 단위로 환산해 비교해야 함(노트 22 교훈).
        parts, nums, srcs = [], [], []
        won_vals = []
        for f in recs:
            rn = f["rcept_no"]
            # 접수연도(rn[:4])를 "N년 보고서"라 쓰면 사업연도와 헷갈림 → 접수번호를 그대로 표기.
            parts.append(f"접수 {rn} 보고서 {_fact_phrase(f)}")
            nums.append(_digit(f["value_raw"]))
            srcs.append({"fact_id": f["fact_id"], "rcept_no": rn, "doc_id": f["doc_id"],
                         "unit_kr": f["unit_kr"], "scale": f["scale"]})
            won_vals.append(Decimal(f["value_decimal"]) * Decimal(f["scale"]))
        same = len(set(won_vals)) == 1
        if same:
            verdict = "모든 보고서의 값이 일치합니다(재작성 없음)."
        else:
            lo, hi = min(won_vals), max(won_vals)
            pct = (hi - lo) / abs(lo) * 100 if lo else None
            verdict = ("보고서마다 값이 다릅니다(재작성/재분류)"
                       + (f" — 원 단위 환산 기준 최대 {round(float(pct), 1)}% 차이." if pct is not None else "."))
        out["numbers"] = nums
        out["sources"] = srcs
        out["restated"] = not same
        out["text"] = (f"{corp}의 {year}년 {SCOPE_KO[sc]} {METRIC_KO[metric]}은 "
                       + " / ".join(parts) + f"로 {verdict}")
        return out

    # existence(부재)는 XBRL 수치경로가 답할 수 없음 → rag narrative로 폴백.
    if p["intent"] == "existence":
        out.update(status="route_narrative", text="")
        return out

    # --- 비율 (choi 원문조회 + SHLEE 재계산 통합) -------------------------------
    if p["intent"] == "ratio":
        if not (corp and year):
            out.update(status="unparsed", text="질문에서 기업/연도를 특정하지 못했습니다.")
            return out
        ratio_ko = p["ratio_ko"]
        rm, rp = p["ratio_metric"], p["ratio_pair"]
        # ① 원문 보고값(MD&A 추출) 우선 — 공시 이용자가 실제로 보는 값.
        reported = []
        if rm:
            scopes = [scope] if scope else ["consolidated", "separate"]
            reported = [(s, store.lookup(cc, rm, s, year)) for s in scopes]
            reported = [(s, f) for s, f in reported if f]
        if reported:
            parts, numbers, sources, cross = [], [], [], []
            for s, f in reported:
                parts.append(f"{SCOPE_KO[s]} {f['value_raw']}")
                numbers.append(_digit(f["value_raw"]))
                sources.append({"scope": s, "fact_id": f["fact_id"], "rcept_no": f["rcept_no"],
                                "source_type": f.get("source_type"), "path": "reported"})
                # ② 같은 조건으로 XBRL 재계산 — 두 독립 경로 대조값을 항상 함께 싣는다.
                cval, nf, df = _ratio_computed(store, cc, s, year, rp)
                if cval is not None:
                    try:
                        rep_val = float(Decimal(f["value_decimal"]))
                    except Exception:  # noqa: BLE001
                        rep_val = None
                    cross.append({"scope": s, "reported": rep_val, "computed": cval,
                                  "diff_pp": None if rep_val is None else round(cval - rep_val, 2),
                                  "num_fact": nf["fact_id"] if nf else None,
                                  "den_fact": df["fact_id"] if df else None})
            note = " — 연결/별도 기준이 다르므로 구분이 필요합니다." if len(reported) == 2 else ""
            out["numbers"] = numbers
            out["sources"] = sources
            out["cross_check"] = cross
            out["text"] = f"{corp}의 {year}년 {ratio_ko}은 " + " / ".join(parts) + note
            return out
        # ③ 원문값이 없으면 XBRL 재계산으로 답한다(자기자본비율은 이 경로만 존재).
        eff_scope = scope or "consolidated"     # 명시 scope 우선 — EDGE-10(scope 무시 silent-wrong) 방지
        cval, nf, df = _ratio_computed(store, cc, eff_scope, year, rp)
        if cval is None:
            status = "calc_error" if (rp and nf and df) else "no_fact"
            out.update(status=status, text="비율 계산에 필요한 fact를 찾지 못했습니다.")
            return out
        num_key, den_key = rp
        out["numbers"] = [f"{cval}%"]
        out["sources"] = [{"fact_id": nf["fact_id"], "path": "computed"},
                          {"fact_id": df["fact_id"], "path": "computed"}]
        out["text"] = (f"{corp}의 {year}년 {SCOPE_KO[eff_scope]} 기준 {ratio_ko}은 {cval}%입니다 "
                       f"({METRIC_KO[num_key]} {nf['value_raw']} / {METRIC_KO[den_key]} {df['value_raw']}, "
                       f"{nf['unit_kr']}, XBRL 재계산).")
        return out

    if not (corp and metric and year):
        out.update(status="unparsed", text="질문에서 기업/지표/연도를 특정하지 못했습니다.")
        return out

    # --- 증감률 (choi 라우팅 + SHLEE sign_flip/가드) ---------------------------
    if p["intent"] == "fact_compute":
        sc = scope or "consolidated"      # compute도 scope 반영(별도 증가율 등)
        cur = store.lookup(cc, metric, sc, year)
        prev = store.lookup_in_doc(cur["doc_id"], metric, sc, year - 1) if cur else None
        if not (cur and prev):
            out.update(status="no_fact", text="계산에 필요한 fact를 찾지 못했습니다.")
            return out
        try:
            r = FA.compute("growth", [cur, prev])
        except (ZeroDivisionError, TypeError) as e:  # F-TEST-004/F-FUNC-002 가드
            out.update(status="calc_error", text=f"계산 불가(분모 0 또는 fact 누락): {e}")
            return out
        pct = round(float(r["value"]), 1)
        cur_v, prev_v = Decimal(cur["value_decimal"]), Decimal(prev["value_decimal"])
        sign_flip = cur_v != 0 and prev_v != 0 and (cur_v < 0) != (prev_v < 0)  # D-004 반영
        out["numbers"] = [f"{pct}%"]
        out["sources"] = [{"fact_id": fid} for fid in r["inputs"]] + \
                         [{"rcept_no": cur["rcept_no"]}]
        if sign_flip:
            direction = "흑자→적자 전환" if prev_v > 0 else "적자→흑자 전환"
            out["text"] = (f"{corp}의 {year}년 {SCOPE_KO[sc]} {METRIC_KO[metric]}은 전년({year-1}년) "
                           f"{prev['value_raw']}에서 {cur['value_raw']}로 {direction}되었습니다 "
                           f"(growth%로 단순 표시하면 {pct}%이지만 부호가 바뀌어 증감률만으로는 오해 소지가 있습니다).")
        else:
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
# 비-XBRL 구조화(doc-type) 조회 — Phase 2b (choi, 무변경)
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


def _load_filings():
    """필링 메타(rcept_no -> rcept_dt/report_nm). answer_struct가 회차를 특정할 때 쓴다.

    배포 패키지에는 원문 코퍼스가 없으므로 out/filings.jsonl(동봉, 4천여 줄)을 먼저 본다.
    개발 환경처럼 data/corpus가 함께 있으면 manifest.jsonl로 폴백한다 — 둘의 필드는 동일.
    """
    if os.path.exists(FILINGS_PATH):
        return [json.loads(l) for l in _open(FILINGS_PATH)]
    return load.load_manifest()


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
        for line in _open(path):
            d = json.loads(line)
            if (d.get("doc_group"), d.get("field_key")) in _STRUCT_KEYS:
                recs.append(d)
        return cls(recs, _load_filings())


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
# 골드셋 팩트경로 채점 (choi, 무변경)
# ---------------------------------------------------------------------------
def _tok_in(tok, text):
    return tok in text


def grade(gold_dir=GOLD_DIR, store=None):
    store = store or FactStore.load()
    report = {"slices": {}, "total": {"n": 0, "pass": 0, "strict_pass": 0, "unparsed": 0, "no_fact": 0}}
    lines = ["# 층 B 골드셋 — 팩트경로 채점 (numqa 병합본, LLM 없음)\n"]
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
