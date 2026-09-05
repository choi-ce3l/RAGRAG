"""기간 표현 파서 — 연도 하나를 넘어서.

지금까지 온톨로지는 "2024년" 같은 절대 연도만 읽었다. 그런데 사람은 이렇게 묻는다.

    "2021~2023 추이와 CAGR"        → 범위
    "최근 3개년 매출액"             → 상대 범위
    "가장 최근 사업보고서 기준"      → 상대 시점
    "3분기 영업이익"                → 분기 (데이터가 아예 없음)

## "가장 최근"을 전역 상수로 두면 안 된다
corpus 전체의 최신은 2025년이지만, **(기업·개념·기준) 조합의 23.8%는 최신이 2025가 아니다.**
삼성전자의 `영업수익`은 2023년이 마지막이다. 그래서 "가장 최근"은 상수가 아니라
**그 조회 조건에서 실제로 값이 있는 최신 연도**로 푼다. 그리고 푼 결과를 답변에 밝힌다 —
사용자가 어느 해를 받았는지 모르면 틀려도 알아챌 수 없다.

## 분기·반기 — 이제 푼다
예전에는 사업보고서만 파싱해서 분기 수치가 아예 없었다. 파서를 분기·반기보고서까지
넓히면서 3개월·반기누적·3분기누적이 fact로 들어왔다.

다만 period_label만으로는 1·2·3분기가 전부 '3개월'이라 구분되지 않는다. 판별자는
종료월이다 — 3월이면 1분기, 6월이면 2분기, 9월이면 3분기.

4분기와 하반기는 공시되지 않는다. 뺄셈으로 만든다.

    4분기 = 연간 − 3분기누적
    하반기 = 연간 − 반기누적
"""

import re

# 범위 — 2021~2023 · 2021년부터 2023년까지
_RANGE = re.compile(
    r"(\d{4})\s*(?:년|사업연도|회계연도)?\s*(?:~|-|–|부터|에서|∼)\s*(\d{4})\s*(?:년|사업연도|회계연도)?")
# 최근 N개년
_RECENT_N = re.compile(r"(?:최근|지난|직전)\s*(\d)\s*(?:개년|년|개 연도|년간|년치)")
_N_YEARS = re.compile(r"(\d)\s*(?:개년|년간|년치)")
# 상대 시점
_LATEST = re.compile(r"가장\s*최근|최신|최근\s*사업보고서|가장\s*마지막|직전\s*사업보고서")
# 추이·성장률 — 범위가 명시 안 돼도 시리즈를 원한다는 신호.
#
# 예전에는 "추이|추세|CAGR"만 봤다. 그래서 사람이 실제로 쓰는 말을 놓쳤다.
#   "최근 영업이익률 흐름은 어때?"        "매출 대비 영업이익이 어떻게 변했어?"
#   "매출이 커지는 만큼 영업이익도 좋아지고 있어?"
# 전부 여러 해를 봐야 답할 수 있는 질문인데 연도가 없다고 되물었다.
_TREND = re.compile(
    # '흐름'은 "현금흐름"·"자금흐름"의 일부이기도 하다. 계정과목 안의 글자를
    # 추이 신호로 읽으면 "재무활동현금흐름 순위"가 시리즈로 새 나간다.
    r"추이|추세|(?<!현금)(?<!자금)(?<!현금성)흐름|CAGR|연복리성장률|연평균\s*성장률"
    r"|변화(?:를|는)?\s*(?:보|알|정리)|어떻게\s*(?:변|되고|바뀌)"
    r"|좋아지|나빠지|개선되|악화되|늘고\s*있|줄고\s*있|커지|작아지"
    r"|증가율|성장률|증감률"
    r"|요즘|최근\s*(?:실적|매출|영업이익|흐름|추세)")

# "2023, 2024, 2025" 처럼 쉼표로 나열된 연도. 범위(~)와 달리 명시 나열이다.
# "FY2023, FY2024, FY2025"처럼 회계연도 접두어가 붙기도 한다 — 접두어를 안 먹으면
# 첫 해만 매치되다 콤마 뒤에서 끊겨 통째로 실패하고, kind가 'range' 대신 'latest' 등
# 엉뚱한 값으로 샌다(KB-05류 실측 확인).
_YEAR_LIST = re.compile(
    r"(?:FY\s*)?(\d{4})\s*년?\s*[,·]\s*(?:FY\s*)?(\d{4})\s*년?\s*[,·]\s*(?:FY\s*)?(\d{4})",
    re.IGNORECASE)
