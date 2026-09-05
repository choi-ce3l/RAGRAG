"""기업 건강도 진단 — 현금흐름 3대 활동 부호 패턴 + ROE·ROA.

## 왜 필요한가

"현금흐름이 어때?"류 질문에 답할 경로가 없었다. PER·PBR처럼 흔히 쓰는 "기업 평가
지표"는 주가(시장 데이터)가 있어야 하는데, 이 프로젝트는 전자공시(재무제표) 기반이라
주가가 없다 — PER은 원천적으로 못 만든다.

대신 **재무제표만으로 계산되는** 두 가지를 쓴다.

  현금흐름 부호 패턴 — 영업·투자·재무 3대 활동의 (+/-) 조합으로 "우량기업형"·
  "성장기업형" 같은 8가지 표준 유형을 판정한다. 회계학에서 이미 정립된 분류라
  지어낸 규칙이 아니다.

  ROE·ROA — 당기순이익 ÷ 자본총계(자산총계). 재무제표만으로 계산되고, corpus
  실측 coverage가 넓다(자본총계·자산총계 70/70개사).

## LLM을 쓰지 않는다

패턴 판정은 부호 3개를 보는 규칙표 조회이고, ROE·ROA는 나눗셈이다. 이 프로젝트가
숫자를 내는 자리에 LLM을 넣지 않는 원칙을 그대로 지킨다.

## 뺀 것

이자보상배율(영업이익÷이자비용)은 후보였지만, corpus에 이자비용이 18/70개사에만
있어 대부분 기업에서 계산 불가로 나올 것 같아 뺐다 — 있는 척하지 않는다.
"""

import re
from decimal import Decimal, InvalidOperation

CF_CONCEPTS = ["영업활동현금흐름", "투자활동현금흐름", "재무활동현금흐름"]

# (영업 순유입?, 투자 순유입?, 재무 순유입?) → (유형명, 해설).
# 투자활동이 "순유입"이라는 건 신규 투자보다 자산 매각(회수)이 컸다는 뜻이라,
# 일반적인 성장기업은 투자활동이 순유출(False)이다 — 직관과 반대라 헷갈리기 쉽다.
PATTERNS = {
    (True, False, False): ("우량기업형",
        "영업활동으로 벌어들인 현금을 투자와 부채상환·배당에 쓰는 전형적인 성숙 우량기업 패턴입니다."),
    (True, False, True): ("성장기업형",
        "영업현금을 창출하면서 투자를 더 늘리기 위해 외부 자금도 추가로 조달하는 성장기 패턴입니다."),
    (True, True, False): ("구조조정형",
        "영업은 안정적이나, 보유 자산을 매각해 확보한 현금으로 부채를 상환하는 패턴입니다."),
    (True, True, True): ("보수적 현금축적형",
        "영업·투자·자금조달 모두에서 현금이 들어오는 드문 패턴으로, 현금을 폭넓게 쌓아두는 시기로 흔히 해석됩니다."),
    (False, False, True): ("신생·성장투자형",
        "영업적자를 외부 자금조달로 메우며 투자를 계속 늘리는, 초기 성장기·확장 투자기에 흔한 패턴입니다."),
    (False, True, True): ("회생시도형",
        "영업적자 상태에서 자산 매각과 외부 자금조달을 동시에 진행하며 버티는 패턴입니다."),
    (False, True, False): ("쇠퇴·축소형",
        "영업적자를 자산 매각으로 메우고 부채까지 상환하며 사업을 축소하는 패턴입니다."),
    (False, False, False): ("위기형",
        "영업·투자·자금조달 전방위에서 현금이 빠져나가는, 유동성 위기 신호로 흔히 해석되는 패턴입니다."),
}

GENERAL = re.compile(r"현금\s*흐름|현금흐름|ROE|ROA|자기자본이익률|총자산이익률")


def wanted(question, p):
    """현금흐름·건강도 진단을 원하는 질문인가.

    두 가지를 실측으로 걸러낸다(둘 다 회귀로 확인됨).

    ① concept이 이미 "...현금흐름"으로 끝나는 구체적 개념(영업활동현금흐름 등)으로
    잡혔으면, "반기 누적 영업활동현금흐름은 얼마인가?"처럼 **하나의 수치**를 정확히
    묻는 질문이다 — 기존 fact_numeric 경로(분기·반기 기간 지정도 처리한다)가 맞고,
    여기서 패턴 진단으로 가로채면 그 기간 지정을 무시하고 "해당 연도 없음"으로
    무너진다(GOLD-W1-HS-02).

    ② "재무건전성"·"건강도"는 GENERAL에서 뺐다 — 이 낱말이 질문에 있다고 사용자가
    진단을 원하는 게 아니라, "'재무건전성 등 기타 참고사항' 표에서 …"처럼 **공시
    섹션 제목**을 그대로 인용한 경우가 실제로 있었다(GOLD-W1-SHG-08, 계열사별
    지표 하나를 콕 집어 묻는 질문이었는데 진단으로 새서 무너짐). ROE·ROA·현금흐름은
    이런 섹션 제목으로 잘 안 쓰여서 남겨둔다.
    """
    concept = p.get("concept") or ""
    if concept.endswith("현금흐름"):
        return False
    return bool(GENERAL.search(question))


