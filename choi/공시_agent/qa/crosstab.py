"""다중기업 크로스탭 — (기업×기간) 매트릭스를 조립하고 그 위에서 순위·스크리닝한다.

## 왜 필요한가

"5개 계열사를 당기순이익이 큰 순서로 나열", "두 회사 각각의 지분율을 두 시점
비교" 같은 질문은 값 하나가 아니라 **여러 기업 × 여러 시점**의 격자를 채워야
답이 나온다. `qa/boolean.py`의 `_judge_two_corp_loss_year_match`·
`_judge_two_corp_dividend_history`가 이미 "2기업 × N해"를 손으로 겹루프 돌며
채우는데, 매번 새로 짠다. 이 모듈은 그 겹루프를 `build()` 하나로 승격한다.

## 셀 하나를 못 찾으면

조용히 빼지 않는다 — `reasons[(entity, period)]`에 이유를 남긴다:
  - `"no_data"`   : 그 좌표 자체에 값이 없다(회사가 그 해 데이터를 안 냈다 등).
  - `"no_source"` : 이 기업이 애초에 조회 대상 저장소에 없다(예: 계열사가
                   labelstore에 자기 이름의 corp_code로 없음).
`rank()`·`screen()`은 이 reasons를 몰라도 되게, 그냥 값이 있는 셀만 갖고
동작한다 — 이유는 판정에 확신이 없을 때 사람이 원인을 추적하라고 남겨 두는
용도다.

## 값 소스(source) — 세 가지, 전부 기존 공개 조회 함수를 반복 호출할 뿐이다

  "labelstore"    : `metric`은 labelstore metric_key(연간). 회사마다 다른
                    corp_code로 `labelstore.lookup_metric_annual()`을 호출한다.
  "shareholder"   : `metric`은 주주명(부분 일치). `shareholders.holder()`로
                    (기업, 연도)별 지분율(pct_close)을 모은다.
  "tables_section": `metric`은 본문 표의 section 이름(부분 일치). 회사 하나의
                    corp_code 아래 여러 "행"(계열사·부문 등)을 열(연도)별로
                    모을 때 쓴다 — `tables.py`의 `rows()`만 쓰고 새 SQL을
                    짜지 않는다. KB-07(계열사별 당기순이익 현황)이 이 경로다.
"""

import re
from decimal import Decimal, InvalidOperation

from . import labelstore, shareholders, tables

_YEAR_HEADER = re.compile(r"^(\d{4})\s*년$")


