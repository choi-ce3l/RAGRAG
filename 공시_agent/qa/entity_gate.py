"""렌더 직전 엔티티 정합 게이트 — 최후 방어선.

## 왜 필요한가

멀티턴 대화에서 재작성(rewrite)·업종 확장·resolve.py LLM 폴백 등 여러 경로가
기업을 서로 다르게 확정할 수 있다. 상류 로직 어디선가 실수를 해도, 최종 답변이
"근거는 A사, 해석은 B사, 본문은 C사"처럼 서로 다른 회사를 가리키면 안 된다 —
실제로 이런 사고가 났다(T5: 답변 본문 "판단할 수 없습니다" 뒤에 이번 대화에
한 번도 언급 안 된 LIG디펜스앤에어로스페이스에 대한 완결된 투자판단 문단이
이어짐, 근거도 그 회사).

이 모듈은 그 사고를 막는 순수 검사 함수 하나다 — 렌더링 로직을 바꾸지 않고,
`qa/pipeline.py::run()`이 최종 리턴 직전에 호출만 한다.

## 규칙 — "같아야 한다"가 아니라 "벗어나면 안 된다"

정상적인 다중기업 비교(예: "삼성전자와 SK하이닉스 비교")는 여러 회사가 정당하게
근거·본문에 다 나오는 게 맞다. interpretation(파싱 결과의 기업 집합)이 애초에
여러 개면 그 전부가 정상 범위다. 그래서

    근거(evidence)의 기업 ⊆ interpretation이 허용한 기업
    본문에 언급된 기업 ⊆ interpretation이 허용한 기업

방향으로 짠다 — "정확히 같아야 한다"가 아니다. 본문 검사의 상한을 evidence가
아니라 interpretation으로 잡는 이유는 아래 check()의 주석 참고 — evidence의
corp_name은 "공시 제출인"만 가리켜서, 그 공시 안에 정당하게 등장하는 제3자
이름(주주·거래상대방 등)까지 걸러내면 오탐이 난다.
"""


def _interp_corps(p):
    p = p or {}
    corps = set(p.get("corps") or [])
    if p.get("corp"):
        corps.add(p["corp"])
    return corps


def _evidence_corps(evidence):
    return {e.get("corp_name") for e in (evidence or []) if e.get("corp_name")}


def _mentioned_corps(text, universe):
    """answer_text에 실제로 등장하는, universe(회사명 후보 전체) 소속 기업명들."""
    text = text or ""
    return {name for name in universe if name and name in text}


def check(p, evidence, answer_text, all_corp_names=None):
    """정합성이 깨졌으면 사유 문자열, 문제없으면 None.

    p: r.parsed (interpretation). evidence: r.evidence. answer_text: r.answer_text.
    all_corp_names: 전체 기업명 사전(있으면 본문 검사 범위를 넓힌다) — 없어도
    interpretation·evidence에 등장한 이름만으로는 검사한다.

    기업이 애초에 특정 안 된 질문(용어설명 등, interpretation 기업 집합이
    비어 있음)은 검사 대상이 아니다 — 검사할 "허용 범위" 자체가 없다.
    """
    interp = _interp_corps(p)
    if not interp:
        return None

    ev_corps = _evidence_corps(evidence)
    if ev_corps and not ev_corps.issubset(interp):
        extra = ev_corps - interp
        return f"근거 기업이 해석 범위를 벗어남: {', '.join(sorted(extra))}"

    # 본문 검사는 evidence가 아니라 interpretation을 상한으로 쓴다. evidence의
    # corp_name은 "그 fact를 낸 공시 주체(=제출인)"만 가리키는 필드라, 정당한
    # 질문에서도 본문이 그 공시 *안에서* 언급된 제3의 이름(주주·거래상대방 등)을
    # 다루면 곧바로 evidence 밖으로 나가 버린다 — 실측 회귀: "현대건설 사업
    # 보고서에 나온 현대자동차·기아·현대모비스 지분율"에서 evidence.corp_name은
    # 전부 "현대건설"인데 본문은 정당하게 세 주주명을 나열해야 해서 오탐이 났다.
    # interpretation(find_corps가 질문 원문에서 이미 찾아낸 기업 전체)을 상한으로
    # 쓰면 이런 정당한 경우를 놓치지 않으면서도, 애초에 질문에 없던 기업(T5의
    # LIG디펜스앤에어로스페이스처럼)이 근거·본문에 새로 등장하는 오염은 그대로 잡는다.
    universe = set(all_corp_names or []) | interp | ev_corps
    body_corps = _mentioned_corps(answer_text, universe)
    if body_corps and not body_corps.issubset(interp):
        extra = body_corps - interp
        return f"본문에 언급된 기업이 근거 범위를 벗어남: {', '.join(sorted(extra))}"

    return None
