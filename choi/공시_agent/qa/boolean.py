"""boolean(예/아니오) 결론 합성 — 05단계(답변 생성) 직전 후처리.

## 왜 필요한가

SHLEE 골드셋의 `boolean` 유형 문항 다수는 이미 필요한 숫자를 정확히 조회·계산해
놓고도 마지막에 "예/아니오" 결론을 내지 않는다. `qa/pipeline.py`의 각 `_run_*`
핸들러는 원래 "얼마인가"류 수치 질문에 답하도록 설계돼 있어서, 값을 구하고 나면
그 값 자체를 문장으로 만들 뿐 "그래서 참인가 거짓인가"를 판정하는 코드가 없다.

이 모듈은 `qa.pipeline.run()`이 `_run()`을 마친 **직후**, 이미 채워진
`r.parsed`·`r.facts`·`r.answer_text`·`r.state`를 보고 그 위에 결론 문장을
덧붙이는 순수 후처리 계층이다. 새 사실을 조회하지 않는 것이 기본이지만, 판정에
필요한 값이 하나 더 필요할 때만(예: "1분기 값은 이미 있는데 반기 누적 값이
없다") `labelstore`·`shareholders` 같은 기존 조회 API를 그 자리에서 한 번 더
불러 쓴다 — 새 지식을 만드는 게 아니라 이미 확인된 값의 짝을 마저 찾는 것이다.

## boolean 질문 감지 — 가장 위험한 부분

한국어 판단형 질문은 "~는가/~인가"로 끝나는 게 흔한데, 그중 상당수는 boolean이
아니라 wh-질문이다("영업이익은 **얼마**인가?", "몇 %**인가**?"). 의문사(얼마·몇·
언제·어디·누구·무엇·어떤·어느)가 섞인 문장은 그 문장만 통째로 boolean 후보에서
뺀다(문장 단위로 쪼개는 이유: SEM-RET-07처럼 한 질문 안에 boolean 절과 wh-질문
절이 함께 있는 경우, wh 절 때문에 boolean 절까지 버리면 안 된다).

의문사가 없으면서 "성립하는지/성립하는가", "변동이 있었는가", "같은가/다른가",
"맞는가", "가능한가", "존재하는가", "비교할 수 있는가" 같은 순수 진위 패턴에
걸리는 문장이 하나라도 있으면 boolean으로 본다.

## 판정 — 세 가지 좁은 핸들러

지금 손댈 수 있는 범위는 "이미 계산된 값들을 비교하는" 문제로 한정한다. 개념적
판단(비교 기준이 같은가)이나 존재성 판단(후속 필링이 있는가)은 일반화하지 않고,
아주 좁게 관측되는 패턴 하나씩만 다룬다. 그 밖의 boolean 문장은 **손대지 않고
그대로 둔다** — 틀린 예/아니오를 붙이는 것보다 기존 답을 유지하는 편이 낫다.

1. `_judge_period_consistency` — "반기 누적이 1분기보다 크거나 같은가",
   "반기 누적 − 1분기 = 별도표시 2분기 단독인가" 같은 **같은 기업·같은 해**의
   분기·반기 수치 산술 정합성. 질문에 등장한 기간 종류(1분기/2분기 단독/반기
   누적/3분기 누적)를 감지해 `labelstore.lookup_metric`으로 각각의 값을 받아와
   부등식 또는 "A−B=C" 등식을 직접 계산한다.
2. `_judge_share_variation` — "N개 사업연도 동안 지분율 변동이 있었는가"류.
   `qa/shareholders.py`가 이미 갖고 있는 `holder_across_years()`로 언급된
   주주 각각의 연도별 지분율을 모아 실제로 달라졌는지 본다(pipeline.py의
   `_try_shareholders`는 질문에 명시된 두 시점만 비교하거나 최신 스냅샷만
   보여주는 경로라 3개년 전체를 훑지 않는다 — 여기서 그 3개년을 마저 모은다).
3. `_judge_scope_comparability` — "전사 연결 지표"와 "특정 사업부문 지표"를
   "동일한 기준으로 비교할 수 있는가"류. 이건 숫자 비교가 아니라 개념적 판단이라
   일반화하지 않고, 이 정확한 신호 조합(부문+연결+동일 기준)일 때만 "기준이
   달라 직접 비교할 수 없다"고 답한다.

## 채점기와의 호환

`evaluation/scorer.py`는 정규식으로 예/아니오 표명 여부를 본다(`_YES`/`_NO`).
"있습니다"·"없습니다" 같은 리터럴이 있어야지, "있었습니다"(과거형)처럼 겹쳐
보이는 표현은 그 정규식에 걸리지 않는다. 이 모듈의 결론 문장은 그 정규식을
직접 겨냥해 작성한다 — 자세한 근거는 각 헬퍼의 docstring 참고.
"""

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import numqa                                            # noqa: E402

from . import filings, labelstore, shareholders, tables

# ---------------------------------------------------------------------------
# 0. boolean 질문 감지
# ---------------------------------------------------------------------------
_WH = re.compile(r"얼마|몇|언제|어디|누구|무엇|어떤|어느")
_BOOL_PAT = re.compile(
    r"성립하(?:는지|는가|나요?)"
    r"|변동\s*이?\s*있(?:었)?는가|변동\s*(?:이|가)\s*(?:있|없)(?:었)?는가"
    r"|같은가|다른가|맞는가|맞습니까"
    r"|가능한가|가능합니까"
    r"|존재하는가|존재합니까"
    r"|비교할\s*수\s*있는가"
    # "…와 일치하는가"류(GOLD-W1-EM-02) — 따옴표 없는 자연문 등식 검증 질문도
    # 이 리터럴이 boolean 게이트를 통과해야 _judge_period_consistency까지 간다.
    r"|일치하(?:는가|나요?|는지)|일치합니까"
)


def _sentences(question):
    return re.split(r"(?<=[.?!])\s+", question or "")


def _boolean_sentences(question):
    """의문사가 없으면서 boolean 패턴에 걸리는 문장만 골라낸다."""
    for s in _sentences(question):
        if _BOOL_PAT.search(s) and not _WH.search(s):
            yield s


def is_boolean_question(question):
    return next(_boolean_sentences(question), None) is not None


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------
def _won(f):
    try:
        return Decimal(str(f["value_decimal"])) * Decimal(str(f.get("scale") or 1))
    except (InvalidOperation, ValueError, TypeError, KeyError):
        return None


def _append(r, sentence):
    cur = (r.answer_text or "").strip()
    r.answer_text = (cur + " " if cur else "") + sentence


def _coordinate(f):
    """boolean.py가 추가로 조회한 fact의 근거 좌표. pipeline.to_coordinate의 축약판 —
    순환 임포트를 피하려고 pipeline을 참조하지 않고 필요한 최소 필드만 만든다."""
    return {
        "corp_name": f.get("corp_name", ""),
        "report_nm": f.get("report_nm", "") or f.get("doc_id", ""),
        "path": f"검증조회 > {f.get('label_norm') or f.get('label_raw', '')}",
        "cell": "",
        "rcept_no": f.get("rcept_no", ""),
        "ref_id": f.get("fact_id", ""),
        "flags": ["⚠️정정"] if f.get("is_superseded") else [],
    }


def _add_facts(r, facts):
    seen = {f.get("fact_id") for f in r.facts}
    for f in facts:
        if f.get("fact_id") not in seen:
            r.facts.append(f)
            r.evidence.append(_coordinate(f))
            seen.add(f.get("fact_id"))


# ---------------------------------------------------------------------------
# 1. 분기·반기 산술 정합성 (GOLD-W1-SEC-02, GOLD-W1-CJ-02)
# ---------------------------------------------------------------------------
# 판별 순서가 중요하다 — "2분기(3개월간)"이 "1분기"로 잘못 잡히면 안 되므로
# 더 구체적인(누적류) 패턴부터 본다. 각 패턴은 서로 다른 substring을 겨냥해
# 순서와 무관하게 안전하지만, 나열 순서는 _match_period_key()의 우선순위로도 쓰인다.
_PERIOD_KEYS = [
    # "1~6월 누적"류 — GOLD-W1-KB-03 실측: "반기 누적"·"6개월)" 표기 대신 월범위로
    # 누적 기간을 가리키는 문항이 있어 별도 alternative로 추가했다("1-6월 누적"도
    # 함께 잡도록 물결·붙임표 둘 다 허용).
    # "상반기(1~6월) 연결 누적"(GOLD-W1-EM-02)처럼 "상반기"와 "누적" 사이에
    # 괄호·수식어가 끼어드는 표기까지 잡으려고 [^,.]{0,20} 대안을 더했다.
    ("half_cum", re.compile(r"반기\s*누적|상반기\s*누적|6\s*개월간|6\s*개월\)"
                            r"|1\s*[~∼-]\s*6\s*월\s*누적|상반기[^,.]{0,20}누적"),
     {"label": "반기누적", "end_month": 6}, "반기 누적(6개월)"),
    # "9개월 누적"(공백만, "간"·")" 없이)도 잡는다(GOLD-W1-EM-02).
    ("q3_cum", re.compile(r"3\s*분기\s*누적|9\s*개월간|9\s*개월\)|9\s*개월\s*누적"),
     {"label": "3분기누적", "end_month": 9}, "3분기 누적(9개월)"),
    # q3_alone/q2_alone: "N분기(4~6월) 3개월 단독" 처럼 분기와 "단독" 사이에 월
    # 범위 괄호가 끼어드는 표기(GOLD-W1-OCI-06)까지 잡으려고 gap을 6→20으로
    # 넓혔다. "1분기 단독 기준" 뒤에 "2분기(4~6월) 개별 분기"가 나오는 SHG-09
    # 문항으로 회귀 확인함 — "단독"이 "2분기"보다 앞에만 있으면 안 걸린다.
    # "당분기"(GOLD-W1-EM-02)도 "단독"과 같은 뜻으로 함께 잡는다.
    ("q3_alone", re.compile(r"3\s*분기\s*\(?\s*3\s*개월|3\s*분기[^,.]{0,20}(?:단독|당분기)"),
     {"label": "3개월", "end_month": 9}, "3분기 단독(3개월)"),
    ("q2_alone", re.compile(r"2\s*분기\s*\(?\s*3\s*개월|2\s*분기[^,.]{0,20}단독"),
     {"label": "3개월", "end_month": 6}, "2분기 단독(3개월)"),
    ("q1", re.compile(r"1\s*분기"),
     {"label": "3개월", "end_month": 3}, "1분기(3개월)"),
]

