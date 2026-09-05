"""문항 채점 — 정확도 / 근거 좌표 / 오류 단계.

LLM-judge를 쓰지 않는다. 숫자는 Decimal로 비교하고, 비교 규칙을 세울 수 없는
답 타입(문자열·순위·서술 등)은 억지로 채점하지 않고 `➖`(채점 대상 아님)로 둔다.
정확도 분모에서도 빠진다 — 틀린 규칙으로 매긴 점수보다 "못 잰다"가 정확하다.
"""

import re
from decimal import Decimal, InvalidOperation

# 값 비교 규칙을 세울 수 있는 답 타입 (ranking은 목록 비교라 따로 다룬다)
# 답을 내지 못한 상태. 문장은 나가지만 질문에 답한 것은 아니다.
REFUSED_STATES = {"S1", "S3", "S6"}

_SPACE = re.compile(r"\s+")

NUMERIC_TYPES = {
    "currency", "percentage", "number", "ratio",
    "count", "percentage_point", "percentage_change",
}

TOLERANCE = Decimal("0.005")        # 상대오차 0.5%
_NUM = re.compile(r"-?[\d,]+(?:\.\d+)?")
_RCEPT_ANY = re.compile(r"(?<!\d)(\d{14})(?!\d)")   # DART 접수번호 14자리


def _dec(x):
    try:
        return Decimal(str(x).replace(",", "").rstrip("%"))
    except (InvalidOperation, ValueError, AttributeError):
        return None


def candidates(result):
    """답변에서 뽑아낸 비교 후보값들.

    ① 근거 fact의 원(KRW) 환산값 — 골드셋의 canonical value가 원 단위라 이게 1순위
    ② numqa가 낸 numbers
    ③ 답변 텍스트에 등장한 모든 숫자
    """
    out = []
    for f in result.facts:
        v, sc = _dec(f.get("value_decimal")), _dec(f.get("scale") or 1)
        if v is not None and sc is not None:
            out.append(v * sc)
            out.append(v)
    for n in result.numbers:
        d = _dec(n)
        if d is not None:
            out.append(d)
    for m in _NUM.finditer(result.answer_text or ""):
        d = _dec(m.group())
        if d is not None:
            out.append(d)
    return out


def score_accuracy(rec, result):
    """('✅'|'❌'|'➖', 설명).

    자료형별로 결정론적 비교가 가능한 것만 채점한다. gold가 예상한 모양이 아니면
    조용히 `➖`로 돌아간다 — 채점기가 특정 골드셋 스키마에 묶이지 않게 하려는 것이다.
    """
    # 근거 부족이 정답인 질문을 먼저 본다 — 그 경우엔 답을 안 낸 것이 맞다.
    got = _score_sentinel(rec, result)
    if got:
        return got

    # 답을 내지 못했으면 자료형과 무관하게 오답이다.
    #
    # 이 검사가 예전에는 숫자 유형 분기 안에만 있었다. 그래서 정답이 boolean·date면
    # 답을 못 내도 '➖ 채점 규칙 없음'으로 분모에서 빠졌다 — 실패가 측정에서 지워진
    # 것이다. qa_gold_final 146문항 중 63건이 그렇게 사라졌다.
    #
    # 순서도 중요하다. 이제 에이전트가 "2020년 자료는 없습니다 (보유 2021~2025년)"
    # 처럼 답하므로, 아래 숫자 추출기에 그대로 흘리면 2021·2025를 정답 후보로
    # 집어 우연히 ✅가 날 수 있다. 그 전에 끊는다.
    if result.state in REFUSED_STATES:
        return "❌", f"답을 내지 못함 (상태 {result.state})"

    if rec["gold_type"] == "ranking":
        return _score_ranking(rec, result)
    for fn in (_score_conclusion, _score_series, _score_paired, _score_typed):
        got = fn(rec, result)
        if got:
            return got
    if rec["gold_type"] not in NUMERIC_TYPES:
        return "➖", f"채점 규칙 없음 (type={rec['gold_type']})"
    gold = _dec(rec["gold_value"])
    if gold is None:
        return "➖", f"gold_value가 스칼라가 아님 ({type(rec['gold_value']).__name__})"
    if not result.answer_text:
        return "❌", "답변 없음"
    for c in candidates(result):
        if gold == 0:
            if c == 0:
                return "✅", f"{c} == {gold}"
        elif abs(c - gold) / abs(gold) <= TOLERANCE:
            return "✅", f"{c} ≈ {gold}"
    return "❌", f"기대 {gold} / 후보에 없음"