def _dec(v):
    if v is None:
        return None
    try:
        return Decimal(str(v).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _won(f):
    try:
        return Decimal(str(f["value_decimal"])) * Decimal(str(f.get("scale") or 1))
    except (InvalidOperation, ValueError, TypeError, KeyError):
        return None


def _normalize_corp_core(name):
    """'KB증권㈜' → 'KB증권'류 — 법인격 표기·앞머리 KB를 벗겨 이름 대조에 쓴다."""
    core = re.sub(r"\((?:주|유|재)\)|주식회사|㈜", "", name or "").strip()
    return core


# ---------------------------------------------------------------------------
# source="labelstore"
# ---------------------------------------------------------------------------
def _fill_labelstore(entities, metric, periods, scope, labels):
    matrix, reasons = {}, {}
    labels = labels or labelstore.get()
    for name, cc in entities:
        row = {}
        for y in periods:
            if not cc:
                row[y] = None
                reasons[(name, y)] = "no_source"
                continue
            f = labels.lookup_metric_annual(cc, metric, scope, y, prefer_latest=True)
            if not f:
                row[y] = None
                reasons[(name, y)] = "no_data"
                continue
            row[y] = _won(f)
        matrix[name] = row
    return matrix, reasons


# ---------------------------------------------------------------------------
# source="shareholder"
# ---------------------------------------------------------------------------
def _fill_shareholder(entities, holder_name, periods, scope=None):
    matrix, reasons = {}, {}
    for name, cc in entities:
        row = {}
        for y in periods:
            if not cc:
                row[y] = None
                reasons[(name, y)] = "no_source"
                continue
            h = shareholders.holder(cc, y, holder_name)
            pct = _dec(h["pct_close"]) if h else None
            if pct is None:
                row[y] = None
                reasons[(name, y)] = "no_data"
                continue
            row[y] = pct
        matrix[name] = row
    return matrix, reasons


# ---------------------------------------------------------------------------
# source="tables_section" — 본문표 한 표(section) 안의 "행"들을 기업(엔터티)
# 축으로 삼는다. KB-07처럼 계열사 이름이 표의 행 라벨인 경우가 이 경로다.
# ---------------------------------------------------------------------------
def _table_row_identity(cell):
    """행 하나의 식별자. row_label이 비어 있으면(실측: KB금융 '계열사별
    당기순이익 현황' 표에서 '국민은행' 행 하나가 row_label 대신 '구 분' 열에
    이름을 담고 있다 — 원문 표의 2단 헤더가 파싱 과정에서 밀린 것으로 보인다)
    '구 분'/'구분' 헤더의 값이 숫자가 아니면 그것을 이름으로 대신 쓴다.
    """
    lab = (cell.get("row_label") or "").strip()
    if lab:
        return lab
    header = (cell.get("header") or "")
    if re.match(r"^구\s*분$", header.strip()):
        v = cell.get("value_raw")
        if v is not None and _dec(v) is None:      # 숫자로 안 읽히면 이름이다
            return str(v).strip()
    return None


def _fill_tables_section(entities, section, periods):
    t = tables.get()
    matrix, reasons = {}, {}
    if t is None:
        for name, _cc in entities:
            matrix[name] = {y: None for y in periods}
            for y in periods:
                reasons[(name, y)] = "no_source"
        return matrix, reasons

    # section 하나는 보통 corp_code 하나에 걸쳐 있다 — entities가 같은
    # corp_code를 반복해서 들고 있어도(예: 계열사 5개가 모두 KB금융 소속)
    # corp_code별로 한 번만 조회한다.
    by_cc = {}
    for _name, cc in entities:
        if cc and cc not in by_cc:
            by_cc[cc] = t.rows(cc, section=section, limit=2000)

    # (corp_code) -> {identity: {year: Decimal}}
    per_cc_grid = {}
    for cc, cells in by_cc.items():
        grid = {}
        for c in cells:
            ident = _table_row_identity(c)
            if not ident:
                continue
            m = _YEAR_HEADER.match((c.get("header") or "").strip())
            if not m:
                continue
            y = int(m.group(1))
            v = _dec(c.get("value_raw"))
            grid.setdefault(ident, {})[y] = v
        per_cc_grid[cc] = grid

    for name, cc in entities:
        row = {}
        grid = per_cc_grid.get(cc, {}) if cc else {}
        core = _normalize_corp_core(name)
        # 정확 일치 우선, 없으면 KB 접두 등을 뗀 핵심 이름으로 대조한다.
        ident = name if name in grid else next(
            (k for k in grid if _normalize_corp_core(k) == core), None)
        for y in periods:
            if not cc:
                row[y] = None
                reasons[(name, y)] = "no_source"
                continue
            if ident is None:
                row[y] = None
                reasons[(name, y)] = "no_data"
                continue
            v = grid[ident].get(y)
            row[y] = v
            if v is None:
                reasons[(name, y)] = "no_data"
        matrix[name] = row
    return matrix, reasons


_BACKENDS = {
    "labelstore": _fill_labelstore,
    "shareholder": _fill_shareholder,
    "tables_section": _fill_tables_section,
}


def build(entities, metric, periods, scope="consolidated", source="labelstore", labels=None):
    """(기업×기간) 매트릭스를 조립한다.

    entities: [(표시명, corp_code), ...]. 같은 corp_code가 반복돼도 된다
        (tables_section에서 한 회사 산하 여러 계열사를 다룰 때).
    metric: source별로 뜻이 다르다(labelstore=metric_key, shareholder=주주명
        부분일치, tables_section=section 이름 부분일치).
    periods: 연도 리스트(int). 분기·반기는 이 버전에서 다루지 않는다 —
        `comparespec.FactRef`가 그 축을 이미 갖고 있어 필요하면 그쪽을 쓴다.
    반환: ({표시명: {연도: Decimal|None}}, {(표시명,연도): 사유 or 생략(값 있음)}).
    """
    if source == "labelstore":
        return _fill_labelstore(entities, metric, periods, scope, labels)
    if source == "shareholder":
        return _fill_shareholder(entities, metric, periods, scope)
    if source == "tables_section":
        return _fill_tables_section(entities, metric, periods)
    raise ValueError(f"알 수 없는 source: {source}")


# ---------------------------------------------------------------------------
# 매트릭스 위 연산 — 얇은 함수 몇 개
# ---------------------------------------------------------------------------
def rank(matrix, period, order="desc"):
    """한 시점 기준 기업 순위. 값이 없는 기업은 조용히 빠진다(순위를 매길
    수가 없으므로) — 그 사실은 build()가 돌려준 reasons에 이미 남아 있다."""
    have = [(name, row.get(period)) for name, row in matrix.items()
            if row.get(period) is not None]
    have.sort(key=lambda t: t[1], reverse=(order == "desc"))
    return [name for name, _v in have]


def screen(matrix, period, threshold, op="ge"):
    """한 시점 기준 임계값 스크리닝. op: ge/le/gt/lt/eq."""
    fn = {"ge": lambda v: v >= threshold, "le": lambda v: v <= threshold,
          "gt": lambda v: v > threshold, "lt": lambda v: v < threshold,
          "eq": lambda v: v == threshold}[op]
    return [name for name, row in matrix.items()
            if row.get(period) is not None and fn(row[period])]