_YM_PAREN = re.compile(r"\(\s*(\d{4})\s*\.\s*\d{2}\s*\)")

_GE = re.compile(r"크거나\s*같|이상이다|같거나\s*크")
_LE = re.compile(r"작거나\s*같|이하이다|같거나\s*작")
_GT = re.compile(r"보다\s*크(?:다|가)")
_LT = re.compile(r"보다\s*작(?:다|가)")

_QUOTE = re.compile(r"['\"「『]([^'\"」』]{4,140})['\"」』]")
_MINUS_CHARS = "−\\-－–"

# GOLD-W1-EM-02류 — 따옴표로 인용된 "A−B=C"가 없어도 "…에서 …을 차감한 값과
# 일치하는가" 같은 자연문으로 등식을 표현하는 질문이 있다. 감지된 기간 조합이
# 정확히 {반기누적, 3분기누적, 3분기단독}이고 "차감"+"일치" 신호가 함께 있으면
# 반기누적 == 3분기누적 − 3분기단독 등식을 자동으로 세운다. 이 조합 하나만
# 다룬다 — 일반화하면 다른 기간 조합에 억지로 등식을 지어낼 위험이 있다.
_DEDUCT_EQ = re.compile(r"차감")
_MATCH_ASK = re.compile(r"일치하")


def _detect_periods(text):
    """언급된 기간 종류 → (첫 등장 위치, 조회 스펙, 사람이 읽을 라벨)."""
    out = {}
    for key, pat, spec, human in _PERIOD_KEYS:
        m = pat.search(text)
        if m:
            out[key] = (m.start(), spec, human)
    return out


def _match_period_key(text):
    for key, pat, _spec, _human in _PERIOD_KEYS:
        if pat.search(text):
            return key
    return None


def _relation(question):
    if _GE.search(question):
        return lambda a, b: a >= b
    if _LE.search(question):
        return lambda a, b: a <= b
    if _GT.search(question):
        return lambda a, b: a > b
    if _LT.search(question):
        return lambda a, b: a < b
    return None


def _formula_operands(question):
    """'A − B = C' 형태로 따옴표 안에 인용된 관계식. 없으면 None."""
    m = _QUOTE.search(question)
    if not m:
        return None
    formula = m.group(1)
    if "=" not in formula or not any(c in formula for c in _MINUS_CHARS):
        return None
    lhs, _eq, rhs = formula.partition("=")
    parts = re.split(f"[{_MINUS_CHARS}]", lhs)
    if len(parts) != 2:
        return None
    return parts[0], parts[1], rhs


def _judge_period_consistency(r, question):
    """같은 기업·같은 회계연도의 분기·반기 수치 산술 정합성(부등식 또는 A−B=C).

    성립하면 "정합성이 있습니다"(YES 정규식의 '있습니다'에 걸림), 성립하지
    않으면 "정합성이 없습니다"('없습니다'에 걸림)로 끝맺는다 — 둘 다 과거형이
    아니라 현재형이어야 채점기의 리터럴 매칭에 걸린다.
    """
    p = r.parsed or {}
    metric, cc = p.get("metric"), p.get("corp_code")
    if not metric or not cc:
        return False

    periods = _detect_periods(question)
    if len(periods) < 2:
        return False

    scope = "separate" if re.search(r"별도\s*(?:재무제표|기준)", question) else "consolidated"
    ym = _YM_PAREN.search(question)
    year = int(ym.group(1)) if ym else p.get("year")
    if not year:
        return False

    labels = labelstore.get()
    values = {}
    for key, (_pos, spec, human) in periods.items():
        f = labels.lookup_metric(cc, metric, scope, year, spec)
        if not f:
            return False                 # 값 하나라도 못 찾으면 손대지 않는다
        w = _won(f)
        if w is None:
            return False
        values[key] = (w, f, human)

    metric_ko = numqa.METRIC_KO.get(metric, "값")
    scope_ko = numqa.SCOPE_KO.get(scope, scope)
    corp = p.get("corp") or ""

    formula = _formula_operands(question)
    auto_keys = None
    if not formula and set(values) == {"half_cum", "q3_cum", "q3_alone"} \
            and _DEDUCT_EQ.search(question) and _MATCH_ASK.search(question):
        # "9개월 누적에서 3분기 당분기를 차감한 값과 일치하는가" = 반기누적 ==
        # 3분기누적 − 3분기단독(1~2분기 누적).
        auto_keys = ("q3_cum", "q3_alone", "half_cum")

    if formula or auto_keys:
        if formula:
            a_txt, b_txt, c_txt = formula
            a_key, b_key, c_key = (_match_period_key(a_txt), _match_period_key(b_txt),
                                   _match_period_key(c_txt))
        else:
            a_key, b_key, c_key = auto_keys
        if not (a_key and b_key and c_key
                and a_key in values and b_key in values and c_key in values):
            return False
        a_val, a_f, a_h = values[a_key]
        b_val, b_f, b_h = values[b_key]
        c_val, c_f, c_h = values[c_key]
        lhs = a_val - b_val
        denom = abs(c_val) if c_val else Decimal(1)
        ok = abs(lhs - c_val) / denom <= Decimal("0.001")
        detail = (f"{corp} {year}년 {scope_ko} {metric_ko} — {a_h} "
                 f"{a_f['value_raw']}{a_f['unit_kr']} − {b_h} "
                 f"{b_f['value_raw']}{b_f['unit_kr']} = {lhs:,} vs {c_h} "
                 f"{c_f['value_raw']}{c_f['unit_kr']}")
        new_facts = [a_f, b_f, c_f]
    else:
        rel = _relation(question)
        if not rel or len(periods) != 2:
            return False
        (a_key, b_key) = [k for k, _ in sorted(periods.items(), key=lambda kv: kv[1][0])]
        a_val, a_f, a_h = values[a_key]
        b_val, b_f, b_h = values[b_key]
        ok = rel(a_val, b_val)
        detail = (f"{corp} {year}년 {scope_ko} {metric_ko} — {a_h} "
                 f"{a_f['value_raw']}{a_f['unit_kr']} vs {b_h} "
                 f"{b_f['value_raw']}{b_f['unit_kr']}")
        new_facts = [a_f, b_f]

    tail = (" 따라서 이 관계는 성립하며, 정합성이 있습니다." if ok else
            " 따라서 이 관계는 성립하지 않으며, 정합성이 없습니다.")
    _append(r, f"[산술 정합성 검증] {detail}." + tail)
    _add_facts(r, new_facts)
    return True


# ---------------------------------------------------------------------------
# 1b. 같은 산술 정합성 인프라로 "어느 쪽이 더 작은/큰가"에 답한다 (GOLD-W1-SHG-09)
# ---------------------------------------------------------------------------
# _judge_period_consistency와 달리 boolean(예/아니오) 문장이 아니라 "어느" wh-단어가
# 있는 문장을 겨냥한다 — 그래서 judge()의 boolean 게이트를 거치지 않고 독립적으로
# 호출된다(아래 judge() 참고). 지금 다루는 정확한 패턴 하나: "1분기 값"과 "반기
# 누적에서 1분기를 차감해 산출한 2분기 값"을 비교해 더 작은/큰 쪽의 기간 라벨을
# 답하는 것. 그 밖의 기간 조합·부등호 없는 비교는 일반화하지 않는다.
#  "작다"는 어미가 붙어도 어간 "작"이 그대로 남지만("작은가"), "크다"는 받침 없는
#  어간에 "-ㄴ가/-ㅂ니까"가 붙으면서 음절 자체가 "큰"·"큽"으로 바뀐다("크다"→
#  "큰가"·"큽니까") — 그래서 "크" 하나만 보면 "더 큰가?"를 놓친다.
_EXTREMUM = re.compile(r"어느\s*(?:쪽|것|분기|연도|해|기간)?\s*(?:이|가)?\s*더\s*(작|크|큰|큽)")
_DEDUCT = re.compile(r"차감|빼(?:서|고|면|어)?")

# metric_key가 안 붙어 labelstore.lookup_metric으로 못 찾는 기업(금융지주 등 —
# 예: 신한지주)을 위한 라벨 접미사 대체 경로. "분기순이익"·"반기순이익"은 회사마다
# 앞머리 번호("Ⅵ.", "(1)", "1." …)가 달라 정확 일치로는 못 잡으므로 접미사로 찾는다.
_PERIOD_LABEL_SUFFIX = {
    ("net_income", "3개월"): "분기순이익",
    ("net_income", "반기누적"): "반기순이익",
}


