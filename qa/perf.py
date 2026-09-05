"""실적 흐름 요약 — 여러 지표를 한 번에 본다.

## 왜 필요한가

정답셋에서 가장 많은 유형(20건, 13.7%)이 이것이다.

    "HD현대일렉트릭은 매출이 커지는 만큼 영업이익도 좋아지고 있어?"
    "포스코홀딩스는 최근 매출 대비 영업이익이 어떻게 변했어?"
    "JYP랑 하이브 중 요즘 본업 수익성이 더 좋은 곳은 어디야?"

한 지표만 답해서는 답이 되지 않는다. 정답은 이런 모양이다.

    {"HD현대일렉트릭_revenue_change_pct": "50.9",
     "HD현대일렉트릭_operating_income_change_pct": "215.8",
     "HD현대일렉트릭_operating_margin_pct": ["11.7", "20.1", "24.4"],
     "faster_growth_metric": "영업이익"}

## 규칙은 corpus에서 확인했다

`최근 3개년`을 잡고 **첫 해 대비 마지막 해** 변화율을 낸다. 영업이익률은 3개년 각각.

    HD현대일렉트릭 2023→2025   매출 +50.9%  영업이익 +215.8%
    영업이익률 [11.7, 20.1, 24.4]

gold와 자릿수까지 맞는다. 지어낸 규칙이 아니라 데이터에서 되짚은 것이다.

## LLM을 쓰지 않는다

"수익성이 좋아지고 있나"는 판단처럼 보이지만, 변화율 부호와 영업이익률 방향으로
결정된다. 숫자를 내는 자리에 LLM을 넣지 않는 이 프로젝트의 원칙을 그대로 지킨다.
"""

import re
from decimal import Decimal, InvalidOperation

SPAN = 3                       # 최근 몇 개년을 볼 것인가
METRICS = [("revenue", "매출액"), ("operating_income", "영업이익"),
           ("net_income", "당기순이익")]

# 실적 전반을 묻는 말. 특정 지표 하나를 콕 집지 않는다.
GENERAL = re.compile(
    r"실적|수익성|본업|재무\\s*상태|경영\\s*성과"
    r"|매출.{0,12}(?:영업이익|영업적자|이익)|영업이익.{0,12}매출"
    r"|매출과\\s*이익|이익\\s*흐름|매출.{0,4}늘.{0,6}(?:이익|적자)")


def wanted(question, p):
    """실적 흐름 요약을 원하는 질문인가.

    지표를 하나로 좁히지 않고 매출·이익을 함께(또는 "수익성"처럼 뭉뚱그려) 묻는
    경우다. 특정 지표 하나를 물었으면(예: "매출액 추이") 기존 시리즈 경로가 맞다.

    "SK텔레콤이랑 KT 중 최근 영업 수익성이 더 높은 곳은?", "매출 성장과 이익 성장이
    같이 가고 있어?"처럼 "추이·흐름" 같은 명시 신호는 없어도 GENERAL이 잡는 표현
    ("수익성", "매출...이익" 결합)이면 성격은 같다 — 신호가 없다고 막으면 ontology가
    "더 높은"을 ranking으로, 또는 "매출"만 fact_numeric으로 좁혀버려서 정작 물은
    "수익성"·"같이 가고 있는지"엔 답하지 못한다. GENERAL 매칭이면 추이 신호 없이도
    통과시킨다.

    다만 "매출총이익이 얼마인가?"처럼 이미 단일 개념으로 정확히 잡힌 질문은 막는다
    (실측 회귀: FIN 12건). GENERAL의 "매출.{0,12}이익" 패턴이 "매출총이익"(단일
    개념, "총"만 사이에 낀 한 단어)에도 걸려서, 이미 정확히 조회 가능한 질문을
    3개년 추이 요약으로 흘려보내고 있었다. concept이 "...이익"/"...적자"로 끝나면
    — 그 자체로 이미 매출·이익을 한 데 묶은 완결된 개념(매출총이익 등)이지,
    "매출"과 "이익"을 별개로 언급한 게 아니다.

    단, 이 접미사 판정은 `p["metric"]`이 없을 때만(=concept이 concepts.py의
    한글 어휘에서 그 자체로 확정된 진짜 복합개념일 때만) 적용한다. 실측 회귀:
    2026-09-04 concept 정규화 수정 이후, "영업이익"·"당기순이익"처럼 numqa의
    단일 metric 추측값(원래 "매출과 영업이익 중 어느 쪽이 더 높아?"류 **다지표**
    질문에서 numqa가 둘 중 하나만 집은 것뿐인데도 우연히 "이익"으로 끝남)까지
    이 조건에 걸려 다지표 비교 질문이 단일지표 답으로 새 버렸다(GOLD-W2B-P02/
    P10/P18). "매출총이익"은 numqa의 8개 metric_key에 없어(ci.match()의 한글
    어휘 경로로만 옴) metric이 비어 있고, "영업이익"류는 metric이 차 있다 —
    이 차이로 진짜 복합개념과 numqa의 단일 추측을 구분한다.
    """
    concept = p.get("concept") or ""
    if not p.get("metric") and (concept.endswith("이익") or concept.endswith("적자")):
        return False
    return bool(GENERAL.search(question))


