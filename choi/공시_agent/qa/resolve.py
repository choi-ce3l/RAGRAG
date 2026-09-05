"""폐집합(closed-set) 표현 해석 — "엔터업계"가 정확히 어느 업종명을 가리키는지 등,
사람이 쓰는 캐주얼한 표현을 시스템이 아는 고정 어휘 목록 중 하나로 좁혀 맵핑한다.

## 왜 규칙기반 별칭 사전이 아닌가
sectors.py·concepts.py·derived.py는 지금까지 "실패 사례 하나 발견 → 별칭 하나 손으로
추가"(ALIASES·_MANUAL_SYNONYMS) 방식이었다. 이 방식은 딱 발견한 표현까지만 고쳐지고,
다음 사용자가 다른 캐주얼 표현("엔터쪽", "제약바이오")을 쓰면 또 막힌다 — 어휘
목록은 유한하고 사람 말은 무한하다.

## 왜 안전한가 — 폐집합 강제
LLM에게 "새 이름을 만들어라"가 아니라 "이 목록 중 하나를 그대로 골라라, 없으면
'없음'"이라고 시킨다. 출력이 후보 목록의 원소와 글자 그대로 일치하지 않으면
버린다(resolve()의 검증) — 할루시네이션이 섞여도 존재하지 않는 값으로는 못 나간다.
narrative.py·glossary.py와 같은 경계다: "숫자는 LLM이 만들지 않는다"를 "고정 어휘도
LLM이 새로 만들지 않는다"로 한 겹 넓힌 것뿐이다.

## 캐시로 "성장"시킨다
같은 질문이 반복되면 규칙기반처럼 즉시 응답한다 — 실사용 트래픽이 스스로 별칭
사전을 채워나가는 구조라, 세션마다 사람이 스크린샷 보고 하나씩 손으로 패치할
필요가 없다. namespace별로 캐시 파일을 분리해 카테고리가 섞이지 않게 한다.
"""

import json
from pathlib import Path

from .narrative import call, enabled

_HERE = Path(__file__).resolve().parent
_CACHE_DIR = _HERE.parent / "data" / "resolve_cache"

_SYSTEM = (
    "당신은 사용자 질문에서 {category}을(를) 식별하는 분류기입니다.\n"
    "아래 '후보 목록'에 있는 표기 중 질문이 가리키는 것과 정확히 일치하는 표기 "
    "하나를 그대로 출력하십시오. 목록에 해당하는 게 없으면 정확히 '없음'이라고만 "
    "출력하십시오.\n"
    "규칙:\n"
    "0. 가장 먼저, 질문이 애초에 {category}를 묻는 질문인지부터 판단하십시오. "
    "질문이 묻는 것의 종류 자체가 후보들과 다르면(예: 후보가 전부 '비율'인데 "
    "질문은 절대 금액·보유 현황을 묻는다) 후보들이 아무리 소재가 비슷해 보여도 "
    "그중 하나를 고르지 말고 '없음'을 출력하십시오.\n"
    "1. 0번을 통과했을 때만 — 질문의 표현이 후보의 동의어·줄임말·다른 표기일 "
    "때만 그 후보를 고르십시오 (예: 후보에 '엔터테인먼트'가 있고 질문이 "
    "'엔터업계'면 '엔터테인먼트').\n"
    "2. 질문이 가리키는 것이 후보 중 하나의 상위 개념·인접 분야·일부일 뿐 그 "
    "후보 자체를 가리키는 게 아니면 절대 아무 후보나 고르지 말고 '없음'을 "
    "출력하십시오 (예: 후보에 '제조'라는 표기가 없는데 질문이 '제조업계'라면, "
    "제조와 관련 있어 보이는 후보를 아무거나 고르지 말고 '없음').\n"
    "3. 목록에 없는 새 이름을 만들어내지 마십시오 — 약간이라도 확신이 없으면 "
    "'없음'을 고르십시오.\n"
    "4. 출력은 후보 표기 하나 또는 '없음' 뿐입니다. 설명·따옴표·이유를 붙이지 "
    "마십시오.")


def _cache_path(namespace):
    return _CACHE_DIR / f"{namespace}.json"


def _cache_load(namespace):
    p = _cache_path(namespace)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _cache_save(namespace, cache):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(namespace).write_text(
        json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8")


def resolve(question, candidates, category, namespace, timeout=8):
    """question이 candidates 중 무엇을 가리키는지 고른다. 없거나 실패하면 None.

    규칙기반 매칭이 이미 실패한 뒤에만 부르는 폴백이다 — 비용이 드는 경로라
    narrative.py와 같은 게이트(NARRATIVE_ENABLED)를 공유한다.
    """
    if not candidates or not enabled():
        return None
    cache = _cache_load(namespace)
    key = question.strip()
    if key in cache:
        return cache[key] or None

    listing = ", ".join(candidates)
    system = _SYSTEM.format(category=category)
    user = f"후보 목록: {listing}\n질문: {question}"
    text, _err, _usage = call(system, user, max_tokens=30, temperature=0.0,
                              timeout=timeout)
    resolved = text.strip().strip('"').strip("'") if text else None
    if resolved not in candidates:            # 폐집합 검증 — 목록 밖 값은 전부 버린다
        resolved = None

    cache[key] = resolved or ""
    _cache_save(namespace, cache)
    return resolved
