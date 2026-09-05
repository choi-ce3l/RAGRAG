"""이벤트 앵커 전후 대조 — 유상증자·공급계약 같은 사건을 시간축 기준점으로 삼아
그 앞뒤 지표값을 대조한다.

## 왜 필요한가

지금 구조는 "사건 하나 찾기"(`contract.find`)와 "지표 하나 조회"(`labelstore`)를
각각 잘한다. 그런데 "그 사건 이후 지표가 어떻게 변했나"처럼 **사건을 시점
기준점으로 놓고 그 앞뒤 지표를 대조**하는 조합 연산자가 없었다.

    "A사 유상증자 이후 부채비율이 얼마나 내려갔나"
    "OO 공급계약 공시 이후 매출은 어떻게 됐나"

## 좁은 게이트 — 넷 다 있을 때만 시도한다

(a) 회사 하나가 특정됨, (b) `contract.EVENT`(재사용)에 걸리는 이벤트 키워드,
(c) "이후"/"이전" 전후비교 신호, (d) 지표(concept)가 특정됨. 하나라도 없으면
`wanted()`가 False를 돌려주고 기존 파이프라인(결국 S6)으로 흘러간다.

"전후"는 신호에서 뺐다 — `qa/compare.py`의 AMEND 게이트가 정확히 "전후"라는
낱말로 정정 전후 대조 경로를 먼저 잡아채므로(`_run()`에서 compare.parse()가 이
모듈보다 먼저 호출된다), 겹치는 낱말을 신호로 쓰면 그 경로가 우리 질문을
가로챌 위험이 있다. "이후"·"이전"은 AMEND에 없어 안전하다.

## 지표 재확인 — p["concept"]를 곧이곧대로 믿지 않는다

`contract.EVENT`의 이벤트 키워드(계약|수주|공급|영업정지|취득|처분|증자|합병|분할)
상당수가 실제로 XBRL 현금흐름표 항목의 정규형으로 corpus에 존재한다("유상증자로
인한현금유입" 등, `concepts.py`가 "유상증자"라는 정규형으로 색인해 둔 것).
`concepts.py::ConceptIndex.match()`는 긴 정규형부터 훑다가 정확 일치를 만나면
그대로 확정하는데, "유상증자"(4자)가 "매출"(2자)보다 길어 먼저 걸린다. 그 결과
"삼성SDI 유상증자 결정 공시 이후 매출 추이는 어때?"에서 01단계가 지표를
"매출"이 아니라 "유상증자"(현금흐름표 항목)로 잘못 특정해 버린다(실측 확인).

이건 `concepts.py` 자체의 사전 순위 문제라 이 모듈의 책임 밖이지만, 이벤트
앵커 질문은 구조적으로 "이벤트 키워드 + 지표"가 한 문장에 같이 오므로 이 함정을
그대로 밟는다. 방어책: 이벤트 키워드가 매칭된 부분을 질문에서 지운 나머지
텍스트로 `ci.match()`를 **다시** 돌려(새 매칭 로직이 아니라 기존 함수를 다른
입력으로 재호출) 지표를 다시 확인한다(`_resolve_concept`). 그 재확인이 아무것도
못 찾으면(또는 파생 규칙도 못 찾으면) 손대지 않고 None을 돌려준다 — 억지로
p["concept"]를 쓰지 않는다.

## 이벤트 특정

`contract.find(question, corp, top=3)`가 정확히 후보 하나로 좁힐 때만 진행한다
(여러 개면 손대지 않는다). 그 공시의 `filings.meta[rn]["rcept_dt"]`(접수일자,
"YYYYMMDD")를 앵커 날짜로 삼는다.

## 전/후 기간 결정 — 근사가 아니라 정공법을 쓴다

처음에 검토한 차선책은 "이벤트 이전에 접수된 가장 최근 정기보고서"를 정기보고서
**접수일자**로 근사하는 것이었다(정기보고서도 filings.meta에 rcept_dt가 있으므로
가능은 하다). 그런데 실제 회계기간 종료일(`period_end`)이 facts.db에 이미 있고,
직접 조회해 확인한 결과 이 corpus의 사업보고서는 **예외 없이 결산월이 12월**이다
(`SELECT DISTINCT substr(period_end,6,5) FROM facts ...` → `12-31`뿐). 그러므로
회계연도 Y의 결산기말은 그냥 `f"{Y}1231"`이고, 이벤트 접수일(`rcept_dt`, 같은
"YYYYMMDD" 형식)과 문자열 그대로 비교할 수 있다 — 근사가 아니라 실제 결산기말
대조다. 이 corpus 밖(3월 결산 등 비12월 결산 기업)에 그대로 적용하면 틀릴 수
있으므로, 이 정책은 이 corpus의 실측 결과에 근거한 것이라고 답변에 명시한다.

전(前): 이벤트 이전(`< event_dt`)에 결산기말이 있는 연도 중 가장 늦은 해.
후(後): 이벤트 이후(`>= event_dt`)에 결산기말이 있는 연도 중 가장 이른 해.

## 한계 — 정직하게 밝힌다

- "후" 연도의 사업보고서는 그 회계연도 **전체**(1월~12월)의 값이다. 이벤트가
  그 해 중간에 있었다면, "후" 값에는 이벤트 이전 몇 개월치도 섞여 있다 —
  연간 단위로만 비교하는 이 모듈의 근본 한계다(분기 단위로 순수하게 이벤트
  이후만 떼어내는 것은 다루지 않는다).
- 후보 연도의 해당 지표 값이 비어 있으면(그 해 사업보고서에 그 항목이 없으면)
  다음/이전 연도로 물러나며 찾는다. 그래도 못 찾으면 조용히 실패한다(None).
- 파생 지표(부채비율 등)는 `derived.py`의 두 피연산자 조회 + `derived.compute()`
  그대로를 쓴다 — 새 계산식을 만들지 않는다.
"""