_RANK_SEP = re.compile(r"\s*>\s*")


def _score_ranking(rec, result):
    """순위는 기업명 순서를 그대로 비교한다 — LLM 없이 결정론적으로 채점된다."""
    gold = rec["gold_value"]
    if isinstance(gold, str) and ">" in gold:
        # gold가 리스트가 아니라 "2024년 > 2023년 > 2022년" 같은 한 줄 문자열로
        # 온 경우다(실측: GOLD-W1-HS-08) — 순서 구분자로 쪼개면 리스트와 똑같이
        # 채점할 수 있다. ">"가 없는 일반 문자열은 건드리지 않는다.
        gold = [g.strip() for g in _RANK_SEP.split(gold) if g.strip()]
    if not isinstance(gold, list) or not gold or not all(isinstance(g, str) for g in gold):
        # gold가 {회사:값} dict를 원소로 갖는 회사×연도 flatten 같은 형태면
        # (예: 현대건설·대우건설을 3개년 함께 한 줄로 늘어놓은 순위) 문자열 순위
        # 비교로는 채점 규칙을 세울 수 없다 — 억지로 set()에 넣으면
        # TypeError: unhashable type: 'dict'로 평가 전체가 죽는다. 정직하게
        # 채점 불가로 남긴다(평가 스크립트가 죽는 것보다 낫다).
        return "➖", "gold가 순위 목록이 아님"
    got = list(result.ranking or [])
    if not got:
        return "❌", "순위를 산출하지 못함"
    if got[:len(gold)] == gold:
        return "✅", f"{' > '.join(gold)}"
    if set(got) == set(gold):
        return "❌", f"구성은 같으나 순서가 다름: {' > '.join(got)}"
    return "❌", f"기대 {' > '.join(map(str, gold))} / 실제 {' > '.join(got) or '없음'}"


# ---------------------------------------------------------------------------
# 행동 축 — "무엇을 답했는가"가 아니라 "답했어야 했는가"
# ---------------------------------------------------------------------------
# 파이프라인 상태를 행동으로 읽는다.
BEHAVIOR_OF_STATE = {
    "S0": "answer",                    # 답했다
    "S2": "answer_with_qualifier",     # 답하되 신뢰도 경고를 붙였다
    "S1": "refuse_out_of_corpus",      # corpus에 없다고 했다
    "S3": "ask_back",                  # 되물었다
    "S6": "unsupported",               # 아직 못 한다고 했다
}

# (기대 행동, 실제 행동) → 판정
#   ✅ 기대와 일치      🟡 해롭진 않으나 미흡      ❌ 틀린 행동
#
# `correct_premise`와 `partial_refuse`가 ✅를 못 받는 것은 의도한 것이다.
# 전제를 지적하거나 일부만 거절하는 기능이 아예 없다. 능력 공백은 지표에 공백으로
# 남는 편이 정확하다 — 비슷한 상태를 ✅로 쳐주면 없는 기능이 있는 것처럼 보인다.
_RUBRIC = {
    "answer":                {"answer": "✅", "answer_with_qualifier": "🟡"},
    "answer_with_qualifier": {"answer_with_qualifier": "✅", "answer": "🟡"},
    "refuse_out_of_corpus":  {"refuse_out_of_corpus": "✅", "ask_back": "🟡",
                              "unsupported": "🟡"},
    "ask_back":              {"ask_back": "✅", "refuse_out_of_corpus": "🟡",
                              "unsupported": "🟡"},
    "correct_premise":       {"refuse_out_of_corpus": "🟡", "ask_back": "🟡",
                              "unsupported": "🟡"},
    "partial_refuse":        {"answer_with_qualifier": "🟡", "refuse_out_of_corpus": "🟡",
                              "ask_back": "🟡", "unsupported": "🟡"},
}

