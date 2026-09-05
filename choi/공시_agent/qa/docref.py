"""문서 지정 참조 — "그 보고서에 기재된 값"을 그 보고서에서 읽는다.

## 왜 필요한가

정답셋에는 이런 질문이 많다.

    CJ제일제당 2025년 사업보고서(제19기)에 **비교표시된** 제17기(2023년) 영업이익은?

우리는 `base_year=2023`으로 조회하고 정정되지 않은 최신 접수번호를 골랐다. 그래서
2023년 값을 **가장 최근 보고서 기준으로** 돌려줬다. 그런데 질문이 원한 것은
**제19기 사업보고서에 인쇄된** 2023년 열이다. 재작성·정정이 있으면 둘은 다르다.

    20260316001116 (사업보고서 2025.12) 안의 연결 영업이익
        제19기 2025  1,233,604,641 천원
        제18기 2024  1,452,067,669 천원
        제17기 2023  1,369,933,419 천원   ← 질문이 원한 값

한 문서 안에 여러 기수 열이 함께 실린다. 문서를 고정하면 그 열을 정확히 집을 수 있다.

## 문서를 가리키는 세 가지 형태

    rcept_no 20260316001116          접수번호 — 가장 확실하다
    사업보고서(2025.12)               보고서 종류 + 결산 연월
    제19기 사업보고서                  보고서 종류 + 자기 기수

## 마스킹

문서 참조 안에도 연도가 들어 있다("2025년 사업보고서(제19기)"). 그대로 두면 조회할
열을 2025년으로 잡는다. 질문이 원한 건 제17기인데도 그렇다. 그래서 참조 부분을
지운 문장을 따로 돌려주고, 열 선택은 그 문장에서 읽는다.
"""

import re

RCEPT = re.compile(r"(?:rcept[_\s]*no|접수번호)\s*[:=]?\s*(\d{14})")
# 괄호 안이든 아니든 14자리 숫자는 접수번호 후보다. 위치로 정하지 않고
# **corpus에 실제로 있는 번호인지**로 가른다 — 형식 규칙보다 데이터가 낫다.
RCEPT_BARE = re.compile(r"(?<!\d)(\d{14})(?!\d)")
KIND = r"(사업|반기|분기)\s*보고서"
# 사업보고서(2025.12) · 분기보고서 (2024.03)
DOC_YM = re.compile(KIND + r"\s*\(?\s*(\d{4})\s*\.\s*(\d{2})")
# 2025년 1분기(2025.03) 분기보고서 · (2023.12 결산) 사업보고서 — 연월이 종류보고서
# 앞에 먼저 오는 어순(DOC_YM은 종류가 먼저 오는 어순만 잡는다).
DOC_YM_LEAD = re.compile(r"\(\s*(\d{4})\s*\.\s*(\d{2})[^)]{0,20}\)\s*" + KIND)
# 2025년 사업보고서 · 제19기 사업보고서 · 사업보고서(제19기 …)
DOC_TERM = re.compile(r"제\s*(\d+)\s*기\s*(?:말\s*)?" + KIND)
DOC_TERM_TAIL = re.compile(KIND + r"\s*\(\s*제\s*(\d+)\s*기")
# 제17기(2024.12) 사업보고서 — 기수와 연월이 함께 괄호 안에 오고 종류가 뒤따르는 어순.
# DOC_TERM/DOC_TERM_TAIL 어느 쪽도 기수+연월이 함께인 경우를 못 잡는다.
DOC_TERM_YM = re.compile(r"제\s*(\d+)\s*기\s*\(\s*(\d{4})\s*\.\s*(\d{2})\s*\)\s*" + KIND)
# 2025년 사업보고서 · 2025년 말 사업보고서 — 결산 연도로 문서를 가리키는 형태
DOC_YEAR = re.compile(r"(\d{4})\s*년\s*(?:말\s*)?" + KIND)
# 2023 사업연도(제16기) — "사업보고서" 대신 "사업연도"로 문서를 가리키는 형태(항상 사업보고서를 뜻함).
DOC_YEAR_TERM = re.compile(r"(\d{4})\s*년?\s*사업연도\s*\(\s*제\s*(\d+)\s*기\s*\)")
# "2026년 3월 24일 기준 유효한 …" — 오늘 시점에 어느 버전이 유효한지 묻는 스냅샷
# 표현이다. 문서 종류·연월이 바로 붙어있지 않아 그 자체로는 문서 참조로 인식되지
# 않지만, 마스킹하지 않으면 ontology.parse()가 이 연도를 조회 연도로 잘못 집는다
# (회귀 실측: SEM-RET-06 — 질문 뒤쪽의 "2023년(제16기)"이 진짜 조회 연도인데
# 앞의 "2026년"이 먼저 걸려 그 해 자료가 없다고 답했다).
AS_OF = re.compile(r"\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,4}\s*일\s*기준(?:으로)?\s*유효한")

