"""공시 건수 집계 — corpus 자체에 대한 질문에 답한다.

## 왜 필요한가

정답셋에는 수치가 아니라 **문서를 세는** 질문이 있다.

    "대우건설이 2023년부터 2026년 3월까지 확보된 분기보고서는 총 몇 건인가?"        gold 7
    "이마트가 제출한 자기주식취득결정 중 [기재정정]이 아닌 최초 제출 건은 몇 건인가?"  gold 4
    "신한지주의 정기보고서 중 [기재정정]이 이루어진 문서는 총 몇 건인가?"            gold 1

fact를 찾을 일이 아니라 manifest를 세면 된다. 그런데 그 경로가 없어 전부 되물었다.

## 기준일이 아니라 기준월로 센다

"2026년 3월까지"의 분기보고서를 세면 7건인데, **접수일로 세면 6건**이다.
2026.03 분기는 5월에 접수되기 때문이다. 사람은 보고서가 다루는 기간으로 세지
접수한 날로 세지 않는다.

    (2026.03) 접수 20260515   ← "2026년 3월까지"에 포함되어야 한다

정기보고서는 이름에 `(2026.03)`이 붙어 있어 그대로 읽으면 된다. 그 표기가 없는
공시(주요사항보고서 등)는 접수일로 센다 — 사건이 곧 시점이기 때문이다.
"""

import re

from . import filings

# 건수를 묻는 신호
COUNT = re.compile(r"총\s*몇\s*건|몇\s*건인가|몇\s*개\s*(?:문서|건)|건수는|몇\s*건이며")
# 보고서 종류
# 서식 목록은 corpus에서 유도한다 — 하드코딩하면 반드시 빠뜨린다.
def KINDS():
    from . import compare
    sub, top = compare.report_kinds()
    return tuple(sub) + tuple(top) + ("정기보고서",)
_PERIODIC = None


def periodic_kinds():
    """정기보고서 = 이름에 결산 연월 `(YYYY.MM)`이 붙는 보고서.

    세 가지를 손으로 적지 않는다. 정기보고서는 **다루는 기간이 이름에 박혀 있다**는
    점으로 구별된다 — 사건 공시에는 그 표기가 없다. 규칙이 데이터에 이미 있다.
    """
    global _PERIODIC
    if _PERIODIC is not None:
        return _PERIODIC
    fl = filings.get()
    out = set()
    for m in fl.meta.values():
        nm = filings.base_name(m.get("report_nm"))
        if _YM_IN_NAME.search(nm):
            out.add(re.sub(r"\s*\(.*$", "", nm).strip())
    _PERIODIC = tuple(sorted(out))
    return _PERIODIC
# 정정 포함 여부
ONLY_CORR = re.compile(r"\[?기재정정\]?(?:이)?\s*(?:이루어진|된|인)")
NO_CORR = re.compile(r"기재정정\]?\s*이?\s*아닌|최초\s*제출|정정\s*제외"
                     r"|기재정정\]?\s*문서(?:는|를)?\s*제외|원본\s*기준|정정본?\s*제외")

_YM_IN_NAME = re.compile(r"\((\d{4})\.(\d{2})\)")
_RANGE = re.compile(r"(\d{4})\s*년\s*(?:(\d{1,2})\s*월)?\s*(?:부터|~|에서)\s*"
                    r"(\d{4})\s*년\s*(?:(\d{1,2})\s*월)?")

# "언제 접수(제출)되었는가" — 온톨로지는 지표를 찾으려다 실패해 되물었다(회귀
# 실측: GOLD-W1-HDC-07 등). 이건 지표 조회가 아니라 manifest의 rcept_dt 하나면
# 끝나는 질문이다. 질문 쪽 결산연월 표기는 report_nm과 달리 "(2024.06 결산)"처럼
# 꼬리말이 붙어 _YM_IN_NAME으로는 못 잡는다.
#
# "언제 접수됐나"·"접수일자는"·"접수된 날짜는 언제인가"·"최초 접수한 날짜는
# 언제인가" — 어순과 동사 어미가 제각각이라(회귀 실측: GOLD-W1-OCI-05·SHG-06)
# 세 형태를 모두 받는다.
WHEN = re.compile(r"언제\s*(?:접수|제출|공시)(?:되었|됐|된|한)?"
                  r"|(?:접수|제출|공시)(?:되었|됐|된|한)?\s*(?:일자|날짜)(?:는|가)?"
                  r"|(?:접수|제출|공시)(?:되었|됐|된|한)?\s*.{0,8}언제")