# "제17기~제19기" — 기수 범위. 단일 기수보다 먼저 봐야 한다.
_TERM_RANGE = re.compile(
    r"제?\s*(\d{1,3})\s*기\s*(?:~|-|–|∼|부터|에서)\s*제?\s*(\d{1,3})\s*기")
# "제17기(2023년)ㆍ제18기(2024년)ㆍ제19기(2025년)" — 기수-연도 쌍을 나란히 나열.
# _TERM_RANGE와 달리 사이 구두점이 ㆍ·,·및 등으로 제각각이라 통으로 안 묶이고,
# "제N기(YYYY년)" 낱개 쌍을 각각 찾아 모은다(회귀 실측: GOLD-W1-CJ-07 — 단일
# _TERM만 걸려 첫 기수 하나로 새 나갔다).
_TERM_YEAR_PAIR = re.compile(r"제\s*\d{1,3}\s*기\s*\(\s*(\d{4})\s*년\s*\)")
# 분기·반기 — 수치는 없지만 문서는 있다
_QUARTER = re.compile(r"([1-4])\s*분기|([1-4])Q|(\d)/4\s*분기")
_HALF = re.compile(r"상반기|하반기|반기")
# 기수 — "제57기" · "57기"
_TERM = re.compile(r"제?\s*(\d{1,3})\s*기(?!간|준|업|타|재|계)")

# "3분기까지" · "누적" — 단독 3개월이 아니라 누적을 원한다는 신호
_CUMUL = re.compile(r"누적|까지의|까지\s*의|말\s*기준\s*누적")

# "최근 8개 분기" — 분기 하나가 아니라 최근 N개 분기를 연속으로 원하는 질문.
# 단일 분기(_QUARTER)와 달리 어느 분기들인지가 코퍼스 보유 현황에 달려 있어
# 여기서 연도·분기를 못 정한다 — kind만 표시하고 pipeline.py가 filings 메타로
# 채운다(qa/pipeline.py의 _recent_quarters 참고).
_RECENT_Q = re.compile(r"(?:최근|지난|직전)\s*(\d{1,2})\s*(?:개)?\s*분기")

MAX_SPAN = 8

# 분기 → (기간표기, 종료월). 4분기는 공시되지 않아 뺄셈으로 만든다.
_Q_CUM = {1: ("3개월", 3), 2: ("반기누적", 6), 3: ("3분기누적", 9)}
_Q_ONE = {1: ("3개월", 3), 2: ("3개월", 6), 3: ("3개월", 9)}


def _quarter_spec(q, cumulative):
    """N분기 → 조회 스펙. 4분기는 연간에서 3분기누적을 뺀다."""
    if q == 4:
        return {"kind": "quarter", "n": 4, "report": "사업보고서", "label": "연간",
                "derive": ("연간", "3분기누적"), "ko": "4분기"}
    label, end = (_Q_CUM if cumulative else _Q_ONE)[q]
    return {"kind": "quarter", "n": q, "label": label, "end_month": end,
            "ko": f"{q}분기" + (" 누적" if cumulative else ""),
            "report": "반기보고서" if q == 2 else "분기보고서"}


def quarter_spec(q, cumulative=False):
    """_quarter_spec의 공개 래퍼 — pipeline.py가 "최근 N개 분기" 매트릭스를
    채울 때 분기 하나하나의 조회 스펙이 필요해서 바깥에서도 쓴다."""
    return _quarter_spec(q, cumulative)


# "A와 B를 이용해 C를 구하면?" — 묻는 것은 C다. 앞부분은 재료를 설명할 뿐이다.
# 이걸 안 보면 먼저 나온 기간(A)을 답으로 잡는다. "9개월 누적과 3개월 단독을
# 이용해 상반기 누적을 구하면?"에 3분기 누적을 답했다.
_TAIL = re.compile(r"(?:을|를)\s*(?:이용|사용|바탕으)(?:해|하여|로)\s*(?P<tail>.+)$")
# 단독 표기 — 누적이라는 말이 함께 나와도 이쪽이 우선이다
_STANDALONE = re.compile(r"단독|3\s*개월간|당분기|당\s*3개월")