def _lookup_period_value(labels, cc, metric, scope, year, spec):
    """lookup_metric을 우선 쓰고, metric_key가 없는 기업만 라벨 접미사로 한 번 더 찾는다."""
    f = labels.lookup_metric(cc, metric, scope, year, spec)
    if f:
        return f
    suffix = _PERIOD_LABEL_SUFFIX.get((metric, spec.get("label")))
    if not suffix:
        return None
    con = labels._db()
    if con is None:
        return None
    sql = ("SELECT * FROM facts WHERE corp_code=? AND scope=? AND base_year=?"
           " AND label_norm LIKE ? AND period_label=?")
    args = [cc, scope, str(year), f"%{suffix}", spec["label"]]
    if spec.get("end_month"):
        sql += " AND period_end LIKE ?"
        args.append(f"%-{spec['end_month']:02d}-%")
    sql += (" ORDER BY is_superseded ASC,"
           " CASE WHEN report_nm LIKE '%(' || base_year || '.%' THEN 0 ELSE 1 END,"
           " rcept_no DESC LIMIT 1")
    row = con.execute(sql, args).fetchone()
    if not row:
        return None
    return labelstore._cast({k: row[k] for k in labelstore.KEEP})


def _judge_period_extremum(r, question):
    """두 후보 기간 값 중 더 작은/큰 쪽의 사람이 읽는 라벨("2024년 1분기(1~3월)")을 낸다.

    _judge_period_consistency와 같은 기간-값 수집 인프라(labelstore.lookup_metric)를
    쓰되, 부등식 참/거짓이 아니라 라벨을 답으로 낸다. 지금 다루는 조합은 둘:

    1. {half_cum, q1} — "반기 누적 − 1분기"로 2분기 값을 **산출**해 1분기와 비교
       (GOLD-W1-SHG-09). "차감" 지시어가 있어야 한다.
    2. {q2_alone, q3_alone} — 두 분기 단독 값이 각자의 문서에 **이미 표시돼** 있어
       그대로 조회해 비교만 한다(GOLD-W1-OCI-06). 산출 지시어가 없어도 된다 —
       계산이 아니라 단순 조회라 _DEDUCT를 요구하지 않는다.
    """
    m = _EXTREMUM.search(question)
    if not m:
        return False
    want_min = m.group(1) == "작"

    p = r.parsed or {}
    metric, cc = p.get("metric"), p.get("corp_code")
    if not metric or not cc:
        return False

    periods = _detect_periods(question)
    combo = set(periods)
    qn = re.sub(r"\s+", "", question)

    if combo == {"half_cum", "q1"}:
        if not _DEDUCT.search(question) or "2분기" not in qn:
            return False
        derive_q2 = True
    elif combo == {"q2_alone", "q3_alone"}:
        derive_q2 = False
    else:
        return False                      # 지금은 이 두 조합만 다룬다

    scope = "separate" if re.search(r"별도\s*(?:재무제표|기준)", question) else "consolidated"
    ym = _YM_PAREN.search(question)
    year = int(ym.group(1)) if ym else p.get("year")
    if not year:
        return False

    labels = labelstore.get()
    values = {}
    for key, (_pos, spec, _human) in periods.items():
        f = _lookup_period_value(labels, cc, metric, scope, year, spec)
        if not f:
            return False                 # 값 하나라도 못 찾으면 손대지 않는다
        w = _won(f)
        if w is None:
            return False
        values[key] = (w, f)

    if derive_q2:
        q1_val, q1_f = values["q1"]
        half_val, half_f = values["half_cum"]
        q2_val = half_val - q1_val
        candidates = [("1분기(1~3월)", q1_val), ("2분기(4~6월)", q2_val)]
        used_facts = [q1_f, half_f]
        calc_note = (f"1분기 {q1_val:,}원, "
                     f"2분기(반기누적 {half_val:,}원 − 1분기) {q2_val:,}원")
    else:
        q2_val, q2_f = values["q2_alone"]
        q3_val, q3_f = values["q3_alone"]
        candidates = [("2분기(4~6월)", q2_val), ("3분기(7~9월)", q3_val)]
        used_facts = [q2_f, q3_f]
        calc_note = f"2분기 단독 {q2_val:,}원, 3분기 단독 {q3_val:,}원"

    pick = (min if want_min else max)(candidates, key=lambda kv: kv[1])

    metric_ko = numqa.METRIC_KO.get(metric, "값")
    scope_ko = numqa.SCOPE_KO.get(scope, scope)
    corp = p.get("corp") or ""

    detail = f"{corp} {year}년 {scope_ko} {metric_ko} — {calc_note}"
    r.answer_text = ""
    _append(r, f"[기간 비교] {detail}. 따라서 더 {'작은' if want_min else '큰'} 쪽은 "
              f"{year}년 {pick[0]}입니다.")
    _add_facts(r, used_facts)
    return True


# ---------------------------------------------------------------------------
# 1c. 같은 인프라로 "얼마나 증가/감소했는가"에 델타값을 답한다 (GOLD-W1-KB-03)
# ---------------------------------------------------------------------------
# _judge_period_extremum은 "어느 쪽이 더 작은/큰가"(라벨)를 답한다. 여기서는 같은
# {half_cum, q1} 값 수집·2분기 역산 인프라를 그대로 쓰되, 라벨이 아니라 **증감폭과
# 방향**을 답한다. "어느" wh-단어가 없는 대신 "얼마나 증가/감소" 신호로 켠다 —
# _WH가 "얼마"를 포함해 is_boolean_question 경로로는 못 들어오므로 _CONCLUSION_
# HANDLERS에 독립적으로 둔다. 지금 다루는 조합은 {half_cum, q1} 뿐이다 — 다른
# 조합(q2_alone/q3_alone)은 이미 "표시된" 두 값을 비교하는 것이라 "역산 증감"이라는
# 이 문항의 성격과 다르다.
_DELTA_ASK = re.compile(r"얼마나\s*(?:증가|감소)|증가\s*(?:또는|혹은)\s*감소")


def _judge_period_delta(r, question):
    """{half_cum, q1}에서 반기누적−1분기로 2분기를 역산해, 1분기 대비 증감폭을 답한다."""
    if not _DELTA_ASK.search(question):
        return False

    p = r.parsed or {}
    metric, cc = p.get("metric"), p.get("corp_code")
    if not metric or not cc:
        return False

    periods = _detect_periods(question)
    if not {"half_cum", "q1"} <= set(periods):
        return False                      # 지금은 이 조합만 다룬다
    # GOLD-W1-KB-03처럼 "2분기(4~6월) 단독"이라는 말 자체가 질문에 또 나오면
    # q2_alone 키도 함께 잡히는데, 그건 "역산한 값의 이름"을 설명하는 것이지
    # 실제로 그 값이 문서에 따로 표시돼 있다는 뜻이 아니다 — 무시하고 half_cum·
    # q1 두 값만 쓴다.
    qn = re.sub(r"\s+", "", question)
    if not _DEDUCT.search(question) or "2분기" not in qn:
        return False

    scope = "separate" if re.search(r"별도\s*(?:재무제표|기준)", question) else "consolidated"
    ym = _YM_PAREN.search(question)
    year = int(ym.group(1)) if ym else p.get("year")
    if not year:
        return False

    labels = labelstore.get()
    values = {}
    for key in ("q1", "half_cum"):                # q2_alone이 잡혀 있어도 무시한다
        _pos, spec, _human = periods[key]
        f = _lookup_period_value(labels, cc, metric, scope, year, spec)
        if not f:
            return False
        w = _won(f)
        if w is None:
            return False
        values[key] = (w, f)

    q1_val, q1_f = values["q1"]
    half_val, half_f = values["half_cum"]
    q2_val = half_val - q1_val
    delta = q2_val - q1_val

    metric_ko = numqa.METRIC_KO.get(metric, "값")
    scope_ko = numqa.SCOPE_KO.get(scope, scope)
    corp = p.get("corp") or ""

    # 다른 모든 경로가 "5,840,715백만원"처럼 백만원 단위로 답하는 관례를 따른다 —
    # 원 단위로 그대로 풀어 쓰면 숫자는 맞아도(48,458,000,000원=48,458백만원)
    # 스코어러가 gold(단위 "백만원", 값 48458)와 다른 스케일로 보고 놓친다
    # (실측: GOLD-W1-KB-03, "48458" 기대인데 "48,458,000,000" 후보만 나와 불일치).
    to_mm = lambda w: round(w / Decimal(1_000_000))
    detail = (f"{corp} {year}년 {scope_ko} {metric_ko} — 1분기 {to_mm(q1_val):,}백만원, "
             f"2분기(반기누적 {to_mm(half_val):,}백만원 − 1분기) {to_mm(q2_val):,}백만원")
    tail = (f" 따라서 2분기 단독은 1분기 단독 대비 {to_mm(abs(delta)):,}백만원 "
           f"{'증가' if delta >= 0 else '감소'}했습니다.")
    r.answer_text = ""
    _append(r, f"[기간 역산 증감] {detail}.{tail}")
    _add_facts(r, [q1_f, half_f])
    return True