_YM_IN_Q = re.compile(r"\((\d{4})\.(\d{2})(?:\s*결산)?\)")
# "2023년 2월 1일부터 2026년 3월 30일 사이" — 일(day) 단위까지 밝힌 범위.
# _RANGE(연·월까지만)로는 못 잡는다.
_RANGE_DAY = re.compile(
    r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*(?:부터|~|에서)\s*"
    r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")


def _span(question):
    m = _RANGE.search(question)
    if not m:
        return None
    a = f"{int(m.group(1)):04d}{int(m.group(2) or 1):02d}"
    b = f"{int(m.group(3)):04d}{int(m.group(4) or 12):02d}"
    return a, b


def wanted(question):
    return bool(COUNT.search(question)) and any(k in question for k in KINDS())


def run(question, corp):
    """(문장, 근거 접수번호들, 숫자들) 또는 (None, 사유, [])."""
    if not corp:
        return (None, "어느 기업의 공시를 셀지 좁히지 못했습니다.", [])
    kinds = [k for k in KINDS() if k in question]
    # "분기보고서(사업보고서·반기보고서는 제외)" — 제외 절 안에 이름이 있으면 뺀다.
    #
    # 예전엔 `이름 + 몇 글자 + 제외` 패턴으로 봤는데, 나열형("A·B는 제외")에서는
    # A 뒤에 B가 끼어 안 맞았다. 대신 **제외라는 말 앞의 구절**을 통째로 보고
    # 그 안에 등장하는 이름을 뺀다 — 나열이 몇 개든 걸린다.
    for m in re.finditer(r"제외", question):
        clause = question[max(0, m.start() - 30):m.start()]
        clause = re.split(r"[(（,、]", clause)[-1]        # 괄호·쉼표 뒤부터가 그 절이다
        kinds = [k for k in kinds if k not in clause]
    if not kinds:
        return (None, "어느 공시 종류를 셀지 좁히지 못했습니다.", [])
    # 가장 **구체적인** 종류를 고른다. "주요사항보고서(자기주식취득결정)"에서
    # 상위어인 '주요사항보고서'를 고르면 회사합병·주식교환까지 함께 세어 버린다.
    # 하위유형을 먼저 본다 — 상위어(주요사항보고서)를 고르면 다른 사건까지 센다.
    from . import compare
    sub, _ = compare.report_kinds()
    # 하위유형 우선, 그다음 질문에 **먼저 나온** 것. 길이로 고르면 같은 길이끼리
    # 순서가 임의가 되어 "분기보고서"를 물었는데 "사업보고서"를 세는 일이 생긴다.
    kind = next((k for k in kinds if k in sub),
                min(kinds, key=lambda k: question.find(k)))
    names = periodic_kinds() if kind == "정기보고서" else (kind,)
    span = _span(question)
    only_corr, no_corr = bool(ONLY_CORR.search(question)), bool(NO_CORR.search(question))

    fl = filings.get()
    hits = []
    for rn, m in fl.meta.items():
        if m.get("corp_name") != corp:
            continue
        nm = m.get("report_nm") or ""
        if not any(k in nm for k in names):
            continue
        corr = filings.is_correction(nm)
        if only_corr and not corr:
            continue
        if no_corr and corr:
            continue
        if span:
            ym = _YM_IN_NAME.search(nm)
            # 정기보고서는 기준월로, 그 표기가 없으면 접수일로 센다
            periodic = any(k in periodic_kinds() for k in names)
            key = (ym.group(1) + ym.group(2)) if (ym and periodic) \
                else (m.get("rcept_dt") or "")[:6]
            if not key or not (span[0] <= key <= span[1]):
                continue
        hits.append((m.get("rcept_dt") or "", rn, nm))
    hits.sort()
    if not hits:
        return (None, f"{corp}의 해당 조건에 맞는 {kind}를 찾지 못했습니다.", [])
    qual = ("[기재정정]본" if only_corr else "최초 제출본" if no_corr else "")
    when = f"{span[0][:4]}.{span[0][4:]}~{span[1][:4]}.{span[1][4:]} " if span else ""
    listed = " · ".join(n[2] for n in hits[:8]) + (" …" if len(hits) > 8 else "")
    # [2026-09-05, 사용자 요청] 코퍼스 안에서 센 개수를 "확정된 전체 건수"처럼
    # 말하면 안 된다 — 코퍼스가 그 기간·유형의 공시를 전부 확보했다는 보장이
    # 없다(실측: GOLD-W1-SEC-04, gold=INSUFFICIENT_EVIDENCE). 그렇다고 기권
    # (되묻기·거절)하지도 않는다 — 셀 수 있는 걸 굳이 숨기지 않고, 문구로만
    # 범위를 밝힌다(state는 그대로 S0 — S2로 냈다가 기존 건수 골드 5건의
    # "행동"만 떨어뜨리고 목표 문항의 "정확"은 못 고쳐 되돌렸다. 상세는
    # REPORT.md "마감 후" 항목 참고).
    return (f"{corp}의 {when}{kind} {qual}은(는) 총 {len(hits)}건입니다 — {listed}."
            " (보유 코퍼스 기준, 범위 밖 공시는 확인 불가)",
            [h[1] for h in hits], [str(len(hits))])


def wanted_when(question):
    return bool(WHEN.search(question)) and any(k in question for k in KINDS())


def run_when(question, corp):
    """이 회사의 특정 회차 보고서가 언제 접수됐는지. (문장, 근거 접수번호들, 숫자들) 또는 (None, 사유, [])."""
    if not corp:
        return (None, "어느 기업의 공시인지 좁히지 못했습니다.", [])
    kinds = [k for k in KINDS() if k in question]
    if not kinds:
        return (None, "어느 공시 종류인지 좁히지 못했습니다.", [])
    from . import compare
    sub, _ = compare.report_kinds()
    kind = next((k for k in kinds if k in sub), min(kinds, key=lambda k: question.find(k)))
    names = periodic_kinds() if kind == "정기보고서" else (kind,)

    ym = _YM_IN_Q.search(question)
    if not ym:
        return (None, f"{corp}의 어느 회차(결산연월)를 묻는지 좁히지 못했습니다.", [])
    target = ym.group(1) + ym.group(2)

    fl = filings.get()
    hits = []
    for rn, m in fl.meta.items():
        if m.get("corp_name") != corp:
            continue
        nm = m.get("report_nm") or ""
        if not any(k in nm for k in names):
            continue
        ymm = _YM_IN_NAME.search(nm)
        if not ymm or (ymm.group(1) + ymm.group(2)) != target:
            continue
        hits.append((m.get("rcept_dt") or "", rn, nm))
    if not hits:
        return (None, f"{corp}의 {kind}({ym.group(1)}.{ym.group(2)}) 문서를 찾지 못했습니다.", [])
    # "언제 접수되었는가"는 특별한 수식이 없는 한 최초(원본) 제출 시점을 묻는
    # 것으로 본다 — [기재정정]본이 있어도 그건 별개 이벤트다(회귀 실측:
    # GOLD-W1-SHG-06이 명시적으로 "최초 접수한 날짜"를 요구).
    hits.sort()
    dt, rn, nm = hits[0]
    disp = f"{dt[:4]}년 {int(dt[4:6])}월 {int(dt[6:8])}일" if len(dt) == 8 else dt
    return (f"{corp}의 {nm}은(는) {disp}에 접수(제출)되었습니다.", [rn], [dt])


def _span_day(question):
    m = _RANGE_DAY.search(question)
    if not m:
        return None
    a = f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    b = f"{int(m.group(4)):04d}{int(m.group(5)):02d}{int(m.group(6)):02d}"
    return a, b


def wanted_multi(question, p):
    """여러 기업 × 여러 공시유형의 건수를 각각 세어 비교하는 질문인가.

    "SK하이닉스는 처분만, 삼성전자는 취득·처분 혼재로 공시했다 — 각각 몇 건씩인가"
    류. 기존 run()은 기업 하나·유형 하나만 다뤄서 이런 질문은 놓쳤다(회귀 실측:
    GOLD-W1-SKH-03).
    """
    corps = p.get("corps") or []
    if len(corps) < 2:
        return False
    from . import compare
    sub, _ = compare.report_kinds()
    kinds = [k for k in KINDS() if k in question and k in sub]
    if len(kinds) < 2:
        return False
    return bool(re.search(r"몇\s*건|건수", question))


def run_multi(question, corps):
    """기업마다 · 유형마다 건수를 나눠 센다. (문장, 근거 접수번호들, 숫자들) 또는 (None, 사유, [])."""
    from . import compare
    sub, _ = compare.report_kinds()
    kinds = [k for k in KINDS() if k in question and k in sub]
    if not kinds:
        return (None, "어느 공시 종류를 셀지 좁히지 못했습니다.", [])
    span_day = _span_day(question)
    span = span_day or _span(question)

    fl = filings.get()
    lines, all_hits, totals = [], [], []
    for corp in corps:
        by_kind, corp_hits = {}, []
        for kind in kinds:
            hits = []
            for rn, m in fl.meta.items():
                if m.get("corp_name") != corp or kind not in (m.get("report_nm") or ""):
                    continue
                if span:
                    key_len = 8 if span_day else 6
                    key = (m.get("rcept_dt") or "")[:key_len]
                    if not key or not (span[0] <= key <= span[1]):
                        continue
                hits.append(rn)
            by_kind[kind] = hits
            corp_hits.extend(hits)
        breakdown = " · ".join(f"{k} {len(v)}건" for k, v in by_kind.items())
        lines.append(f"{corp} {len(corp_hits)}건({breakdown})")
        all_hits.extend(corp_hits)
        totals.append(len(corp_hits))

    if not any(totals):
        return (None, "해당 조건에 맞는 공시를 찾지 못했습니다.", [])
    return (", ".join(lines) + ".", all_hits, [str(t) for t in totals])
