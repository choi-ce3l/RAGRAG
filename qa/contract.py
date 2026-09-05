"""사건 지목 조회 — 계약·공시 하나를 특정하고 그 안에서 읽는다.

## 왜 필요한가

정답셋에는 특정 계약을 가리키며 그 수치를 묻는 질문이 있다.

    "'행당제7구역 주택재개발정비사업' 관련 단일판매·공급계약의 계약금액과 매출액대비 비율은?"
    "삼성전자-테슬라 반도체 위탁생산 공급계약에 기재된 최근매출액은?"

접수번호를 대주지 않는다. 대신 **계약 이름을 말한다.** 그리고 그 이름은 공시에
`체결계약명`으로 그대로 들어 있다.

```
20250729800001  대우건설 · [기재정정]단일판매ㆍ공급계약체결
   체결계약명      행당제7구역 주택재개발정비사업
   계약금액(원)    255,316,876,300
   매출액대비(%)   3.13
   계약상대       행당제7구역 주택재개발정비사업 조합
```

## 계산하지 않는다

"매출액대비 몇 %"는 나눗셈처럼 보이지만 **공시에 이미 적혀 있다.** 우리가 계산하면
공시된 값과 미세하게 달라질 수 있다(회사가 어떤 매출액을 썼는지 모른다).
적혀 있으면 적힌 값을 쓴다.

## 필드 매칭이 여기서는 쉽다

`보고자`가 9개 키에, `보통주식`이 58개 키에 붙어 있어 전역 필드 매칭은 어렵다.
그런데 **공시 하나로 좁히면 필드가 10개뿐이다.** 좁히고 나서 고르면 된다 —
정정 전후 대조에서 쓴 것과 같은 수법이다.
"""

import re

from . import filings

# 계약·사건을 지목하는 신호
EVENT = re.compile(r"계약|수주|공급|영업정지|취득|처분|증자|합병|분할")
# 공시를 가리키는 데 쓸 필드를 목록으로 정하지 않는다.
#
# 처음엔 체결계약명·계약상대·판매지역만 봤다. 그러면 그 목록에 없는 서식
# (영업양수·합병·증권발행 …)은 영영 못 찾는다. 서식마다 이름 칸이 다르다.
#
# 대신 **모든 텍스트 필드**를 후보로 두고, 질문과 연속 6자 이상 겹치는지로 가른다.
# 6자면 우연히 겹치기 어렵고, 짧은 상투어(주식회사·해당없음)는 자연히 걸러진다.
# 판단을 목록이 아니라 값이 하게 둔다.
MIN_MATCH = 6


def _norm(s):
    return re.sub(r"[\s'\"·ㆍ()]", "", str(s or ""))


def _overlap(val, qn):
    """공시의 이름이 질문에 얼마나 길게 들어 있는가.

    전체 일치만 보면 놓친다 — 공시의 체결계약명은 "반도체 위탁생산 공급계약"인데
    질문은 "반도체 위탁생산 관련 단일판매·공급계약체결"이라고 쓴다. 사이에 말이
    끼어 통째로는 안 맞는다.

    그래서 **연속 6자 이상**이 겹치는지를 본다. 6자는 우연히 겹치기 어렵다.
    """
    if len(val) < MIN_MATCH:
        return 0
    best = 0
    for i in range(len(val) - MIN_MATCH + 1):
        for j in range(len(val), i + MIN_MATCH - 1, -1):
            if val[i:j] in qn:
                best = max(best, j - i)
                break
    return best


def find(question, corp, top=3):
    """질문이 가리킨 공시. 텍스트 필드 값이 질문에 얼마나 길게 들어 있는지로 고른다."""
    if not EVENT.search(question):
        return []
    fl = filings.get()
    qn = _norm(question)
    scored = []
    for rn, m in fl.meta.items():
        if m.get("corp_name") != corp:
            continue
        f = fl.fields(rn)
        if not f:
            continue
        best = 0
        for k, v in f.items():
            if v.get("kind") != "text":
                continue
            best = max(best, _overlap(_norm(v.get("raw")), qn))
        if best:
            scored.append((best, rn))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [rn for _, rn in scored[:top]]


def read(question, rcept_no):
    """그 공시에서 질문이 요구한 필드를 읽는다. 못 고르면 빈 목록."""
    fl = filings.get()
    f = fl.fields(rcept_no)
    qn = _norm(question)
    out = []
    for k, v in f.items():
        lab = str(v.get("label") or k)
        # 라벨은 "3. 계약상대" · "- 체결계약명"처럼 앞머리 번호·기호를 달고 있다.
        # 그걸 안 지우면 질문의 "계약 상대방"과 영영 안 맞는다.
        core = re.sub(r"^[\s.\-]*\d*\s*[.)]?\s*", "", lab)
        core = _norm(re.sub(r"\(.*?\)", "", core))      # "계약금액(원)" → "계약금액"
        if len(core) >= 2 and core in qn:
            # 답변에는 서식 번호를 떼고 보여준다 — "3. 계약상대"가 아니라 "계약상대"
            show = re.sub(r"^[\s.\-]*\d*\s*[.)]?\s*", "", lab).strip()
            out.append({"label": show or lab, "raw": v.get("raw"), "dec": v.get("dec"),
                        "kind": v.get("kind")})
    return out


# "그 시점의 공시 문서상" · "최초로 제출한 … 원문에서" — 정정 전 원본을 묻는 말.
# 이걸 안 보면 최신 정정본을 답한다. 계약상대방이 "글로벌 대형기업"에서
# "테슬라"로 바뀐 공시에서, 최초 표기를 물었는데 정정 후 표기를 답했다.
FIRST = re.compile(r"최초(?:로)?\s*(?:제출|공시|기재)|그\s*시점의?\s*공시|원문에서"
                   r"|처음\s*(?:제출|공시)|정정\s*전")


def answer(question, corp):
    """(문장, 근거 접수번호들, 숫자들, 읽은 필드들) 또는 (None, 사유, [], [])."""
    rns = find(question, corp)
    if not rns:
        return (None, "질문이 가리킨 계약·공시를 찾지 못했습니다.", [], [])
    fl = filings.get()
    # 기본은 유효본(최신). 다만 최초 공시를 물었으면 가장 이른 것을 본다.
    rn = (min(rns, key=lambda r: r) if FIRST.search(question)
          else max(rns, key=lambda r: r))
    got = read(question, rn)
    m = fl.meta.get(rn, {})
    if not got:
        f = fl.fields(rn)
        opts = " · ".join(str(v.get("label") or k) for k, v in list(f.items())[:8])
        return (None, f"{corp}의 {m.get('report_nm','')}을(를) 찾았지만 어느 항목을 "
                      f"묻는지 좁히지 못했습니다 — {opts} 중 어느 것입니까?", [], [])
    parts = " / ".join(f"{g['label']} {g['raw']}" for g in got)
    return (f"{corp} {m.get('report_nm','')} ({m.get('rcept_dt','')}) 기준 — {parts}.",
            [rn], [str(g["raw"]).replace(",", "") for g in got], got)
