"""파생 개념 온톨로지 — corpus에 없는 개념을 있는 개념들로 만든다.

지금까지의 온톨로지는 "corpus에 있는 말"의 목록이었다. 여기서 한 겹 올라간다.
`영업이익률`은 어느 재무제표에도 행으로 없지만, `영업이익 ÷ 매출액`으로 만들 수 있다.
개념 그래프에 연산 엣지를 넣는 것이다.

  (영업이익률) ─DERIVES_FROM→ (영업이익, 매출액, ÷×100)

## 규칙은 어디서 오는가 — 정직하게
표기·동의어·업종은 corpus에서 뽑았지만, **파생 규칙은 회계 지식에서 온다.**
corpus가 "부채비율은 부채총계 나누기 자본총계"라고 말해주지는 않는다.

대신 **검증은 corpus로 한다.** `factx_periodic`에 이미 계산된 비율이 215건 있으므로,
공식이 그 값을 재현하는지 대조할 수 있다. 재현하지 못하는 공식은 그 사실을 달고 다닌다.
부채비율은 174건 중 170건(97.7%)을 재현한다.

## 우선순위
corpus에 명시값이 있으면 그것을 쓴다. 없을 때만 계산한다. 둘 다 있으면 04 검증이 대조한다.
"""

import collections
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "data" / "derived_validation.json"

# (연산자, 피연산자 개념들, 단위, 배수, 검증 대상 metric_key)
RULES = {
    "부채비율":     ("÷", ("부채총계", "자본총계"), "%", 100, "debt_ratio"),
    "유동비율":     ("÷", ("유동자산", "유동부채"), "%", 100, "current_ratio"),
    "자기자본비율":  ("÷", ("자본총계", "자산총계"), "%", 100, None),
    "영업이익률":    ("÷", ("영업이익", "매출액"), "%", 100, None),
    "매출총이익률":  ("÷", ("매출총이익", "매출액"), "%", 100, None),
    "순이익률":     ("÷", ("당기순이익", "매출액"), "%", 100, None),
    "매출원가율":    ("÷", ("매출원가", "매출액"), "%", 100, None),
    "재고자산회전율": ("÷", ("매출액", "재고자산"), "회", 1, None),
    "총자산회전율":  ("÷", ("매출액", "자산총계"), "회", 1, None),
    # GOLD-W1-EM-06: "배당에 관한 사항" 표의 현금배당성향. corpus에 "주당배당금"
    # 항목이 없어(현금흐름표의 "배당금지급"만 있음) 그것으로 대신한다. 현금흐름표의
    # 배당금지급은 유출이라 음수로 저장되므로 compute()에서 절대값을 취한다 —
    # 당기순이익이 적자인 해에는 배당성향이 음수가 되는 게 맞다(배당은 항상
    # 양수 지급액, 분모만 부호를 가진다).
    "현금배당성향":  ("÷", ("배당금지급", "당기순이익"), "%", 100, None),
}

# 질문에 나타나는 표기 변형 → 규칙 이름
ALIASES = {
    "부채 비율": "부채비율", "유동 비율": "유동비율",
    "영업이익 마진": "영업이익률", "영업 이익률": "영업이익률",
    "자기자본 비율": "자기자본비율", "매출 총이익률": "매출총이익률",
    "순이익 마진": "순이익률", "재고자산 회전율": "재고자산회전율",
    # 구어체 복합 표현. "수익성"은 corpus에도 concepts._RATIO_TAIL에도 없어
    # 그냥 두면 "영업수익"(매출액의 동의어) 같은 절대액 지표로 잘못 잡힌다.
    # "본업 수익성"·"영업 수익성"·"수익성"은 실무에서 전부 영업이익률을 가리킨다.
    "수익성": "영업이익률", "본업 수익성": "영업이익률",
    "현금 배당성향": "현금배당성향",
    # 사람은 "현금"을 안 붙이고 그냥 "배당성향"이라고 묻는다("배당성향을
    # 계산해줘") — 규칙 이름 자체는 "현금배당성향"뿐이라 이 별칭이 없으면
    # find()가 아예 못 찾아 narrative 경로로 새 나갔다(실측: 삼성전자 배당성향
    # 질문이 엉뚱한 사업개요 문단을 근거로 "정보 없음" 답변을 냄).
    "배당성향": "현금배당성향",
    # 2026-09-04 튜닝 데이터(rule_concept_abbrevs) → 사전 이관. "매출총익률"은
    # 등록 전엔 derived.find()가 못 잡아 concept이 엉뚱하게 "매출액"으로 새는
    # 실제 버그였다(부분 문자열 "매출"이 numqa 쪽에서 먼저 잡힘) — 이 등록이
    # 그 버그도 같이 고친다.
    "부채율": "부채비율", "유동비": "유동비율", "자기자본비": "자기자본비율",
    "영업이익율": "영업이익률", "매출총익률": "매출총이익률",
}

