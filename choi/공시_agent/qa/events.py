"""이벤트 통합 뷰 — 계약·투자·자금조달·지분변동을 한 번에 훑어본다.

## 왜 필요한가

D-TRACE 계획서가 말하는 "이벤트"(계약·시설투자·자금조달·지분변동)는 지금
`contract.py`·`shareholders.py`·`fields.py`·`docstats.py`가 각자 따로 특정 질문
유형만 받는다. 그런데 "이 회사 최근 이벤트 뭐 있어?" 같은 **열린** 질문은 어느
모듈도 못 받는다 — `contract.find()`는 질문이 계약 이름을 대야 찾고(연속 6자 이상
겹침), `shareholders`는 주주현황 전용이다. 열린 질문은 처음부터 빠져 있었다.

## 무엇을 하는가

새 엔티티나 새 데이터를 만들지 않는다. `qa/filings.py`(major/exchange/holding
공시 — 정기공시가 아닌 수시·지분공시 전체)를 기업 하나로 좁혀 접수일 역순으로
나열하고, 문서마다 핵심 텍스트 필드 1~2개를 뽑아 한 줄 요약으로 만든다.
계산·판단은 하지 않는다 — 공시에 적힌 그대로 나열할 뿐이다.

## 한계 — 정직하게 밝힌다

- 이 목록은 원본과 정정본을 **둘 다** 보여줄 수 있다("[정정]" 표시만 붙인다) —
  D-TRACE의 "정정 가능성" 갭 자체를 여기서 다시 풀지는 않는다(qa/pipeline.py의
  correction_flag가 그 역할이고, 이 모듈이 반환하는 evidence에도 그대로 얹힌다).
- 텍스트 필드가 없는 서식(순수 수치 공시)은 요약 줄이 문서명만 남는다.
- 이건 "목록"이지 "설명"이 아니다 — "왜 이 계약을 했는지" 같은 해설은 narrative
  경로(원문 읽기)의 몫이다.
"""

import re

from . import filings

# "최근 이벤트/소식/공시 정리해줘" 류 — 특정 계약 이름을 대는 contract.py의
# EVENT(계약|수주|공급...)와 겹치지 않게, "무엇을"이 아니라 "다 보여줘"를 신호로 본다.
GENERAL = re.compile(r"(?:최근|주요)?\s*이벤트|(?:최근|주요)\s*(?:소식|동향)"
                     r"|(?:최근|주요)\s*공시.{0,6}(?:뭐|어떤|정리|나열|알려)")

MAX_ITEMS = 8
_TEXT_MAXLEN = 40  # 이보다 긴 텍스트는 요약 줄에 넣기엔 장문(본문 서술)이라 뺀다
# filings.py는 정기보고서(사업·반기·분기)도 같이 읽는다(정정 전후 대조용으로
# 설계됨) — "이벤트"는 수시·지분공시만 뜻하므로 여기서 걷어낸다.
_PERIODIC = re.compile(r"사업보고서|반기보고서|분기보고서")
_BLANK = {"-", "", "–", "—", "해당없음", "해당 없음"}


def wanted(question, p):
    """이벤트 통합 뷰를 원하는 질문인가. 기업 하나가 특정돼야 한다."""
    if not p.get("corp") or len(p.get("corps") or []) != 1:
        return False
    return bool(GENERAL.search(question))


def _pick_texts(fields_):
    """문서 하나의 필드 중 요약 줄에 넣을 짧은 텍스트 필드 최대 2개. 빈 값("-" 등)은 뺀다."""
    out = []
    for v in (fields_ or {}).values():
        if v.get("kind") != "text":
            continue
        raw = str(v.get("raw") or "").strip()
        if not raw or raw in _BLANK or len(raw) > _TEXT_MAXLEN:
            continue
        out.append(raw)
        if len(out) >= 2:
            break
    return out


def recent(corp, top=MAX_ITEMS):
    """이 회사의 최근 수시·지분공시(계약·투자·자금조달·지분변동) 목록. 정기보고서는 뺀다.

    반환: [(rcept_no, 요약줄, is_correction), ...] 접수일 역순.
    """
    fl = filings.get()
    docs = [(rn, m) for rn, m in fl.meta.items()
            if m.get("corp_name") == corp and not _PERIODIC.search(m.get("report_nm") or "")]
    docs.sort(key=lambda x: x[1].get("rcept_dt") or "", reverse=True)

    out = []
    for rn, m in docs[:top]:
        f = fl.fields(rn)
        name = filings.base_name(m.get("report_nm"))
        texts = _pick_texts(f)
        dt = (m.get("rcept_dt") or "")[:10]
        corr = " [정정]" if m.get("is_correction") else ""
        line = f"[{dt}] {name}{corr}" + (f" — {' · '.join(texts)}" if texts else "")
        out.append((rn, line, bool(m.get("is_correction"))))
    return out