KIND_KO = {"사업": "사업보고서", "반기": "반기보고서", "분기": "분기보고서"}


_KNOWN = None


def _known(rcept_no):
    """corpus에 있는 접수번호인가. 형식만 맞는 14자리 숫자를 거른다."""
    global _KNOWN
    if _KNOWN is None:
        try:
            from . import rcept
            _KNOWN = set(rcept.load())
        except Exception:                                  # noqa: BLE001
            _KNOWN = set()
    return rcept_no in _KNOWN


def parse(question):
    """(spec|None, 마스킹된 질문).

    spec은 문서를 가리키는 단서만 담는다. 어느 열을 읽을지는 마스킹된 문장에서
    기존 파서가 읽는다.
    """
    spec, spans = {}, []

    for rx in (RCEPT, RCEPT_BARE):
        hit = None
        for m in rx.finditer(question):
            if _known(m.group(1)):
                hit = m
                break
        if hit:
            spec["rcept_no"] = hit.group(1)
            spans.append(hit.span())
            break

    m = DOC_TERM_YM.search(question)
    if m:
        spec.setdefault("term", int(m.group(1)))
        spec.setdefault("kind", KIND_KO[m.group(4)])
        spec["ym"] = (int(m.group(2)), int(m.group(3)))
        spans.append(m.span())

    m = DOC_YM.search(question)
    if m:
        spec.setdefault("kind", KIND_KO[m.group(1)])
        spec.setdefault("ym", (int(m.group(2)), int(m.group(3))))
        spans.append(m.span())

    if "ym" not in spec:
        m = DOC_YM_LEAD.search(question)
        if m:
            spec.setdefault("kind", KIND_KO[m.group(3)])
            spec["ym"] = (int(m.group(1)), int(m.group(2)))
            spans.append(m.span())

    if "ym" not in spec:
        m = DOC_YEAR.search(question)
        if m:
            spec.setdefault("kind", KIND_KO[m.group(2)])
            spec["year"] = int(m.group(1))
            spans.append(m.span())

    if "year" not in spec and "ym" not in spec:
        m = DOC_YEAR_TERM.search(question)
        if m:
            spec.setdefault("kind", "사업보고서")
            spec["year"] = int(m.group(1))
            spec.setdefault("term", int(m.group(2)))
            spans.append(m.span())

    for rx in (DOC_TERM, DOC_TERM_TAIL):
        m = rx.search(question)
        if not m:
            continue
        g = m.groups()
        term, kind = (g[0], g[1]) if rx is DOC_TERM else (g[1], g[0])
        spec.setdefault("term", int(term))
        spec.setdefault("kind", KIND_KO[kind])
        spans.append(m.span())
        break

    m = AS_OF.search(question)
    if m:
        spans.append(m.span())

    if not spec:
        return None, question

    # 참조 앞에 붙은 "2025년"도 함께 지운다 — 그것도 문서를 가리키는 말이다.
    masked = question
    for a, b in sorted(spans, reverse=True):
        head = masked[:a]
        lead = re.search(r"(\d{4})\s*년\s*$", head)
        if lead:
            a = lead.start()
        masked = masked[:a] + " " * (b - a) + masked[b:]
    return spec, masked


# 문서를 고정해야만 답이 달라지는 신호. 접수번호를 직접 대거나, 그 보고서에
# **비교표시된** 값을 묻는 경우다. 재작성·정정이 있으면 기본 조회와 값이 다르다.
WANTED = re.compile(r"비교\s*표시|재작성|최초\s*확정치|당시\s*기재|원문에\s*기재")