BEHAVIOR_KO = {
    "answer": "답변", "answer_with_qualifier": "답변+단서",
    "refuse_out_of_corpus": "거절(없음)", "ask_back": "되물음",
    "unsupported": "미지원", "correct_premise": "전제지적",
    "partial_refuse": "부분거절",
}


def score_behavior(rec, result):
    """('✅'|'🟡'|'❌', 설명). 내용 정확도와 독립된 축이다.

    수치 비교가 안 되는 문항도 전부 채점된다 — 되물어야 할 때 되물었는지,
    거절해야 할 때 거절했는지는 답의 자료형과 무관하기 때문이다.
    """
    expected = rec.get("expected_behavior") or "answer"
    actual = BEHAVIOR_OF_STATE.get(result.state, "unsupported")
    mark = _RUBRIC.get(expected, {}).get(actual, "❌")
    return mark, f"기대 {BEHAVIOR_KO.get(expected, expected)} / 실제 {BEHAVIOR_KO.get(actual, actual)}"


def _score_conclusion(rec, result):
    """비교 결론 — "A와 B 중 어디가 큰가". gold는 기업명 문자열이다.

    우리는 순위를 산출하므로 1위와 대조하면 된다.
    """
    if rec["gold_type"] != "comparison_conclusion":
        return None
    gold = rec["gold_value"]
    if isinstance(gold, bool):
        # comparison_conclusion인데 결론 자체가 예/아니오인 경우다(실측:
        # GOLD-W1-CJ-10) — _score_typed의 boolean 분기와 같은 규칙을 쓴다.
        text = result.answer_text or ""
        yes, no = bool(_YES.search(text)), bool(_NO.search(text))
        if yes == no:
            return "❌", "예/아니오를 밝히지 않았음"
        return ("✅", f"판정 일치 ({gold})") if (yes == gold) else \
               ("❌", f"기대 {gold} / 답변은 {'예' if yes else '아니오'}")
    if not isinstance(gold, str) or not gold.strip():
        return None
    top = (result.ranking or [None])[0]
    if top:
        return ("✅", f"1위 {top}") if top == gold.strip() else ("❌", f"기대 {gold} / 실제 {top}")
    # 순위를 못 냈으면 답변 문장에 기업명이 있는지라도 본다
    if gold.strip() in (result.answer_text or ""):
        return "✅", f"답변에 '{gold}' 포함"
    return "❌", f"기대 {gold} / 순위 산출 실패"


def _score_series(rec, result):
    """연도별 추이 — gold `{"series": {연도: 값}, "cagr_pct": n}`.

    우리는 `result.series`(연도별 fact)와 `result.cagr`를 낸다. 단위가 다르므로
    원(KRW)으로 환산해 비교한다.
    """
    gold = rec["gold_value"]
    if not isinstance(gold, dict) or "series" not in gold:
        return None
    want = {str(k): _dec(v) for k, v in (gold["series"] or {}).items()}
    if not want or not result.series:
        return "❌", f"기대 {len(want)}개 연도 / 산출 {len(result.series)}개"
    from decimal import Decimal
    got = {}
    for y, f in result.series:
        try:
            got[str(y)] = Decimal(f["value_decimal"]) * Decimal(f.get("scale") or 1)
        except Exception:                                  # noqa: BLE001
            pass
    miss = [y for y in want if y not in got]
    bad = [y for y in want if y in got and want[y] and
           abs(got[y] - want[y]) / abs(want[y]) > TOLERANCE]
    if miss or bad:
        return "❌", (f"결측 {miss} " if miss else "") + (f"불일치 {bad}" if bad else "")
    if gold.get("cagr_pct") is not None and result.cagr is not None:
        g = _dec(gold["cagr_pct"])
        if g is not None and abs(_dec(result.cagr) - g) > Decimal("0.3"):
            return "❌", f"시리즈 일치, CAGR 기대 {g} / 실제 {result.cagr}"
    return "✅", f"{len(want)}개 연도 일치" + (f" · CAGR {result.cagr}%" if result.cagr else "")