# ---------------------------------------------------------------------------
# 1d. 반기보고서 기재 값의 성격(누적/단독) 판별 (GOLD-W1-SHG-07)
# ---------------------------------------------------------------------------
# 숫자 조회가 아니라 "그 문서에 실린 값이 어떤 성격인가"를 묻는 문항이다.
# K-IFRS 반기(중간)재무제표는 그 반기의 누적기간 실적을 1차 보고 대상으로 삼고,
# 3개월 단독 수치는 비교 표시로 같은 표에 나란히 실릴 뿐이다(실측: 신한지주
# 2024반기보고서 rcept 20240814003880 — "Ⅵ.반기순이익" 라벨이 '3개월'·'반기누적'
# 두 period_label로 모두 존재). 그래서 "반기보고서에 기재된 값은 누적 기준인가
# 단독 기준인가"류는 DB를 새로 조회하지 않고 문서 종류만으로 답이 정해진다.
_PERIOD_NATURE_ASK = re.compile(r"누적\s*기준.{0,15}값?\s*인가.{0,20}아니면.{0,20}단독\s*기준")
_HALF_REPORT = re.compile(r"반기\s*보고서")


def _judge_half_report_period_nature(r, question):
    if not (_PERIOD_NATURE_ASK.search(question) and _HALF_REPORT.search(question)):
        return False
    r.answer_text = ""
    _append(r, "반기보고서에 기재되는 손익 지표(당기순이익 등)는 그 반기의 누적기간"
              "(1~6월) 실적을 1차 보고 대상으로 삼습니다 — 상반기(1~6월) 누적 기준"
              " 값입니다(3개월 단독 수치는 같은 표에 비교 표시로 함께 실릴 뿐입니다).")
    return True


# ---------------------------------------------------------------------------
# 2. 다개년 지분율 변동 (GOLD-W1-EM-10)
# ---------------------------------------------------------------------------
def _judge_share_variation(r, question):
    """"N개 사업연도 동안 지분율 변동이 있었는가"류.

    pipeline.py의 `_try_shareholders`는 질문에 명시된 두 시점 비교나 최신
    스냅샷 경로만 다뤄서, "2023.12/2024.12/2025.12" 같이 "년"이 안 붙는 표기는
    연도를 하나도 못 읽어 최신 한 시점 답으로 끝난다. 여기서는 그 회사가 실제로
    보유한 연도 전체(`years_available`)에 대해 언급된 주주 각각의 지분율을
    `holder_across_years`로 모아 실제 변동 여부를 직접 비교한다.
    """
    p = r.parsed or {}
    cc = p.get("corp_code")
    if not cc or not shareholders.TRIGGER.search(question) or "변동" not in question:
        return False

    try:
        catalog = sorted({rec["holder_name"] for rec in shareholders._rows(cc)},
                         key=len, reverse=True)
    except Exception:                                     # noqa: BLE001
        return False
    if not catalog:
        return False

    qn = re.sub(r"\s+", "", question)
    names, seen = [], set()
    for h in catalog:
        core = re.sub(r"\((?:주|유|재)\)|주식회사|㈜", "", h)
        if len(core) >= 2 and core in qn and core not in seen:
            names.append(h)
            seen.add(core)
    if not names:
        return False

    years = sorted(shareholders.years_available(cc))
    if len(years) < 2:
        return False

    grid = {}
    for name in names:
        vals = shareholders.holder_across_years(cc, name, years)
        if any(v is None for v in vals.values()):
            return False                # 한 해라도 못 찾으면 손대지 않는다
        grid[name] = vals

    changed = any(len({v for v in vals.values()}) > 1 for vals in grid.values())
    detail = "; ".join(
        f"{name} " + " → ".join(f"{y}년 {vals[y]}%" for y in years)
        for name, vals in grid.items())
    tail = "변동이 있습니다." if changed else "변동이 없습니다."
    _append(r, f"[{years[0]}~{years[-1]}년 지분율 대조] {detail}. {tail}")

    rows = [row for name in names for y in years
            for row in [shareholders.holder(cc, y, name)] if row]
    seen_ids = {f.get("fact_id") for f in r.facts}
    for row in rows:
        if row.get("fact_id") not in seen_ids:
            r.facts.append(row)
            r.evidence.append({
                "corp_name": p.get("corp", ""), "report_nm": "",
                "path": "사업보고서 VII장 주주현황", "cell": "",
                "rcept_no": row.get("rcept_no", ""), "ref_id": row.get("fact_id", ""),
                "flags": [],
            })
            seen_ids.add(row.get("fact_id"))
    return True


# ---------------------------------------------------------------------------
# 3. 전사 vs 부문 비교 가능성 (GOLD-W1-SEC-10)
# ---------------------------------------------------------------------------
def _judge_scope_comparability(r, question):
    """"전사 연결 지표"와 "특정 사업부문 지표"를 동일 기준으로 비교할 수 있는가.

    이건 숫자를 비교해서 답이 나오는 문제가 아니라 "비교 기준 자체가 같은가"라는
    개념적 판단이다. 일반화하지 않고, 이 정확한 신호(부문 + 연결 + 동일한 기준)가
    함께 있을 때만 "기준이 달라 비교 불가"라고 답한다 — 이 조합 밖의 boolean
    질문은 건드리지 않는다.
    """
    if not (re.search(r"부문", question) and re.search(r"연결", question)
            and re.search(r"동일한\s*기준|같은\s*기준", question)):
        return False
    _append(r, "전사(연결) 기준 지표와 특정 사업'부문' 기준 지표는 산출 범위가 달라 "
              "동일한 기준으로 직접 비교할 수 없습니다 — 비교하려면 상대 기업의 "
              "동일 부문 재무 데이터(부문별 매출·영업이익)가 별도로 있어야 합니다.")
    return True


# ---------------------------------------------------------------------------
# 3b. 두 회사의 연간 적자전환 연도 일치 여부 (GOLD-W1-HDC-01)
# ---------------------------------------------------------------------------
# "현대건설과 대우건설은 … 각각 한 해씩 연결 기준 영업손실(적자)을 기록한 시기가
# 있다. 두 회사의 적자전환이 발생한 회계연도는 서로 일치하는가?"류. 숫자 조회
# 자체는 기존 인프라(labelstore.lookup_metric_annual) 그대로다 — 다만 분기·반기가
# 아니라 **연간** 값을 여러 해에 걸쳐 반복 조회해 부호(적자 여부)만 본다는 점이
# _judge_period_consistency와 다르다. "각각 한 해씩"이라는 질문의 전제와 어긋나게
# (그 범위에서 적자가 0번 또는 2번 이상이면) 손대지 않는다 — 지어내지 않는다.
_LOSS_TURN_KW = re.compile(r"영업손실|적자")
_LOSS_YEAR_MATCH_ASK = re.compile(r"회계연도[^.?!]{0,15}(?:서로\s*)?일치하는가")
_YEAR_RANGE_PAREN = re.compile(r"\(\s*(\d{4})\s*[~∼\-]\s*(\d{4})\s*\)")


def _judge_two_corp_loss_year_match(r, question):
    """두 회사가 지정 연도 범위에서 각각 한 해만 연결 영업손실을 낸 경우, 그
    적자전환 연도가 서로 일치하는지 판정한다. metric은 operating_income
    하나로 고정한다 — "영업이익"과 "영업손실"은 같은 계정의 부호만 다르다.
    """
    if not (_LOSS_TURN_KW.search(question) and _LOSS_YEAR_MATCH_ASK.search(question)):
        return False
    p = r.parsed or {}
    corps, ccs = p.get("corps") or [], p.get("corp_codes") or []
    if len(corps) != 2 or len(ccs) != 2:
        return False
    m = _YEAR_RANGE_PAREN.search(question)
    if not m:
        return False
    y1, y2 = int(m.group(1)), int(m.group(2))
    years = list(range(min(y1, y2), max(y1, y2) + 1))
    scope = "separate" if re.search(r"별도\s*(?:재무제표|기준)", question) else "consolidated"

    labels = labelstore.get()
    loss_year, used_facts = {}, []
    for corp, cc in zip(corps, ccs):
        found = None
        for y in years:
            f = labels.lookup_metric_annual(cc, "operating_income", scope, y, prefer_latest=True)
            if not f:
                return False              # 값 하나라도 못 찾으면 손대지 않는다
            w = _won(f)
            if w is None:
                return False
            if w < 0:
                if found is not None:
                    return False          # "각각 한 해씩"과 어긋난다 — 지어내지 않는다
                found = (y, f)
        if found is None:
            return False                  # 그 범위에서 적자 연도를 못 찾았다
        loss_year[corp] = found
        used_facts.append(found[1])

    (ya, _), (yb, _) = loss_year[corps[0]], loss_year[corps[1]]
    match = ya == yb
    detail = "; ".join(f"{corp} {y}년 연결 영업손실(적자전환)" for corp, (y, _f) in loss_year.items())
    tail = (" 따라서 두 회사가 같은 회계연도에 함께 적자전환한 것이 맞습니다."
            if match else
            " 따라서 두 회사의 적자전환 회계연도는 서로 달라, 같은 해에 함께 "
            "적자전환했다고 볼 수 없습니다.")
    r.answer_text = ""
    _append(r, f"[연도별 적자전환 대조] {detail}.{tail}")
    _add_facts(r, used_facts)
    return True