def parse(question):
    """기간 의도. 절대 연도 인식은 기존 파서가 맡고, 여기서는 그 위를 다룬다."""
    mt = _TAIL.search(question)
    if mt and len(mt.group("tail")) >= 8:
        tail = mt.group("tail")
        if _QUARTER.search(tail) or _HALF.search(tail):
            question = tail            # 실제로 묻는 부분만 본다
    out = {"kind": None, "years": [], "n": None, "trend": bool(_TREND.search(question)),
           "sub_annual": None, "term": None, "terms": [], "unsupported": None}

    mrq = _RECENT_Q.search(question)
    if mrq:
        n = int(mrq.group(1))
        if 2 <= n <= 16:
            out.update(kind="quarter_recent_n", n=n)
            return out

    # 분기·반기 — XBRL 수치는 사업보고서에만 있다. 다만 문서는 corpus에 있으므로
    # "없다"로 끝내지 않고 어느 보고서를 봐야 하는지로 넘긴다.
    # "3개월간(단독)"과 "누적"이 한 문장에 함께 나오면 단독이 우선이다.
    # 질문은 대개 둘을 나란히 소개한 뒤 하나를 묻는데, 누적이라는 말만 보고
    # 누적을 골라 왔다.
    cum = bool(_CUMUL.search(question)) and not _STANDALONE.search(question)
    mq = _QUARTER.search(question)
    if mq:
        q = int(next(g for g in mq.groups() if g))
        out["sub_annual"] = _quarter_spec(q, cum)
        return out
    if _HALF.search(question):
        half = "하반기" if "하반기" in question else "상반기"
        out["sub_annual"] = (
            {"kind": "half", "n": 2, "report": "사업보고서", "label": "연간",
             "derive": ("연간", "반기누적"), "ko": "하반기"} if half == "하반기" else
            {"kind": "half", "n": 1, "report": "반기보고서", "label": "반기누적",
             "end_month": 6, "ko": "상반기"})
        return out

    mtr = _TERM_RANGE.search(question)
    if mtr:
        a, b = int(mtr.group(1)), int(mtr.group(2))
        if a > b:
            a, b = b, a
        if 1 <= b - a + 1 <= MAX_SPAN:
            out.update(kind="term_range", terms=list(range(a, b + 1)))
            return out

    mty = _TERM_YEAR_PAIR.findall(question)
    if len(mty) >= 2:
        ys = sorted({int(y) for y in mty})
        if len(ys) >= 2 and ys[-1] - ys[0] + 1 <= MAX_SPAN:
            out.update(kind="range", years=ys)
            return out

    mt = _TERM.search(question)
    if mt:
        out["term"] = int(mt.group(1))
        out["kind"] = "term"
        return out

    ml = _YEAR_LIST.search(question)
    if ml:
        ys = sorted({int(g) for g in ml.groups()})
        if len(ys) >= 2 and ys[-1] - ys[0] + 1 <= MAX_SPAN:
            out.update(kind="range", years=ys)
            return out

    m = _RANGE.search(question)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            a, b = b, a
        if 1 <= b - a + 1 <= MAX_SPAN:
            out.update(kind="range", years=list(range(a, b + 1)))
            return out

    m = _RECENT_N.search(question) or (_N_YEARS.search(question) if out["trend"] else None)
    if m:
        n = int(m.group(1))
        if 2 <= n <= MAX_SPAN:
            out.update(kind="recent_n", n=n)
            return out

    if _LATEST.search(question):
        out.update(kind="latest")
        return out

    if out["trend"]:
        # "추이를 보여줘"인데 기간이 없으면 보유한 연도 전부
        out.update(kind="all")
    return out


def resolve(spec, available):
    """기간 의도 + 실제 보유 연도 → 조회할 연도 목록.

    available은 그 (기업·개념·기준) 조합이 실제로 가진 연도다. 전역 최신이 아니라
    이것으로 풀어야 "삼성전자 영업수익 가장 최근" 같은 질문이 맞는다.
    """
    yrs = sorted(available)
    if not yrs:
        return []
    kind = spec.get("kind")
    if kind == "term":
        y = spec.get("resolved_year")
        return [y] if y in available else []
    if kind == "range":
        return [y for y in spec["years"] if y in available]
    if kind == "recent_n":
        return yrs[-spec["n"]:]
    if kind == "latest":
        return [yrs[-1]]
    if kind == "all":
        return yrs
    return []


def cagr(first, last, periods):
    """연평균 성장률(%). 부호가 바뀌거나 시작값이 0이면 계산하지 않는다."""
    from decimal import Decimal, InvalidOperation
    try:
        a, b = Decimal(str(first)), Decimal(str(last))
    except (InvalidOperation, ValueError):
        return None
    if periods < 1 or a <= 0 or b <= 0:
        return None
    return (float(b / a) ** (1.0 / periods) - 1.0) * 100
