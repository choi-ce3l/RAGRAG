"""intent_parse.py — parse_intent 재구축 (jin/router, 원본 파일명 parse.py).

choi/code_chunkingandparsing/src에도 `parse.py`(문서 XML 파서)가 있고 원래는 이 파일과
이름이 같았다. choi의 rag.py가 내부에서 `import parse`로 그 문서 파서를 가정하고 쓰기
때문에(수정 금지 대상), 이식 시 이 파일을 `intent_parse.py`로 리네임해 이름 충돌을
원천 제거했다(jin/INTEGRATION_PLAN.md §2-2 제안대로) — ragrag/pipeline/parse.py(문서
파서)와 ragrag/router/intent_parse.py(질문 파서)는 이제 정식 패키지 경로가 달라 충돌하지
않는다.

choi numqa.py의 parse_intent 대비 확장:
  - 기업 매칭: prefix 전용 → 문장 내 위치 무관(부분 문자열, 긴 이름 우선)
  - 기간: "N년" 외에 분기/상반기·하반기/작년·재작년·올해(상대연도)/전체 날짜(as_of)
  - scope: 연결/별도 + K-IFRS 개별/별도재무제표 표현
  - metric: choi._ONTOLOGY 시드 + jin 확장(vocab.py), 금융업 순이자손익 계열은
    명시적으로 미해결 처리(매핑 금지)
  - version 신호: "정정 전/후", "당시 기준", "원래 공시" 등 → version_selector
  - intent: 슬롯+트리거 조합의 명명된 규칙 cascade, 발동 규칙을 frame.fired_rules에 기록
"""
import datetime as _dt
import re
import sys

from ragrag.pipeline import load as choi_load  # noqa: E402  (choi 원본, import만)

from . import frame as fr    # noqa: E402  (jin/router 형제 모듈 — 이름 충돌 없음)
from . import vocab           # noqa: E402

_CORP_INDEX = None


def _load_corp_index():
    """universe.csv 기준 (corp_name, corp_code) 목록, 이름 길이 내림차순.

    manifest corp_name 그대로 — "삼전" 같은 비공식 별칭은 이번 범위에서 제외(지시사항).
    """
    global _CORP_INDEX
    if _CORP_INDEX is None:
        uni = choi_load.load_universe()
        # 방어적 우회(choi 미수정): universe.csv가 UTF-8 BOM으로 시작하는데
        # choi.load.load_universe()가 encoding="utf-8"(utf-8-sig 아님)로 열어서
        # csv.DictReader의 첫 컬럼명이 "corp_code"가 아니라 "﻿corp_code"로
        # 깨진다(코드 확인, choi 신고 안 된 버그) — 두 키 다 시도한다.
        pairs = [(name, meta.get("corp_code") or meta.get("﻿corp_code"))
                 for name, meta in uni.items() if name]
        pairs = [(n, c) for n, c in pairs if c]
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        _CORP_INDEX = pairs
    return _CORP_INDEX


def _match_corp(q):
    """문장 내 위치 무관 부분일치, 가장 긴 이름 우선(짧은 이름이 긴 이름의 부분이 되는
    오매칭 방지 — 예: "삼성"이 "삼성전자"보다 먼저 매칭되지 않도록)."""
    for name, code in _load_corp_index():
        if name in q:
            return name, code
    return None, None


def _match_period(q, reference_date):
    """기간 슬롯 추출. 패턴 우선순위: 전체날짜(as_of) > 분기 > 반기 > 단순연도 > 상대연도.

    상대연도(작년/올해/재작년)만 reference_date에 의존한다 — 코퍼스 자체에는 "지금"이라는
    개념이 없으므로, 이 슬롯만 실행 시각(또는 호출측이 넘긴 고정 기준일)에 좌우된다.
    """
    fired = []
    p = fr.Period()

    m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", q)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        p.as_of_date = f"{y:04d}{mo:02d}{d:02d}"
        p.year = y
        fired.append(f"PERIOD_ASOF_DATE:{p.as_of_date}")
        return p, fired

    m = re.search(r"(\d{4})\s*년\s*(\d)\s*분기", q)
    if m:
        p.year, p.quarter = int(m.group(1)), int(m.group(2))
        fired.append(f"PERIOD_QUARTER:{p.year}Q{p.quarter}")
        return p, fired

    for term, half in vocab.HALF_TERMS.items():
        m = re.search(r"(\d{4})\s*년\s*" + term, q)
        if m:
            p.year, p.half = int(m.group(1)), half
            fired.append(f"PERIOD_HALF:{p.year}{term}")
            return p, fired

    m = re.search(r"(\d{4})\s*년", q)
    if m:
        p.year = int(m.group(1))
        fired.append(f"PERIOD_YEAR:{p.year}")
        return p, fired

    for term, offset in vocab.RELATIVE_YEAR_TERMS.items():
        if term in q:
            ref = reference_date or _dt.date.today()
            p.year = ref.year + offset
            fired.append(f"PERIOD_RELATIVE_YEAR:{term}->{p.year}(ref={ref.isoformat()})")
            return p, fired

    return p, fired


# ---------------------------------------------------------------------------
# intent 판정 규칙 — 이름 붙여 순서대로 검사. 첫 매칭이 결정한다.
# ---------------------------------------------------------------------------
def _rule_existence(q):
    for term in vocab.EXISTENCE_TERMS:
        if term in q:
            return term
    return None


def _rule_comparison(q):
    for term in vocab.VERSION_PAIR_TERMS:
        if term in q:
            return term
    return None


