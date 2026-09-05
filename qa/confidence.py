"""신뢰도 요약 — 이미 계산된 검증 결과를 사람이 읽을 한 줄로 포맷팅만 한다.

pipeline.stage04_verify() 계열이 만드는 `verification = {"passed": bool,
"checks": [{"name","ok","detail"}, ...]}`와, 근거 좌표(`evidence`)마다 붙는
`flags`(⚠️정정 · ⛔대체됨 · ❓정정 여부 미확정)를 그대로 옮겨 적을 뿐,
여기서 새로운 판정 기준을 만들지 않는다.
"""

# to_coordinate()/struct_coordinate() 등이 붙이는 flags 문자열 그대로 — qa/pipeline.py 참고.
_SUPERSEDED = "⛔대체됨"
_CORRECTED = "⚠️정정"


def summarize(verification: dict, evidence: list, note_unverified: bool = False) -> str:
    """검증 checks와 근거 flags를 한 줄 요약으로 포맷팅한다. 볼 게 없으면 빈 문자열.

    note_unverified: [P3] Q_STATE_COHERENCE 플래그(기본 off, 호출부에서
    넘긴다). True면 checks가 0개일 때 조용히 빈 문자열을 내는 대신
    "미검증"임을 명시한다. 기본 off인 이유 — 이건 기존에 confidence_summary가
    비어 있던 수십 개 답변 경로의 화면 문구를 한꺼번에 바꾸는 변경이라,
    다른 P3 항목들과 함께 플래그로 묶어 사용자가 켜고 확인하게 한다.
    """
    parts = []

    checks = (verification or {}).get("checks") or []
    if checks:
        total = len(checks)
        failed = [c for c in checks if not c.get("ok")]
        if failed:
            parts.append(f"⚠️ {len(failed)}/{total} 검증 실패 — 근거 재확인 권장")
        else:
            parts.append(f"✅ {total}개 교차검증 통과")
    elif note_unverified:
        # 호출부(qa/pipeline.py)가 skip_confidence_note로 이 경로 자체가
        # "검증" 개념과 안 맞는 kind(용어 설명·감성판정 등)를 이미 걸러
        # 주므로, 여기 도달했다는 건 검증 가능한 종류의 답인데 실제로는
        # 안 거쳤다는 뜻이다.
        parts.append("❓ 미검증 — 이 경로는 별도 교차검증을 거치지 않았습니다")

    flags = {f for e in (evidence or []) for f in (e.get("flags") or [])}
    if _SUPERSEDED in flags:
        parts.append("⚠️ 이 값은 대체된 근거가 있어 재확인 권장")
    elif _CORRECTED in flags:
        parts.append("⚠️ 이 값은 정정 이력이 있어 재확인 권장")
    elif any(f.startswith("❓") for f in flags):
        parts.append("⚠️ 정정 여부가 확정되지 않아 재확인 권장")

    return " · ".join(parts)
