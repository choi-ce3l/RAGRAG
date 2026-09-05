"""소송사건 현황 표 — 원고(피고)명으로 행을 찾아 소송가액을 합산한다.

## 왜 필요한가

"부산광역시 상대 손해배상청구, 개인 149인 상대 분양대금반환청구, 행당제7구역
주택재개발정비사업조합 상대 채무부존재확인청구 세 건의 소송가액을 모두 합하면
얼마인가"류(SEM-NUM-02) — 온톨로지 어휘(qa/ontology.py)엔 "소송가액" 개념이
없어 01단계가 "지표를 특정하지 못함"으로 막힌다. 이 모듈은 온톨로지를 넓히는
대신, 질문에 열거된 원고명을 tables.py가 이미 갖고 있는 본문 표에서 부분/규칙
일치로 찾아 그 표의 값을 그대로 합산한다.

## 데이터소스 경계 — "20억원 이상" 표와 재무제표 주석 23.2 표는 다른 표다

사업보고서 재무제표 주석(예: "23.2 계류중인 소송현황")의 원고/피고/소송가액을
담은 큐레이션 10건 요약표는 build_tables.py가 재무제표 주석 섹션 자체를
파싱하지 않아 tables.db에 없다(직접 확인: 대우건설 rcept 20260318001029에
section이 '23.'으로 시작하는 행이 전혀 없다). 반면 "II. 사업의 내용" 쪽
"소송가액 20억원 이상인 중요한 소송사건 현황" 표(같은 rcept에 개별 소송을
나열한 표, 대우건설 기준 100건 이상)는 파싱돼 있고, 실측 결과 그 표의 개별
행 값이 재무제표 주석 표에 실리는 것과 같은 소송 건의 값과 일치한다(부산광역시
26,360백만원 등, SEM-NUM-02 gold와 대조 확인).

그래서 이 모듈은 그 "20억원 이상" 표만 조회한다. 재무제표 주석 23.2의 10건
큐레이션 표 **전체를 그대로 나열**하는 질문(예: SEM-BIZ-03)은 이 모듈로 풀 수
없다 — 그 표엔 이 표에 없는 원고(예: 대한민국 30,565백만원, 개인 6인
9,004백만원)가 실려 있고, 반대로 이 표엔 재무제표 주석 표에 없는 다른 소송도
많이 섞여 있다. 두 표는 선별 기준이 다른 서로 다른 표라 하나를 다른 하나의
대용품으로 쓰면 안 된다 — 그런 질문은 이 모듈이 손대지 않고 그대로 둔다.

## 원고명 매칭 규칙

1. 정확/부분 일치 — "부산광역시"·"행당제7구역주택재개발정비사업조합"처럼
   질문의 표현이 표의 원고 값과 그대로(또는 부분) 겹치는 경우.
2. "개인 N인"/"개인 N명" — 다수 개인이 함께 낸 소송은 표에 대표 원고 1인 +
   "외(N-1)"로 적힌다(예: "강서연 외148" = 강서연 포함 149인). 이 관례를 따라
   표에서 "외(N-1)"로 끝나는 원고를 찾는다. 후보가 둘 이상이면(확신이 없으면)
   손대지 않는다.
"""

import re

from . import tables

# "소송가액 20억원 이상" 표 — 사업의 내용 절에 있는 개별 소송 나열표.
# 재무제표 주석 23.2(계류중인 소송현황) 큐레이션 표와는 다른 표다(모듈
# docstring 참고).
_SECTION = "소송가액이 20억원 이상인 중요한 소송사건 현황은 아래와 같습니다."

# "...세 건의 소송가액을 모두 합하면..." · "...소송가액을 합산하면..." 류만
# 잡는다 — 이 모듈이 다루는 건 "열거된 여러 소송의 가액을 더하라"는 질문뿐이다.
_SUM_ASK = re.compile(r"소송가액.{0,20}(?:모두\s*)?(?:합하면|합산|합계|합치면)")

# 원고명 추출 — "OO 상대 XX청구"류에서 OO만 뽑는다. 두 패턴으로 나눈 이유:
# 조직·지역명(부산광역시 등)은 내부에 공백이 없는 한 토큰이라 그 한 토큰만
# 정확히 잡으면 되지만, 그걸 "토큰 여러 개까지 허용"으로 넓히면 한국어 어순상
# 바로 앞 수식어("...열거된 부산광역시")까지 함께 삼켜버린다(실측: "표에 열거된
# 부산광역시"가 통째로 잡힘). "개인 149인"류만 예외적으로 공백 낀 두 토큰
# (키워드+숫자단위)이므로, 그 키워드 자체로 앵커를 걸어 별도 패턴으로 뽑는다.
_GROUP_PHRASE = re.compile(
    r"((?:개인|주민|채권자|임차인|원고)\s*\d+\s*(?:인|명))\s*"
    r"(?:상대|을\s*상대로|를\s*상대로|에\s*대한)")