import re
from decimal import Decimal, InvalidOperation

from . import concepts, contract, derived, filings

# "이후"/"이전" — "전후"는 qa/compare.py의 AMEND와 겹쳐 일부러 뺐다(모듈 docstring 참고).
PREPOST = re.compile(r"이후|이전")

_SCOPE_KO = {"consolidated": "연결", "separate": "별도"}


def wanted(question, p):
    """이 경로를 시도할 질문인가 — 넷 다 있을 때만."""
    if not p.get("corp") or len(p.get("corps") or []) != 1:
        return False
    if not p.get("concept"):
        return False
    # G7: contract.EVENT 정규식(계약|수주|공급|영업정지|취득|처분|증자|합병|분할)만
    # 보면 "감자결정"처럼 events_vocab.py가 corpus 전체에서 뽑아낸 더 넓은 이벤트
    # 어휘를 놓친다. ontology.py가 이미 채워둔 p["event"]가 있으면 그걸 우선
    # 신뢰하고, 없을 때만 옛 정규식으로 물러난다(OR) — 기존에 잘 되던 케이스
    # (예: 삼성SDI×NextEra Energy 공급계약, EVENT 정규식만 걸리고 p["event"]는
    # 없을 수 있는 경우)를 깨뜨리지 않기 위해서다.
    if not (p.get("event") or contract.EVENT.search(question)):
        return False
    return bool(PREPOST.search(question))


def _won(f):
    """fact 하나의 스케일 반영 값. comparespec.py의 동명 헬퍼와 같은 계산이지만
    다른 파일의 private 함수에 기대지 않으려고 그대로 다시 쓴다(같은 이유가
    comparespec.py 자체 docstring에도 있다)."""
    try:
        return Decimal(str(f["value_decimal"])) * Decimal(str(f.get("scale") or 1))
    except (InvalidOperation, ValueError, TypeError, KeyError):
        return None