def _score_paired(rec, result):
    """증감폭 — gold `{"delta": n, "rate_pct": n}`. 변화율로 대조한다.

    부호는 보지 않는다. 질문이 "감소했는데 감소폭은?"이라고 전제해도 실제로는
    증가인 경우가 있어(전제 오류), gold도 크기만 담고 있다.
    """
    gold = rec["gold_value"]
    if not isinstance(gold, dict) or "rate_pct" not in gold:
        return None
    g = _dec(gold["rate_pct"])
    if g is None:
        return None
    import re as _re
    m = _re.search(r"(-?\d+(?:\.\d+)?)\s*%", result.answer_text or "")
    if not m:
        return "❌", f"기대 {g}% / 변화율 산출 실패"
    got = _dec(m.group(1))
    ok = got is not None and abs(abs(got) - abs(g)) <= Decimal("0.15")
    return ("✅", f"{got}% ≈ {g}%") if ok else ("❌", f"기대 {g}% / 실제 {got}%")


# 정답이 값이 아니라 **제어 토큰**인 문항. `INSUFFICIENT_EVIDENCE`처럼
# 대문자와 밑줄로만 이루어진다.
_SENTINEL = re.compile(r"^[A-Z][A-Z0-9_]{4,}$")
# 업종 부적합을 밝혔는지 확인할 문구
_NA_SAID = re.compile(r"적합한\s*지표가\s*아닙니다|해당하지\s*않|산출할\s*수\s*없|보고하지\s*않")


def _score_sentinel(rec, result):
    """제어 토큰 정답을 하나의 규칙으로 처리한다.

    ## 왜 하나로 묶나

    `INSUFFICIENT_EVIDENCE`·`CLARIFICATION_REQUIRED`·`METRIC_NOT_APPLICABLE`을
    각각 리터럴로 처리하고 있었다. 그러다 `NEED_CLARIFICATION`이 나오자 그것만
    오답이 됐다 — **같은 뜻인데 철자가 다르다는 이유로.**

    토큰을 열거하는 대신 **뜻을 담은 낱말**로 가른다. 앞으로 나올 다른 철자
    (`AMBIGUOUS`·`NO_DATA`·`UNANSWERABLE` …)도 함께 덮인다.

    ## 세 갈래는 서로 다르다

    - **되물음** — 자료는 있는데 질문이 안 좁혀졌다. S3가 정답이다.
    - **근거 없음** — 자료 자체가 없다. S1/S3/S6 어느 쪽이든 "없다"고 밝히면 된다.
    - **지표 부적합** — 그 업종에 맞지 않는 지표다. 그렇게 **말했는지**를 본다
      (상태가 아니라 문장을 본다 — 부적합 판정은 S0로 나가기 때문이다).
    """
    gold = rec["gold_value"]
    if not isinstance(gold, str) or not _SENTINEL.match(gold.strip()):
        return None
    g = gold.strip()
    text = result.answer_text or ""

    if "NOT_APPLICABLE" in g or "INAPPLICABLE" in g:
        return (("✅", "업종 부적합을 밝힘") if _NA_SAID.search(text)
                else ("❌", "업종에 맞지 않는 지표인데 그렇게 밝히지 않았음"))

    if any(w in g for w in ("CLARIF", "AMBIG", "NEED_")):
        return (("✅", f"되물음으로 답함 (상태 {result.state})") if result.state == "S3"
                else ("❌", f"되물어야 하는데 {result.state}로 답했음"))

    if any(w in g for w in ("INSUFFICIENT", "NO_DATA", "UNANSWER", "UNKNOWN",
                            "NOT_FOUND", "NO_EVIDENCE")):
        ok = result.state in ("S1", "S3", "S6")
        return (("✅", f"근거 없음을 밝힘 (상태 {result.state})") if ok
                else ("❌", f"근거가 없는데 답을 냈음 (상태 {result.state})"))

    return None                       # 뜻을 모르는 토큰은 추측하지 않는다


_DATE = re.compile(r"(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})")
# 예/아니오를 실제로 표명한 문구만 인정한다. 없으면 답한 것으로 치지 않는다.
_YES = re.compile(r"정정되었습니다|있습니다|맞습니다|그렇습니다|해당합니다|존재합니다|되었습니다")
_NO = re.compile(r"없습니다|아닙니다|않았습니다|해당하지\s*않|정정된 적이 없")
# 짧은 문자열만 부분일치로 본다. 긴 서술형 정답은 글자 비교로 판정할 수 없다.
MAX_STR = 24

