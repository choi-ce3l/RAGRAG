"""본문 표 조회 — 사업보고서 본문의 표에서 값을 찾는다.

## 왜 필요한가

XBRL 재무제표에도, 공시 구조화 fact에도 없는 값들이 있다.

    최대주주 및 특수관계인 지분율 · 직원 연간급여총액 · 소송현황 소송가액
    배당에 관한 사항 · 사업부문별 매출

전부 사업보고서 **본문의 표**에 있다. `build_tables.py`가 문서·절·표·행·열 좌표를
보존한 채 뽑아 뒀다.

## 조회 방식 — 행 라벨과 열 머리글의 교차

표는 2차원이다. "현대자동차의 지분율"은 **행(현대자동차) × 열(지분율)**의 교차점이다.
질문에서 둘을 뽑아 교차점을 집는다. 하나만으로는 못 집는다 —
같은 행에 소유주식수와 지분율이 나란히 있기 때문이다.
"""

import re
import sqlite3
from pathlib import Path

_HERE = Path(__file__).resolve().parent
DB = _HERE.parent / "data" / "tables.db"

# 열을 가리키는 말 → 머리글에 들어갈 법한 낱말
COL_WORDS = ("지분율", "비율", "소유주식수", "주식수", "금액", "가액", "인원",
             "급여", "총액", "매출", "영업이익", "수량", "1인평균")

_SPACE = re.compile(r"\s+")


def _norm(s):
    return _SPACE.sub("", str(s or ""))


class Tables:
    def __init__(self, con):
        self.con = con

    def rows(self, corp_code, label=None, section=None, limit=200):
        sql = "SELECT * FROM cells WHERE corp_code=? AND is_superseded='0'"
        args = [corp_code]
        if label:
            sql += " AND row_label LIKE ?"
            args.append(f"%{label}%")
        if section:
            sql += " AND section LIKE ?"
            args.append(f"%{section}%")
        sql += " ORDER BY rcept_no DESC, table_idx, row_idx, col_idx LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.con.execute(sql, args)]

    def cell(self, corp_code, row_label, col_word, rcept_no=None):
        """행 라벨 × 열 머리글의 교차점. 최신 보고서 우선.

        row_label은 완전 일치로 본다 — 유일한 호출부(answer())가 이미
        _row_labels()로 그 표에 실제로 있는 완전한 값을 뽑아 넘긴다. 여기서
        LIKE 와일드카드를 쓰면 "㈜카카오"가 "㈜카카오엔터테인먼트"처럼 자기
        이름을 포함하는 별개 행에 우연히 매치돼 무관한 값을 확신 있게
        답해버린다(실측: "카카오 감자 금액이 얼마야" → 타법인출자 현황의
        카카오엔터프라이즈 행을 오답으로 냄).
        """
        sql = ("SELECT * FROM cells WHERE corp_code=? AND is_superseded='0'"
               " AND row_label=? AND header LIKE ?")
        args = [corp_code, row_label, f"%{col_word}%"]
        if rcept_no:
            sql += " AND rcept_no=?"
            args.append(rcept_no)
        sql += " ORDER BY rcept_no DESC, table_idx, row_idx LIMIT 1"
        r = self.con.execute(sql, args).fetchone()
        return dict(r) if r else None


_STORE = None


def get():
    global _STORE
    if _STORE is None:
        if not DB.exists():
            return None
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, check_same_thread=False)
        con.row_factory = sqlite3.Row
        _STORE = Tables(con)
    return _STORE


def _overlap(val, qn, floor=4):
    """val이 질문에 연속으로 몇 글자나 들어 있는가. 절 이름 대조에 쓴다."""
    if len(val) < floor:
        return 0
    best = 0
    for i in range(len(val) - floor + 1):
        for j in range(len(val), i + floor - 1, -1):
            if val[i:j] in qn:
                best = max(best, j - i)
                break
    return best


def _strip_corp_suffix(s):
    return _norm(re.sub(r"\((?:주|유|재)\)|주식회사|㈜", "", s))