_VALIDATION = None


def normalize(term):
    t = ALIASES.get(term, term)
    return t if t in RULES else None


def find(question, trace=None):
    """질문에서 파생 개념을 찾는다. 긴 표기부터.

    규칙기반이 실패하면(ALIASES에 없는 캐주얼한 표현 — "재무 건전성", "이 회사
    남는 장사야?" 등) 폐집합 LLM 폴백(resolve.py)으로 넘긴다. RULES 10개뿐이라
    후보 목록이 작아 통째로 넘겨도 된다 — 이 목록 밖 이름은 절대 못 고른다.

    trace: 있으면(dict) 어느 경로로 찾았는지만 옆에 적어준다 — 매칭 로직 자체는
    바꾸지 않는다. qa/ontology.py의 슬롯 출처 태그(p["_source"]) 용도.
    """
    qn = question.replace(" ", "")
    for name in sorted(set(RULES) | set(ALIASES), key=len, reverse=True):
        if name.replace(" ", "") in qn:
            if trace is not None:
                trace["source"] = "utterance"
            return normalize(name)
    from . import resolve
    picked = resolve.resolve(question, sorted(RULES),
                             "재무비율(전부 %나 배수로 표현되는 비율 — 절대 금액이나 "
                             "보유 현황을 묻는 질문은 해당 없음)", "derived")
    if trace is not None:
        trace["source"] = "resolve_llm" if picked else None
    return picked


def compute(name, values):
    """피연산자 값(Decimal)들로 파생값을 계산한다."""
    op, _, unit, mult, _ = RULES[name]
    if any(v is None for v in values):
        return None
    a, b = values
    if name == "현금배당성향":
        a = abs(a)                # 현금흐름표의 배당금지급은 유출이라 음수로 저장됨
    if op == "÷":
        if b == 0:
            return None
        return a / b * mult
    return None


# ---------------------------------------------------------------------------
# corpus 검증 — 공식이 corpus의 명시값을 재현하는가
# ---------------------------------------------------------------------------
def validate(store, labels, tolerance=Decimal("0.01")):
    """metric_key가 있는 규칙에 대해 명시값 대비 재현율을 계산한다."""
    global _VALIDATION
    if _VALIDATION is not None:
        return _VALIDATION
    if CACHE.exists():
        try:
            cached = json.loads(CACHE.read_text(encoding="utf-8"))
            if set(cached) == set(RULES):        # 규칙이 바뀌면 다시 검증한다
                _VALIDATION = cached
                return _VALIDATION
        except json.JSONDecodeError:
            pass
    from . import concepts, pipeline
    ci = concepts.get(labels.facts)
    out = {}
    stated = collections.defaultdict(list)
    for f in labels.facts:
        if f.get("statement") == "ratio" and f.get("metric_key"):
            stated[f["metric_key"]].append(f)

    for name, (_, operands, _, _, mk) in RULES.items():
        if not mk or mk not in stated:
            out[name] = {"검증": "불가", "사유": "corpus에 명시값이 없음"}
            continue
        ok = bad = miss = 0
        for f in stated[mk]:
            vals = []
            for oper in operands:
                canon, _, _ = ci.match(oper)
                p = {"metric": ci.metric_of.get(canon or oper), "label": canon or oper,
                     "year": f["base_year"], "scope": f["scope"], "statement": None}
                g = pipeline._lookup_one(p, f["corp_code"], store, labels)
                vals.append(Decimal(g["value_decimal"]) if g else None)
            calc = compute(name, vals)
            if calc is None:
                miss += 1
                continue
            try:
                got = Decimal(str(f["value_raw"]).replace("%", "").replace(",", ""))
            except InvalidOperation:
                miss += 1
                continue
            if abs(calc - got) / max(abs(got), Decimal(1)) <= tolerance:
                ok += 1
            else:
                bad += 1
        total = ok + bad
        out[name] = {"검증": "됨" if total and ok / total >= 0.9 else "실패",
                     "일치": ok, "불일치": bad, "피연산자없음": miss,
                     "재현율": round(100 * ok / total, 1) if total else None}
    _VALIDATION = out
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out