def _series(store, corp_code, metric, scope="consolidated"):
    out = {}
    for y in range(2015, 2031):
        try:
            f = store.lookup(corp_code, metric, scope, y)
        except Exception:                                  # noqa: BLE001
            f = None
        if f:
            try:
                out[y] = Decimal(f["value_decimal"]) * Decimal(f.get("scale") or 1)
            except (InvalidOperation, ValueError, TypeError):
                pass
    return out


def _pct(a, b):
    if a is None or b is None or a == 0:
        return None
    return float((b - a) / abs(a) * 100)


def summarize(store, corp, corp_code, scope="consolidated"):
    """(요약 dict | None). 숫자는 전부 fact에서 나온다."""
    ser = {k: _series(store, corp_code, k, scope) for k, _ in METRICS}
    years = sorted(set(ser["revenue"]) & set(ser["operating_income"]))
    if len(years) < 2:
        return None
    years = years[-SPAN:]
    out = {"corp": corp, "years": years, "change": {}, "margin": []}
    for k, ko in METRICS:
        s = ser[k]
        if years[0] in s and years[-1] in s:
            out["change"][ko] = _pct(s[years[0]], s[years[-1]])
    for y in years:
        r, o = ser["revenue"].get(y), ser["operating_income"].get(y)
        out["margin"].append(None if not r else round(float(o / r * 100), 1))
    return out


def render(sums):
    """여러 기업이면 결론까지. 한 기업이면 흐름만."""
    lines = []
    for s in sums:
        ys = f"{s['years'][0]}~{s['years'][-1]}년"
        ch = " · ".join(f"{ko} {v:+.1f}%" for ko, v in s["change"].items() if v is not None)
        mg = " → ".join(f"{m}%" for m in s["margin"] if m is not None)
        lines.append(f"{s['corp']}({ys}) {ch}" + (f" · 영업이익률 {mg}" if mg else ""))
    text = " / ".join(lines)

    if len(sums) == 1:
        s = sums[0]
        rev, op = s["change"].get("매출액"), s["change"].get("영업이익")
        if rev is not None and op is not None:
            faster = "영업이익" if op > rev else "매출액"
            text += f". 증가율이 더 높은 쪽은 {faster}입니다"
        mg = [m for m in s["margin"] if m is not None]
        if len(mg) >= 2:
            text += f", 영업이익률은 {'개선' if mg[-1] > mg[0] else '악화'} 추세입니다"
        return text + "."

    # 여러 기업 — 최근 영업이익률이 높은 쪽을 고른다
    cand = [(s["corp"], s["margin"][-1]) for s in sums
            if s["margin"] and s["margin"][-1] is not None]
    if cand:
        best = max(cand, key=lambda x: x[1])
        text += f". 최근 영업이익률이 더 높은 곳은 {best[0]}입니다({best[1]}%)."
    return text


def numbers(sums):
    """채점·검증용 숫자. 변화율과 영업이익률을 그대로 낸다."""
    out = []
    for s in sums:
        out += [f"{v:.1f}" for v in s["change"].values() if v is not None]
        out += [str(m) for m in s["margin"] if m is not None]
    return out