# 정답 dict의 키 중 **답에 담을 내용이 아닌 것**. 무엇을 하지 말아야 하는지를
# 적어 둔 항목이라 답변에 그 글자가 없는 것이 정상이다.
_META_KEYS = ("refused", "refuse", "should_refuse", "거부", "note", "comment",
              "reason", "caveat")


def _dates(text):
    out = set()
    for m in _DATE.finditer(text or ""):
        out.add(f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}")
    return out


def _gold_numbers(gv):
    """정답이 dict·list여도 그 안의 숫자를 모두 꺼낸다."""
    out = []
    if isinstance(gv, dict):
        for v in gv.values():
            out += _gold_numbers(v)
    elif isinstance(gv, list):
        for v in gv:
            out += _gold_numbers(v)
    else:
        d = _dec(gv)
        if d is not None:
            out.append(d)
    return out


def _gold_strings(gv, key=None):
    """정답 안의 짧은 문자열 값. dict형 정답을 숫자만으로 판정하면 안 된다.

    "계약상대방 표기가 어떻게 달라졌는가"의 정답에는 계약금액도 함께 들어 있어,
    숫자만 보면 그 하나가 우연히 맞아 통과한다. 계약상대방을 전혀 답하지 않았는데도.
    """
    if key and any(m in str(key).lower() for m in _META_KEYS):
        return []
    out = []
    if isinstance(gv, dict):
        for k, v in gv.items():
            out += _gold_strings(v, k)
    elif isinstance(gv, list):
        for v in gv:
            out += _gold_strings(v, key)
    elif isinstance(gv, str) and _dec(gv) is None:
        t = _SPACE.sub("", gv)
        if 2 <= len(t) <= MAX_STR:
            out.append(t)
    return out


def _has_all(nums, result):
    cands = candidates(result)
    for g in nums:
        if not any((c == g) if g == 0 else (abs(c - g) / abs(g) <= TOLERANCE)
                   for c in cands):
            return False
    return bool(nums)


def _score_typed(rec, result):
    """숫자가 아닌 정답을 자료형별로 본다.

    **오탐을 만들지 않는다.** 판정이 애매하면 ➖를 유지한다. 다만 정답 유형이
    요구하는 형태의 답을 아예 내지 않았으면 ❌로 센다.
    """
    t, gv = rec["gold_type"], rec["gold_value"]
    text = result.answer_text or ""
    if not text.strip():
        return None

    if t == "date":
        want = _dates(str(gv))
        if not want:
            return None
        return ("✅", f"날짜 일치 {sorted(want)[0]}") if want & _dates(text) else \
               ("❌", f"기대 {sorted(want)[0]} / 답변에 없음")

    if t == "boolean" and isinstance(gv, bool):
        yes, no = bool(_YES.search(text)), bool(_NO.search(text))
        if yes == no:
            return "❌", "예/아니오를 밝히지 않았음"
        return ("✅", f"판정 일치 ({gv})") if (yes == gv) else \
               ("❌", f"기대 {gv} / 답변은 {'예' if yes else '아니오'}")

    if t in ("string", "categorical", "comparison", "text"):
        if not isinstance(gv, str):
            return None
        want = _SPACE.sub("", gv)
        if not want or len(want) > MAX_STR:
            return None
        return ("✅", f"'{gv}' 포함") if want in _SPACE.sub("", text) else \
               ("❌", f"기대 '{gv}' / 답변에 없음")

    if t in ("structured", "structured_summary", "paired_value", "list", "table",
             "multi_metric_trend", "evidence_citation"):
        nums, strs = _gold_numbers(gv), _gold_strings(gv)
        if t == "evidence_citation" and isinstance(gv, str):
            # "[기재정정]사업보고서(접수번호 20251017000151)"처럼 gold 문자열
            # 전체가 MAX_STR(24자)를 넘어 _gold_strings가 통째로 버리는 경우다
            # (실측: GOLD-W1-HDC-06). 안에 박힌 14자리 접수번호는 그 자체로
            # 답을 가르는 핵심 식별자이니, 길이 제한과 무관하게 숫자 후보로
            # 따로 뽑는다 — candidates()가 답변 텍스트의 숫자를 그대로 추출하므로
            # "rcept_no 20251017000151"처럼 적혀 있으면 그대로 걸린다.
            nums = nums + [Decimal(m) for m in _RCEPT_ANY.findall(gv)]
        if not nums and not strs:
            return None
        body = _SPACE.sub("", text)
        # [2026-09-05] "2023 사업연도(제55기)"처럼 회계연도 문자열은 흔히
        # "연도 + 기수 괄호"로 저작된다. 우리 답변은 내용은 맞아도 "2023년"
        # 처럼 연도만 말하지 "(제55기)" 같은 기수 표기는 안 쓴다(실측:
        # GOLD-W1-SEC-05 — 값·근거 다 맞는데 이 리터럴만 안 걸려 ❌). 문자열
        # 전체 일치가 실패하면, 그 문자열이 "YYYY년"/"YYYY 사업연도"로
        # 시작할 때만 "YYYY년"이 답변에 있는지로 한 번 더 봐준다 — 이 대체
        # 판정은 그 좁은 모양일 때만 적용되어, 값이 실제로 다른 답은 여전히
        # ❌로 남는다(느슨해진 게 아니라 표기 차이만 흡수).
        def _str_ok(x):
            if x in body:
                return True
            m = re.match(r"(\d{4})(?:년|사업연도)", x)
            return bool(m and f"{m.group(1)}년" in body)
        miss_s = [x for x in strs if not _str_ok(x)]
        if nums and not _has_all(nums, result):
            return "❌", f"정답 수치 {len(nums)}개 중 일부가 답변에 없음"
        if miss_s:
            return "❌", f"정답 항목 '{miss_s[0]}'이(가) 답변에 없음"
        return "✅", f"정답 수치 {len(nums)}개 · 항목 {len(strs)}개 모두 포함"

    return None


