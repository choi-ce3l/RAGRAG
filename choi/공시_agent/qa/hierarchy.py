"""계정 계층 유도 — 합계 관계를 corpus에서 뽑는다.

XBRL 택소노미의 calculation linkbase가 주는 것("자산총계 = 유동자산 + 비유동자산")을
외부 자료 없이 우리 데이터에서 유도한다. 값이 이미 다 있으므로 합이 맞는지 검사하면 된다.

## 방법
재무제표 한 장(doc·statement·scope·연도)의 행을 원문 순서(row_index)대로 놓고,
각 행 i에 대해 바로 앞의 연속 구간 j..i-1의 합이 값[i]와 같은지 본다. 맞으면
`행 i = 행 j..i-1의 합`이라는 관계 후보다.

한 장에서 우연히 맞을 수 있으므로, **여러 기업에서 반복 확인된 관계만** 채택한다.
파생 개념에서 "규칙은 회계 지식, 검증은 corpus"였다면 여기는 **관계도 검증도 corpus**다.

## 쓰임
- 04 검증에 "합계가 맞는가" 규칙 추가
- 온톨로지에 PART_OF 관계 — 개념 그래프가 평평한 목록에서 계층으로
"""

import collections
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .concepts import canonical

CACHE = Path(__file__).resolve().parent.parent / "data" / "hierarchy.json"

MAX_CHILDREN = 12          # 이보다 긴 구간은 우연 일치 가능성이 커진다
MIN_CHILDREN = 2
TOL = Decimal("0.005")     # 상대오차 0.5% — 원문 반올림 흡수
MIN_CORPS = 5              # 이 기업 수 이상에서 확인된 관계만 채택


def _won(f):
    try:
        return Decimal(f["value_decimal"]) * Decimal(f.get("scale") or 1)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _sheets(facts):
    """재무제표 한 장 단위로 묶는다 — 같은 문서·표·기준·연도."""
    by = collections.defaultdict(list)
    for f in facts:
        lab = f.get("label_norm") or f.get("label_raw")
        v = _won(f)
        if not lab or v is None or f.get("row_index") is None:
            continue
        key = (f["doc_id"], f["statement"], f["scope"], f["base_year"])
        by[key].append((f["row_index"], canonical(lab), v, f["corp_name"]))
    for key, rows in by.items():
        rows.sort(key=lambda r: r[0])
        # 같은 행이 여러 번 나오면 첫 것만
        seen, uniq = set(), []
        for r in rows:
            if r[0] in seen:
                continue
            seen.add(r[0])
            uniq.append(r)
        if len(uniq) >= 3:
            yield key, uniq


def _close(a, b):
    return b != 0 and abs(a - b) / abs(b) <= TOL


def _find_sums(rows, max_pass=14):
    """합계-구성요소 관계를 찾되, 찾을 때마다 접고 다시 본다.

    한국 재무제표는 **소계가 세부 항목보다 앞에** 온다.

        유동자산            227,062,266      ← 소계가 먼저
          현금및현금성자산      53,705,579
          단기금융상품         58,909,334
          …
        비유동자산          287,469,682      ← 또 소계가 먼저
          유형자산          205,945,209
          …
        자산총계            514,531,948      ← 이건 뒤에 오는 총계

    그래서 두 방향을 다 본다 — 뒤따르는 구간의 합(선행 소계)과 앞선 구간의 합(후행 총계).
    찾은 구간은 부모 한 줄로 접는다. 세부가 소계로 접히고 나면 소계들이 이웃이 되어
    상위 총계가 드러난다. 계층이 아래에서 위로 나온다.
    """
    found, cur = [], list(rows)
    for _ in range(max_pass):
        vals = [r[2] for r in cur]
        pre = [Decimal(0)]
        for v in vals:
            pre.append(pre[-1] + v)
        hit = None

        # ① 선행 소계 — 값[i] == 뒤따르는 k개의 합
        for i in range(0, len(cur) - MIN_CHILDREN):
            if vals[i] == 0:
                continue
            for k in range(MIN_CHILDREN, min(MAX_CHILDREN, len(cur) - i - 1) + 1):
                if _close(pre[i + 1 + k] - pre[i + 1], vals[i]):
                    hit = ("lead", i, i + 1, i + 1 + k)
                    break
            if hit:
                break

        # ② 후행 총계 — 값[i] == 앞선 구간의 합
        if not hit:
            for i in range(MIN_CHILDREN, len(cur)):
                if vals[i] == 0:
                    continue
                for j in range(0, i - MIN_CHILDREN + 1):
                    if i - j > MAX_CHILDREN:
                        continue
                    if _close(pre[i] - pre[j], vals[i]):
                        hit = ("trail", i, j, i)
                        break
                if hit:
                    break

        if not hit:
            break
        _, pi, cs, ce = hit
        parent = cur[pi][1]
        children = tuple(cur[k][1] for k in range(cs, ce))
        if parent not in children and len(set(children)) == len(children):
            found.append((parent, children))
        cur = [r for idx, r in enumerate(cur) if not (cs <= idx < ce)]
    return found


def derive(facts):
    """(부모, 자식들) → 확인된 기업 수."""
    hits = collections.defaultdict(set)
    for _, rows in _sheets(facts):
        corp = rows[0][3]
        for parent, children in _find_sums(rows):
            if parent in children:            # 자기 자신을 포함하면 무효
                continue
            hits[(parent, children)].add(corp)
    return {k: v for k, v in hits.items() if len(v) >= MIN_CORPS}


def build(facts):
    rel = derive(facts)
    out = []
    for (parent, children), corps in sorted(rel.items(), key=lambda x: -len(x[1])):
        out.append({"parent": parent, "children": list(children), "corps": len(corps)})
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


_REL = None


def get(facts=None, rebuild=False):
    global _REL
    if _REL is not None and not rebuild:
        return _REL
    if CACHE.exists() and not rebuild:
        try:
            _REL = json.loads(CACHE.read_text(encoding="utf-8"))
            return _REL
        except json.JSONDecodeError:
            pass
    if facts is None:
        from . import labelstore
        facts = labelstore.get().facts
    _REL = build(facts)
    return _REL


def parents_of(concept):
    """이 개념이 어느 합계에 들어가는가."""
    return [r for r in get() if concept in r["children"]]


def children_of(concept):
    """이 합계는 무엇들의 합인가. 가장 널리 확인된 것 우선."""
    return sorted([r for r in get() if r["parent"] == concept],
                  key=lambda r: -r["corps"])
