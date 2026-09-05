"""재무제표 근거의 목차 번호 — "손익계산서(연결)"만으론 DART 문서 안에서 어디를
봐야 할지 알 수 없다. 목차엔 번호가 있다("Ⅲ. 재무에 관한 사항 > 2-2. 연결
손익계산서"). XBRL fact 자체엔 이 번호가 없다(statement·label_raw·셀 위치뿐 —
pipeline.py의 `_evidence()` 주석 참고). 원본 raw chunks.jsonl(4.4GB, 배포 환경엔
없음)에만 있어서, 로컬에서 한 번 스캔해 rcept_no별 목차 캐시로 압축해 둔다
(scripts/build_statement_toc.py). 배포본은 이 캐시(수백 KB)만 읽는다.

## 왜 항상 정확한 하위 번호를 못 주는가
DART 보고서마다 재무제표 목차 세분화 정도가 다르다 — 어떤 보고서는
"2-1. 연결 재무상태표"·"2-2. 연결 손익계산서"처럼 개별 표마다 번호가 있고,
어떤 보고서는 "2. 연결재무제표" 하나로 뭉쳐 있다. 세분화된 번호가 있으면 그걸
쓰고, 없으면 상위 그룹 번호("2. 연결재무제표")로 대신한다 — 그룹 번호만 알아도
목차에서 훨씬 빨리 찾는다.
"""

import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
CACHE = _HERE.parent / "data" / "statement_toc_index.json"

_KW_PRIORITY = ["재무상태표", "포괄손익계산서", "손익계산서", "현금흐름표", "자본변동표", "재무제표"]

_INDEX = None


def _load():
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    if CACHE.exists():
        try:
            _INDEX = json.loads(CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _INDEX = {}
    else:
        _INDEX = {}
    return _INDEX


def lookup(rcept_no, stmt_ko, scope_ko):
    """(rcept_no, "손익계산서", "연결") → "Ⅲ. 재무에 관한 사항 > 2-2. 연결 손익계산서"
    같은 번호 붙은 목차 경로. 세분화된 항목이 없으면 그룹 번호("N. 연결재무제표")로,
    그것도 없으면 None."""
    idx = _load()
    doc = idx.get(rcept_no)
    if not doc:
        return None
    scope = scope_ko if scope_ko in ("연결", "별도") else "별도"
    if stmt_ko in _KW_PRIORITY:
        hit = doc.get(f"{scope}:{stmt_ko}")
        if hit:
            return hit
    return doc.get(f"{scope}:재무제표")