def _num(f):
    """fact → Decimal(원). value_decimal에 부호가 이미 들어 있다(pipeline.to_won과 동일 규칙)."""
    if not f:
        return None
    try:
        return Decimal(f["value_decimal"]) * Decimal(f.get("scale") or 1)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _forms(ci, concept):
    """이 개념의 동의어 표기 전부 — 기업마다 같은 뜻을 다른 말로 쓴다.

    "당기순이익"으로만 조회하면, 실제로는 "당기순이익(손실)"이라 적은 기업이
    전부 빠진다(SK하이닉스가 실측으로 그랬다). available_years()·_run_derived와
    같은 이유로 group_members()를 거친다.
    """
    return ci.group_members(concept)


def _years_any(labels, ci, corp_code, concept, scope):
    out = set()
    for form in _forms(ci, concept):
        out |= labels.years(corp_code, form, scope)
    return out


def _lookup_any(labels, ci, corp_code, concept, scope, year):
    for form in _forms(ci, concept):
        f = labels.lookup(corp_code, form, scope, year)
        if f:
            return f
    return None


def _latest_common_year(labels, ci, corp_code, concepts_, scope):
    """이 개념들을 전부 가진 가장 최근 연도. 하나라도 없으면 그 해는 후보에서 뺀다."""
    years_per = []
    for c in concepts_:
        ys = _years_any(labels, ci, corp_code, c, scope)
        if not ys:
            return None
        years_per.append(ys)
    common = set.intersection(*years_per)
    return max(common) if common else None


def diagnose(labels, corp, corp_code, scope="consolidated"):
    """(dict | None). 현금흐름 3대 활동이 전부 있는 해가 없으면 None."""
    from . import concepts as _concepts
    ci = _concepts.get(labels.facts)

    cf_year = _latest_common_year(labels, ci, corp_code, CF_CONCEPTS, scope)
    out = {"corp": corp, "cf_year": cf_year, "pattern": None, "cf": {},
           "roe": None, "roa": None, "roe_year": None}

    if cf_year:
        vals = {}
        for c in CF_CONCEPTS:
            f = _lookup_any(labels, ci, corp_code, c, scope, cf_year)
            v = _num(f)
            if v is None:
                cf_year = None
                break
            vals[c] = v
        if cf_year:
            out["cf_year"] = cf_year
            out["cf"] = {c: vals[c] for c in CF_CONCEPTS}
            sign = tuple(vals[c] >= 0 for c in CF_CONCEPTS)
            out["pattern"] = PATTERNS.get(sign)

    roe_year = _latest_common_year(labels, ci, corp_code, ["당기순이익", "자본총계", "자산총계"], scope)
    if roe_year:
        ni = _num(_lookup_any(labels, ci, corp_code, "당기순이익", scope, roe_year))
        eq = _num(_lookup_any(labels, ci, corp_code, "자본총계", scope, roe_year))
        asset = _num(_lookup_any(labels, ci, corp_code, "자산총계", scope, roe_year))
        if ni is not None and eq not in (None, 0):
            out["roe"] = round(float(ni / eq * 100), 1)
        if ni is not None and asset not in (None, 0):
            out["roa"] = round(float(ni / asset * 100), 1)
        out["roe_year"] = roe_year

    if out["cf_year"] is None and out["roe"] is None:
        return None
    return out


def _fmt(v):
    return f"{v:,.0f}원"


def render(d):
    parts = []
    if d.get("pattern"):
        name, note = d["pattern"]
        cf = d["cf"]
        signs = " / ".join(f"{c.replace('활동현금흐름','')} {_fmt(cf[c])}" for c in CF_CONCEPTS)
        parts.append(f"{d['corp']}의 {d['cf_year']}년 현금흐름은 '{name}' 패턴입니다 — {note} "
                     f"({signs})")
    elif d.get("cf_year") is None:
        parts.append(f"{d['corp']}은 3대 현금흐름(영업·투자·재무)이 모두 갖춰진 연도를 찾지 못했습니다")

    if d.get("roe") is not None or d.get("roa") is not None:
        bits = []
        if d.get("roe") is not None:
            bits.append(f"ROE(자기자본이익률) {d['roe']}%")
        if d.get("roa") is not None:
            bits.append(f"ROA(총자산이익률) {d['roa']}%")
        parts.append(f"{d['roe_year']}년 기준 " + " · ".join(bits))

    return ". ".join(parts) + "." if parts else ""
