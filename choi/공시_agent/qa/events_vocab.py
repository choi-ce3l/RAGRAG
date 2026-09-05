"""이벤트 유형 어휘 — corpus의 filings.report_nm에서 뽑은 폐집합.

## 왜 필요한가

온톨로지에 기업·지표·연도·스코프 슬롯은 있는데 **이벤트 유형** 슬롯이 없었다.
그래서 "유상증자" 같은 표면형이 갈 곳이 XBRL 라벨(현금흐름표의 "유상증자로
인한현금유입" 같은 금액 행)밖에 없었다 — "유상증자를 언제 결정했나"처럼 날짜를
묻는 질문에마저 금액으로 답하는 사고가 났다(실측: T2/T3).

## 왜 수작업 사전이 아닌가

`qa/concepts.py`·`qa/sectors.py`가 corpus에서 어휘를 뽑는 것과 같은 원칙이다.
`qa/filings.py`의 report_nm을 전수 열거해 실제로 존재하는 이벤트 유형만 어휘로
인정한다 — 없는 유형을 지어내지 않는다. 구어체 별칭(`_EVENT_ALIASES`)만 손으로
등록하되, 매핑 대상은 반드시 이 폐집합 안에 실제로 있는 이름이어야 한다
(match()가 이를 다시 검증한다).
"""

import re

from . import filings

# "주요사항보고서(유상증자결정)" → "유상증자결정". filings.base_name()이 이미
# 정정 대괄호([기재정정] 등)를 벗겨 주므로, 여기서는 "주요사항보고서(...)" 감싸는
# 괄호만 한 겹 더 벗긴다.
_WRAP = re.compile(r"^주요사항보고서\s*\(([^)]+)\)$")


def normalize(report_nm):
    """report_nm → 정규화된 이벤트 유형 핵심어. 정기보고서 등은 그대로 돌려준다
    (정기보고서 이름 자체를 "이벤트 유형"으로 쓰진 않지만, 호출부가 걸러낸다)."""
    base = filings.base_name(report_nm)
    if not base:
        return None
    m = _WRAP.match(base)
    return m.group(1).strip() if m else base.strip()


# 정기보고서(결산 연월이 이름에 박힘)는 "사건"이 아니다 — events_vocab이 다루는
# 이벤트 유형에서 뺀다. docstats.py의 _YM_IN_NAME과 같은 판별 방식이다.
_PERIODIC = re.compile(r"\(\d{4}\.\d{2}\)")

_TYPES = None


def event_types(rebuild=False):
    """corpus에 실제로 존재하는 이벤트 유형 → {"count": 건수, "corps": 보유 기업 수}."""
    global _TYPES
    if _TYPES is not None and not rebuild:
        return _TYPES
    fl = filings.get()
    counts, corps = {}, {}
    for m in fl.meta.values():
        nm = m.get("report_nm") or ""
        if _PERIODIC.search(nm):
            continue
        t = normalize(nm)
        if not t:
            continue
        counts[t] = counts.get(t, 0) + 1
        corps.setdefault(t, set()).add(m.get("corp_name"))
    _TYPES = {t: {"count": counts[t], "corps": len(corps[t])} for t in counts}
    return _TYPES


# qa/llmparse.py(슬롯 JSON 폐집합 폴백)에 넣을 이벤트 유형 후보 — 보유 기업 수
# 기준 상위만 추린다. concepts.py::llm_candidates(LLM_MIN_COVERAGE=10)와 같은
# 방식이다. event_types()는 156개 유형 전체를 주는데, 이대로 프롬프트에 넣기엔
# 너무 크고 대다수(1~2개 기업만 쓰는 유형)는 캐주얼한 질문이 실제로 가리킬
# 확률도 낮다. 실측(2026-09-04, 이 corpus 기준): 임계값 3이면 156개 중 24개가
# 남는다(유상증자결정 13개사·회사합병결정 13개사 등 이번 사고와 관련된 유형은
# 모두 포함된다).
EVENT_LLM_MIN_COVERAGE = 3


def llm_candidates(min_coverage=EVENT_LLM_MIN_COVERAGE):
    """보유 기업 수 기준 상위 이벤트 유형만 후보로 추린다. event_types()·match()는
    건드리지 않는다 — 이 함수만 새로 추가한다."""
    types = event_types()
    return sorted((t for t, v in types.items() if v["corps"] >= min_coverage),
                  key=lambda t: -types[t]["corps"])


# 구어체 별칭 → corpus 실제 이벤트 유형 이름. 매핑 대상이 event_types()에 실제로
# 없으면 match()가 그 별칭을 무시한다(존재하지 않는 유형을 지어내지 않는다).
_EVENT_ALIASES = {
    "유상증자": "유상증자결정", "증자": "유상증자결정",
    "무상증자": "무상증자결정",
    "자사주취득": "자기주식취득결정", "자기주식취득": "자기주식취득결정",
    "자사주처분": "자기주식처분결정", "자기주식처분": "자기주식처분결정",
    "공급계약": "단일판매ㆍ공급계약체결", "납품계약": "단일판매ㆍ공급계약체결",
    "수주계약": "단일판매ㆍ공급계약체결",
    "소송": "소송등의제기", "피소": "소송등의제기",
    "합병": "회사합병결정",
    "회사분할": "회사분할결정", "인적분할": "회사분할결정", "물적분할": "회사분할결정",
    "감자": "감자결정",
    "전환사채": "전환사채권발행결정", "교환사채": "교환사채권발행결정",
}


def match(question):
    """질문에서 이벤트 유형을 찾는다. (정규화 이벤트 유형, source) 또는 (None, None).

    source는 항상 "utterance"다 — 정확 일치·별칭 모두 결정론 규칙이고, 여기엔
    resolve.py(LLM 폐집합 폴백)를 태우지 않는다(비용·범위 모두 이번 사고의
    핵심이 아니다 — 새 LLM 호출을 늘리지 않는다는 원칙을 지킨다).
    """
    types = event_types()
    qn = question.replace(" ", "")
    for name in sorted(types, key=len, reverse=True):
        if name.replace(" ", "") in qn:
            return name, "utterance"
    for alias, name in sorted(_EVENT_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if alias in qn and name in types:
            return name, "utterance"
    return None, None
