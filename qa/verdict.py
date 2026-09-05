"""실적 감성판정 — 이미 검증된 수치를 LLM이 해석한다.

## 왜 고정 규칙표가 아닌가

"매출 증가율이 X% 이상이면 개선"처럼 임계치를 하드코딩하면 조합이 늘어날 때마다
규칙을 새로 추가해야 하고, "매출은 줄었지만 의도적 비용 축소로 이익은 늘었다"
같은 미묘한 케이스를 담지 못한다. 대신 이미 검증된 수치(qa/health.py의 현금흐름
패턴·ROE/ROA, 최근 2개년 매출·영업이익·당기순이익)를 그대로 LLM에 주고 그 위에서만
해석하게 한다.

## 경계 — 숫자는 여전히 LLM이 만들지 않는다

이 모듈이 LLM에 넘기는 숫자는 전부 qa/health.py·기존 fact lookup이 이미 계산·
검증한 값이다. LLM은 그 값을 바꾸거나 새 숫자를 더하지 않고, "좋다/나쁘다" 같은
해석 문장만 만든다 — narrative.py가 원문을 읽고 서술하는 것과 같은 경계를,
원문 대신 이미 확정된 수치에 대해 적용한 것이다. 답변은 항상 "해석이며 확정된
사실이 아님"을 명시한다.
"""

import re

from . import concepts, health, narrative

# "좋았나요/나빴나요/괜찮나요/개선됐나요/많은 편인가요 …" — 판단·평가를 구하는
# 문장 형태. 특정 임계치 규칙이 아니라 질문 형태로만 감지한다.
#
# "어때(요)?"는 일부러 뺐다 — "최근 영업이익률 흐름은 어때?"처럼 perf.py가 이미
# 잘 다루는 열린 추이 질문과 겹쳐서, 여기서 가로채면 (a) perf.py가 이미 아는
# 임의 파생비율 조회 능력이 없어 더 나쁜 답이 되고 (b) 회귀가 생긴다(실측:
# 크래프톤 영업이익률 흐름 질문이 S2→S1로 나빠짐). "어때"는 명시적 예/아니오·
# 정도 판단형(좋았나요 등)보다 훨씬 넓은 열린 질문이라 perf.py에 남겨둔다.
_VERDICT = re.compile(
    r"좋았나요|좋은가요|좋아졌나요|나빴나요|나쁜가요|괜찮나요|괜찮은가요|"
    r"개선됐나요|개선되었나요|악화됐나요|악화되었나요|늘었나요|줄었나요|"
    r"많은\s*(?:편|회사)인가요|적은\s*편인가요|충분한\s*편인가요|충분한가요")

SYSTEM = (
    "당신은 재무 데이터를 해석하는 애널리스트입니다.\n"
    "규칙:\n"
    "1. 아래 '참고 수치'에 있는 값만 근거로 판단하십시오. 제공되지 않은 사실을 지어내지 마십시오.\n"
    "2. 제공된 숫자를 다시 계산하거나 바꾸지 말고 그대로 인용하십시오.\n"
    "3. 이것은 확정된 사실이 아니라 해석/의견임을 답변에서 분명히 하십시오.\n"
    "4. 한국어로 3문장 이내로 간결하게 답하십시오."
)

_LABEL = "\n\n(ℹ️ 위 판단은 실측 수치를 바탕으로 한 해석이며, 확정된 평가가 아닙니다.)"

_CONCEPTS = ("매출액", "영업이익", "당기순이익")


def wanted(question, p):
    """이 경로를 원하는 질문인가. 기업 하나가 특정돼야 한다."""
    if not p.get("corp") or len(p.get("corps") or []) > 1:
        return False
    return bool(_VERDICT.search(question))


def _recent_trend(labels, ci, corp_code, concept, scope):
    """(연도, 원화환산값(증감률 계산용), fact) — 표시는 fact의 value_raw/unit_kr을
    쓴다. _num()이 돌려주는 값은 scale이 이미 곱해진 '원' 단위라, 그 값에
    unit_kr(원래 보고 단위, 예 '백만원')을 그대로 붙이면 자릿수가 100만 배
    부풀려 보인다 — 증감률 계산에만 쓰고 화면엔 원래 표기(value_raw+unit_kr)를 쓴다.
    """
    years = sorted(health._years_any(labels, ci, corp_code, concept, scope))[-2:]
    out = []
    for y in years:
        f = health._lookup_any(labels, ci, corp_code, concept, scope, y)
        v = health._num(f)
        if f and v is not None:
            out.append((y, v, f))
    return out


def _facts_text(labels, corp, corp_code, scope):
    """이미 검증된 수치를 사람이 읽을 문장으로 모은다. LLM엔 이 텍스트만 준다."""
    ci = concepts.get(labels.facts)
    lines = []

    for concept in _CONCEPTS:
        trend = _recent_trend(labels, ci, corp_code, concept, scope)
        if len(trend) >= 2:
            (y0, v0, f0), (y1, v1, f1) = trend[-2], trend[-1]
            pct = f"{(float(v1 - v0) / float(v0) * 100):+.1f}%" if v0 else "산출불가"
            lines.append(f"{concept}: {y0}년 {f0['value_raw']}{f0.get('unit_kr', '')} → "
                        f"{y1}년 {f1['value_raw']}{f1.get('unit_kr', '')} ({pct})")
        elif len(trend) == 1:
            y0, v0, f0 = trend[0]
            lines.append(f"{concept}: {y0}년 {f0['value_raw']}{f0.get('unit_kr', '')} (비교연도 없음)")

    d = health.diagnose(labels, corp, corp_code, scope)
    if d:
        if d.get("pattern"):
            name, note = d["pattern"]
            lines.append(f"{d['cf_year']}년 현금흐름 패턴: {name} ({note})")
        if d.get("roe") is not None:
            lines.append(f"{d['roe_year']}년 ROE {d['roe']}%"
                         + (f" / ROA {d['roa']}%" if d.get("roa") is not None else ""))

    return "\n".join(lines)


def answer(question, p, labels):
    """(해석 텍스트, 근거로 쓴 수치 텍스트, 사용량) 또는 (None, 사유, None)."""
    corp, cc = p["corp"], p.get("corp_code")
    scope = p.get("scope") or "consolidated"
    if not cc:
        return None, "기업 코드를 확보하지 못함", None
    facts = _facts_text(labels, corp, cc, scope)
    if not facts:
        return None, "해석에 쓸 검증된 수치를 찾지 못함", None
    user = f"참고 수치({corp}):\n{facts}\n\n질문: {question}"
    text, err, usage = narrative.call(SYSTEM, user, max_tokens=250)
    if err:
        return None, err, None
    return text + _LABEL, facts, usage
