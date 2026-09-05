"""DART 공시목록 원자료(raw 코퍼스) 조회 — 문서의 "존재"를 확인할 때 쓴다.

## 왜 필요한가

구조화 파이프라인(`qa/filings.py`의 struct_facts.jsonl·`qa/labelstore.py`의
facts.db)은 XBRL·정형 필드가 있는 공시만 담는다. "이 유형의 보고서가 그 뒤에
또 나온 적이 있는가"·"접수번호가 가장 큰 문서가 실제로도 최신 유효본인가"류는
**값이 아니라 공시가 났다는 사실 자체**를 물어서, 구조화 데이터가 빠뜨린 문서가
있으면 답이 뒤집힌다.

실측: KB금융 2025 사업연도(제18기) 사업보고서의 세 번째 정정본(20260619000667)은
PDF+viewer_html로만 존재해 struct_facts에 없다. `qa/filings.py`의
`Filings.chain()`으로 이 계열을 조회하면 정정본 2건만 보여 "그 뒤로 추가 정정
없음"이라는 잘못된 결론이 나올 위험이 있다(SEM-EVT-08).

원문 raw 코퍼스(`data/corpus/raw/{periodic,major,...}/<기업명>/list_*.json`,
레포 루트 기준)는 DART 공시목록 API 원자료를 그대로 담고 있어 이 빈틈이
없다. 이 모듈은 그 원자료를 **읽기 전용**으로 조회해 공시 유형·날짜·접수번호만
돌려준다 — 계산도 판단도 하지 않는다(판단은 호출부인 qa/boolean.py가 한다).

## 경계

이 raw 코퍼스는 공시_agent가 만든 것이 아니라 프로젝트 공용 데이터
(레포 루트의 `data/corpus/raw`, 5GB+로 커서 이번 제출 데이터 패키지엔 포함하지
않음 — README.md "데이터" 참고)다. 이 모듈은 그 데이터를 읽기만 하고
쓰지 않는다. 디렉터리·기업 폴더가 없으면 조용히 빈 결과를 돌려준다 — 이
프로젝트가 그 데이터의 존재를 보장하지 않기 때문이다(boolean.py의 "문서 존재
확인" 질의만 이 데이터 없이는 답을 못 낸다 — 그 외 경로엔 영향 없음).
"""

import json
import re
import unicodedata
from pathlib import Path

_HERE = Path(__file__).resolve().parent
RAW_BASE = _HERE.parent.parent / "data" / "corpus" / "raw"
CATEGORIES = ("periodic", "major", "holding", "exchange")

# 정정 표기(대괄호 블록)를 뗀 기준 이름. qa/filings.py의 _BRACKET·base_name과
# 같은 규칙이지만, 그 모듈의 private 상수에 기대지 않고 이 파일 안에서
# 독립적으로 쓴다(다른 저장소를 다루는 모듈이라 결합을 늘리지 않는다).
_BRACKET = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")
_SPACE = re.compile(r"\s+")


def _norm(s):
    return _SPACE.sub("", str(s or ""))


def base_name(report_nm):
    return _BRACKET.sub("", report_nm or "").strip()


def _topic_key(report_nm):
    """"...상장폐지결정)"과 "...상장폐지)"를 같은 사안으로 묶는다.

    "결정"은 예고(장래 확정 예정), 그 뒤가 실제 집행 보고서다. 접미사만 다를 뿐
    같은 사안이므로, 대괄호를 뗀 뒤 "결정)"을 ")"으로 접어 같은 키로 만든다.
    """
    return _norm(base_name(report_nm)).replace("결정)", ")")


def _find_corp_dir(base, corp_name):
    if not base.exists():
        return None
    target = unicodedata.normalize("NFC", corp_name or "")
    for d in base.iterdir():
        if d.is_dir() and unicodedata.normalize("NFC", d.name) == target:
            return d
    return None


def manifest(corp_name, categories=CATEGORIES):
    """이 기업의 원자료 공시목록 전부(list_*.json 병합).

    각 항목은 {corp_name, report_nm, rcept_no, rcept_dt, ...} dict. 기업 폴더를
    하나도 못 찾으면 빈 리스트.
    """
    out = []
    for cat in categories:
        cdir = _find_corp_dir(RAW_BASE / cat, corp_name)
        if not cdir:
            continue
        for f in cdir.glob("list_*.json"):
            try:
                rows = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rows, list):
                out.extend(row for row in rows
                          if isinstance(row, dict) and row.get("rcept_no"))
    return out


def available():
    return RAW_BASE.exists()


def doc_series_check(corp_name, year, doc_keyword):
    """(ok, 해당 계열 문서 전부, 접수번호 최댓값 문서) 또는 (None, [], None).

    ok는 "접수번호가 가장 큰 문서 == 접수일자가 가장 늦은 문서"인지 — 구조화
    데이터가 놓친 문서가 raw 코퍼스에는 있는지까지 함께 반영된 결과다.
    """
    rows = manifest(corp_name, categories=("periodic",))
    if not rows:
        return None, [], None
    year_tag = f"({year}."
    cand = [row for row in rows
           if doc_keyword in (row.get("report_nm") or "")
           and year_tag in (row.get("report_nm") or "")]
    if not cand:
        return None, [], None
    # 결산월이 다른 계열이 섞여 들어오면(예: "(2025.12)"와 "(2025.06)") 안전하게
    # 손을 뗀다 — year_tag가 "(2025."까지만 봐서 드물게 섞일 수 있다.
    bases = {base_name(row["report_nm"]) for row in cand}
    if len(bases) != 1:
        return None, [], None
    by_no = max(cand, key=lambda row: row.get("rcept_no") or "")
    by_date = max(cand, key=lambda row: ((row.get("rcept_dt") or ""), row.get("rcept_no") or ""))
    ok = by_no.get("rcept_no") == by_date.get("rcept_no")
    return ok, sorted(cand, key=lambda row: row.get("rcept_no") or ""), by_no


def repeat_filing_check(corp_name, ref_date, keyword):
    """ref_date(YYYYMMDD)에 keyword를 포함한 report_nm으로 공시된 것과 같은
    사안(결정↔집행 묶음 포함)이 그 이후에도 다시 공시됐는지.

    (seed 문서들, ref_date 이후 같은 사안 문서들) 또는 못 찾으면 None.
    """
    rows = manifest(corp_name, categories=("major",))
    if not rows:
        return None
    seeds = [row for row in rows
            if (row.get("rcept_dt") or "") == ref_date
            and keyword in _norm(row.get("report_nm") or "")]
    if not seeds:
        return None
    topic_keys = {_topic_key(row["report_nm"]) for row in seeds}
    later = [row for row in rows
            if _topic_key(row.get("report_nm") or "") in topic_keys
            and (row.get("rcept_dt") or "") > ref_date]
    return seeds, later