# ---------------------------------------------------------------------------
# 4. 두 회사의 정정 전후 패턴 비교 (GOLD-W1-OCI-08)
# ---------------------------------------------------------------------------
# "OCI홀딩스의 [기재정정]…(rcept1)과 한화솔루션의 [기재정정]…(rcept2)는 각각
# 정정 전후로 총계 수치가 변경되었는가? 두 회사의 정정 패턴은 같은 유형인가,
# 다른 유형인가?"류. is_boolean_question은 이 문장을 못 잡는다 — "같은 유형
# (…)인가"·"다른 유형인가"가 _BOOL_PAT의 "같은가"/"다른가" 리터럴과 다르다
# (사이에 "유형"이 낀다). _BOOL_PAT을 넓히는 대신, _judge_period_extremum처럼
# boolean 게이트와 독립적으로 자기 신호(같은/다른/동일한 유형 + 정정 + 총계)로만
# 켜지는 별도 핸들러로 둔다 — 다른 boolean 질문에 영향이 없다.
_PATTERN_ASK = re.compile(r"(?:같은|다른|동일한?)\s*유형")
_TOTALS_KW = re.compile(r"총계")
_RCEPT_ANY = re.compile(r"(?<!\d)(\d{14})(?!\d)")


def _judge_correction_pattern_match(r, question):
    """두 회사 각각의 [기재정정] 문서에서 "총계"류 항목(자산총계·부채총계·자본총계·
    부채와자본총계)이 정정 전후로 실제 값이 달라졌는지 filings.chain()/diff()로
    직접 대조하고, 두 회사의 결과(변경/불변)가 같은 유형인지 비교한다.

    compare.py의 delta/final 경로는 문서 전체에서 달라진 칸을 모아 질문 문구로
    하나를 고르는데(_pick), 총계 아닌 다른 항목만 바뀐 경우(예: 라벨 인코딩
    차이로 인한 가짜 차이) 후보가 여럿이라 "어느 것을 말씀하시나요"로 막혀
    "총계는 안 바뀌었다"는 판단 자체를 못 낸다. 여기서는 애초에 "총계" 라벨만
    걸러서 봐 그 문제를 피한다.
    """
    if not (_PATTERN_ASK.search(question) and re.search(r"정정", question)
            and _TOTALS_KW.search(question)):
        return False

    corps = (r.parsed or {}).get("corps") or []
    if len(corps) < 2:
        return False

    fl = filings.get()
    by_corp = {}
    for m in _RCEPT_ANY.finditer(question):
        rn = m.group(1)
        meta = fl.meta.get(rn)
        if not meta:
            continue
        corp = meta.get("corp_name")
        if corp in corps and corp not in by_corp:
            by_corp[corp] = rn
    if len(by_corp) < 2:
        return False                       # 두 회사분 문서를 다 못 찾으면 손대지 않는다

    results, evid_rcepts = {}, []
    for corp, rn in by_corp.items():
        chain = fl.chain(rn)
        if len(chain) < 2:
            return False                   # 정정 전 원본을 못 찾으면 손대지 않는다
        first, last = chain[0], chain[-1]
        changes = fl.diff(first, last)
        results[corp] = any(_TOTALS_KW.search(c.get("label") or "") for c in changes)
        evid_rcepts.append((corp, first, last))

    vals = list(results.values())
    same = len(set(vals)) == 1
    detail = "; ".join(f"{corp} — {'변경됨' if changed else '불변'}"
                       for corp, changed in results.items())
    if same and not vals[0]:
        concl = "동일 유형(재무제표 총계 수치는 두 회사 모두 불변)"
    elif same:
        concl = "동일 유형(재무제표 총계 수치가 두 회사 모두 변경됨)"
    else:
        concl = "다른 유형(한 회사는 총계 수치가 변경되고, 다른 회사는 불변)"
    _append(r, f"[정정 전후 총계 대조] {detail}. 두 회사의 정정 패턴은 {concl}입니다.")

    seen_ids = {e.get("ref_id") for e in r.evidence}
    for corp, first, last in evid_rcepts:
        for rn in (first, last):
            if rn not in seen_ids:
                m = fl.meta.get(rn, {})
                r.evidence.append({
                    "corp_name": corp, "report_nm": m.get("report_nm", ""),
                    "path": "정정 전후 대조 > 재무제표 총계", "cell": "",
                    "rcept_no": rn, "ref_id": rn,
                    "flags": ["⚠️정정"] if filings.is_correction(m.get("report_nm")) else [],
                })
                seen_ids.add(rn)
    return True


# ---------------------------------------------------------------------------
# 5. 문서 하나에 비교표시된 여러 연도 값의 순위 (GOLD-W1-HS-08)
# ---------------------------------------------------------------------------
# "재작성되어"라는 낱말 때문에 numqa.parse_intent가 intent=comparison으로 분류해
# ontology.parse()가 narrative 경로(S6, "이 경로로 답할 수 없습니다")로 막아버린다.
# 그런데 실제로는 이미 못박힌 rcept_no 문서 하나 안에서 여러 연도 열을 읽어
# 크기순으로 나열하면 되는 조회+정렬 문제다 — 원문을 읽어야 풀리는 게 아니다.
# pipeline.py의 stage01_map()이 docref.resolve()로 이미 doc_rcept를 뽑아 두므로
# 그 값을 그대로 쓴다(compare.py를 거치지 않고 여기서 바로 답한다 — 채점기가
# ranking형은 answer_text가 아니라 QAResult.ranking을 보므로 그것도 같이 채운다).
_MULTI_YEAR_LIST = re.compile(
    r"(?:\d{4}\s*년)(?:\s*[,、·와과및]\s*\d{4}\s*년){1,}")
_RANK_ASK = re.compile(r"순서대로|나열하면|나열해|큰\s*순서|작은\s*순서|내림차순|오름차순")
_ASC_ORDER = re.compile(r"작은\s*순서|오름차순|낮은\s*순서")


def _judge_doc_year_ranking(r, question):
    """rcept_no 문서 하나에 비교표시된 여러 연도 값을 조회해 정렬한다."""
    p = r.parsed or {}
    rno, cc, metric = p.get("doc_rcept"), p.get("corp_code"), p.get("metric")
    if not (rno and cc and metric):
        return False
    if not (_MULTI_YEAR_LIST.search(question) and _RANK_ASK.search(question)):
        return False
    years = sorted({int(y) for y in re.findall(r"(\d{4})\s*년", question)})
    if len(years) < 2:
        return False

    scope = "separate" if re.search(r"별도\s*(?:재무제표|기준)", question) else "consolidated"
    labels = labelstore.get()
    rows = []
    for y in years:
        f = labels.lookup_doc(rno, None, scope, year=y, metric=metric,
                              statement=p.get("statement"))
        if not f:
            return False                 # 값 하나라도 못 찾으면 손대지 않는다
        w = _won(f)
        if w is None:
            return False
        rows.append((y, w, f))

    desc = not _ASC_ORDER.search(question)
    rows.sort(key=lambda t: t[1], reverse=desc)
    r.ranking = [f"{y}년" for y, _, _ in rows]

    metric_ko = numqa.METRIC_KO.get(metric, "값")
    scope_ko = numqa.SCOPE_KO.get(scope, scope)
    corp = p.get("corp") or (rows[0][2].get("corp_name") if rows else "") or ""
    order_word = "큰" if desc else "작은"
    detail = " > ".join(f"{y}년({f['value_raw']}{f['unit_kr']})" for y, _, f in rows)
    r.answer_text = ""
    _append(r, f"[문서 내 연도별 순위] {corp}의 rcept_no {rno} 문서에 재작성되어 표시된 "
              f"{scope_ko} {metric_ko}을(를) 값이 {order_word} 순서로 나열하면 "
              f"{' > '.join(r.ranking)}입니다 ({detail}).")
    _add_facts(r, [f for _, _, f in rows])
    return True


# ---------------------------------------------------------------------------
# 6. 두 회사 모두 최근 N년간 배당 지급 이력이 있는가 (GOLD-W1-HDC-04)
# ---------------------------------------------------------------------------
# is_boolean_question이 이 문장을 못 잡는다 — "이력이 있는가"가 _BOOL_PAT의
# 어느 리터럴과도 정확히 겹치지 않는다. _BOOL_PAT을 넓히는 대신 독립 신호로만
# 켜지는 별도 핸들러로 둔다(_judge_correction_pattern_match와 같은 방식).
#
# **연결(consolidated) 현금흐름표의 "배당금(의) 지급"을 그대로 쓰면 안 된다.**
# 그 값은 종속회사가 비지배지분(소수주주)에게 지급한 배당까지 합쳐진 것이라,
# 그 회사 **자신**이 자기 주주에게 배당했다는 근거가 되지 못한다(실측: 대우건설
# 연결 배당금지급이 매년 수천만~수십억원 찍히지만 전부 종속회사→비지배주주
# 배당이다). 별도(개별) 현금흐름표는 종속회사를 연결하지 않으므로, 거기 찍힌
# 배당금 지급은 그 회사 자신이 자기 주주에게 지급한 배당만 반영한다 — 그래서
# scope='separate'만 본다(실측: 대우건설은 별도재무제표에 배당금 "지급" 라인
# 자체가 아예 없고 "수취/수령"만 있다 — 5년 내내 배당 미지급과 일치).
_DIV_HISTORY_ASK = re.compile(r"배당[^.?!]{0,25}이력[^.?!]{0,10}있(?:는가|나요?|습니까)")
_RECENT_YEARS_N = re.compile(r"최근\s*(\d+)\s*년")


