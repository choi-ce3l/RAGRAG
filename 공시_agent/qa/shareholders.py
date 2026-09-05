"""주주현황(최대주주·특별관계자·지분율) 조회 — ragrag/out/shareholders.jsonl 재사용.

## 왜 새 파일인가

`build_tables.py`는 사업보고서 "VII장 최대주주 및 특수관계인 소유주식 현황" 표를
아예 추출하지 못했다 — 직접 확인: `tables.db`에 "최대주주"를 담은 row_label이
존재하지 않고, "지분율" 헤더로 매칭되는 표는 전부 무관한 "지분법 적용 투자주식"
표뿐이다(대우건설·현대자동차 둘 다 확인). `qa/fields.py`의 대량보유(holding,
5%룰) 문서군이 "최대주주"·"특별관계자" 라벨을 갖고 있어 예전엔 그쪽으로 잘못
새어 엉뚱한 기관투자자를 최대주주라고 답했다(SEM-NUM-13/GOLD-W1-DGN-10, 이번
세션에서 별도로 고침 — `_AMBIGUOUS_HOLDING` 가드).

같은 저장소 안 `ragrag/` 파이프라인이 이 표를 이미 별도로(주요사항보고서가 아닌
periodic 문서의 acode BSH_SPCL 기준) 정확히 추출해 `ragrag/out/shareholders.jsonl`
로 갖고 있다 — 실측 대조 완료: 대우건설 중흥토건㈜ 40.60%/중흥건설㈜ 10.15%
(gold와 정확 일치), KB금융 국민연금공단 2023년 8.30%→2025년 8.68%(=+0.38%p,
gold와 정확 일치). `build_tables.py`를 다시 만드는 대신 이 파일을 읽기 전용으로
재사용한다 — choi 원본도 ragrag 원본도 손대지 않는다.

## 범위

일반 자연어 질의응답을 전부 지원하지 않는다. 명시적으로 다루는 형태만 답하고,
그 외에는 None을 돌려줘 기존 경로(narrative 등)로 그대로 넘어가게 한다.
"""

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 배포 서버 컨테이너는 choi/SHLEE/ragrag 형제 폴더 구조 전체를 마운트하지 않는다
# (qa/·data/·code_chunkingandparsing/out만 마운트) — 그래서 ragrag/out/ 상대경로가
# 컨테이너 안에서는 "/"까지 올라가 버려 항상 존재하지 않는 파일이 됐고, 이 모듈은
# 예외 없이 조용히 빈 리스트로 새 나갔다(라이브에서만 재현, tables.py로 폴백해
# 값은 맞았지만 이 모듈의 근거·순위 필드는 채워지지 않았다). data/ 아래 복사본을
# 먼저 찾고, 없으면(로컬 개발 환경) 원래 ragrag/out/ 경로로 돌아간다 — 두 원본
# 다 손대지 않는다는 원칙은 그대로 지킨다.
_LOCAL_COPY = _HERE.parent / "data" / "shareholders.jsonl"
_RAGRAG_SOURCE = _HERE.parent.parent.parent / "ragrag" / "out" / "shareholders.jsonl"
SOURCE = _LOCAL_COPY if _LOCAL_COPY.exists() else _RAGRAG_SOURCE

TRIGGER = re.compile(r"최대주주|특별관계자|특수관계자|지분율")

_RECORDS = None


def _load():
    global _RECORDS
    if _RECORDS is None:
        recs = []
        if SOURCE.exists():
            with SOURCE.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        recs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        _RECORDS = recs
    return _RECORDS


def _dec(v):
    try:
        return Decimal(str(v).replace(",", ""))
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _rows(corp_code, base_year=None):
    """유효 행(합계행 제외). 정정되지 않은 것을 우선하되, 그 연도에 정정 안 된
    필링이 하나도 없으면(실측: KB금융 2025년 국민연금공단 — 필링 2건 다
    is_superseded=True) 정정된 것이라도 쓴다 — 없는 것보다는 낫다.
    """
    all_rows = [r for r in _load()
                if r.get("corp_code") == corp_code and not r.get("is_total")
                and (r.get("holder_name") or "").strip() != "계"]
    if base_year is not None:
        all_rows = [r for r in all_rows if r.get("base_year") == base_year]
    fresh = [r for r in all_rows if not r.get("is_superseded")]
    return fresh or all_rows


def years_available(corp_code):
    return sorted({r["base_year"] for r in _rows(corp_code)})


def largest_holder(corp_code, base_year):
    """그 해 최대주주 한 행. relation에 '최대주주'가 있는 것 우선, 없으면 지분율 최댓값.

    필링이 여러 건(정정 등)이면 최신 rcept_no를 쓴다.
    """
    rows = _rows(corp_code, base_year)
    if not rows:
        return None
    tagged = [r for r in rows if "최대주주" in (r.get("relation") or "")]
    pool = tagged or rows
    pool = sorted(pool, key=lambda r: (r["rcept_no"], _dec(r.get("pct_close")) or Decimal(0)),
                  reverse=True)
    # 같은 rcept_no 안에서 최댓값을 우선한다(최신 필링 우선 후 지분율 내림차순).
    latest_rcept = pool[0]["rcept_no"]
    same_rcept = [r for r in pool if r["rcept_no"] == latest_rcept]
    same_rcept.sort(key=lambda r: _dec(r.get("pct_close")) or Decimal(0), reverse=True)
    return same_rcept[0]


def holder(corp_code, base_year, name_fragment):
    """이름(부분 일치)으로 특정 주주 한 명. 최신 필링 우선."""
    rows = [r for r in _rows(corp_code, base_year)
            if name_fragment in (r.get("holder_name") or "")]
    if not rows:
        return None
    rows.sort(key=lambda r: r["rcept_no"], reverse=True)
    return rows[0]


def holder_across_years(corp_code, name_fragment, years):
    """이름으로 여러 연도의 지분율을 모은다. {year: Decimal|None}."""
    out = {}
    for y in years:
        h = holder(corp_code, y, name_fragment)
        out[y] = _dec(h["pct_close"]) if h else None
    return out
