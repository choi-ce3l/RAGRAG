"""compose.py — 결정론적 문장화.

숫자는 execute.py의 fact/compute 출력에서만 가져온다(LLM 호출 없음, 이 단계는 현재도
앞으로도 그렇다). execute.py의 각 경로 함수(fact_numeric/dual/compute/comparison/
existence)는 이미 사람이 읽을 수 있는 text를 만들어 반환하므로, 이 모듈의 역할은
"그 위에 출처(sources)를 표준 형식으로 덧붙이는 것"이다 — numqa의 기존 방식(문장 뒤에
근거를 별도로 붙이지 않고 answer dict의 sources 필드로만 남기는 방식)을 따르되, 지시된
compose.py 산출물 계약(sources에 rcept_no/fact_id/version/supersede_method 필수)을
이 레이어에서 강제한다.
"""

REQUIRED_SOURCE_KEYS = ("rcept_no", "fact_id", "version", "supersede_method")


def _normalize_source(src):
    """execute.py 각 경로가 넣는 source dict의 키가 조금씩 다르므로(예: fact_numeric은
    aclass/row도 넣고, existence는 rcept_no만 넣기도 함) 4개 필수 키를 항상 채워서
    반환한다 — 없는 키는 None으로 명시(누락을 숨기지 않는다)."""
    out = dict(src)
    for k in REQUIRED_SOURCE_KEYS:
        out.setdefault(k, None)
    return out


def compose(frame, result):
    """execute.py 결과(dict) -> 최종 답변 dict(text, numbers, sources, status, intent).

    narrative 스텁(status="stub")은 text가 비어 있으므로 그대로 통과시킨다 — 문장화할
    숫자 자체가 없다(§ 원칙: 숫자는 fact/compute 출력에서만).
    """
    sources = [_normalize_source(s) for s in result.get("sources", [])]
    text = result.get("text", "")
    return {
        "question": frame.raw_question,
        "intent": frame.intent.value if hasattr(frame.intent, "value") else frame.intent,
        "status": result.get("status", "unknown"),
        "text": text,
        "numbers": result.get("numbers", []),
        "sources": sources,
        "version_selector": (frame.version_selector.value
                              if hasattr(frame.version_selector, "value")
                              else frame.version_selector),
        "confidence": frame.confidence,
    }