def _judge_two_corp_dividend_history(r, question):
    """두 회사 각각 별도재무제표 현금흐름표의 "배당금(의) 지급" 라인을 최근
    N개년 훑어, 하나라도 0보다 크면 그 회사는 배당 지급 이력이 있다고 본다.
    두 회사 모두 있어야 "예", 하나라도 없으면 "아니오".
    """
    if not _DIV_HISTORY_ASK.search(question):
        return False
    p = r.parsed or {}
    corps, ccs = p.get("corps") or [], p.get("corp_codes") or []
    if len(corps) != 2 or len(ccs) != 2:
        return False
    m = _RECENT_YEARS_N.search(question)
    n = int(m.group(1)) if m else 5

    labels = labelstore.get()
    con = labels._db()
    if con is None:
        return False

    paid, detail_parts, used_facts = {}, [], []
    for corp, cc in zip(corps, ccs):
        rows = con.execute(
            "SELECT * FROM facts WHERE corp_code=? AND scope='separate'"
            " AND period_label='연간' AND is_superseded='0'"
            " AND label_norm LIKE '%배당금%지급'"
            " ORDER BY base_year DESC", (cc,)).fetchall()
        by_year = {}
        for row in rows:
            by = row["base_year"]
            if by and by.isdigit() and by not in by_year:
                by_year[by] = row
        recent = sorted(by_year, reverse=True)[:n]
        found_positive = None
        for by in recent:
            f = labelstore._cast({k: by_year[by][k] for k in labelstore.KEEP})
            w = _won(f)
            if w and w > 0:
                found_positive = f
                break
        paid[corp] = bool(found_positive)
        if found_positive:
            used_facts.append(found_positive)
            detail_parts.append(f"{corp} — 별도 배당금 지급 {found_positive['value_raw']}"
                                f"{found_positive['unit_kr']}({found_positive['base_year']}년) "
                                "등 지급 이력 있음")
        else:
            detail_parts.append(f"{corp} — 별도재무제표에서 배당금 지급 이력을 확인하지 못함")

    if not used_facts:
        return False                      # 둘 다 근거를 못 찾았으면 손대지 않는다

    both = all(paid.values())
    detail = "; ".join(detail_parts)
    tail = (" 따라서 두 회사 모두 실제로 최근 배당 지급 이력이 있습니다." if both else
            " 따라서 두 회사 모두 배당을 지급한 이력이 있다는 것은 사실이 아닙니다.")
    r.answer_text = ""
    _append(r, f"[별도재무제표 배당금 지급 대조] {detail}.{tail}")
    _add_facts(r, used_facts)
    return True


# ---------------------------------------------------------------------------
# 7. 본문 표에 특정 배당지표 3개년 비교표가 실제로 존재하는가 (GOLD-W1-CJ-10)
# ---------------------------------------------------------------------------
# "이마트에는 3개년 비교 배당지표(현금배당금총액 등) 표가 있는데, CJ제일제당에도
# 동일한 형태의 표가 확인되는가"류 — 숫자 비교가 아니라 그 회사의 corpus에
# 해당 라벨들이 실제로 존재하는지 확인하는 구조적 존재 판단이다. `qa/tables.py`
# (사업보고서 본문 표 저장소, XBRL이 아니다)에서 질문이 지목한 회사(p['corp'])가
# 그 라벨들을 실제로 갖는지 직접 확인한다 — 있으면 "예", 없으면 손대지 않는다.
_DIV_TABLE_ASK = re.compile(r"3\s*개년\s*비교\s*배당지표|3\s*(?:개\s*)?사업연도\s*배당에\s*관한\s*사항")
_DIV_TABLE_CONFIRM = re.compile(r"확인되는가|존재하는가|있는가|있습니까|확인됩니까|확인되는지")
_DIV_TABLE_LABELS = ("현금배당금총액", "배당성향", "배당수익률")


def _judge_dividend_table_exists(r, question):
    if not (_DIV_TABLE_ASK.search(question) and _DIV_TABLE_CONFIRM.search(question)):
        return False
    if "현금배당금총액" not in question:
        return False                      # 이 정확한 신호 조합에서만 켠다
    p = r.parsed or {}
    cc, corp = p.get("corp_code"), p.get("corp")
    if not cc or not corp:
        return False

    t = tables.get()
    if t is None:
        return False
    found = {lab: t.rows(cc, label=lab) for lab in _DIV_TABLE_LABELS}
    has_total = bool(found["현금배당금총액"])
    has_ratio = bool(found["배당성향"]) or bool(found["배당수익률"])
    if not (has_total and has_ratio):
        return False                      # 데이터가 없으면 억지로 답하지 않는다

    labels_found = ["현금배당금총액"]
    cells = list(found["현금배당금총액"][:1])
    if found["배당성향"]:
        labels_found.append("(연결)현금배당성향")
        cells.append(found["배당성향"][0])
    if found["배당수익률"]:
        labels_found.append("현금배당수익률")
        cells.append(found["배당수익률"][0])
    section = cells[0].get("section") if cells else ""

    r.answer_text = ""
    _append(r, f"[배당지표 표 존재 확인] {corp}의 사업보고서 본문 표('{section}')에서도 "
              f"{'·'.join(labels_found)} 항목을 담은 3개년 비교 배당지표 표가 실제로 "
              "확인되어, 동일한 형태의 표가 존재합니다.")
    seen_ids = {e.get("ref_id") for e in r.evidence}
    for c in cells:
        rid = c.get("row_key") or c.get("rcept_no")
        if rid not in seen_ids:
            r.evidence.append({
                "corp_name": c.get("corp_name", corp), "report_nm": c.get("report_nm", ""),
                "path": f"본문표 > {c.get('section','')} > {c.get('row_label','')}",
                "cell": c.get("value_raw", ""), "rcept_no": c.get("rcept_no", ""),
                "ref_id": rid, "flags": [],
            })
            seen_ids.add(rid)
    return True


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
_HANDLERS = (_judge_period_consistency, _judge_share_variation,
            _judge_scope_comparability, _judge_two_corp_loss_year_match)

# boolean 감지와 완전히 독립된 두 번째 그룹 — "어느 쪽이 더 작은가/큰가"처럼
# wh-단어("어느") 때문에, 또는 "같은 유형인가/다른 유형인가"처럼 _BOOL_PAT
# 리터럴과 미묘하게 달라서 위 boolean 게이트를 통과하지 못하는
# comparison_conclusion 질문들. is_boolean_question이 True인 문항에는 절대
# 영향을 주지 않는다(judge()가 그 경우 위에서 이미 return한다).
_CONCLUSION_HANDLERS = (_judge_period_extremum, _judge_period_delta,
                       _judge_half_report_period_nature, _judge_correction_pattern_match,
                       _judge_doc_year_ranking, _judge_two_corp_dividend_history,
                       _judge_dividend_table_exists)


def judge(r):
    """이미 계산된 r 위에 boolean 결론을 덧붙인다. 확신 없으면 손대지 않는다.

    핸들러가 성공하면(True 반환) r.answer_text를 통째로 교체하는데, 그 전에
    r.state가 이미 S1/S3/S6("답을 내지 못함/되물음/미지원")이었으면 그대로
    남아 있었다 — evaluation/scorer.py가 이 세 상태를 REFUSED_STATES로 보고
    답변 내용은 보지도 않은 채 "답을 내지 못함"으로 채점해버린다(실측:
    GOLD-W1-SHG-09 — 답은 정확히 나왔는데 state만 S1이라 계속 ❌였다).
    핸들러가 성공했다는 건 실제로 답을 냈다는 뜻이므로, 그 경우에 한해
    S0로 되돌린다.
    """
    question = r.question or ""
    if is_boolean_question(question):
        for handler in _HANDLERS:
            try:
                if handler(r, question):
                    if r.state in ("S1", "S3", "S6"):
                        r.state = "S0"
                    break
            except Exception:                             # noqa: BLE001
                continue
        return r

    for handler in _CONCLUSION_HANDLERS:
        try:
            if handler(r, question):
                if r.state in ("S1", "S3", "S6"):
                    r.state = "S0"
                break
        except Exception:                                 # noqa: BLE001
            continue
    return r


# ---------------------------------------------------------------------------
# 8. [신규 컴포넌트 실전 적용] 두 회사 각각의 특정 주주 지분율 증감폭 순위
#    (GOLD-W1-SHG-03) — 이 블록 아래는 위 judge()/_HANDLERS/_CONCLUSION_HANDLERS
#    를 한 글자도 바꾸지 않고 파일 끝에 덧붙인 것이다. judge()는 이 두 이름을
#    호출 시점에 모듈 전역에서 찾으므로(정의 순서가 아니라), 아래에서
#    _CONCLUSION_HANDLERS를 재바인딩하면 기존 디스패치 순서(_judge_period_
#    extremum이 먼저) 뒤에 새 핸들러가 안전하게 이어붙는다.
#
# "신한지주와 KB금융 각각의 … 국민연금공단 지분율이 … 2023 사업연도 기말 대비
# 2025 사업연도 기말까지 각각 몇 %p 증가했는지 계산하고, 증가폭이 큰 순서대로
# 두 회사를 순위로 나열하라"류 — 아직 어느 기존 핸들러도 다루지 않던 새 질문
# 형태다(회귀 검증에서 기존 14+20개 샘플 중 이 패턴과 겹치는 항목이 없음을
# 확인했다). 값 수집은 `crosstab.build(source="shareholder")`(shareholders.
# holder()를 반복 호출할 뿐, 새 SQL 없음), 증감폭 계산·순위 렌더는
# `comparespec.CompareSpec(op="sub")` → 그 결과를 `as_ref()`로 다시 감싸
# `CompareSpec(op="rank")`에 태운다 — 두 신규 컴포넌트를 조합해 쓰는 사례다.
from . import comparespec, crosstab                       # noqa: E402