_SINGLE = re.compile(
    r"([가-힣A-Za-z0-9]+)\s*(?:상대|을\s*상대로|를\s*상대로|에\s*대한)")

# "개인 149인" · "채권자 6명"류 — 표에는 대표 원고 1인 + "외(N-1)"로 적힌다.
_GROUP_N = re.compile(r"^(?:개인|주민|채권자|임차인|원고)\s*(\d+)\s*(?:인|명)$")


def _norm(s):
    return re.sub(r"\s+", "", str(s or ""))


def wanted(question):
    """이 경로를 시도할 질문인가."""
    return bool(_SUM_ASK.search(question or ""))


def _extract_plaintiffs(question):
    names, seen = [], set()
    for seg in re.split(r"[,、]", question or ""):
        m = _GROUP_PHRASE.search(seg) or _SINGLE.search(seg)
        if not m:
            continue
        name = m.group(1).strip()
        key = _norm(name)
        if key and key not in seen:
            names.append(name)
            seen.add(key)
    return names


def _fetch_rows(corp_code):
    """(corp_code)의 "20억원 이상" 소송 표 전체 — 사업보고서 안의 최신본 우선.

    같은 소송이 여러 보고서(사업보고서·반기·분기)에 반복 등장하므로, "사업보고서"
    로 한정해 그중 가장 최근 것을 쓴다(labelstore.lookup_metric_annual이
    "그 해 사업보고서를 1차 출처로 삼는다"는 정책과 같은 결로 맞춘다).
    """
    t = tables.get()
    if t is None:
        return None
    cur = t.con.execute(
        "SELECT * FROM cells WHERE corp_code=? AND section=? AND is_superseded='0'"
        " AND report_nm LIKE '%사업보고서%' ORDER BY rcept_no DESC",
        (corp_code, _SECTION))
    rows = [dict(r) for r in cur]
    if not rows:
        return None
    by_row = {}
    for c in rows:
        key = (c.get("rcept_no"), c.get("table_idx"), c.get("row_idx"))
        by_row.setdefault(key, {})[c.get("header")] = c
    by_plaintiff = {}
    for key, cells in by_row.items():
        pl = cells.get("원고", {}).get("value_raw")
        if not pl:
            continue
        prev = by_plaintiff.get(pl)
        if prev is None or key[0] > prev[0][0]:      # 여러 rcept에 겹치면 최신 것
            by_plaintiff[pl] = (key, cells)
    return by_plaintiff


def _match_row(name, by_plaintiff):
    """이름 하나에 대응하는 표 행(cells dict)을 찾는다. 확신 없으면 None."""
    core = _norm(name)
    exact = [cells for pl, (_k, cells) in by_plaintiff.items() if _norm(pl) == core]
    if len(exact) == 1:
        return exact[0]
    partial = [cells for pl, (_k, cells) in by_plaintiff.items()
              if core and (core in _norm(pl) or _norm(pl) in core)]
    if len(partial) == 1:
        return partial[0]

    m = _GROUP_N.match(name.strip())
    if m:
        n = int(m.group(1))
        cand = [cells for pl, (_k, cells) in by_plaintiff.items()
               if re.search(rf"외\s*{n - 1}(?:\D|$)", pl)]
        if len(cand) == 1:
            return cand[0]
    return None


def sum_named(question, corp_code, corp_name):
    """(문장, 근거 rcept_no 목록, 숫자 목록) 또는 (None, 사유, [])."""
    names = _extract_plaintiffs(question)
    if len(names) < 2:
        return None, "질문에서 원고명을 둘 이상 뽑지 못했습니다.", []

    by_plaintiff = _fetch_rows(corp_code)
    if not by_plaintiff:
        return None, "소송사건 현황 표를 찾지 못했습니다.", []

    picked = []
    for name in names:
        cells = _match_row(name, by_plaintiff)
        if cells is None:
            return None, f"표에서 원고 '{name}'을(를) 특정하지 못했습니다.", []
        picked.append((name, cells))

    total = 0
    parts, used_rcepts, nums = [], set(), []
    for name, cells in picked:
        amt_cell = cells.get("당사분소송가액")
        if not amt_cell or amt_cell.get("value_num") in (None, ""):
            return None, f"'{name}'의 소송가액을 표에서 읽지 못했습니다.", []
        try:
            amt = int(round(float(amt_cell["value_num"])))
        except (TypeError, ValueError):
            return None, f"'{name}'의 소송가액 형식을 읽지 못했습니다.", []
        total += amt
        parts.append(f"{cells['원고']['value_raw']}({amt:,})")
        used_rcepts.add(amt_cell.get("rcept_no"))
        nums.append(str(amt))

    report_nm = picked[0][1]["원고"].get("report_nm", "")
    text = (f"{corp_name} {report_nm} 본문 표('소송가액 20억원 이상인 중요한 "
           f"소송사건 현황') 기준 — {' + '.join(parts)} = {total:,}백만원.")
    nums.append(str(total))
    return text, sorted(used_rcepts, reverse=True), nums