def _row_labels(con, corp_code, question, col, corp_name=None):
    """질문에 등장하는 행 라벨을 찾는다 — 목록을 적지 않고 표에서 역으로 맞춘다.

    "현대자동차·기아·현대모비스의 지분율"에서 세 이름은 표의 행 라벨이다.
    후보를 적어 두는 대신 **그 기업 표의 행 라벨을 훑어 질문에 들어 있는 것**을 고른다.
    표가 아는 이름만 쓰므로 오탐이 적다.

    질문 대상 기업 **자기 자신**의 이름과 겹치는 행 라벨은 후보에서 뺀다 —
    질문에는 항상 그 기업 이름이 들어 있어(질문 대상이니까) 표에 자기 이름이
    행으로 등장하기만 하면(예: "비경상적 주요 계약" 표의 계약당사자 행) 실제
    묻는 바와 무관해도 항상 걸린다(실측: "카카오 감자 금액이 얼마야" → 감자와
    무관한 계약표의 "㈜카카오" 행 100,000을 오답으로 냄). 자사 보유분을 가리키는
    행은 "자기주식"처럼 별도 명칭을 쓰지 회사명 그대로 반복하지 않는다.

    완전히 같은지("==")만 보면 부족하다 — "두산"은 "두산로보틱스"의 완전한
    부분(모회사/계열사명)이고, "화재"·"해상"도 "삼성화재해상보험"의 부분이다.
    질문 대상 기업 **자기 이름 안에 이미 들어있는 조각**은 회사가 자기 이름을
    말했다는 신호일 뿐 질문이 실제로 지목한 별도 행이 아니므로, 부분문자열
    포함("in")으로 제외 조건을 넓힌다.
    """
    qn = _norm(question)
    self_norm = _strip_corp_suffix(corp_name) if corp_name else None
    rows = con.execute(
        "SELECT DISTINCT row_label FROM cells WHERE corp_code=? AND is_superseded='0'"
        " AND header LIKE ? AND LENGTH(row_label) BETWEEN 2 AND 30",
        (corp_code, f"%{col}%")).fetchall()
    hits = []
    for r in rows:
        lab = r["row_label"]
        core = _strip_corp_suffix(lab)
        if self_norm and core and core in self_norm:
            continue
        if len(core) >= 3 and core in qn:
            hits.append((len(core), lab))
    hits.sort(reverse=True)
    out, seen = [], set()
    for _, lab in hits:                      # 긴 것 우선, 서로 포함되면 하나만
        k = _norm(lab)
        if not any(k in s2 for s2 in seen):
            out.append(lab)
            seen.add(k)
    return out[:6]


def answer(question, corp_code, corp_name):
    """(문장, 근거, 숫자, 읽은 셀들) 또는 (None, 사유, [], [])."""
    t = get()
    if t is None:
        return (None, "본문 표 저장소가 없습니다.", [], [])
    col = col_word(question)
    if not col:
        return (None, "표의 어느 항목을 묻는지 좁히지 못했습니다.", [], [])
    labels = _row_labels(t.con, corp_code, question, col, corp_name)
    if not labels:
        return (None, "질문에 나온 이름을 표에서 찾지 못했습니다.", [], [])
    got = []
    for lab in labels:
        c = t.cell(corp_code, lab, col)
        if c:
            got.append(c)
    if not got:
        return (None, "표에서 해당 항목을 읽지 못했습니다.", [], [])

    # 표를 잘못 고르지 않았는지 확인한다.
    #
    # 행 라벨만 보고 고르면 엉뚱한 표에 걸린다 — "수주잔고"를 물었는데 "주요 원재료
    # 매입 현황" 표의 같은 이름 행을 집었다. 표는 절(section) 안에 있고, 질문은 대개
    # 그 절을 함께 말한다("VII. 주주에 관한 사항", "직원 등 현황").
    #
    # **절 이름이 질문과 겹치거나, 서로 다른 행 라벨이 둘 이상 맞을 때**만 답한다.
    # 둘 다 아니면 우연히 맞은 것으로 보고 답하지 않는다 — 확신 있는 오답보다 낫다.
    qn = _norm(question)
    sect_ok = any(_overlap(_norm(c.get("section")), qn) >= 4 for c in got)
    if not sect_ok and len({c["row_label"] for c in got}) < 2:
        return (None, "표를 특정하지 못했습니다 (절 이름이나 항목을 함께 알려 주세요).", [], [])
    desc = bool(re.search(r"높은\s*순|큰\s*순|많은\s*순|내림차순", question))
    asc = bool(re.search(r"낮은\s*순|작은\s*순|적은\s*순|오름차순", question))
    if desc or asc:
        got.sort(key=lambda c: float(c["value_num"] or 0), reverse=desc)
    # 비율 열은 "이름(값%)"로 적는다 — 사람이 지분율을 그렇게 쓴다.
    # 법인격 표기((주)·㈜)는 이름의 일부가 아니라 형식이므로 함께 벗긴다.
    def _fmt(c):
        name = re.sub(r"\s*\((?:주|유|재)\)|㈜|주식회사\s*", "", c["row_label"]).strip()
        v = str(c["value_raw"]).strip()
        if "%" in str(c.get("header") or "") or "율" in str(c.get("header") or ""):
            return f"{name}({v}%)" if not v.endswith("%") else f"{name}({v})"
        return f"{name} {v}"

    parts = [_fmt(c) for c in got]
    sect = got[0].get("section") or ""
    return (f"{corp_name} {got[0]['report_nm']} {sect} 표 기준 — " + " / ".join(parts) + ".",
            [c["rcept_no"] for c in got],
            [str(c["value_raw"]).replace(",", "") for c in got],
            got)


def col_word(question):
    """질문이 어느 열을 묻는가."""
    qn = _norm(question)
    for w in COL_WORDS:
        if w in qn:
            return w
    return None