def score_evidence(rec, result):
    """('✅'|'🟡'|'❌'|'➖', 설명). fact_id가 있으면 fact 단위로, 없으면 rcept 단위로 본다."""
    got_f = {e["ref_id"] for e in result.evidence if e.get("ref_id")}
    got_r = {e["rcept_no"] for e in result.evidence if e.get("rcept_no")}
    for gold, got, unit in ((set(rec["gold_fact_ids"]), got_f, "fact"),
                            (set(rec["gold_rcepts"]), got_r, "rcept")):
        if not gold:
            continue
        hit = gold & got
        if hit == gold:
            return "✅", f"{unit} {len(hit)}/{len(gold)} 일치"
        if hit:
            return "🟡", f"{unit} {len(hit)}/{len(gold)} 일치"
        return "❌", f"{unit} 0/{len(gold)} 일치"
    return "➖", "정답 좌표 없음"


def error_stage(result, acc, ev):
    """오류가 난 서브에이전트 단계.

    파이프라인이 실제로 실패한 단계가 있으면 그것. 파이프라인은 끝까지 갔는데 답이
    틀렸다면 근거가 맞았는지로 가른다 — 근거가 맞는데 답이 틀리면 계산(03),
    근거부터 틀렸으면 검색(02). 리포트 차트 ③의 판정 규칙과 같다.
    """
    failed = [s.no for s in result.stages if s.status == "실패"]
    if failed:
        return f"[{failed[0]}]"
    if acc == "❌":
        return "[03]" if ev == "✅" else "[02]"
    return "-"


def score(rec, result, elapsed):
    acc, acc_why = score_accuracy(rec, result)
    ev, ev_why = score_evidence(rec, result)
    beh, beh_why = score_behavior(rec, result)
    ver = "➖"
    if result.verification:
        ver = "✅" if result.verification.get("passed") else "⚠️"
    return {
        "qid": rec["qid"],
        "question": rec["question"],
        "kind": rec["kind"],
        "state": result.state,
        "정확": acc, "정확_사유": acc_why,
        "행동": beh, "행동_사유": beh_why,
        "기대행동": rec.get("expected_behavior") or "answer",
        "기대행동_명시": rec.get("expected_behavior_given", False),
        "근거": ev, "근거_사유": ev_why,
        "검증": ver,
        "오류단계": error_stage(result, acc, ev),
        "소요": elapsed,
        "gold": rec["gold_value"],
        "pred": result.answer_text,
        "evidence": result.evidence,
    }