def _resolve_concept(question, p, labels):
    """이벤트 키워드를 지운 나머지 문장에서 지표를 다시 확인한다.

    반환: {"kind": "metric", "metric": ..} | {"kind": "label", "label": ..}
        | {"kind": "derived", "name": .., "operands": [(metric|None, label|None), ...]}
        | None(재확인 실패 — 손대지 않는다).
    """
    masked = contract.EVENT.sub(" ", question)
    ci = concepts.get(labels.facts)
    canon, _cands, _why = ci.match(masked, corp_codes=p.get("corp_codes"))
    if canon:
        metric = ci.metric_of.get(canon)
        return {"kind": "metric" if metric else "label",
                "metric": metric, "label": None if metric else canon,
                "label_ko": canon}
    name = derived.find(masked)
    if name and name in derived.RULES:
        _op, operands, unit, _mult, _mk = derived.RULES[name]
        resolved = []
        for oper in operands:
            oc, _c2, _w2 = ci.match(oper)
            oc = oc or oper
            mk = ci.metric_of.get(oc)
            resolved.append((mk, None if mk else oc))
        return {"kind": "derived", "name": name, "operands": resolved,
                "unit": unit, "label_ko": name}
    return None


def _annual_fact(labels, cc, metric, label, statement, year, scope):
    """그 해 사업보고서(당해 연도 원본)를 1차 출처로 삼는다(labelstore.py 기본
    정책과 같다). "이전/이후" 대조는 그 시점에 실제 보고된 값끼리 맞대는 것이
    자연스럽다 — prefer_latest=True를 쓰면 두 시점 모두 가장 최신 사업보고서
    (예: 2025년치 하나)의 비교표시 열에서 값을 끌어와, "이전"이라고 표시해
    놓고 실제로는 "이후" 보고서에서 값을 읽어오는 혼란스러운 근거가 된다."""
    if metric:
        return labels.lookup_metric_annual(cc, metric, scope, year, statement,
                                           prefer_latest=False)
    if label:
        return labels.lookup(cc, label, scope, year, statement)
    return None


def _period_value(labels, cc, concept, year, scope, statement=None):
    """(값 Decimal, 근거 fact 목록) 또는 (None, [])."""
    if concept["kind"] == "derived":
        vals, facts = [], []
        for metric, label in concept["operands"]:
            f = _annual_fact(labels, cc, metric, label, None, year, scope)
            if not f:
                return None, []
            try:
                vals.append(Decimal(str(f["value_decimal"])))
            except (InvalidOperation, TypeError):
                return None, []
            facts.append(f)
        v = derived.compute(concept["name"], vals)
        return v, (facts if v is not None else [])
    f = _annual_fact(labels, cc, concept.get("metric"), concept.get("label"),
                     statement, year, scope)
    if not f:
        return None, []
    return _won(f), [f]


def _find_bracket(labels, cc, concept, candidate_years, scope, statement):
    """값이 실제로 있는 연도를 만날 때까지 candidate_years 순서대로 시도한다.

    candidate_years는 호출부가 이미 원하는 순서로 정렬해 넘긴다 — "전"이면
    이벤트에 가까운 해부터(내림차순), "후"면 이벤트에 가까운 해부터(오름차순).
    """
    for y in candidate_years:
        v, facts = _period_value(labels, cc, concept, y, scope, statement)
        if v is not None:
            return y, v, facts
    return None, None, []