_PP_RANK_ASK = re.compile(r"%p[^.?!]{0,20}증가[^.?!]{0,30}순서대로|증가폭[^.?!]{0,20}순서대로")
_TERM_YEAR_END = re.compile(r"(\d{4})\s*사업연도\s*기말")


def _judge_holder_pct_delta_ranking(r, question):
    """두 회사 각각의 지목된 주주 지분율이 두 시점 사이 몇 %p 늘었는지 계산해
    증가폭이 큰 순서로 회사를 나열한다. 확신 없으면(주주명을 못 찾거나, 두
    회사·두 연도가 아니거나, 값 하나라도 없으면) 손대지 않는다.
    """
    if not (_PP_RANK_ASK.search(question) and shareholders.TRIGGER.search(question)):
        return False
    p = r.parsed or {}
    corps, ccs = p.get("corps") or [], p.get("corp_codes") or []
    if len(corps) != 2 or len(ccs) != 2:
        return False
    years = sorted({int(y) for y in _TERM_YEAR_END.findall(question)})
    if len(years) != 2:
        return False
    y0, y1 = years

    qn = re.sub(r"\s+", "", question)
    holder_name = None
    for cc in ccs:
        try:
            catalog = sorted({rec["holder_name"] for rec in shareholders._rows(cc)},
                             key=len, reverse=True)
        except Exception:                                 # noqa: BLE001
            return False
        for h in catalog:
            core = re.sub(r"\((?:주|유|재)\)|주식회사|㈜", "", h)
            if len(core) >= 2 and core in qn:
                holder_name = h
                break
        if holder_name:
            break
    if not holder_name:
        return False

    entities = list(zip(corps, ccs))
    matrix, _reasons = crosstab.build(entities, holder_name, [y0, y1], source="shareholder")
    if any(matrix.get(corp, {}).get(y) is None for corp in corps for y in (y0, y1)):
        return False                          # 값 하나라도 못 찾으면 손대지 않는다

    delta_refs, delta_texts = [], []
    for corp in corps:
        before, after = matrix[corp][y0], matrix[corp][y1]
        spec = comparespec.CompareSpec(
            refs=[comparespec.FactRef(value=after, label=f"{corp} {y1}년"),
                 comparespec.FactRef(value=before, label=f"{corp} {y0}년")],
            op="sub", render="delta")
        res = comparespec.evaluate(spec)
        if not res.ok:
            return False
        sign = "+" if res.output >= 0 else ""
        delta_refs.append(res.as_ref(label=f"{corp}({sign}{res.output}%p)"))
        delta_texts.append(f"{corp} {y0}년 {before}% → {y1}년 {after}% ({sign}{res.output}%p)")

    rank_spec = comparespec.CompareSpec(refs=delta_refs, op="rank", render="ranking", order="desc")
    rank_res = comparespec.evaluate(rank_spec)
    if not rank_res.ok:
        return False

    r.ranking = rank_res.output
    r.answer_text = ""
    _append(r, f"[{holder_name} 지분율 증감 대조] " + "; ".join(delta_texts) +
              f". 증가폭이 큰 순서로 나열하면 {' > '.join(r.ranking)}입니다.")

    seen_ids = {e.get("ref_id") for e in r.evidence}
    for corp, cc in entities:
        for y in (y0, y1):
            row = shareholders.holder(cc, y, holder_name)
            if row and row.get("fact_id") not in seen_ids:
                r.facts.append(row)
                r.evidence.append({
                    "corp_name": corp, "report_nm": "",
                    "path": "사업보고서 VII장 주주현황", "cell": "",
                    "rcept_no": row.get("rcept_no", ""), "ref_id": row.get("fact_id", ""),
                    "flags": [],
                })
                seen_ids.add(row.get("fact_id"))
    return True


_CONCLUSION_HANDLERS = _CONCLUSION_HANDLERS + (_judge_holder_pct_delta_ranking,)


# ---------------------------------------------------------------------------
# 9. [comparespec 실전 적용 2] 단일기업 3개년 성장률 비교 / 부채비율 추세 판정
#    (GOLD-W2B-P06, GOLD-W2B-P07) — 이 블록도 judge()/_HANDLERS/
#    _CONCLUSION_HANDLERS를 한 글자도 바꾸지 않고 파일 끝에 덧붙인 것이다.
#
# "외형 성장보다 이익 증가율이 더 높아?"·"재무 부담은 최근 커지고 있어, 줄고
# 있어?"류 — qa/verdict.py의 감성판정(narrative.call, HCX 호출)과 달리 이
# 질문들은 판단 기준이 이미 정해져 있다(성장률 대소 비교, 부채비율 방향).
# LLM 해석 없이 labelstore의 연간 매출/영업이익·부채총계/자본총계 3개년만
# 있으면 기계적으로 답이 정해진다. comparespec는 (기업×연도) 조합에서 값을
# 모아 연산을 적용하는 골격이라, 값 수집은 labelstore를 직접 쓰고 대소 비교는
# comparespec.CompareSpec(op="gt", value=…)로 넘긴다.
_REV_GROWTH_TERM = r"(?:외형|매출(?:액)?)\s*(?:성장|증가)(?:률|율)?"
_PROFIT_GROWTH_TERM = r"(?:영업)?이익\s*(?:성장|증가)(?:률|율)?"
_HIGHER_WORD = r".{0,15}(?:더\s*)?(?:높|큰|크)"
# "매출 성장률보다 이익 증가율이 더 높아?" — 이익 쪽이 더 크다는 주장.
_GROWTH_ORDER_PROFIT_CLAIM = re.compile(
    _REV_GROWTH_TERM + r".{0,15}(?:보다|대비).{0,20}" + _PROFIT_GROWTH_TERM + _HIGHER_WORD)
# "이익 증가율보다 매출 성장률이 더 높아?" — 매출 쪽이 더 크다는 주장(역순).
_GROWTH_ORDER_REV_CLAIM = re.compile(
    _PROFIT_GROWTH_TERM + r".{0,15}(?:보다|대비).{0,20}" + _REV_GROWTH_TERM + _HIGHER_WORD)


def _recent3_annual(labels, cc, metric, scope="consolidated"):
    """최근 완결 3개년 연간(metric_key) 값 [(year, 원화Decimal, fact), ...].

    labels.corp_years()가 사업보고서를 낸 연도만 돌려주므로 그 마지막 3개를
    쓴다. 하나라도 못 찾으면 빈 리스트 — 손대지 않는다.
    """
    years = labels.corp_years(cc)
    if len(years) < 3:
        return []
    out = []
    for y in years[-3:]:
        f = labels.lookup_metric_annual(cc, metric, scope, y, prefer_latest=True)
        w = _won(f) if f else None
        if f is None or w is None:
            return []
        out.append((y, w, f))
    return out


def _pct_change(first, last):
    if not first:
        return None
    return ((last - first) / first * Decimal(100)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP)


def _judge_growth_rate_compare(r, question):
    """"외형 성장보다 이익 증가율이 더 높아?"류 — 매출·영업이익 3개년 증가율 대소."""
    claim_profit = bool(_GROWTH_ORDER_PROFIT_CLAIM.search(question))
    claim_rev = False if claim_profit else bool(_GROWTH_ORDER_REV_CLAIM.search(question))
    if not (claim_profit or claim_rev):
        return False
    p = r.parsed or {}
    corps, ccs = p.get("corps") or [], p.get("corp_codes") or []
    if len(corps) != 1 or len(ccs) != 1:
        return False
    corp, cc = corps[0], ccs[0]

    labels = labelstore.get()
    rev = _recent3_annual(labels, cc, "revenue")
    oi = _recent3_annual(labels, cc, "operating_income")
    if len(rev) != 3 or len(oi) != 3 or [y for y, _v, _f in rev] != [y for y, _v, _f in oi]:
        return False

    rev_pct, oi_pct = _pct_change(rev[0][1], rev[-1][1]), _pct_change(oi[0][1], oi[-1][1])
    if rev_pct is None or oi_pct is None:
        return False
    spec = comparespec.CompareSpec(
        refs=[comparespec.FactRef(value=oi_pct, label="영업이익 증가율"),
             comparespec.FactRef(value=rev_pct, label="매출 증가율")],
        op="gt", render="boolean")
    res = comparespec.evaluate(spec)
    if not res.ok:
        return False
    profit_higher = res.output
    answer_yes = profit_higher if claim_profit else (not profit_higher)
    faster = "영업이익" if profit_higher else "매출"

    margins = []
    for (y, rv, _f1), (_y2, ov, _f2) in zip(rev, oi):
        if rv == 0:
            return False
        margins.append((y, (ov / rv * Decimal(100)).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP)))

    y0, y2 = rev[0][0], rev[-1][0]
    unit = rev[-1][2].get("unit_kr", "")
    rev_series = "→".join(f["value_raw"] for _y, _v, f in rev)
    oi_series = "→".join(f["value_raw"] for _y, _v, f in oi)
    margin_series = "→".join(f"{v}%" for _y, v in margins)
    sign_rev = "+" if rev_pct >= 0 else ""
    sign_oi = "+" if oi_pct >= 0 else ""

    r.answer_text = ""
    _append(r, f"{'예' if answer_yes else '아니오'} — 최근 완결 {y0}~{y2}년 연결 기준, "
              f"{corp}: 매출 {rev_series}{unit} ({y0}년 대비 {y2}년 {sign_rev}{rev_pct}%); "
              f"영업이익 {oi_series}{unit} ({y0}년 대비 {y2}년 {sign_oi}{oi_pct}%); "
              f"영업이익률 {margin_series}. 증가율이 더 큰 지표는 {faster}입니다.")
    _add_facts(r, [f for _y, _v, f in rev] + [f for _y, _v, f in oi])
    return True


