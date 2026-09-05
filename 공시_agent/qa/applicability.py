"""업종별 지표 적합성 — 이 지표가 이 회사에 의미가 있는가.

은행에 재고자산회전율을 계산해 주면 숫자는 나오지만 뜻이 없다. 게임사에 매출원가율도
마찬가지다. 이런 판단을 손으로 표를 만들어 넣으면 오버핏이고 업종이 늘 때마다 사람이
써야 한다.

**corpus가 답을 갖고 있다** — 그 업종 기업들이 실제로 그 계정을 보고하는가.

    재고자산   금융·보험 0/8 · 게임 0/3 · 반도체 4/5 · 건설 2/3
    수수료수익  금융·보험 7/8 · 그 외 전부 0

동의어 그룹으로 세는 것이 중요하다. 게임 3사는 `매출액`을 안 쓰고 `영업수익`을 쓰므로,
표기만 보면 "게임 업종에 매출액은 부적합"이라는 오판이 나온다. 그룹으로 보면 3/3이다.
"""

import collections
import json
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "data" / "applicability.json"

ZERO = 0.0          # 아무도 안 쓰면 부적합
RARE = 0.3          # 3할 미만이면 드묾 — 답하되 경고

_TABLE = None


def _build(facts, ci, sector_index):
    have = collections.defaultdict(set)
    for f in facts:
        lab = f.get("label_norm") or f.get("label_raw")
        if lab and f.get("corp_name"):
            from .concepts import canonical
            have[f["corp_name"]].add(canonical(lab))
    corp_sector = {c: s for s, ms in sector_index.items() for c in ms}
    return {"have": {k: sorted(v) for k, v in have.items()},
            "corp_sector": corp_sector,
            "sector_members": {s: sorted(set(ms) & set(have)) for s, ms in sector_index.items()}}


def load(rebuild=False):
    global _TABLE
    if _TABLE is not None and not rebuild:
        return _TABLE
    if CACHE.exists() and not rebuild:
        try:
            _TABLE = json.loads(CACHE.read_text(encoding="utf-8"))
            _TABLE["have"] = {k: set(v) for k, v in _TABLE["have"].items()}
            return _TABLE
        except json.JSONDecodeError:
            pass
    from . import concepts, labelstore, sectors
    t = _build(labelstore.get().facts, concepts.get(), sectors.load()["sector"])
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")
    t["have"] = {k: set(v) for k, v in t["have"].items()}
    _TABLE = t
    return _TABLE


def reporting_rate(sector, concept):
    """그 업종에서 이 개념(동의어 포함)을 보고하는 기업 수 / 전체."""
    from . import concepts
    t = load()
    ci = concepts.get()
    grp = set(ci.group_members(concept)) or {concept}
    members = t["sector_members"].get(sector, [])
    hits = sum(1 for m in members if grp & t["have"].get(m, set()))
    return hits, len(members)


def sector_signature(sector, top=4):
    """그 업종만 쓰는 대표 계정 — '대신 이런 걸 씁니다'를 말하기 위한 것."""
    from . import concepts
    t = load()
    ci = concepts.get()
    members = set(t["sector_members"].get(sector, []))
    if not members:
        return []
    others = set(t["have"]) - members
    other_concepts = set()
    for m in others:
        other_concepts |= t["have"][m]
    cnt = collections.Counter()
    for m in members:
        for c in t["have"][m]:
            if c not in other_concepts:
                cnt[c] += 1
    return [c for c, n in cnt.most_common(top * 3)
            if n >= max(2, len(members) // 2) and ci.coverage(c) >= 2][:top]


def assess(corp_name, checked):
    """(적합성 판정, 근거). checked는 확인할 개념 목록(파생이면 피연산자들)."""
    t = load()
    sector = t["corp_sector"].get(corp_name)
    own = t["have"].get(corp_name, set())
    from . import concepts
    ci = concepts.get()

    rows, worst = [], 1.0
    for c in checked:
        grp = set(ci.group_members(c)) or {c}
        if grp & own:                       # 회사가 직접 보고하면 더 볼 것 없다
            rows.append({"개념": c, "보고": "자사 보고", "비율": 1.0})
            continue
        if not sector:
            rows.append({"개념": c, "보고": "업종 미상", "비율": None})
            continue
        h, n = reporting_rate(sector, c)
        r = (h / n) if n else None
        rows.append({"개념": c, "보고": f"{h}/{n}개사", "비율": r})
        if r is not None:
            worst = min(worst, r)

    verdict = ("부적합" if worst <= ZERO else "드묾" if worst < RARE else "적합")
    return {"sector": sector, "verdict": verdict, "worst": worst,
            "checks": rows,
            "signature": sector_signature(sector) if verdict == "부적합" and sector else []}


def concepts_to_check(p):
    """이 질문이 실제로 어떤 계정에 의존하는가.

    metric 경로에서는 concept이 `revenue` 같은 영문 키다. 그대로 라벨과 대조하면
    어느 업종에서도 0건이 나와 전부 "부적합"이 된다. 한글 대표 개념으로 되돌린다.
    """
    from . import concepts as C
    from . import derived
    if p.get("derived") and p["derived"] in derived.RULES:
        return list(derived.RULES[p["derived"]][1])
    if p.get("label"):
        # XBRL 재무제표 계정일 때만 적합성을 따진다. `자기주식취득`·`현금배당`처럼
        # 주요사항보고서 항목은 애초에 재무제표 어휘가 아니므로 "0/4개사"가 당연하고,
        # 그걸 부적합으로 읽으면 답할 수 있는 질문을 거절하게 된다.
        return [p["label"]] if p["label"] in C.get().surfaces else []
    if p.get("metric"):
        ci = C.get()
        members = [c for c, m in ci.metric_of.items() if m == p["metric"]]
        return [max(members, key=ci.coverage)] if members else []
    return []