def wanted(question, spec):
    """문서 고정이 정말 필요한 질문인가.

    보고서 이름은 질문에 흔히 등장한다("2025년 사업보고서 매출액"). 그때마다
    문서를 고정하면 얻는 것 없이 조회 범위만 좁아진다 — 실제로 분기 문서를 잘못
    골라 답을 못 내는 회귀가 났다. 값이 달라질 수 있는 경우에만 건다.

    (시도했다가 되돌림) spec에 kind+ym/term이 있으면 트리거 단어 없이도 건다는
    조건을 추가했다가 회귀 2건을 냈다 — 문서를 **두 개** 언급하며 그 사이를
    비교하는 질문(GOLD-W1-CJ-03: 반기보고서(2025.06) vs 분기보고서(2025.09) 중
    어느 분기가 큰가)에서 첫 번째 문서 하나로 고정해버려 비교 자체가 무너지고,
    한 문서 **안에서** 두 기간을 빼는 질문(GOLD-W1-EM-01: 9개월 누적 - 3개월 =
    상반기 역산)에서 문서 고정 경로(_lookup_doc)가 derive 연산을 지원하지 않아
    "자료를 찾지 못했습니다"로 무너졌다. 두 실패 모두 "문서 참조가 있다고 해서
    항상 그 문서 하나로 단순 조회하면 되는 건 아니다"라는 같은 원인이라, 안전한
    일반 규칙을 못 찾아 트리거 단어 요구를 원래대로 되돌린다(범위 밖으로 보류).
    """
    return bool(spec.get("rcept_no")) or bool(WANTED.search(question))


def resolve(spec, corp_code, labels):
    """spec → rcept_no. 못 고르면 None (그러면 평소 조회로 돌아간다)."""
    if not spec:
        return None
    con = labels._db()
    if con is None:
        return None
    if spec.get("rcept_no"):
        if corp_code:
            hit = con.execute(
                "SELECT 1 FROM facts WHERE rcept_no=? AND corp_code=? LIMIT 1",
                (spec["rcept_no"], corp_code)).fetchone()
            if hit:
                return spec["rcept_no"]
            # 그 rcept_no가 이 corp_code 것이 아니다 — 다중기업 질문에서 다른
            # 기업의 접수번호가 spec에 잡혀 있는 경우다(parse()는 질문에 등장하는
            # 첫 접수번호만 담는다). corp_code를 무시하고 그대로 돌려주면 엉뚱한
            # 회사에 남의 문서가 배정된다. 아래 kind/ym/term 단서로 이 기업 자신의
            # 문서를 다시 찾는 fallback으로 흘려보낸다.
        else:
            hit = con.execute("SELECT 1 FROM facts WHERE rcept_no=? LIMIT 1",
                              (spec["rcept_no"],)).fetchone()
            return spec["rcept_no"] if hit else None
    if not corp_code:
        return None

    sql = ("SELECT rcept_no, report_nm, MAX(CAST(fiscal_term AS INT)) mx"
           " FROM facts WHERE corp_code=?")
    args = [corp_code]
    if spec.get("kind"):
        sql += " AND report_nm LIKE ?"
        args.append(f"%{spec['kind']}%")
    if spec.get("ym"):
        sql += " AND report_nm LIKE ?"
        args.append(f"%({spec['ym'][0]}.{spec['ym'][1]:02d})%")
    elif spec.get("year"):
        sql += " AND report_nm LIKE ?"
        args.append(f"%({spec['year']}.%")
    sql += " GROUP BY rcept_no"
    rows = con.execute(sql, args).fetchall()
    if spec.get("term"):
        # 그 기수를 자기 기수로 갖는 문서. 비교표시 열로만 등장하는 문서는 제외한다.
        rows = [r for r in rows if r["mx"] == spec["term"]]
    if not rows:
        return None
    # 정정본이 있으면 그쪽이 유효본이다 — 접수번호가 큰 쪽.
    return max(rows, key=lambda r: r["rcept_no"])["rcept_no"]