def _rule_compute(q):
    for term in vocab.COMPUTE_TERMS:
        if term in q:
            return term
    return None


REQUIRED_SLOTS_FACT = ("corp_code", "metric", "period.year|as_of_date")


def _confidence_fact(corp_code, metric, period):
    filled = sum([bool(corp_code), bool(metric), bool(period.year or period.as_of_date)])
    return filled / len(REQUIRED_SLOTS_FACT)


def parse_intent(q, reference_date=None):
    """질문 텍스트 -> Frame.

    reference_date: "작년/올해" 해석 기준일(datetime.date). None이면 실행 시각(오늘)을
    쓴다 — diagnose.py처럼 재현 가능한 결과가 필요한 배치 실행에서는 명시적으로 고정값을
    넘길 것(골드셋 질문은 전부 "N년" 절대표현이라 이 경로를 실제로 타지는 않는다).
    """
    fired = []
    corp_name, corp_code = _match_corp(q)
    if corp_name:
        fired.append(f"CORP_MATCH:{corp_name}")

    period, pfired = _match_period(q, reference_date)
    fired.extend(pfired)

    scope, sterm = vocab.match_scope(q)
    if scope:
        fired.append(f"SCOPE_MATCH:{scope}:{sterm}")

    metric, mterm = vocab.match_metric(q)
    if metric:
        fired.append(f"METRIC_MATCH:{metric}:{mterm}")
    elif mterm:
        fired.append(f"METRIC_UNRESOLVED_FINANCIAL:{mterm}")

    vsel, vterm = vocab.match_version_selector(q)
    if vsel:
        fired.append(f"VERSION_TRIGGER:{vsel}:{vterm}")
    elif period.as_of_date:
        vsel = "as_of"
        fired.append("VERSION_ASOF_FROM_DATE")

    existence_term = _rule_existence(q)
    comparison_term = _rule_comparison(q)
    compute_term = _rule_compute(q)

    missing = []
    operation = None

    if existence_term:
        intent = fr.Intent.EXISTENCE
        fired.append(f"RULE_EXISTENCE:{existence_term}")
        if not corp_code:
            missing.append("corp")
        confidence = sum([bool(corp_code), bool(metric)]) / 2

    elif comparison_term:
        intent = fr.Intent.COMPARISON
        operation = "diff"
        fired.append(f"RULE_COMPARISON:{comparison_term}")
        if not vsel:
            vsel = "pair"
        for slot, ok in (("corp", corp_code), ("metric", metric), ("period", period.year)):
            if not ok:
                missing.append(slot)
        confidence = _confidence_fact(corp_code, metric, period)

    elif compute_term:
        intent = fr.Intent.COMPUTE
        operation = "growth"
        fired.append(f"RULE_COMPUTE:{compute_term}")
        for slot, ok in (("corp", corp_code), ("metric", metric), ("period", period.year)):
            if not ok:
                missing.append(slot)
        confidence = _confidence_fact(corp_code, metric, period)

    elif mterm and not metric:
        # 금융업 순이자손익 계열처럼 "인식은 했으나 매핑 정책이 없는" metric.
        intent = fr.Intent.CLARIFICATION
        fired.append("RULE_CLARIFICATION_METRIC_UNRESOLVED")
        missing.append(f"metric(unresolved:{mterm} — 매핑 정책 미결정, vocab.py 근거 참고)")
        confidence = 0.0

    elif corp_code and metric and (period.year or period.as_of_date):
        if scope:
            intent = fr.Intent.FACT_NUMERIC
            fired.append("RULE_FACT_NUMERIC")
        else:
            intent = fr.Intent.DUAL
            fired.append("RULE_DUAL_SCOPE_UNSPECIFIED")
        confidence = _confidence_fact(corp_code, metric, period)

    elif metric:
        # 지표는 인식했으나(=fact 의도가 분명함) 기업/기간이 부족함 — clarification.
        # (corp_code만 있고 metric이 아예 없는 경우는 fact 의도로 볼 근거가 없으므로
        # 여기서 걸지 않고 아래 narrative로 보낸다 — "삼성전자의 주요 사업 부문은?" 같은
        # 순수 서술형 질문을 clarification으로 오분류하지 않기 위함.)
        intent = fr.Intent.CLARIFICATION
        fired.append("RULE_CLARIFICATION_MISSING_SLOT")
        for slot, ok in (("corp", corp_code), ("metric", metric),
                         ("period", period.year or period.as_of_date)):
            if not ok:
                missing.append(slot)
        confidence = _confidence_fact(corp_code, metric, period)

    elif corp_code:
        intent = fr.Intent.NARRATIVE
        fired.append("RULE_NARRATIVE_FALLBACK_WITH_CORP")
        confidence = 0.5

    else:
        intent = fr.Intent.UNPARSED
        fired.append("RULE_UNPARSED_NO_ANCHOR")
        confidence = 0.0

    version_selector = vsel or "latest_effective"

    return fr.Frame(
        intent=intent, corp_code=corp_code, corp_name=corp_name, period=period,
        scope=scope, metric=metric, operation=operation,
        version_selector=version_selector, confidence=round(confidence, 3),
        fired_rules=fired, raw_question=q, missing_slots=missing,
    )


if __name__ == "__main__":
    import json
    if len(sys.argv) > 1:
        f = parse_intent(" ".join(sys.argv[1:]))
        print(json.dumps(f.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(__doc__)