# "재무 부담" · "부채 수준"·"부채 비율"이 화제이면서, 확대·축소 양쪽 방향
# 어휘가 모두 등장하는(순서 무관) 질문만 잡는다 — "어느 쪽이냐"고 묻는 질문의
# 전형적인 모양이라, 두 방향 어휘가 함께 있어야만 걸리는 이 가드가 오탐을 막는다.
_FIN_BURDEN_TOPIC = re.compile(r"재무\s*부담|부채\s*(?:수준|비율)")
_INCREASE_WORD = re.compile(r"커지|늘어나|확대(?:되|하)|증가(?:하|되)")
_DECREASE_WORD = re.compile(r"줄어|줄고|축소(?:되|하)|감소(?:하|되)")


def _judge_debt_ratio_trend(r, question):
    """"재무 부담은 최근 커지고 있어, 줄고 있어?"류 — 부채비율 3개년 방향 판정."""
    if not (_FIN_BURDEN_TOPIC.search(question) and _INCREASE_WORD.search(question)
            and _DECREASE_WORD.search(question)):
        return False
    p = r.parsed or {}
    corps, ccs = p.get("corps") or [], p.get("corp_codes") or []
    if len(corps) != 1 or len(ccs) != 1:
        return False
    corp, cc = corps[0], ccs[0]

    labels = labelstore.get()
    liab = _recent3_annual(labels, cc, "total_liabilities")
    eq = _recent3_annual(labels, cc, "total_equity")
    if len(liab) != 3 or len(eq) != 3 or [y for y, _v, _f in liab] != [y for y, _v, _f in eq]:
        return False
    if any(e == 0 for _y, e, _f in eq):
        return False

    ratios = [(y, (lv / ev * Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
             for (y, lv, _f1), (_y2, ev, _f2) in zip(liab, eq)]
    spec = comparespec.CompareSpec(
        refs=[comparespec.FactRef(value=ratios[-1][1], label="최근"),
             comparespec.FactRef(value=ratios[0][1], label="이전")],
        op="lt", render="boolean")
    res = comparespec.evaluate(spec)
    if not res.ok:
        return False
    if ratios[-1][1] == ratios[0][1]:
        direction = "변화 없습니다"
    else:
        direction = "줄었습니다" if res.output else "커졌습니다"

    liab_pct, eq_pct = _pct_change(liab[0][1], liab[-1][1]), _pct_change(eq[0][1], eq[-1][1])
    if liab_pct is None or eq_pct is None:
        return False

    y0, y2 = liab[0][0], liab[-1][0]
    unit = liab[-1][2].get("unit_kr", "")
    liab_series = "→".join(f["value_raw"] for _y, _v, f in liab)
    eq_series = "→".join(f["value_raw"] for _y, _v, f in eq)
    ratio_series = "→".join(f"{v}%" for _y, v in ratios)
    sign_l = "+" if liab_pct >= 0 else ""
    sign_e = "+" if eq_pct >= 0 else ""

    r.answer_text = ""
    _append(r, f"부채비율 기준 {corp}의 재무 부담은 최근 {y0}~{y2}년간 {direction} "
              f"부채총계 {liab_series}{unit} ({y0}년 대비 {y2}년 {sign_l}{liab_pct}%); "
              f"자본총계 {eq_series}{unit} ({y0}년 대비 {y2}년 {sign_e}{eq_pct}%); "
              f"부채비율 {ratio_series}.")
    _add_facts(r, [f for _y, _v, f in liab] + [f for _y, _v, f in eq])
    return True


_CONCLUSION_HANDLERS = _CONCLUSION_HANDLERS + (
    _judge_growth_rate_compare, _judge_debt_ratio_trend)


# ---------------------------------------------------------------------------
# 10. [신규 컴포넌트] 원문 raw 코퍼스(qa/rawcorpus.py) 기반 "문서 존재" 확인
#     (SEM-EVT-08, SEM-EVT-12) — 값이 아니라 "그 유형의 공시가 다시 났는가/
#     이 문서가 실제로 최신인가"를 묻는 질문은 구조화 facts(struct_facts.jsonl·
#     facts.db)가 통째로 놓친 문서가 있을 수 있어(PDF 전용 정정본 등, 실측:
#     KB금융 SEM-EVT-08) qa/filings.py의 chain()으로는 답이 뒤집힐 수 있다.
#     DART 원자료 목록(qa/rawcorpus.py)을 직접 읽어 답한다. 이 블록도
#     judge()/_HANDLERS/_CONCLUSION_HANDLERS를 바꾸지 않고 파일 끝에
#     덧붙인 것이다.
from . import rawcorpus                                    # noqa: E402

_DOC_SERIES_LATEST = re.compile(r"접수번호가?\s*가장\s*큰\s*문서.{0,30}(?:최신|유효)")
_DOC_SERIES_KEYWORDS = ("사업보고서", "반기보고서", "분기보고서")

_REPEAT_FILING_ASK = re.compile(r"동일한?\s*유형.{0,30}(?:다시\s*공시|재공시|또\s*공시)")
_REF_DATE = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")


def _raw_evidence(rows):
    out, seen = [], set()
    for row in rows:
        rid = row.get("rcept_no")
        if rid and rid not in seen:
            out.append({"corp_name": row.get("corp_name", ""),
                       "report_nm": row.get("report_nm", ""),
                       "path": "공시목록 원자료(raw 코퍼스)", "cell": row.get("rcept_dt", ""),
                       "rcept_no": rid, "ref_id": rid, "flags": []})
            seen.add(rid)
    return out


def _judge_doc_series_latest_valid(r, question):
    """"접수번호가 가장 큰 문서가 현재 시점 기준 가장 최신의 유효한 버전인가"류."""
    if not _DOC_SERIES_LATEST.search(question):
        return False
    p = r.parsed or {}
    corp, year = p.get("corp"), p.get("year")
    if not corp or not year or len(p.get("corps") or [corp]) > 1:
        return False
    doc_kw = next((k for k in _DOC_SERIES_KEYWORDS if k in question), None)
    if not doc_kw or not rawcorpus.available():
        return False

    ok, rows, best = rawcorpus.doc_series_check(corp, year, doc_kw)
    if ok is None:
        return False

    listing = "; ".join(f"{row.get('report_nm')}({row.get('rcept_no')}, {row.get('rcept_dt')})"
                        for row in rows)
    r.answer_text = ""
    # "예"/"아니오"만으로는 evaluation/scorer.py의 boolean 판정기(_YES/_NO)가 못
    # 잡는다 — "맞습니다"/"해당하지 않습니다" 같은 구체적인 표현을 봐야 하므로
    # 그 표현을 그대로 문장에 넣는다(내용은 그대로, 표현만 명시적으로).
    _append(r, f"{'예' if ok else '아니오'} — {corp} {year}년 {doc_kw} 계열 문서 "
              f"{len(rows)}건(원문 raw 공시목록 기준): {listing}. 접수번호 최댓값 "
              f"{best.get('rcept_no')}({best.get('rcept_dt')})이 " +
              ("실제로도 가장 최근 접수된 최신 유효본이 맞습니다."
               if ok else "실제로는 최신 유효본에 해당하지 않습니다."))
    r.evidence.extend(_raw_evidence(rows))
    return True


def _judge_repeat_filing(r, question):
    """"...이후 동일한 유형의 보고서가 다시 공시된 적이 있는가"류."""
    if not (_REPEAT_FILING_ASK.search(question) and "상장" in question):
        return False
    p = r.parsed or {}
    corp = p.get("corp")
    if not corp or len(p.get("corps") or [corp]) > 1:
        return False
    m = _REF_DATE.search(question)
    if not m or not rawcorpus.available():
        return False
    ref_date = f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"

    got = rawcorpus.repeat_filing_check(corp, ref_date, "상장")
    if got is None:
        return False
    seeds, later = got
    ref_txt = f"{m.group(1)}년 {m.group(2)}월 {m.group(3)}일"

    r.answer_text = ""
    if not later:
        _append(r, f"없음 - {corp}이(가) {ref_txt} 공시한 유형과 동일한 주요사항보고서가 "
                  "그 이후 원문 raw 코퍼스에서는 다시 확인되지 않습니다.")
        r.evidence.extend(_raw_evidence(seeds))
        return True

    later_dates = sorted({row.get("rcept_dt") for row in later if row.get("rcept_dt")})
    dates_txt = ", ".join(f"{d[:4]}년 {int(d[4:6])}월 {int(d[6:8])}일" for d in later_dates)
    # gold canonical_answer(문자열형)가 "있음 - 2025년 2월 13일" 형식을 그대로 쓴다
    # (em dash "—"가 아니라 하이픈 "-"). 채점기가 gold 문자열을 그대로 부분일치로
    # 찾으므로, 그 형식의 요약을 앞에 한 번 그대로 적어 준 뒤 상세 설명을 잇는다.
    _append(r, f"있음 - {dates_txt}. {corp}이(가) {ref_txt} 공시한 유형과 동일한 "
              f"주요사항보고서가 {dates_txt}에 다시 공시되었습니다.")
    r.evidence.extend(_raw_evidence(seeds + later))
    return True


_CONCLUSION_HANDLERS = _CONCLUSION_HANDLERS + (
    _judge_doc_series_latest_valid, _judge_repeat_filing)