def answer(question, p, labels):
    """(문장, 근거 rcept_no들, 숫자들) 또는 (None, 사유, [])."""
    corp, cc = p.get("corp"), p.get("corp_code")
    if not corp or not cc:
        return None, "기업을 특정하지 못했습니다.", []

    # G6: 앵커의 1차 출처는 p["event"]["anchors"](qa/ontology.py가
    # qa/events_vocab.py + qa/filings.py::find_events()로 이미 산출해 둔 것)다.
    # 이게 있으면 그대로 쓰고, 여럿이면 contract.find()(폐기하지 않는다)로
    # 질문의 고유명사(거래상대방·프로젝트명 등)를 2차 필터로만 걸어 좁힌다.
    # p["event"]가 없는 경우(이벤트 유형 어휘가 못 잡은 표현)만 예전처럼
    # contract.find() 단독 경로로 물러난다.
    anchor_note = ""
    ev = p.get("event") or {}
    anchors = ev.get("anchors") or []
    if anchors:
        chosen = anchors
        if len(anchors) > 1:
            narrowed = set(contract.find(question, corp, top=len(anchors) + 2))
            filtered = [a for a in anchors if a["rcept_no"] in narrowed]
            chosen = filtered if len(filtered) == 1 else anchors[:1]
            if len(chosen) < len(anchors):
                anchor_note = f" (같은 유형의 다른 결정 {len(anchors) - len(chosen)}건이 더 있습니다.)"
        event_rn = chosen[0]["rcept_no"]
    else:
        cands = contract.find(question, corp, top=3)
        if len(cands) != 1:
            return None, f"이벤트 공시를 하나로 좁히지 못함 (후보 {len(cands)}건).", []
        event_rn = cands[0]

    fl = filings.get()
    meta = fl.meta.get(event_rn) or {}
    event_dt = meta.get("rcept_dt") or ""
    if len(event_dt) != 8 or not event_dt.isdigit():
        return None, "이벤트 접수일자를 확인하지 못했습니다.", []

    concept = _resolve_concept(question, p, labels)
    if not concept:
        return None, "이벤트 표현을 제외한 나머지 문장에서 지표를 특정하지 못했습니다.", []

    years = sorted(labels.corp_years(cc))
    if not years:
        return None, "이 기업의 사업보고서 보유 연도를 확인하지 못했습니다.", []

    # 사업연도 결산기말은 이 corpus에서 예외 없이 12월 31일이다(모듈 docstring
    # 참고 — 직접 조회로 확인함). 따라서 "YYYY1231"을 그 해 결산기말로 그대로 쓴다.
    before_years = sorted((y for y in years if int(f"{y}1231") < int(event_dt)), reverse=True)
    after_years = sorted(y for y in years if int(f"{y}1231") >= int(event_dt))
    if not before_years or not after_years:
        return None, "이벤트를 사이에 두고 앞뒤로 사업보고서가 모두 있는지 확인하지 못했습니다.", []

    scope = p.get("scope") or "consolidated"
    statement = p.get("statement")
    before_year, before_v, before_facts = _find_bracket(
        labels, cc, concept, before_years, scope, statement)
    after_year, after_v, after_facts = _find_bracket(
        labels, cc, concept, after_years, scope, statement)
    if before_v is None or after_v is None:
        return None, "이벤트 전후 지표 값을 모두 찾지 못했습니다.", []

    delta = after_v - before_v
    unit = (concept.get("unit") if concept["kind"] == "derived"
            else (before_facts[0].get("unit_kr") or after_facts[0].get("unit_kr") or ""))

    def _fmt(v):
        return f"{float(v):,.1f}{unit}" if unit == "%" else f"{v:,.0f}{unit}"

    scope_ko = _SCOPE_KO.get(scope, "")
    event_name = filings.base_name(meta.get("report_nm"))
    event_date_ko = f"{event_dt[:4]}-{event_dt[4:6]}-{event_dt[6:]}"
    direction = "증가" if delta >= 0 else "감소"

    def _report_note(y, facts):
        rn = facts[0].get("report_nm") or f"{y}년 사업보고서"
        return rn

    text = (f"{corp} {event_name}({event_rn}, 접수일 {event_date_ko}) 전후 "
           f"{scope_ko} {concept['label_ko']} — "
           f"이전({_report_note(before_year, before_facts)}) {_fmt(before_v)} → "
           f"이후({_report_note(after_year, after_facts)}) {_fmt(after_v)}, "
           f"{_fmt(abs(delta))} {direction}"
           f" (연간 결산기말 12/31 기준 대조 — 이벤트가 '이후' 회계연도 중간에 "
           f"있었다면 그 값엔 이벤트 이전 몇 개월치도 섞여 있습니다)." + anchor_note)

    ev = sorted({event_rn, *(f.get("rcept_no") for f in before_facts if f.get("rcept_no")),
                *(f.get("rcept_no") for f in after_facts if f.get("rcept_no"))})
    nums = [str(before_v), str(after_v), str(delta)]
    return text, ev, nums
