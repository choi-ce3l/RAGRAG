"""업종 → 기업 엣지.

`chunks.jsonl`의 `sector`(20종)·`industry`(8종) 필드에서 만든다. 질문이 기업을
열거하지 않고 "통신 업종에서"처럼 업종만 지시하는 경우, 이 엣지를 타고 기업 목록으로
편다. 그래프상 (Sector)-[HAS_MEMBER]->(Company) 에 해당한다.

원본 스캔이 5초쯤 걸리므로 작은 json으로 캐시한다.
"""

import json
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
CACHE = _HERE.parent / "data" / "sector_index.json"
CHUNKS = _HERE.parent.parent / "code_chunkingandparsing" / "out" / "chunks.jsonl"

# 업종 지시어가 함께 있어야 업종 확장을 켠다. "통신비" 같은 오탐을 막는다.
MARKERS = ("업종", "섹터", "산업", "분야")
# 업종명 바로 뒤에 붙으면 마커 없이도 인정하는 접미사 — "건설사"·"반도체업체".
_COMPANY_SUFFIX = re.compile(r"^(사|업체|기업|업계)")

_INDEX = None
_ALLOWED = None          # None이면 제한 없음. holdout에서 train 기업만 남길 때 쓴다.


def restrict(companies):
    """업종 소속을 이 기업들로 제한한다. None이면 해제."""
    global _ALLOWED
    prev, _ALLOWED = _ALLOWED, (set(companies) if companies is not None else None)
    return prev


def _build():
    sector, industry = {}, {}
    if not CHUNKS.exists():
        return {"sector": {}, "industry": {}}
    with CHUNKS.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            corp = d.get("corp_name")
            if not corp:
                continue
            for key, table in (("sector", sector), ("industry", industry)):
                v = d.get(key)
                if v:
                    table.setdefault(v, set()).add(corp)
    return {k: {n: sorted(v) for n, v in t.items()}
            for k, t in (("sector", sector), ("industry", industry))}


def load(rebuild=False):
    global _INDEX
    if _INDEX is not None and not rebuild:
        return _INDEX
    if CACHE.exists() and not rebuild:
        try:
            _INDEX = json.loads(CACHE.read_text(encoding="utf-8"))
            return _INDEX
        except json.JSONDecodeError:
            pass
    _INDEX = _build()
    if _INDEX["sector"]:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(_INDEX, ensure_ascii=False), encoding="utf-8")
    return _INDEX


def names():
    """긴 이름 우선으로 정렬한 업종명 (부분일치 오탐 방지)."""
    idx = load()
    return sorted(list(idx["sector"]) + list(idx["industry"]), key=len, reverse=True)


def members(name):
    idx = load()
    out = idx["sector"].get(name) or idx["industry"].get(name) or []
    return [c for c in out if c in _ALLOWED] if _ALLOWED is not None else out


def _match_in(text, has_marker):
    for n in names():
        for cand in [n] + n.split("·"):
            idx = text.find(cand)
            if idx < 0:
                continue
            if has_marker or _COMPANY_SUFFIX.match(text[idx + len(cand):]):
                return n
    return None


def find(question, trace=None):
    """질문에서 업종을 찾는다.

    "업종/섹터/산업/분야"가 있으면 인정하고, 없어도 업종명 바로 뒤에 회사를 가리키는
    접미사("건설**사**"·"반도체**업체**")가 붙으면 인정한다 — "건설사에서 제일
    매출이 큰 기업은?"에 마커가 없다는 이유로 아예 시도조차 안 하던 문제(실측
    확인)를 고친 것이다. "삼성전자 반도체 매출은?"처럼 업종명이 우연히 낱말로만
    등장한 경우는 이 접미사가 없어 여전히 걸러진다.

    "반도체·전자부품"처럼 복합 업종명은 구성 낱말 각각으로도 찾는다 — 사람은
    "반도체 분야에서는?"처럼 앞쪽 한 단어만 쓴다("반도체·전자부품" 전체를 그대로
    말하지 않는다, 실측 확인).

    3단계로 시도한다: (1) 원문 그대로, (2) 공백 제거 후 재시도 — "2차 전지"(질문
    표기)와 코퍼스 taxonomy의 "2차전지"(공백 없음) 같은 띄어쓰기 불일치, "통신
    기업"처럼 접미사 앞에 공백이 오는 경우 둘 다 이걸로 잡힌다. 새 업종이 추가돼도
    그대로 적용되는 정규화라 사전을 안 늘려도 된다. (3) 그래도 안 잡히면 "엔터"→
    "엔터테인먼트"처럼 표기 자체가 다른 축약·구어체 표현 — 이건 정규화로 못 푸니
    폐집합 LLM 폴백(resolve.py)에 넘긴다. 후보는 여기 등록된 업종·산업명뿐이라
    없는 이름을 지어내진 못한다.

    trace: 있으면(dict) 어느 경로로 찾았는지만 옆에 적어준다
    (``trace["source"] = "utterance"`` 또는 ``"resolve_llm"``) — 매칭 로직
    자체는 이 인자와 무관하게 그대로다. qa/ontology.py가 슬롯 출처 태그
    (p["_source"])를 채우는 데 쓴다.
    """
    has_marker = any(m in question for m in MARKERS)
    found = _match_in(question, has_marker)
    if found:
        if trace is not None:
            trace["source"] = "utterance"
        return found

    qn = question.replace(" ", "")
    found = _match_in(qn, has_marker or any(m in qn for m in MARKERS))
    if found:
        if trace is not None:
            trace["source"] = "utterance"
        return found

    from . import resolve
    picked = resolve.resolve(question, names(), "업종/산업 분류", "sector")
    if trace is not None:
        trace["source"] = "resolve_llm" if picked else None
    return picked
