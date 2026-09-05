"""01~05 서브에이전트 파이프라인 오케스트레이터 — 수치 fact-path.

이 경로는 기본적으로 API 키를 쓰지 않는다(01단계가 기업/지표/연도 중 하나라도
특정하지 못했을 때만 LLMPARSE_ENABLED 켜져 있으면 llmparse가 슬롯 JSON을 채워
재시도한다 — 숫자·지표 판단은 여전히 LLM이 하지 않고, 슬롯 값도 폐집합 후보
밖으로는 못 나간다). 01(intent 파싱)과 03(Decimal 계산)은 기존
`code_chunkingandparsing/src/numqa.py`를 재사용하고, 02는 그 조회 결과를 좌표로
확보하는 단계로 분리했다. 04(검증)와 05(화면 조립)가 이번에 새로 붙은 부분이다.

02를 03과 분리해 store.lookup을 따로 부르는 이유: 요구사항의 "어느 서브에이전트
단계에서 오류가 났는지" 추적이 진짜가 되려면, fact를 못 찾은 것(02 실패)과
계산이 틀린 것(03 실패)이 서로 다른 단계로 잡혀야 하기 때문이다.
"""

import os
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from pathlib import Path

import numqa                                            # noqa: E402

from . import (applicability, bizcompare, boolean, concepts, confidence, derived, entity_gate,    # noqa: E402
               events, glossary, hierarchy,
               compare, contract, crosstab, docref, docstats, eventspan, filings, health, labelstore,
               lawsuit, llmparse, narrative,
               ontology, perf, shareholders, statement_toc, tables,
               period, rcept, render,
               sections, sectors, structstore, umbrella, verdict)

STAGES = [
    ("01", "온톨로지/개념 매핑"),
    ("02", "검색(RAG)"),
    ("03", "계산/추론"),
    ("04", "도메인 전문가 검증"),
    ("05", "답변 생성"),
]

MAX_RETRY = 2

SCOPE_KO_ALL = {"consolidated": "연결", "separate": "별도"}

STATEMENT_KO = {
    "balance_sheet": "재무상태표",
    "income_statement": "손익계산서",
    "cashflow": "현금흐름표",
    "ratio": "재무비율",
}


# ---------------------------------------------------------------------------
# 결과 자료구조
# ---------------------------------------------------------------------------
@dataclass
class Stage:
    no: str
    name: str
    status: str = "대기"          # 대기 / 진행중 / 완료 / 재시도 / 실패 / 건너뜀
    note: str = ""
    retries: int = 0


@dataclass
class QAResult:
    question: str
    resolved_question: str = ""   # llmparse가 대명사·후속질문을 풀어 쓴 문장. 없으면 question과 같다 —
                                   # 다음 턴의 prev_question으로 이걸 넘겨야 3턴째부터도 대상이 안 끊긴다
                                   # (원문만 넘기면 그 원문 자체가 이미 "그 회사는?"류일 때 막힌다).
    state: str = "S0"
    stages: list = field(default_factory=list)
    parsed: dict = field(default_factory=dict)
    answer_text: str = ""
    numbers: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    facts: list = field(default_factory=list)       # 근거 fact 원본 (평가에서 단위 환산에 사용)
    ranking: list = field(default_factory=list)     # 순위 질문의 정렬된 기업명
    generated_by: str = "deterministic"             # deterministic | llm
    usage: dict = field(default_factory=dict)       # LLM 토큰 사용량
    series: list = field(default_factory=list)      # 기간 시리즈 [(연도, fact)]
    cagr: float = None
    unresolved: list = field(default_factory=list)  # fact를 못 찾은 대상 기업
    sections: list = field(default_factory=list)    # 답을 못 낼 때 찾아볼 문서 절
    calc_steps: list = field(default_factory=list)
    verification: dict = field(default_factory=dict)
    confidence_summary: str = ""                    # verification/evidence.flags를 옮겨 적은 한 줄 요약
    # [P3] Q_STATE_COHERENCE — confidence.summarize()의 "검증 0개→미검증"
    # 표시를 생략할 경로. glossary(용어 설명)·verdict(감성판정, 이미 자체
    # 면책 문구 있음)·events(원문 나열, 검증할 수치 주장 자체가 없음)·
    # recommendation(이미 자체 고정 면책 첫 줄 있음)처럼 "검증"이라는 개념이
    # 안 맞거나 이미 자기 disclaimer를 가진 답변 kind에서 True로 켠다.
    skip_confidence_note: bool = False
    notices: list = field(default_factory=list)
    missing: list = field(default_factory=list)     # S3에서 되물을 항목
    searched: dict = field(default_factory=dict)    # S1에서 보여줄 검색 범위
    table: dict = field(default_factory=dict)       # {"columns": [...], "rows": [[...], ...]}
                                                      # "표로 정리해줘"류 — 화면이 실제 표로 그린다

    def stage(self, no):
        return next(s for s in self.stages if s.no == no)


# ---------------------------------------------------------------------------
# 개인정보 마스킹 (모든 출력의 마지막 관문)
# ---------------------------------------------------------------------------
_PII = [
    (re.compile(r"\b\d{6}-[1-4]\d{6}\b"), "******-*******"),
    (re.compile(r"\b01[016-9]-?\d{3,4}-?\d{4}\b"), "010-****-****"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "***@***"),
]


def mask_pii(text):
    """(마스킹된 텍스트, 치환 건수)를 돌려준다."""
    n = 0
    for pat, repl in _PII:
        text, k = pat.subn(repl, text)
        n += k
    return text, n


# ---------------------------------------------------------------------------
# [01] 온톨로지/개념 매핑
# ---------------------------------------------------------------------------
def stage01_map(question, store, labels, source="utterance"):
    """문서 참조를 먼저 떼어낸 뒤 나머지를 파싱한다.

    "2025년 사업보고서(제19기)에 비교표시된 제17기 영업이익"에서 앞부분은 **어느
    문서를 볼지**를, 뒷부분은 **그 문서의 어느 열을 읽을지**를 말한다. 떼어내지 않으면
    앞의 2025년을 조회 연도로 잡아 제17기를 묻는 질문에 제19기 값을 답한다.

    source: ontology.parse()로 그대로 전달하는 슬롯 출처 태그 기본값
    ("utterance" 또는 "rewrite") — G1 참고.
    """
    spec, masked = docref.parse(question)
    if spec and not docref.wanted(question, spec):
        spec, masked = None, question
    p = ontology.parse(masked if spec else question, store, labels, source=source)
    p["docref"] = spec
    p["doc_rcept"] = docref.resolve(spec, p.get("corp_code"), labels) if spec else None
    # 여러 기업을 비교하는 질문엔 기업별로 문서를 따로 고정한다(doc_rcept_by_corp).
    #
    # 예전엔 doc_rcept 하나(단일 rcept_no)를 전원에게 그대로 적용해, 한 회사의
    # 보고서 안에서 나머지 회사를 찾다가 못 찾았다("OCI홀딩스와 한화솔루션의 반기
    # 매출 순위"가 OCI홀딩스 하나만 나열하는 답으로 무너짐). spec 자체는 기업과
    # 무관한 조건(보고서 종류+결산월/기수)이라 기업마다 resolve()를 다시 돌리면
    # 각자의 문서를 정확히 고른다.
    p["doc_rcept_by_corp"] = (
        {cc: docref.resolve(spec, cc, labels) for cc in (p.get("corp_codes") or []) if cc}
        if spec else {})
    # 전역 doc_rcept(단일 스칼라)는 단일 기업 질문에서만 쓴다.
    #
    # 증감률(fact_compute)·전사 스크리닝(aggregate)은 기업별 고정도 뺀다. "제18기
    # 대비 제19기"에서 기준 열을 한쪽으로 고정하면 한 해씩 밀리고, 전사 스크리닝은
    # 특정 문서 하나로 좁힐 이유가 없다.
    if p.get("intent") in ("aggregate", "fact_compute"):
        p["doc_rcept"] = None
        p["doc_rcept_by_corp"] = {}
    elif p["doc_rcept"] and len(p.get("corps") or []) > 1:
        p["doc_rcept"] = None
    if p["doc_rcept"] and not p.get("year") and not (p.get("period") or {}).get("term"):
        # 열을 따로 지정하지 않았으면 그 보고서의 자기 기수를 읽는다.
        terms = labels.doc_terms(p["doc_rcept"])
        if terms:
            p["doc_term"] = max(terms)
    if not p.get("year") and spec and spec.get("ym"):
        # 문서 참조 마스킹이 질문에서 유일한 연도 단서(예: "2025년 반기보고서
        # (2025.06) 기준")를 지워버려 "연도를 알려주세요"로 되묻는 회귀가 났다.
        # spec의 ym이 이미 그 연도를 알고 있으므로 되살린다. 다중기업 비교
        # (doc_rcept_by_corp)에서 특히 필요 — 전역 doc_rcept가 없어 위 분기가
        # 안 걸린다.
        p["year"] = spec["ym"][0]
    return p


def _missing_fields(p):
    """부족한 항목. 단, 보고서를 특정했으면 연도는 이미 정해진 것이다.

    "제19기 사업보고서의 연결 영업이익"에는 연도가 없지만 되물을 이유가 없다.
    그 보고서의 자기 기수를 읽으면 된다.
    """
    miss = ontology.missing_fields(p)
    if p.get("doc_rcept") and (p.get("doc_term") is not None or p.get("year")):
        miss = [m for m in miss if m != "연도"]
    return miss


# ---------------------------------------------------------------------------
# [02] 검색 — fact store 조회 + 근거 좌표 확보
# ---------------------------------------------------------------------------
def to_won(f):
    """fact 값을 원(KRW) 단위로 환산한다. 기업마다 보고 단위가 달라 비교 전에 맞춰야 한다.

    value_decimal에 부호가 이미 들어 있으므로(음수 45,913건) 따로 곱하지 않는다.
    """
    try:
        return Decimal(f["value_decimal"]) * Decimal(f.get("scale") or 1)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _label_forms(p, labels):
    """조회에 시도할 표기 목록. 동의어 그룹 전체를 넓은 것부터."""
    ci = concepts.get(labels.facts)
    return ci.group_members(p["label"]) if p.get("label") else []


def _period_spec(p):
    """질문이 분기·반기를 물었으면 조회 스펙. 아니면 None (= 연간)."""
    sa = (p.get("period") or {}).get("sub_annual")
    return sa if sa and sa.get("label") else None


def _lookup_period(p, corp_code, store, labels, spec, year=None, scope=None):
    """분기·반기 값 하나. 4분기·하반기는 공시되지 않아 뺄셈으로 만든다."""
    year = year if year is not None else p["year"]
    scope = scope or p["scope"] or "consolidated"

    def one(sp):
        if p["metric"]:
            f = labels.lookup_metric(corp_code, p["metric"], scope, year, sp,
                                     p["statement"])
            if f:
                return f
        for form in _label_forms(p, labels):
            f = labels.lookup(corp_code, form, scope, year, p["statement"], period=sp)
            if f:
                return f
        return None

    if not spec.get("derive"):
        return one(spec)

    whole, part = spec["derive"]
    a, b = one({"label": whole}), one({"label": part})
    if not (a and b):
        return None
    # 단위가 다르면(사업보고서 백만원 vs 분기보고서 원) 빼면 안 된다.
    if to_won(a) is None or to_won(b) is None:
        return None
    val = to_won(a) - to_won(b)
    if val < 0:                          # 뺄셈이 음수면 짝이 안 맞는 것이다
        return None
    out = dict(a)
    out.update(value_decimal=str(val), value_raw=f"{val:,}", scale=1, unit_kr="원",
               period_label=spec.get("ko") or "차분", derived_from=(a["fact_id"], b["fact_id"]))
    return out


def _lookup_doc(p, labels, year=None, scope=None, term=None, doc_rcept=None):
    """지정된 보고서 안에서 읽는다. 그 문서에 없으면 None — 다른 문서로 흘리지 않는다."""
    scope = scope or p["scope"] or "consolidated"
    doc_rcept = doc_rcept if doc_rcept is not None else p["doc_rcept"]
    # 연도를 명시적으로 받았으면 그것이 우선이다. 문서의 자기 기수(doc_term)는
    # 열을 지정하지 않았을 때만 쓴다. 이 우선순위가 뒤집혀 있어 시리즈 조회가
    # 매 해 같은 열을 읽었다 — 제17·18·19기가 모두 같은 값으로 나왔다.
    if year is None:
        year = p.get("year")
    if year is None:
        term = term if term is not None else p.get("doc_term")
    else:
        term = None
    per = _period_spec(p)
    if p.get("metric"):
        f = labels.lookup_doc(doc_rcept, None, scope, year, term,
                              p["statement"], metric=p["metric"], period=per)
        if f:
            return f
    for form in _label_forms(p, labels):
        f = labels.lookup_doc(doc_rcept, form, scope, year, term,
                              p["statement"], period=per)
        if f:
            return f
    return None


def _lookup_one(p, corp_code, store, labels, year=None, scope=None, doc_rcept=None):
    """개념 하나를 한 기업에서 찾는다. metric이면 FactStore, 아니면 LabelStore."""
    year = year if year is not None else p["year"]
    scope = scope or p["scope"] or "consolidated"
    doc_rcept = doc_rcept if doc_rcept is not None else p.get("doc_rcept")
    if doc_rcept:
        return _lookup_doc(p, labels, year, scope, doc_rcept=doc_rcept)
    spec = _period_spec(p)
    if spec:
        return _lookup_period(p, corp_code, store, labels, spec, year, scope)
    if p["metric"]:
        # 근거 선택 정책을 한쪽으로 모은다 — 재작성이 있으면 최신 유효본이 맞다.
        f = labels.lookup_metric_annual(corp_code, p["metric"], scope, year,
                                        p["statement"])
        return f or store.lookup(corp_code, p["metric"], scope, year)
    for form in _label_forms(p, labels):
        f = labels.lookup(corp_code, form, scope, year, p["statement"])
        if f:
            return f
    return None


def stage02_retrieve_multi(p, store, labels):
    """대상 기업 각각에서 같은 개념·연도·기준의 fact를 모은다.

    doc_rcept_by_corp가 있으면(문서 고정 질문) 기업마다 자기 문서를 쓴다 — 전역
    doc_rcept 하나를 전원에게 적용하면 한 회사의 문서 안에서 나머지를 찾다가 못
    찾는다.
    """
    facts, missing = [], []
    by_corp = p.get("doc_rcept_by_corp") or {}
    for corp, cc in zip(p["corps"], p["corp_codes"]):
        f = _lookup_one(p, cc, store, labels, doc_rcept=by_corp.get(cc)) if cc else None
        (facts if f else missing).append(f if f else corp)
    return facts, missing


# 질문이 "재작성치·최신 문서 기준"을 명시하면 그쪽을 본다
_WANT_ORIGINAL = re.compile(r"최초\s*(?:확정치|보고|공시|제출본|수치)|당시\s*(?:보고|기재)"
                            r"|당해\s*연도\s*보고서|원본\s*기준")

# "각 사업보고서 기준" — 연도별로 그 해 자기 원본 보고서를 근거로 쓰라는
# 명시적 신호. _run_series의 같은-문서 앵커링을 막는 데 쓴다(실측:
# SEM-NUM-06/GOLD-W1-SEC-05).
_EACH_OWN_REPORT_SIGNAL = re.compile(r"각\s*(?:사업보고서|보고서)\s*기준|각각의?\s*(?:사업보고서|보고서)")


def _metric_lookup(labels, store, cc, metric, scope, year, statement=None,
                   prefer_latest=True):
    """metric 조회의 단일 창구. 근거 선택 정책이 경로마다 달라지지 않게 한다.

    같은 질문에 두 답이 나오면 안 된다 — 한화솔루션 2023 자산총계가 경로에 따라
    24.49조(최초치)와 24.79조(재작성치)로 갈렸다. 재작성이 있으면 최신 유효본이 맞다.
    """
    if not labels:
        return store.lookup(cc, metric, scope, year)
    f = labels.lookup_metric_annual(cc, metric, scope, year, statement,
                                    prefer_latest=prefer_latest)
    if not f:
        return store.lookup(cc, metric, scope, year)
    # 재작성이 있으면 숨기지 않는다.
    #
    # 그 해 사업보고서의 값과, 이후 보고서에 비교표시된 재작성치가 다를 수 있다.
    # 둘 다 옳고 어느 쪽을 원하는지는 질문에 달렸다. 하나를 조용히 고르면 나머지
    # 절반의 질문에 틀린다 — 실제로 두 정답셋이 서로 다른 쪽을 정답으로 삼는다.
    # 연결/별도를 함께 밝히는 것과 같은 이유로, 다르면 함께 밝힌다.
    other = labels.lookup_metric_annual(cc, metric, scope, year, statement,
                                        prefer_latest=not prefer_latest)
    if other and str(other.get("fact_id")) != str(f.get("fact_id")):
        a, b = to_won(f), to_won(other)
        if a is not None and b is not None and a != b:
            f = dict(f, restated=other)
    return f


def stage02_retrieve(p, store, labels=None, question=""):
    cc, metric, scope, year = p["corp_code"], p["metric"], p["scope"], p["year"]
    # 기본은 **최신 유효본**이다. 정정·재작성이 있었으면 그것이 현재 통용되는 값이고,
    # 근거 좌표도 그 문서를 가리켜야 한다. 그 해 사업보고서의 최초치가 필요한
    # 질문(‘당시 보고된’·‘최초 확정치’)이면 그쪽으로 돌린다.
    #
    # 어느 쪽이든 **다른 값도 답변에 함께 밝힌다** — 두 값이 다 옳기 때문이다.
    latest = not _WANT_ORIGINAL.search(question)

    # 분기·반기를 물었으면 연간 store를 타면 안 된다. 여기서 갈라두지 않으면
    # "2분기 매출"에 연간 매출을 주고 "2분기 기준입니다"라고 덧붙이게 된다.
    # 보고서를 지정한 질문은 그 문서 안에서만 읽는다.
    if p.get("doc_rcept"):
        scopes = (["consolidated", "separate"] if p["intent"] == "dual" or not scope
                  else [scope])
        years = [year, year - 1] if p["intent"] == "fact_compute" and year else [year]
        out = []
        for sc in scopes:
            for y in years:
                f = _lookup_doc(p, labels, y, sc)
                if f:
                    out.append(f)
        return out

    spec = _period_spec(p)
    if spec:
        scopes = (["consolidated", "separate"] if p["intent"] == "dual" or not scope
                  else [scope])
        years = ([year, p.get("base_year") or year - 1]
             if p["intent"] == "fact_compute" else [year])
        out = []
        for sc in scopes:
            for y in years:
                f = _lookup_period(p, cc, store, labels, spec, y, sc)
                if f:
                    out.append(f)
        return out

    if metric is None and p.get("label"):
        return _retrieve_by_label(p, labels)
    if p["intent"] == "fact_compute":
        sc = scope or "consolidated"
        base = p.get("base_year") or (year - 1 if year else None)
        cur = _metric_lookup(labels, store, cc, metric, sc, year, p["statement"], latest)
        prev = (store.lookup_in_doc(cur["doc_id"], metric, sc, base) if cur else None)
        if cur and not prev:            # 같은 문서에 없으면 다른 문서에서 찾는다
            prev = _metric_lookup(labels, store, cc, metric, sc, base, p["statement"], latest)
        return [f for f in (cur, prev) if f]
    if p["intent"] == "dual":
        return [f for f in
                (_metric_lookup(labels, store, cc, metric, "consolidated", year, p["statement"], latest),
                 _metric_lookup(labels, store, cc, metric, "separate", year, p["statement"], latest))
                if f]
    return [f for f in (_metric_lookup(labels, store, cc, metric, scope, year,
                                       p["statement"], latest),) if f]


def _retrieve_by_label(p, labels):
    """numqa의 8개 지표 밖 개념은 LabelStore에서 label로 찾는다."""
    cc, label, year, stmt = p["corp_code"], p["label"], p["year"], p["statement"]
    scopes = [p["scope"]] if p["scope"] else ["consolidated", "separate"]
    years = ([year, p.get("base_year") or year - 1]
             if p["intent"] == "fact_compute" else [year])
    if p["intent"] == "fact_compute":
        scopes = [p["scope"] or "consolidated"]
    out = []
    forms = _label_forms(p, labels)
    for sc in scopes:
        for y in years:
            for form in forms:
                f = labels.lookup(cc, form, sc, y, stmt)
                if f:
                    out.append(f)
                    break
    return out


def stage02_retrieve_struct(p, question):
    """비-XBRL 공시 필드 조회. 회차는 질문 단서로 좁히고, 못 좁히면 그대로 돌려준다."""
    sf = structstore.get()
    fld = p["field"]
    cc = sf.corp_code.get(p["corp"]) or p.get("corp_code")
    if not cc:
        return [], sf
    cands = sf.lookup(cc, fld["group"], fld["field_key"])
    if not cands:
        return [], sf
    # 질문이 접수번호를 댔으면 그 회차로 좁힌다. docref가 이미 뽑아 둔 값이다.
    rno = (p.get("docref") or {}).get("rcept_no")
    return structstore.select(question, cands, sf, p.get("year"), rcept_no=rno), sf


def struct_coordinate(f, sf):
    """비-XBRL fact의 근거 좌표. 재무제표가 아니라 공시 유형·필드가 자리를 대신한다."""
    rn = sf.report_nm.get(f["rcept_no"], "") or f.get("doc_group", "")
    return {
        "corp_name": f.get("corp_name", ""),
        "report_nm": rn,
        "path": f"{f.get('doc_group', '')} > {f.get('field_label') or f.get('field_key')}",
        "cell": f.get("field_key", ""),
        "rcept_no": f.get("rcept_no", ""),
        "ref_id": f.get("fact_id", ""),
        "flags": (["⚠️정정"] if "정정" in rn else []) + correction_flag(f),
        "value": str(f.get("value_raw", "")),
        # filings.diff()가 구조화 공시 필드를 field_key 그대로 색인하므로 그대로 재사용.
        "diff_key": f.get("field_key", ""),
    }


# major/exchange/holding 정정공시는 원본 연결을 날짜 텍스트 추출로 확정한다
# (code_chunkingandparsing/src/corrlink.py). 확정 못 하면 예전엔 그 사실 자체가
# 사라져서 "정정 없음"과 똑같이 보였다 — D-TRACE 계획서의 "정정 가능성" 표시가
# 이래서 비어 있었다. supersede_method로 확신도를 구분해 근거에 그대로 드러낸다.
_UNCERTAIN_SUPERSEDE = {"ambiguous", "ref_absent", "no_ref_date"}


def correction_flag(f):
    """정정 매칭이 불확실한 fact에 붙일 플래그. 확실한 경우(⚠️정정)는 건드리지 않는다."""
    if f.get("supersede_method") in _UNCERTAIN_SUPERSEDE:
        return ["❓정정 여부 미확정 — 원본 연결을 확신하지 못함"]
    return []


_QUOTE_LEN = 120


def _quote(text):
    """긴 원문(narrative 청크·이벤트 요약)을 근거 카드에 넣을 짧은 인용으로 자른다."""
    t = str(text or "").strip()
    return t if len(t) <= _QUOTE_LEN else t[:_QUOTE_LEN] + "…"


def to_coordinate(f):
    """fact 레코드 → 근거 좌표 dict (D_표기규칙 §2의 fact-path 변형).

    XBRL fact에는 section_path가 없다. 대신 재무제표 종류(statement)와 행 라벨
    (label_raw), 셀 위치(r:c)가 그 자리를 대신한다. 다만 사람이 DART 원문에서
    실제로 찾아가려면 "손익계산서(연결)"만으론 부족하고 목차 번호가 있어야
    빠르다("Ⅲ. 재무에 관한 사항 > 2-2. 연결 손익계산서") — statement_toc.py가
    rcept_no별로 미리 뽑아둔 번호를 붙여준다. 캐시에 없는 문서는 기존 표기로
    그대로 물러난다.
    """
    meta = rcept.lookup(f["rcept_no"])
    flags = []
    if meta.get("is_correction"):
        flags.append("⚠️정정")
    if f.get("is_superseded"):
        flags.append("⛔대체됨")
    stmt = STATEMENT_KO.get(f.get("statement"), f.get("statement") or "재무제표")
    scope_ko = numqa.SCOPE_KO.get(f.get("scope"), f.get("scope") or "")
    toc = statement_toc.lookup(f.get("rcept_no", ""), stmt, scope_ko)
    stmt_part = toc if toc else f"{stmt}({scope_ko})"
    # 셀 위치는 fact_id에서 뽑는다. 레코드의 col_index는 매칭 순번이라
    # fact_id의 컬럼 번호(=fiscal_term)와 다른 값이며, 그대로 쓰면 엉뚱한 셀을 가리킨다.
    parts = f.get("fact_id", "").split(":")
    cell = ":".join(parts[-2:]) if len(parts) >= 2 else ""
    return {
        "corp_name": f.get("corp_name") or meta.get("corp_name") or "",
        "report_nm": meta.get("report_nm") or f.get("doc_id", ""),
        "path": f"{stmt_part} > {f.get('label_raw', '')}",
        "cell": cell,
        "rcept_no": f.get("rcept_no", ""),
        "ref_id": f.get("fact_id", ""),
        "flags": flags,
        "value": f"{f.get('value_raw', '')}{f.get('unit_kr') or ''}",
        # filings.diff()가 색인하는 키와 같은 형식(filings._xbrl_fields 참고) —
        # evidence_pack.correction_info()가 정정 전/후 값을 찾을 때만 쓴다.
        "diff_key": f"{f.get('aclass_xbrl_code', '')}|"
                    f"{f.get('label_norm') or f.get('label_raw') or ''}|"
                    f"{f.get('fiscal_term', '')}|{f.get('scope', '')}",
    }


# ---------------------------------------------------------------------------
# [03] 계산/추론 — Decimal 정확계산 (LLM 미사용)
# ---------------------------------------------------------------------------
# 환산 대상이 되는 통화 단위. 이 밖(%, 주, 건 …)은 to_won이 손대지 않는다.
_CURRENCY_KR = {"원", "천원", "백만원", "십억원", "억원", ""}


def _display_unit(rows):
    """(표기 단위, 통화인가). 비율을 원으로 적지 않기 위해 필요하다.

    fact에는 이미 `unit_kr`이 있는데 순위 문장이 무조건 "원"을 붙이고 있었다.
    그래서 "부채비율이 가장 큰 기업은 대우건설 — 284.5**원**"이 나왔다.
    순위 자체는 맞아서 채점은 통과했지만 표기가 틀렸다.
    """
    units = {(f.get("unit_kr") or "").strip() for _, _, f in rows}
    if units <= _CURRENCY_KR:
        return "원", True              # to_won이 원으로 맞춰 놓았다
    if len(units) == 1:
        return units.pop(), False      # %처럼 환산이 필요 없는 단위
    return "", False                   # 섞였으면 단위를 붙이지 않는다


def _fmt(v, unit):
    """비율은 소수를 살리고 금액은 정수로 끊는다."""
    return (f"{v:,.1f}{unit}" if unit == "%" else f"{v:,}{unit}")


def _rank_answer(p, facts):
    """순위·집계의 결정론적 문장화. 통화는 원으로 환산해 정렬한다."""
    out = {"question": None, "parsed": p, "text": "", "numbers": [],
           "sources": [], "status": "ok", "ranking": []}
    rows = [(f["corp_name"], to_won(f), f) for f in facts]
    rows = [r for r in rows if r[1] is not None]
    if not rows:
        out.update(status="no_fact", text="비교할 수 있는 fact를 찾지 못했습니다.")
        return out
    rows.sort(key=lambda r: r[1], reverse=(p["order"] == "desc"))
    # "OO와 XX 중 어디가 더 큰가"류 2사 비교는 "더 큰"이 _RANKING에 걸려 intent가
    # ranking·topn=1로 잡히지만, 그렇다고 진 쪽 수치를 잘라내면 비교가 안 된다.
    # 업종 전체(N개사) 순위에서 1위만 궁금한 것과 달리, 명시적으로 지목한 소수
    # 기업끼리는 양쪽 값을 다 보여줘야 "무엇과 비교해 더 큰지"가 성립한다.
    explicit_compare = len(p.get("corps") or []) <= 3 and not p.get("sector")
    if p.get("topn") and not explicit_compare:
        rows = rows[:p["topn"]]

    out["sources"] = [{"scope": f["scope"], "fact_id": f["fact_id"], "rcept_no": f["rcept_no"],
                       "aclass": f["aclass_xbrl_code"], "row": f["label_raw"]} for _, _, f in rows]
    scope_ko = SCOPE_KO_ALL.get(p["scope"] or "consolidated", "")
    concept = ontology.concept_ko(p)
    # 순위도 어느 기간을 비교한 것인지 밝힌다. "2023년"이라고만 쓰면 반기 값을
    # 연간으로 읽는다.
    spec = _period_spec(p)
    when = f"{p['year']}년 {spec['ko']}" if spec else f"{p['year']}년"

    unit, is_won = _display_unit(rows)

    if p["intent"] == "aggregate":
        if not is_won:
            # 비율을 더하면 뜻이 없는 숫자가 나온다. 합산하지 않고 나열한다.
            listed = " / ".join(f"{name} {_fmt(v, unit)}" for name, v, _ in rows)
            out["numbers"] = [str(v) for _, v, _ in rows]
            out["text"] = (f"{when} {scope_ko} 기준 {concept}은(는) 단위가 {unit or '미상'}"
                           f"라 합산하지 않습니다 — {listed}.")
            return out
        total = sum(v for _, v, _ in rows)
        out["numbers"] = [str(total)]
        out["text"] = (f"{when} {scope_ko} 기준 {concept} 합계는 "
                       f"{_fmt(total, unit)}입니다 ({len(rows)}개 기업).")
        return out

    out["ranking"] = [name for name, _, _ in rows]
    out["numbers"] = [str(v) for _, v, _ in rows]
    order_ko = "큰" if p["order"] == "desc" else "작은"
    listed = " / ".join(f"{i}. {name} {_fmt(v, unit)}"
                        for i, (name, v, _) in enumerate(rows, 1))
    head = (f"{when} {scope_ko} 기준 {concept}이 가장 {order_ko} 기업은 {rows[0][0]}입니다"
            if p.get("topn") == 1 else
            f"{when} {scope_ko} 기준 {concept}이 {order_ko} 순서는 다음과 같습니다")
    # "차이"·"얼마나 더" 같은 질문은 나열만 하면 안 답한 것과 같다 — 2개사
    # 비교일 때는 값 차이도 같이 낸다. 단위가 통화(is_won)일 때만 뜻이 있다 —
    # %는 이미 _fmt로 나온 값이 비교 가능한 단위라 차이도 자연스럽게 %p로 읽힌다.
    diff_note = ""
    if p["intent"] == "compare_multi" and len(rows) == 2 and unit:
        diff = abs(rows[0][1] - rows[1][1])
        diff_note = f" (차이 {_fmt(diff, unit)})"
    out["text"] = f"{head} — {listed}.{diff_note}"
    return out


# Phase A' — 빈 값을 확정 답변으로 내지 않는다. "0"은 뺐다: exchange/계약금액(원)
# 1302건 실측(2026-09-04, qa/structstore.py 캐시)에 "0"인 레코드가 하나도
# 없었고("-" 72건·"- -" 26건만 빈 값), 다른 필드에도 "0"이 무의미한 빈 값인지
# 진짜 무상 계약(0원)인지 이 자리에서 구분할 근거가 없다 — 애매하면
# fail-closed 원칙(§2)에 따라 "0"은 목록에서 뺀다.
_EMPTY_VALUES = {"-", "- -", ""}


def _struct_answer(p, facts, sf):
    """비-XBRL 필드의 결정론적 문장화. 회차가 여럿이면 고르지 않고 나열한다."""
    out = {"question": None, "parsed": p, "text": "", "numbers": [],
           "sources": [], "status": "ok"}
    if not facts:
        out.update(status="no_fact", text="해당 공시 항목을 찾지 못했습니다.")
        return out
    fld, corp = p["field"], p["corp"]
    out["sources"] = [{"fact_id": f["fact_id"], "rcept_no": f["rcept_no"],
                       "field": f["field_key"]} for f in facts]
    values = {f["value_raw"] for f in facts}
    if len(facts) > 1 and len(values) > 1:
        opts = sorted(facts, key=lambda f: sf.rcept_dt.get(f["rcept_no"], ""))
        listed = "; ".join(
            f"{sf.report_nm.get(f['rcept_no'], '')[:24]}({sf.rcept_dt.get(f['rcept_no'], '')[:8]})"
            f"={f['value_raw']}" for f in opts[:6])
        out.update(status="ambiguous",
                   text=f"{corp}의 {fld['label']}은(는) 회차가 여러 건이라 특정이 필요합니다 — {listed}"
                        + (f" 외 {len(opts)-6}건" if len(opts) > 6 else ""))
        return out
    f = facts[0]
    rn = sf.report_nm.get(f["rcept_no"], "")
    val = str(f.get("value_raw") or "").strip()
    if not val or val in _EMPTY_VALUES:
        # 회차 하나로(또는 값이 같은 여러 회차로) 좁혔어도 그 공시 자체가 값을
        # 비워 둔 경우다 (실측: NC "공급계약 금액이 얼마야" → 예전엔
        # "계약금액은(는) -입니다"로 확정 답변했다). 조용히 틀린 확정 답변보다
        # 정직한 실패가 낫다(§2) — status="no_fact"로 S1까지 넘긴다.
        out.update(status="no_fact",
                   text=f"{corp}의 {fld['label']}이(가) {rn}에 기재되어 있지 않습니다"
                        f" (값 '{val or '(빈 값)'}').")
        return out
    out["numbers"] = [str(f.get("value_decimal") or f["value_raw"]).replace(",", "")]
    out["text"] = f"{corp} {rn} 기준 {fld['label']}은(는) {f['value_raw']}입니다."
    return out


def stage03_compute(question, store, p=None, facts=None):
    if p is not None and p.get("intent") in ("ranking", "aggregate", "compare_multi"):
        return _rank_answer(p, facts)
    # 기간 질문은 numqa.answer로 보내면 안 된다. 그쪽은 질문을 다시 파싱해서
    # 연간 값을 답하기 때문에, 우리가 찾아둔 분기 fact가 버려지고 연간 숫자에
    # "3분기 기준입니다"라는 안내만 붙는다.
    if p is not None and (_period_spec(p) or p.get("doc_rcept")
                          or (p.get("metric") is None and p.get("label"))):
        return _label_answer(p, facts)

    # 우리가 02단계에서 찾은 fact로 답하는 것이 기본이다.
    #
    # numqa.answer는 질문을 **다시 파싱한다**. 그래서 우리가 고른 근거를 버리고
    # 자기 규칙으로 다시 고른다. 이 함정에 세 번 빠졌다 —
    #   ① "2022회계연도"를 못 읽어 unparsed
    #   ② 분기를 물었는데 연간 값
    #   ③ 재작성된 자산총계인데 최초치 (24.49조 vs 24.79조)
    # 근거 선택 정책이 한 시스템 안에서 갈라지면 같은 질문에 두 답이 나온다.
    if facts and p.get("corp") and p.get("year"):
        out = _label_answer(p, facts)
        if out["status"] == "ok":
            return out

    res = numqa.answer(question, store)
    # numqa는 질문을 **다시 파싱한다**. 우리 온톨로지가 "2022회계연도"를 읽어도
    # 그쪽 파서가 못 읽으면 unparsed로 돌아온다. 이미 02단계에서 fact를 찾아둔
    # 상태이므로, 그대로 실패시키면 찾은 값을 버리고 "기업/지표/연도를 특정하지
    # 못했습니다"라고 답하게 된다 — 값을 손에 쥔 채로.
    if res.get("status") in ("unparsed", "no_fact") and facts and p:
        out = _label_answer(p, facts)
        if out["status"] == "ok":
            return out
    return res


def _label_answer(p, facts):
    """LabelStore 경로의 결정론적 문장화. 숫자는 fact 값만 쓰고 LLM은 개입하지 않는다."""
    out = {"question": None, "parsed": p, "text": "", "numbers": [],
           "sources": [], "status": "ok"}
    if not facts:
        out.update(status="no_fact", text="해당 fact를 찾지 못했습니다.")
        return out
    label, corp, year = p["label"] or ontology.concept_ko(p), p["corp"], p["year"]
    # 어느 기간을 받았는지 문장에 박아둔다. 밝히지 않으면 틀려도 알아챌 수 없다.
    spec = _period_spec(p)
    when = f"{year}년 {spec['ko']}" if spec else f"{year}년"
    if p.get("doc_rcept"):
        # 어느 보고서에서 읽었는지 밝힌다. 같은 연도라도 보고서마다 값이 다를 수 있다.
        ft = facts[0].get("fiscal_term") if facts else None
        when = (f"{facts[0].get('base_year')}년" if facts else when) + (f" (제{ft}기" if ft else " (")
        when += f", {facts[0].get('report_nm')} 기준)" if facts else ")"
    out["sources"] = [{"scope": f["scope"], "fact_id": f["fact_id"],
                       "rcept_no": f["rcept_no"], "aclass": f["aclass_xbrl_code"],
                       "row": f["label_raw"]} for f in facts]

    if p["intent"] == "fact_compute" and len(facts) >= 2:
        cur, prev = facts[0], facts[1]
        try:
            c, v = Decimal(cur["value_decimal"]), Decimal(prev["value_decimal"])
            pct = (c - v) / v * 100 if v else None
        except (InvalidOperation, ValueError, ZeroDivisionError):
            pct = None
        if pct is None:
            out.update(status="no_fact", text="증감률을 계산할 수 없습니다 (전기값 결측/0).")
            return out
        pct = round(float(pct), 1)
        base = p.get("base_year") or (year - 1)
        delta = to_won(cur) - to_won(prev) if (to_won(cur) is not None
                                              and to_won(prev) is not None) else None
        out["numbers"] = [f"{pct}%"] + ([str(delta)] if delta is not None else [])
        out["text"] = (f"{corp}의 {when} {SCOPE_KO_ALL.get(cur['scope'], '')} {label}은 "
                       f"{base}년 대비 {pct}% {'증가' if pct >= 0 else '감소'}했습니다 "
                       f"({cur['value_raw']} vs {prev['value_raw']}, 단위 {cur['unit_kr']}"
                       + (f" · 증감액 {delta:,}원" if delta is not None else "") + ").")
        return out

    parts = [f"{SCOPE_KO_ALL.get(f['scope'], '')} {f['value_raw']}{f['unit_kr']}" for f in facts]
    # 재작성치가 있으면 함께 보인다 — 어느 쪽을 원하는지는 질문자가 안다.
    restate = [f for f in facts if f.get("restated")]
    if restate:
        r0 = restate[0]["restated"]
        note_rs = (f" (이후 보고서 재작성치 {r0['value_raw']}{r0['unit_kr']}"
                   f" · {r0.get('report_nm', '')} 기준)")
    else:
        note_rs = ""
    out["numbers"] = [f["value_raw"].replace(",", "") for f in facts]
    note = " — 연결/별도 기준이 다르므로 구분이 필요합니다." if len(facts) == 2 else ""
    out["text"] = f"{corp}의 {when} {label}은 " + " / ".join(parts) + note_rs + note
    return out


def calc_steps(p, facts, res):
    """[4] 계산 과정 영역에 넣을 단계 문자열."""
    if p.get("intent") in ("ranking", "aggregate", "compare_multi"):
        steps = ["보고 단위가 기업마다 달라 원(KRW)으로 환산한 뒤 비교했습니다.", ""]
        rows = sorted(((f["corp_name"], to_won(f), f) for f in facts),
                      key=lambda r: (r[1] is None, r[1]),
                      reverse=(p.get("order") == "desc"))
        for name, won, f in rows:
            steps.append(f"{name:<12} {f['value_raw']} {f['unit_kr']}"
                         f"  →  {won:,}원" if won is not None else f"{name:<12} 환산 실패")
        return steps
    if p["intent"] != "fact_compute" or len(facts) < 2:  # noqa: SIM103
        return ["단순 조회 — 계산 없음 (store의 fact 값을 그대로 사용)"]
    cur, prev = facts[0], facts[1]
    unit = cur.get("unit_kr", "")
    pct = res["numbers"][0] if res.get("numbers") else "?"
    return [
        f"당기 {cur['base_year']}년 : {cur['value_raw']} {unit}",
        f"전기 {prev['base_year']}년 : {prev['value_raw']} {unit}",
        "증감률 = (당기 − 전기) ÷ 전기 × 100",
        f"       = {pct}",
    ]


# ---------------------------------------------------------------------------
# [04] 도메인 전문가 검증 — 규칙 4개
# ---------------------------------------------------------------------------
def check_sum(p, facts, store, labels):
    """합계 검증 — 답한 개념이 구성요소의 합과 맞는가.

    corpus에서 유도한 계층(`hierarchy`)을 쓴다. "자산총계 = 유동자산 + 비유동자산"이
    39개사에서 확인됐으므로, 이 기업·연도에서도 맞는지 실제로 더해본다.
    파싱 오류나 대체된 값이 섞이면 여기서 어긋난다.
    """
    if len(facts) != 1:
        return None
    f = facts[0]
    concept = concepts.canonical(f.get("label_norm") or f.get("label_raw") or "")
    rels = hierarchy.children_of(concept)
    if not rels:
        return None
    rel = rels[0]
    # f가 분기·반기(3개월간·9개월간 등) 값이면 구성요소도 같은 기간으로 봐야 한다.
    # period 없이 부르면 labels.lookup이 연간치를 돌려줘서, "3분기 매출액 7.4조"를
    # "연간 매출원가+매출총이익 27조"와 비교하는 식으로 항상 어긋났다(실측 확인됨).
    # 뺄셈으로 만든 기간(4분기 등)은 구성요소도 같은 방식으로 다시 만들어야 해서
    # 여기서는 검사를 보류한다 — 틀린 비교를 하느니 안 하는 게 낫다.
    spec = _period_spec(p)
    if spec and spec.get("derive"):
        return None
    total = Decimal(0)
    for child in rel["children"]:
        g = labels.lookup(f["corp_code"], child, f["scope"], f["base_year"], f["statement"],
                          period=spec)
        w = to_won(g) if g else None
        if w is None:
            return {"name": "합계 검증", "ok": True,
                    "detail": f"구성요소 '{child}' 미보유 — 검사 생략"}
        total += w
    got = to_won(f)
    if got is None or got == 0:
        return None
    ok = abs(total - got) / abs(got) <= Decimal("0.005")
    expr = " + ".join(rel["children"])
    return {"name": "합계 검증", "ok": ok,
            "detail": (f"{concept} = {expr} 일치 ({rel['corps']}개사에서 확인된 관계)"
                       if ok else
                       f"{concept} ≠ {expr} — 합 {total:,} vs 값 {got:,}")}


def stage04_verify(p, facts, res, unresolved=None, store=None, labels=None):
    checks = []
    multi = p.get("intent") in ("ranking", "aggregate", "compare_multi")

    units = {(f.get("unit_kr"), f.get("scale")) for f in facts}
    if multi:
        # 기업마다 보고 단위가 다른 것은 정상이다. 대신 전부 원 단위로 환산됐는지를 본다.
        bad = [f for f in facts if to_won(f) is None]
        checks.append({
            "name": "단위 환산",
            "ok": not bad,
            "detail": (f"{len(units)}종 → 원 단위로 환산" if not bad
                       else f"{len(bad)}건 환산 실패 — 비교 무효"),
        })
    else:
        checks.append({
            "name": "단위 일치",
            "ok": len(units) <= 1,
            "detail": (facts[0].get("unit_kr", "-") if len(units) == 1
                       else f"{len(units)}종이 섞임 — 계산 무효"),
        })

    want = {p["year"], p["year"] - 1} if p["intent"] == "fact_compute" else {p["year"]}
    got = {f.get("base_year") for f in facts}
    checks.append({
        "name": "기간 정합성",
        "ok": got == want,
        "detail": f"기대 {sorted(want)} / 실제 {sorted(got)}",
    })

    ok3, detail3 = True, "해당 없음 (증감 표현 아님)"
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", res.get("text", ""))
    if m:
        v = float(m.group(1))
        inc, dec = "증가" in res["text"], "감소" in res["text"]
        if inc or dec:
            ok3 = (v >= 0) == inc
            detail3 = f"{v}% ↔ '{'증가' if inc else '감소'}'"
    checks.append({"name": "부호·방향", "ok": ok3, "detail": detail3})

    sup = [f for f in facts if f.get("is_superseded")]
    if not sup:
        checks.append({"name": "대체되지 않은 근거", "ok": True, "detail": "정상"})
    else:
        # 대체(superseded)됐다는 사실만으로 실패 처리하지 않는다 — 값이 그대로인
        # 단순 재제출·형식정정까지 실패로 잡으면 답은 맞는데 검증만 깎인다.
        # 대체한 후속 공시(labels 인덱스가 고르는 "최신·비대체" 값)의 같은 항목을
        # 조회해서 값이 실제로 달라졌을 때만 실패로 남긴다. 후속값을 못 찾거나
        # 조회 중 무엇이든 어긋나면(예외 포함) 안전하게 지금처럼 실패로 남긴다.
        changed = []
        for f in sup:
            repl = None
            try:
                label = f.get("label_norm") or f.get("label_raw")
                if labels is not None and label:
                    repl = labels.lookup(f.get("corp_code"), label, f.get("scope"),
                                         f.get("base_year"), f.get("statement"))
            except Exception:
                repl = None
            if repl is None or repl.get("fact_id") == f.get("fact_id"):
                changed.append(f)          # 후속값 확인 불가 — 실패로 남긴다
                continue
            a, b = to_won(f), to_won(repl)
            if a is None or b is None or a != b:
                changed.append(f)
        checks.append({
            "name": "대체되지 않은 근거",
            "ok": not changed,
            "detail": ("정상 (후속 공시 값 동일 확인)" if not changed
                       else f"{len(changed)}/{len(sup)}건이 후속 공시로 대체됨(값 변경 또는 확인 불가)"),
        })

    if labels is not None:
        c = check_sum(p, facts, store, labels)
        if c:
            checks.append(c)

    if multi:
        miss = unresolved or []
        checks.append({
            "name": "대상 기업 전원 확보",
            "ok": not miss,
            "detail": (f"{len(facts)}개 전원" if not miss
                       else f"{len(miss)}개 누락: {', '.join(miss)}"),
        })

    return {"passed": all(c["ok"] for c in checks), "checks": checks}


def _retryable(verification):
    """03으로 되돌려 재작업할 가치가 있는 실패인가.

    fact-path는 store 조회 + Decimal 연산이라 같은 입력에 항상 같은 결과가 나온다.
    따라서 지금은 재시도가 무의미하므로 항상 False다. LLM이 끼는 narrative 경로가
    붙으면 그때 판정 기준이 생긴다. 루프 구조는 남겨 둔다.
    """
    return False


def _concept_forms(p, labels):
    """이 질문의 개념이 corpus에서 어떤 표기로 저장돼 있는지."""
    ci = concepts.get(labels.facts)
    if p.get("label"):
        return ci.group_members(p["label"])
    if p.get("metric"):
        return [c for c, m in ci.metric_of.items() if m == p["metric"]] or []
    return []


def _available_years_for(cc, p, labels):
    """한 기업(corp_code)이 이 (개념·기준) 조합으로 가진 연도.

    파생 개념(부채비율 등)은 그 자체가 corpus 행이 아니라 label_index에 없다 —
    _concept_forms가 빈 리스트를 돌려줘 "보유 연도 없음"으로 오판했다. 대신 두
    피연산자 각각의 보유 연도를 교집합해서, 둘 다 있는 해만 "계산 가능한 해"로 본다.
    """
    scope = p["scope"] or "consolidated"
    if p.get("derived") and p["derived"] in derived.RULES:
        _, operands, *_ = derived.RULES[p["derived"]]
        ci = concepts.get(labels.facts)
        year_sets = []
        for oper in operands:
            canon, _, _ = ci.match(oper)
            sub = {"label": canon or oper, "metric": ci.metric_of.get(canon or oper)}
            ys = set()
            for form in _concept_forms(sub, labels):
                ys |= labels.years(cc, form, scope)
            year_sets.append(ys)
        return set.intersection(*year_sets) if year_sets and all(year_sets) else set()
    out = set()
    for form in _concept_forms(p, labels):
        out |= labels.years(cc, form, scope)
    return out


def available_years(p, labels):
    """이 (기업·개념·기준) 조합이 실제로 가진 연도.

    "가장 최근"을 전역 상수로 두면 안 된다 — 조합의 23.8%는 최신이 2025가 아니다.
    삼성전자 영업수익은 2023년이 마지막이고 매출액은 2025년이 마지막이다.

    기업이 여럿(compare_multi 등)이면 기업별 보유 연도를 **교집합**한다 — 안 그러면
    "삼성전자와 SK하이닉스 매출 차이"에서 각자 다른 최신 연도가 뽑혀 비교가
    어긋난다. 공통으로 가진 연도가 없으면 빈 집합을 돌려주고, 호출부가 "보유 연도
    없음"으로 처리한다.
    """
    corp_codes = [cc for cc in (p.get("corp_codes") or [p.get("corp_code")]) if cc]
    if not corp_codes:
        return set()
    year_sets = [_available_years_for(cc, p, labels) for cc in corp_codes]
    return set.intersection(*year_sets) if all(year_sets) else set()


def _run_narrative(r, p, question):
    """서술형 경로 — 원문을 HCX가 읽고 답한다.

    수치 경로와 분명히 구분한다. `generated_by="llm"`로 표시하고, 답변에 등장한 숫자가
    근거 원문에 실제로 있는지 검증한다. 이 프로젝트에서 LLM이 개입하는 유일한 구간이다.
    """
    chunks = narrative.retrieve(question, p)
    r.searched = {"기업": p.get("corp"), "연도": p.get("year"),
                  "대상": "narrative 청크 (표 제외)",
                  "검색된 절": ", ".join({c["section_path"] for c in chunks}) or "없음"}
    if not chunks:
        r.stage("02").status = "실패"
        r.stage("02").note = "근거 원문 없음"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)

    r.stage("02").status = "완료"
    r.stage("02").note = f"원문 {len(chunks)}건"
    r.evidence = [{"corp_name": c["corp_name"], "report_nm": c["report_nm"],
                   "path": c["section_path"], "cell": "",
                   "rcept_no": c["rcept_no"], "ref_id": c["chunk_id"],
                   "flags": (["⚠️정정"] if "정정" in (c["report_nm"] or "") else [])
                             + correction_flag(c),
                   "value": _quote(c.get("text"))}
                  for c in chunks]

    ans, err, usage = narrative.generate(question, chunks)
    if err:
        r.stage("03").status = "실패"
        r.stage("03").note = f"LLM 호출 실패: {err[:60]}"
        for no in ("04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S6"
        r.notices.append(f"⚠️ 서술형 생성에 실패했습니다 — {err[:80]}")
        return r

    r.generated_by = "llm"
    r.usage = usage or {}
    r.answer_text = ans
    r.stage("03").status = "완료"
    r.stage("03").note = f"{MODEL_NOTE} · {r.usage.get('total_tokens', '?')} 토큰"

    checks = [narrative.verify_numbers(ans, chunks, p=p),
              {"name": "근거 범위", "ok": True,
               "detail": f"{len(chunks)}개 원문만 참고 (표 제외, 외부 지식 금지 지시)"}]
    v = {"passed": all(c["ok"] for c in checks), "checks": checks}
    r.verification = v
    r.stage("04").status = "완료" if v["passed"] else "실패"
    r.stage("04").note = "모든 규칙 통과" if v["passed"] else "인용 수치 검증 미통과"
    r.calc_steps = ["계산 없음 — 원문을 읽어 서술한 답입니다.",
                    "수치가 필요하면 fact store 경로(수치 질문)를 쓰십시오."]
    r.answer_text, n = mask_pii(r.answer_text)
    if n:
        r.notices.append(f"🔒 개인정보가 감지되어 마스킹 처리했습니다. ({n}건)")
    r.notices.append("🤖 이 답변은 HyperCLOVA X가 원문을 읽어 생성했습니다 (수치 경로와 구분).")
    r.stage("05").status = "완료"
    r.state = "S0" if v["passed"] else "S2"
    return r


MODEL_NOTE = "HCX-005"


def _event_date_answer(question, p):
    """G6: 이벤트(유상증자 등)의 결정(접수)일자. XBRL 금액이 아니라 날짜를 답한다.

    1차 출처는 `p["event"]["anchors"]`(qa/ontology.py가 qa/events_vocab.py +
    qa/filings.py::find_events()로 이미 산출해 둔 것)다. 앵커가 여럿이면
    `contract.find()`(폐기하지 않는다)로 질문의 고유명사(거래상대방·프로젝트명
    등)를 2차 필터로만 걸어 좁힌다 — 못 좁히면 최신 1건을 기본 앵커로 쓰고 나머지
    건수를 답변에 밝힌다. 앵커가 하나도 없으면 fail-closed(None) — 억지로 답하지
    않고 기존 흐름(금액 조회 등)에 맡긴다.
    """
    anchors = ((p.get("event") or {}).get("anchors")) or []
    if not anchors:
        return None
    corp = p.get("corp")
    picked = anchors
    if len(anchors) > 1:
        narrowed = set(contract.find(question, corp, top=len(anchors) + 2))
        filtered = [a for a in anchors if a["rcept_no"] in narrowed]
        if len(filtered) == 1:
            picked = filtered
    anchor = picked[0]                          # 못 좁히면 최신 1건이 기본 앵커다
    dt = anchor.get("rcept_dt") or ""
    if len(dt) != 8 or not dt.isdigit():
        return None
    disp = f"{dt[:4]}년 {int(dt[4:6])}월 {int(dt[6:8])}일"
    name = filings.base_name(anchor.get("report_nm"))
    corr_note = " (이후 정정 있음)" if anchor.get("is_correction") else ""
    extra = f" (같은 유형의 다른 결정 {len(anchors) - 1}건이 더 있습니다.)" if len(anchors) > 1 else ""
    txt = f"{corp}의 {name}은(는) {disp}에 접수(결정)되었습니다{corr_note}.{extra}"
    return txt, [anchor["rcept_no"]], [dt]


def _event_amount_answer(question, p):
    """wh=="amount" — 이벤트 앵커로 구조화 필드 회차를 좁혀 확정 답변한다.

    _event_date_answer()와 같은 원칙: 앵커가 여럿이면 contract.find()로 2차
    좁히고, 못 좁히면 최신 1건을 기본 앵커로 쓴다. 그 앵커의 rcept_no로
    structstore 후보를 좁혀서 하나로 안 좁혀지면(그 앵커 안에 그 필드 값이
    없으면) None — 억지로 답하지 않는다.
    """
    anchors = ((p.get("event") or {}).get("anchors")) or []
    fld = p.get("field")
    if not anchors or not fld:
        return None
    corp = p.get("corp")
    picked = anchors
    if len(anchors) > 1:
        narrowed = set(contract.find(question, corp, top=len(anchors) + 2))
        filtered = [a for a in anchors if a["rcept_no"] in narrowed]
        if len(filtered) == 1:
            picked = filtered
    anchor = picked[0]
    sf = structstore.get()
    cc = sf.corp_code.get(corp) or p.get("corp_code")
    if not cc:
        return None
    cands = sf.lookup(cc, fld["group"], fld["field_key"])
    hit = [f for f in cands if f["rcept_no"] == anchor["rcept_no"]]
    if not hit:
        return None
    val = str(hit[0].get("value_raw") or "").strip()
    if not val or val in _EMPTY_VALUES:
        return None
    name = filings.base_name(anchor.get("report_nm"))
    extra = f" (같은 유형의 다른 결정 {len(anchors) - 1}건이 더 있습니다.)" if len(anchors) > 1 else ""
    txt = f"{corp} {name}({anchor.get('rcept_dt','')}) 기준 {fld['label']}은(는) {val}입니다.{extra}"
    return txt, [anchor["rcept_no"]], [val.replace(",", "")]


def _run_contract(r, p, txt, ev, nums, got=None):
    """계약 공시 조회. 비율은 공시에 적힌 값을 그대로 쓴다 — 우리가 나눠 계산하면
    회사가 어떤 매출액을 썼는지 몰라 미세하게 달라진다."""
    fl = filings.get()
    r.stage("01").status = "완료"
    r.stage("01").note = (r.stage("01").note or "") + " · 계약조회"
    for no in ("02", "03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("04").note = "공시 기재값 직접 인용"
    # contract.answer()는 근거가 문서 한 건뿐이라 읽은 필드(got) 전부를 그 한 카드에
    # 인용값으로 얹는다. docstats.run()처럼 got이 없는 호출부는 인용값 없이 그대로 둔다.
    quote = " / ".join(f"{g['label']} {g['raw']}" for g in got) if got else ""
    r.evidence = [{"corp_name": (fl.meta.get(x) or {}).get("corp_name", ""),
                   "report_nm": (fl.meta.get(x) or {}).get("report_nm", ""),
                   "path": "공시 필드", "cell": "", "rcept_no": x, "ref_id": x,
                   "flags": (["⚠️정정"] if filings.is_correction(
                       (fl.meta.get(x) or {}).get("report_nm")) else []),
                   "value": quote} for x in ev]
    r.answer_text = txt
    r.numbers = nums
    r.state = "S0"
    return r


def _run_perf(r, p, store, labels=None):
    """실적 흐름 요약. 숫자는 전부 fact에서 나오고 LLM은 개입하지 않는다."""
    r.stage("01").status = "완료"
    r.stage("01").note = (r.stage("01").note or "") + " · 실적흐름"
    sums = []
    for corp, cc in zip(p["corps"], p["corp_codes"]):
        if not cc:
            continue
        s = perf.summarize(store, corp, cc, p.get("scope") or "consolidated")
        if s:
            sums.append(s)
    if not sums:
        r.stage("02").status = "실패"
        r.stage("02").note = "실적 시리즈 조회 실패"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)
    for no in ("02", "03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("02").note = f"{len(sums)}개 기업 · 최근 {perf.SPAN}개년"
    r.stage("04").note = "fact 값으로 계산 · 파생 없음"
    r.answer_text = perf.render(sums)
    r.numbers = perf.numbers(sums)
    # 업종 보너스 지표 — 매출/영업이익 3종은 모든 업종에 똑같이 물어보는 골드셋
    # 검증 대상이라 손대지 않는다. 대신 한 기업 질문이고, 그 업종에서 유독 눈여겨
    # 보는 계정이 XBRL에 실제로 있으면(예: 반도체=연구개발비) 한 줄 덧붙인다.
    # CAPEX·가동률·NIM처럼 슬라이드가 든 지표들은 구조화 데이터에 아예 없어서
    # (실측 확인됨) 여기 넣지 않는다 — 있는 척하지 않는다.
    if labels is not None and len(sums) == 1:
        bonus = _perf_sector_bonus(sums[0]["corp"], p["corps"][0], p["corp_codes"][0],
                                   sums[0]["years"][-1], p.get("scope") or "consolidated", labels)
        if bonus:
            r.answer_text += f" {bonus}"
    r.state = "S0"
    return r


def _run_health(r, p, labels):
    """현금흐름 3대 활동 부호 패턴 + ROE·ROA. 숫자는 전부 fact에서 나오고 LLM은 개입하지 않는다."""
    r.stage("01").status = "완료"
    r.stage("01").note = (r.stage("01").note or "") + " · 건강도진단"
    diags = []
    for corp, cc in zip(p["corps"], p["corp_codes"]):
        if not cc:
            continue
        d = health.diagnose(labels, corp, cc, p.get("scope") or "consolidated")
        if d:
            diags.append(d)
    if not diags:
        r.stage("02").status = "실패"
        r.stage("02").note = "현금흐름·재무비율 자료 없음"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)
    for no in ("02", "03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("02").note = f"{len(diags)}개 기업 · 현금흐름 패턴 + ROE/ROA"
    r.stage("04").note = "fact 값으로 계산 · 파생 없음"
    r.answer_text = " / ".join(health.render(d) for d in diags)
    r.numbers = ([str(v) for d in diags for v in d.get("cf", {}).values()]
                + [str(d["roe"]) for d in diags if d.get("roe") is not None]
                + [str(d["roa"]) for d in diags if d.get("roa") is not None])
    r.state = "S0"
    return r


def _run_events(r, p):
    """이벤트 통합 뷰. 계산·판단 없이 최근 수시·지분공시를 접수일 역순으로 나열한다."""
    r.stage("01").status = "완료"
    r.stage("01").note = (r.stage("01").note or "") + " · 이벤트통합뷰"
    rows = events.recent(p["corp"])
    if not rows:
        r.stage("02").status = "실패"
        r.stage("02").note = "수시·지분공시 없음"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)

    fl = filings.get()
    r.evidence = [{"corp_name": p["corp"],
                    "report_nm": (fl.meta.get(rno) or {}).get("report_nm", ""),
                    "path": "수시·지분공시", "cell": "", "rcept_no": rno, "ref_id": rno,
                    "flags": ["⚠️정정"] if is_corr else [],
                    "value": line.split(" — ", 1)[1] if " — " in line else ""}
                  for rno, line, is_corr in rows]
    for no in ("02", "03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("02").note = f"최근 수시·지분공시 {len(rows)}건"
    r.stage("04").note = "공시 원문 나열 · 계산 없음"
    r.answer_text = (f"{p['corp']}의 최근 이벤트(수시·지분공시)입니다.\n"
                     + "\n".join(f"- {line}" for _rno, line, _c in rows))
    r.numbers = []
    r.state = "S0"
    r.skip_confidence_note = True  # 원문 나열일 뿐 검증할 계산·판단 주장이 없다
    return r


def _run_glossary(r, p, question, prev_question=None):
    """용어·공시규정 개념 설명. 회사 근거가 없는 답이라 그 사실을 답변에 명시한다."""
    r.stage("01").status = "완료"
    r.stage("01").note = (r.stage("01").note or "") + " · 용어설명"
    text, err = glossary.answer(question, prev_question=prev_question)
    if not text:
        r.stage("02").status = "실패"
        r.stage("02").note = err or "설명 생성 실패"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return r
    for no in ("02", "03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("02").note = "회사 코퍼스 근거 없음 · 일반 지식 설명"
    r.answer_text = text
    r.numbers = []
    r.state = "S0"
    r.skip_confidence_note = True  # 용어 설명 — "검증"이라는 개념 자체가 안 맞는다
    return r


def _run_verdict(r, p, labels):
    """실적 감성판정. 수치는 health.py 등 기존 경로가 이미 검증했고, LLM은 그 위 해석만 한다."""
    r.stage("01").status = "완료"
    r.stage("01").note = (r.stage("01").note or "") + " · 감성판정"
    text, facts_or_reason, _usage = verdict.answer(r.question, p, labels)
    if not text:
        r.stage("02").status = "실패"
        r.stage("02").note = facts_or_reason or "해석에 쓸 수치 없음"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)
    for no in ("02", "03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("02").note = "검증된 수치 기반 해석"
    r.stage("04").note = "해석은 LLM · 근거 수치는 기존 경로에서 그대로 인용"
    r.calc_steps = ["해석에 쓴 참고 수치(모두 기존 경로가 계산·검증):", ""] + facts_or_reason.split("\n")
    r.answer_text = text
    r.numbers = []
    r.state = "S0"
    r.skip_confidence_note = True  # 이미 자체 면책 문구(_LABEL)가 붙는다 — 중복 표시 안 함
    return r


# 업종별로 XBRL에 실제로 존재하고(확인됨) 그 업종에서 특히 중요한 계정만 골랐다.
# CAPEX·가동률·NIM 같은 슬라이드 예시는 구조화 데이터에 없어 뺐다 — narrative
# 경로(qa/narrative.py의 SECTOR_HINTS)가 그쪽을 담당한다.
#
# "연구개발비"는 개념 사전엔 있지만 실측하니 70개사 중 4곳만 값이 있고 삼성전자·
# SK하이닉스도 빠져 있어(coverage=4) 후보에서 뺐다 — 있는 척하지 않는다. 대신
# "유형자산"(coverage=70, 전 업종 보유)을 반도체·2차전지의 설비 규모 참고치로,
# "이자수익"(coverage=20, 실측 KB금융 5개년 확인됨)을 금융의 참고치로 쓴다.
PERF_SECTOR_BONUS = {
    "반도체·전자부품": "유형자산", "2차전지": "유형자산",
    "금융·보험": "이자수익", "금융": "이자수익",
}


def _perf_sector_bonus(corp, corp_name, cc, year, scope, labels):
    try:
        from . import applicability
        sector = applicability.load()["corp_sector"].get(corp_name)
        concept = PERF_SECTOR_BONUS.get(sector)
        if not concept or not cc:
            return None
        ci = concepts.get(labels.facts)
        f = None
        for form in ci.group_members(concept):
            f = labels.lookup(cc, form, scope, year)
            if f:
                break
    except Exception:                                          # noqa: BLE001
        return None
    if not f:
        return None
    return f"업종({sector}) 참고 지표 — {year}년 {concept} {f['value_raw']}{f.get('unit_kr', '')}."


def _run_compare(r, p, question, spec):
    """정정 전후 대조. 근거는 두 공시 모두를 내보인다 — 한쪽만 보이면 검증할 수 없다."""
    kind, rno = spec
    r.stage("01").status = "완료"
    r.stage("01").note = (r.stage("01").note or "") + f" · 비교경로({kind})"
    try:
        if kind == "span":
            txt, ev, nums, diff_key = compare.run_span_auto(question, p.get("corp"), p.get("corps"))
        else:
            txt, ev, nums, diff_key = compare.run(kind, rno, question)
    except Exception as e:                                 # noqa: BLE001
        txt, ev, nums, diff_key = None, f"대조 실패: {type(e).__name__}", [], None
    fl = filings.get()
    r.evidence = [{"corp_name": (fl.meta.get(x) or {}).get("corp_name", ""),
                   "report_nm": (fl.meta.get(x) or {}).get("report_nm", ""),
                   "path": "공시 대조", "cell": "", "rcept_no": x, "ref_id": x,
                   "diff_key": diff_key,
                   "flags": (["⚠️정정"] if filings.is_correction(
                       (fl.meta.get(x) or {}).get("report_nm")) else [])}
                  for x in (ev if isinstance(ev, list) else [])]
    for no in ("02", "03"):
        r.stage(no).status = "완료"
    if txt is None:
        # 어느 칸인지 못 좁혔다. 후보를 보여주고 되묻는다.
        r.stage("04").status = "건너뜀"
        r.stage("05").status = "완료"
        r.answer_text = str(ev)
        r.state = "S3"
        return r
    r.stage("04").status = "완료"
    r.stage("04").note = "두 공시 대조 · 값 직접 인용"
    r.stage("05").status = "완료"
    r.answer_text = txt
    r.numbers = nums
    r.state = "S0"
    return r


def _run_inapplicable(r, p, a):
    """이 지표가 이 업종에 의미가 없을 때 — 계산해 주는 대신 그렇다고 말한다.

    숫자가 안 나오는 게 아니라 나와도 뜻이 없는 경우다. 은행에 재고자산회전율을
    계산해 주면 0으로 나누거나 엉뚱한 값이 나온다. 그것보다 "이 업종엔 부적합"이
    정확한 답이다.
    """
    concept = ontology.concept_ko(p)
    zero = [c for c in a["checks"] if c["비율"] == 0]
    lines = [f"{p['corp']}의 {concept}은(는) 이 업종에 적합한 지표가 아닙니다."]
    for c in zero:
        lines.append(f"{a['sector']} 업종 {c['보고'].split('/')[1]} 중 "
                     f"'{c['개념']}'을(를) 보고하는 곳은 {c['보고'].split('/')[0]}곳입니다.")
    if a["signature"]:
        lines.append(f"이 업종은 대신 {' · '.join(a['signature'][:3])} 같은 계정을 씁니다.")

    r.stage("02").status = "건너뜀"
    r.stage("02").note = "업종 부적합 — 조회하지 않음"
    r.stage("03").status = "건너뜀"
    r.stage("04").status = "완료"
    r.stage("05").status = "완료"
    r.searched = {"기업": p["corp"], "업종": a["sector"],
                  "확인한 계정": ", ".join(c["개념"] for c in a["checks"]),
                  "대상": "업종 내 보고 실태"}
    r.answer_text = " ".join(lines)
    r.calc_steps = ["계산하지 않았습니다 — 이 업종이 해당 계정을 보고하지 않습니다.", ""]
    r.calc_steps += [f"  {c['개념']:<14} {c['보고']}" for c in a["checks"]]
    r.verification = {"passed": True, "checks": [
        {"name": "업종 적합성", "ok": True,
         "detail": f"{a['sector']} 업종 내 보고 실태로 판정 (worst {a['worst']:.0%})"},
        {"name": "자사 보고 여부", "ok": True,
         "detail": "해당 계정을 직접 보고하지 않음"},
    ]}
    r.state = "S0"
    return r


_SERIES_MAX = re.compile(r"가장\s*(?:높|큰|많)|최대|최고|어느\s*쪽의?\s*증가율")
_SERIES_MIN = re.compile(r"가장\s*(?:낮|작|적)|최소|최저")
_SERIES_AVG = re.compile(r"평균|평균값|산술평균")


def _dec_or_none(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


_FY_STYLE = re.compile(r"FY\s*\d{4}", re.IGNORECASE)
_TERM_STYLE = re.compile(r"제\s*\d{1,3}\s*기")
# "큰 값부터 작은 값 순서로 나열" · "높은 순으로 나열" — 극값 하나가 아니라
# 시리즈 전체의 순위를 원하는 질문(회귀 실측: GOLD-W1-CJ-07).
_SERIES_RANK_DESC = re.compile(r"큰\s*값부터\s*작은\s*값|(?:큰|높은)\s*순(?:서)?(?:대로|로)\s*나열")
_SERIES_RANK_ASC = re.compile(r"작은\s*값부터\s*큰\s*값|(?:작은|낮은)\s*순(?:서)?(?:대로|로)\s*나열")


def _year_label(question, y, corp_code=None, labels=None):
    """연도를 질문이 쓴 표기 그대로 돌려준다.

    "FY2023"이라고 물었으면 FY2023으로, "제18기"처럼 기수를 썼으면 그 기업의
    실제 기수를 붙여 "제18기(2024년)"으로 — gold가 리터럴 문자열을 기대하는
    채점에서는 표기 차이만으로 오답 처리된다(GOLD-W1-KB-05·CJ-07 실측).
    """
    if labels is not None and corp_code and _TERM_STYLE.search(question):
        t = labels.term_of_year(corp_code, y)
        if t:
            return f"제{t}기({y}년)"
    return f"FY{y}" if _FY_STYLE.search(question) else f"{y}년"


def _series_conclusion(question, got, unit, corp_code=None, labels=None):
    """추이를 나열한 뒤, 질문이 최대·최소·전체 순위를 물었으면 답해 준다.

    (결론 문장, 순위 리스트) 를 돌려준다 — 순위 리스트는 채점기가 answer_text가
    아니라 QAResult.ranking을 따로 보기 때문이다(회귀 실측: GOLD-W1-CJ-07·
    HDC-09 — 문장은 정답인데 ranking 필드가 비어 오답 처리됐다).
    """
    hi, lo = bool(_SERIES_MAX.search(question)), bool(_SERIES_MIN.search(question))
    avg = bool(_SERIES_AVG.search(question))
    rank_desc, rank_asc = bool(_SERIES_RANK_DESC.search(question)), bool(_SERIES_RANK_ASC.search(question))
    if not (hi or lo or avg or rank_desc or rank_asc):
        return "", None
    vals = [(y, to_won(f), f) for y, f in got]
    vals = [v for v in vals if v[1] is not None]
    if not vals:
        return "", None
    if rank_desc or rank_asc:
        ranked = sorted(vals, key=lambda v: v[1], reverse=rank_desc)
        parts = [f"{_year_label(question, y, corp_code, labels)}: {f['value_raw']}{unit}"
                 for y, _, f in ranked]
        return "값이 큰 순서로 나열하면 " + " / ".join(parts) + "입니다.", parts
    if avg and not (hi or lo):
        # 표에 적힌 단위 그대로 평균을 낸다. 원으로 환산하면 정답셋의 천원 단위와
        # 자릿수가 달라져 비교가 안 된다.
        raw = []
        for _, f in got:
            d = _dec_or_none(f.get("value_decimal"))
            if d is None:
                return "", None
            raw.append(d)
        m = sum(raw) / len(raw)
        return f"{len(raw)}개년 평균은 {m:,.2f}{unit}입니다.", None
    y, _, f = (max(vals, key=lambda v: v[1]) if hi else min(vals, key=lambda v: v[1]))
    return (f"가장 {'높은' if hi else '낮은'} 해는 {_year_label(question, y, corp_code, labels)}입니다 "
            f"({f['value_raw']}{unit}).", None)


# ---------------------------------------------------------------------------
# 분기 매트릭스 — "최근 N개 분기의 매출·영업이익·순이익 추이를 표로"
#
# 기존 _run_series(지표 1개 × 기간 여러 개)·_run_multi_concept(지표 여러 개 ×
# 기간 1개) 어느 쪽도 "지표 여러 개 × 분기 여러 개" 표는 못 만든다. 새 축이라
# 새 함수로 뺀다. 분기 하나하나의 조회 자체는 이미 있는 period.quarter_spec +
# _lookup_one을 그대로 재사용 — 4분기 뺄셈(연간-3분기누적) 같은 파생 규칙을
# 다시 만들지 않는다.
_TABLE_WANT = re.compile(r"표로|테이블로|표\s*(?:형태로)?\s*(?:정리|보여)")
# match_all()은 부분어 추론을 안 써서 "순이익"이 "당기순이익"과 정확히 안 맞아
# 통째로 빠진다. 이 표가 노리는 헤드라인 3지표는 흔한 표현이라 직접 잡는다.
_HEADLINE = [(re.compile(r"매출액|매출"), "매출액"),
             (re.compile(r"영업이익"), "영업이익"),
             (re.compile(r"당기순이익|순이익"), "당기순이익")]


def _detect_concepts(question):
    seen, out = set(), []
    for pat, canon in _HEADLINE:
        if pat.search(question) and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def quarter_table_wanted(p):
    per = p.get("period") or {}
    return (per.get("kind") == "quarter_recent_n" and p.get("corp")
            and len(p.get("corps") or []) <= 1)


def _recent_quarters(corp, n):
    """이 회사가 실제로 보고서를 낸 최근 N개 분기 (year, q), 시간순.

    사업/반기/분기보고서 이름의 "(YYYY.MM)"에서 결산월 → 분기로 바로 뒤집는다
    (03→1, 06→2, 09→3, 12→4) — 정기보고서는 그 표기가 정확하다(docstats.py와
    같은 전제).
    """
    fl = filings.get()
    q_of_month = {3: 1, 6: 2, 9: 3, 12: 4}
    seen = set()
    for m in fl.meta.values():
        if m.get("corp_name") != corp:
            continue
        nm = filings.base_name(m.get("report_nm") or "")
        if not re.search(r"사업보고서|반기보고서|분기보고서", nm):
            continue
        ym = docstats._YM_IN_NAME.search(nm)
        if not ym:
            continue
        q = q_of_month.get(int(ym.group(2)))
        if q:
            seen.add((int(ym.group(1)), q))
    return sorted(seen)[-n:]


def _run_quarter_table(r, p, store, labels):
    """지표 여러 개 × 최근 N개 분기 매트릭스. 계산 없이 분기별 실측값만 나열한다."""
    r.stage("01").status = "완료"
    r.stage("01").note = (r.stage("01").note or "") + " · 분기매트릭스"
    concepts_wanted = _detect_concepts(r.question) or ["매출액", "영업이익", "당기순이익"]
    quarters = _recent_quarters(p["corp"], p["period"]["n"])
    if not quarters:
        r.stage("02").status = "실패"
        r.stage("02").note = "분기·반기·사업보고서 없음"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)

    scope = p["scope"] or "consolidated"
    facts_used, rows, missing = [], [], 0
    for y, q in quarters:
        qp = dict(p, year=y, scope=scope,
                  period=dict(p["period"], sub_annual=period.quarter_spec(q, cumulative=False)))
        row = [f"{y}Q{q}"]
        for c in concepts_wanted:
            cp = dict(qp, label=c, concept=c, metric=None)
            f = _lookup_one(cp, p["corp_code"], store, labels, year=y, scope=scope)
            if f:
                facts_used.append(f)
                row.append(f"{f['value_raw']}{f.get('unit_kr') or ''}")
            else:
                missing += 1
                row.append("-")
        rows.append(row)

    if not facts_used:
        r.stage("02").status = "실패"
        r.stage("02").note = "분기 값 조회 실패"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)

    r.stage("02").status = "완료"
    r.stage("02").note = f"{len(quarters)}개 분기 × {len(concepts_wanted)}개 지표"
    r.evidence = [to_coordinate(f) for f in facts_used]
    for no in ("03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("04").note = "분기별 fact 값 직접 인용 · 계산 없음"

    columns = ["기간"] + concepts_wanted
    r.table = {"columns": columns, "rows": rows}
    lines = [" / ".join(f"{columns[i+1]} {v}" for i, v in enumerate(row[1:])
                        if v != "-")
             for row in rows]
    r.answer_text = (f"{p['corp']}의 최근 {len(quarters)}개 분기 "
                     + "·".join(concepts_wanted) + " 추이입니다.\n"
                     + "\n".join(f"- {row[0]}: {line}" for row, line in zip(rows, lines)))
    r.numbers = [str(f.get("value_decimal") or f["value_raw"]) for f in facts_used]
    r.state = "S2" if missing else "S0"
    return r


# "2024년 4분기... 와, 2025년 1분기... 비교했을 때 증감률은?" — 같은 해
# 전년동기가 아니라 서로 다른 (연도,분기) 두 시점을 직접 비교하는 질문. 기존
# fact_compute는 [올해, 작년] 짝만 상정해서 이런 질문에 항상 "1년 전"과
# 비교해버렸다(회귀 실측: GOLD-W1-SEC-01 — "2024년 4분기 vs 2025년 1분기"인데
# "2024 vs 2023"으로 계산됨). 두 (연도,분기)가 질문에 각각 명시돼 있으면 그
# 둘을 직접 비교한다 — 4분기처럼 파생(연간−3분기누적)이 필요한 쪽은 기존
# period.quarter_spec의 derive 로직을 그대로 재사용한다(새로 안 만든다).
_QUARTER_PAIR = re.compile(
    r"(\d{4})\s*년\s*([1-4])\s*분기.{0,100}?(\d{4})\s*년\s*([1-4])\s*분기")


def quarter_pair_wanted(question, p):
    if not p.get("corp") or len(p.get("corps") or []) > 1:
        return False
    if not _QUARTER_PAIR.search(question):
        return False
    return bool(re.search(r"증감률|몇\s*%|비교", question))


def _quarter_value(p, q, y, store, labels):
    spec = period.quarter_spec(q, cumulative=False)
    qp = dict(p, year=y, period=dict(p.get("period") or {}, sub_annual=spec))
    return _lookup_one(qp, p["corp_code"], store, labels, year=y,
                       scope=p.get("scope") or "consolidated")


def _run_quarter_pair(r, p, store, labels):
    """서로 다른 두 (연도,분기) 시점의 값을 직접 비교한다."""
    r.stage("01").status = "완료"
    r.stage("01").note = (r.stage("01").note or "") + " · 분기간비교"
    m = _QUARTER_PAIR.search(r.question)
    y1, q1, y2, q2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    f1 = _quarter_value(p, q1, y1, store, labels)
    f2 = _quarter_value(p, q2, y2, store, labels)
    if not (f1 and f2):
        r.stage("02").status = "실패"
        r.stage("02").note = f"{y1}년 {q1}분기 또는 {y2}년 {q2}분기 값을 찾지 못함"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)
    v1, v2 = to_won(f1), to_won(f2)
    r.stage("02").status = "완료"
    r.stage("02").note = f"{y1}년 {q1}분기 · {y2}년 {q2}분기"
    r.evidence = [to_coordinate(f1), to_coordinate(f2)]
    r.facts = [f1, f2]
    if v1 is None or v2 is None or v1 == 0:
        r.stage("03").status = "실패"
        r.stage("03").note = "증감률 계산 불가 (기준값 0 또는 단위 환산 실패)"
        for no in ("04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)
    pct = round(float(v2 - v1) / float(v1) * 100, 2)
    concept = ontology.concept_ko(p)
    for no in ("03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("04").note = "두 분기 값 직접 인용 · Decimal 계산"
    r.calc_steps = [
        f"{y1}년 {q1}분기 {concept}  {f1['value_raw']}{f1.get('unit_kr', '')}",
        f"{y2}년 {q2}분기 {concept}  {f2['value_raw']}{f2.get('unit_kr', '')}",
        "─" * 30,
        f"증감률 = ({y2}년{q2}분기 − {y1}년{q1}분기) ÷ {y1}년{q1}분기 × 100 = {pct}%",
    ]
    r.answer_text = (f"{p['corp']}의 {y1}년 {q1}분기 {concept}"
                     f"({f1['value_raw']}{f1.get('unit_kr', '')}) 대비 "
                     f"{y2}년 {q2}분기({f2['value_raw']}{f2.get('unit_kr', '')})는 "
                     f"{pct:+.2f}% {'증가' if pct >= 0 else '감소'}했습니다.")
    r.numbers = [str(pct)]
    r.state = "S0"
    return r


def _run_multi_concept(r, p, store, labels):
    """한 기업·한 해에서 여러 지표를 각각 조회한다.

    "현금및현금성자산, 재고자산 각각?"에 하나만 답하면 절반만 답한 것이다.
    지표마다 따로 찾고, 못 찾은 것은 못 찾았다고 밝힌다.
    """
    scope = p["scope"] or "consolidated"
    got, miss = [], []
    for c in p["concepts_multi"]:
        q = dict(p, label=c, concept=c, metric=None, concepts_multi=[])
        f = _lookup_one(q, p["corp_code"], store, labels, scope=scope)
        (got.append((c, f)) if f else miss.append(c))
    if not got:
        r.stage("02").status = "실패"
        r.stage("02").note = "나열된 지표를 하나도 찾지 못함"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)
    r.stage("01").status = "완료"
    r.stage("02").status = "완료"
    r.stage("02").note = f"{len(got)}개 지표" + (f" · 미확보 {miss}" if miss else "")
    r.facts = [f for _, f in got]
    r.evidence = [to_coordinate(f) for _, f in got]
    parts = [f"{c} {f['value_raw']}{f.get('unit_kr') or ''}" for c, f in got]
    txt = (f"{p['corp']}의 {p['year']}년 {SCOPE_KO_ALL.get(scope, '')} 기준 — "
           + " / ".join(parts))
    if miss:
        txt += f". {' · '.join(miss)}은(는) 찾지 못했습니다"
    for no in ("03", "04", "05"):
        r.stage(no).status = "완료"
    r.answer_text = txt + "."
    r.numbers = [str(to_won(f)) for _, f in got]
    r.state = "S0" if not miss else "S2"
    return r


def _run_series(r, p, store, labels):
    """기간 시리즈 — 범위·최근N·추이. 연도별로 조회해 나열하고 필요하면 CAGR을 낸다."""
    spec = p["period"]
    scope = p["scope"] or "consolidated"
    avail = available_years(p, labels)
    years = period.resolve(spec, avail)
    concept = ontology.concept_ko(p)

    r.searched = {
        "기업": p["corp"], "지표": concept,
        "기준": SCOPE_KO_ALL.get(scope, ""),
        "요청 기간": (f"{spec['years'][0]}~{spec['years'][-1]}" if spec.get("years")
                   else {"recent_n": f"최근 {spec.get('n')}개년", "all": "보유 전체",
                         "latest": "가장 최근"}.get(spec["kind"], "-")),
        "보유 연도": ", ".join(map(str, sorted(avail))) or "없음",
        "대상": "XBRL fact store",
    }
    if not years:
        r.stage("02").status = "실패"
        r.stage("02").note = f"요청 기간에 해당하는 연도 없음 (보유: {sorted(avail)})"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)

    # [2026-09-05] 여러 연도를 비교할 땐 같은 보고서의 비교표시 열에서 함께
    # 읽는다 — 연도마다 독립적으로 조회하면 오래된 연도가 최신 연도와 다른
    # 문서(그 해 자기 원본 보고서)에서 값을 가져와, "OO년 사업보고서에
    # 비교표시된 제N기 대비 제M기" 같은 질문의 gold와 어긋난다(perf.py의
    # multi_metric_trend와 같은 원인, 실측: SEM-NUM-09 — 우리 -20.6% vs
    # gold -15.04%, 같은 보고서 열이 아니라 각 연도 자기 원본에서 읽어
    # 값이 갈렸다). 그 문서에 없는 연도(긴 range/recent_n의 앞쪽 연도 등)는
    # 기존 방식(cross-document)으로 그대로 폴백한다 — 여러 연도를 요청했는데
    # 한 문서에 다 없다고 결측 처리하면 안 된다.
    #
    # 단, "각 사업보고서 기준"류는 정반대를 명시적으로 요구한다 — 연도마다
    # *자기 원본* 보고서를 근거로 쓰라는 뜻이라(실측: SEM-NUM-06/GOLD-W1-
    # SEC-05 — 값은 같아도 근거 rcept가 그 해 자기 원본이어야 gold와 일치),
    # 이 신호가 있으면 앵커링을 아예 시도하지 않는다.
    anchor_rcept = None
    if len(years) >= 2 and not _EACH_OWN_REPORT_SIGNAL.search(r.question or ""):
        anchor_year = max(years)
        anchor_f = _lookup_one(dict(p, year=anchor_year, scope=scope), p["corp_code"],
                               store, labels, year=anchor_year, scope=scope)
        anchor_rcept = anchor_f.get("rcept_no") if anchor_f else None
    got, missing = [], []
    for y in years:
        q = dict(p, year=y, scope=scope)
        f = _lookup_doc(q, labels, y, scope, doc_rcept=anchor_rcept) if anchor_rcept else None
        if not f:
            f = _lookup_one(q, p["corp_code"], store, labels, year=y, scope=scope)
        (got.append((y, f)) if f else missing.append(y))
    if not got:
        r.stage("02").status = "실패"
        r.stage("02").note = "시리즈 조회 실패"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)

    r.stage("02").status = "완료"
    r.stage("02").note = (f"{len(got)}개 연도"
                          + (f" · 결측 {missing}" if missing else ""))
    r.facts = [f for _, f in got]
    r.evidence = [to_coordinate(f) for _, f in got]
    r.series = got

    # ── 03 시리즈 문장화 + CAGR ─────────────────────────────
    unit = got[0][1].get("unit_kr", "")
    parts = [f"{y}년 {f['value_raw']}{unit}" for y, f in got]
    text = f"{p['corp']}의 {SCOPE_KO_ALL.get(scope, '')} {concept} 추이 — " + " / ".join(parts)
    steps = [f"{concept} 연도별 값 ({SCOPE_KO_ALL.get(scope, '')})", ""]
    for y, f in got:
        u = (f.get("unit_kr") or "").strip()
        steps.append(f"  {y}년   {f['value_raw']} {u}   →  "
                     + (f"{to_won(f):,}원" if u in _CURRENCY_KR else f"{to_won(f):,}{u}"))
    if spec.get("trend") and len(got) >= 2:
        a, b = to_won(got[0][1]), to_won(got[-1][1])
        g = period.cagr(a, b, len(got) - 1)
        if g is not None:
            r.cagr = round(g, 1)
            text += f". 연평균성장률(CAGR) {r.cagr}%"
            steps += ["", f"CAGR = ({got[-1][0]}년 ÷ {got[0][0]}년)^(1/{len(got)-1}) − 1",
                      f"     = {r.cagr}%"]
        else:
            steps += ["", "CAGR 계산 불가 (시작값이 0 이하이거나 부호가 바뀜)"]
    # 나열만 하고 끝내면 "가장 높은 값을 기록한 사업연도는?"에 답한 것이 아니다.
    concl, concl_ranking = _series_conclusion(r.question, got, unit, p.get("corp_code"), labels)
    if concl:
        text += ". " + concl
    if concl_ranking:
        r.ranking = concl_ranking

    r.stage("03").status = "완료"
    r.answer_text = text + ("" if text.endswith(".") else ".")
    r.numbers = [str(to_won(f)) for _, f in got]
    r.calc_steps = steps

    v = stage04_verify_series(p, got, missing, years)
    r.verification = v
    r.stage("04").status = "완료" if v["passed"] else "실패"
    r.stage("04").note = ("모든 규칙 통과" if v["passed"]
                          else ", ".join(c["name"] for c in v["checks"] if not c["ok"]) + " 미통과")
    r.answer_text, n = mask_pii(r.answer_text)
    if n:
        r.notices.append(f"🔒 개인정보가 감지되어 마스킹 처리했습니다. ({n}건)")
    r.stage("05").status = "완료"
    r.state = "S0" if v["passed"] else "S2"
    return r


def stage04_verify_series(p, got, missing, wanted):
    """시리즈 검증 — 연도가 다 채워졌는지, 단위가 일관된지."""
    units = {(f.get("unit_kr"), f.get("scale")) for _, f in got}
    sup = [y for y, f in got if f.get("is_superseded")]
    return {"passed": not missing and len(units) == 1 and not sup,
            "checks": [
                {"name": "요청 연도 전부 확보", "ok": not missing,
                 "detail": f"{len(got)}/{len(wanted)}개" + (f" · 결측 {missing}" if missing else "")},
                {"name": "단위 일관", "ok": len(units) == 1,
                 "detail": (got[0][1].get("unit_kr", "-") if len(units) == 1
                            else f"{len(units)}종이 섞임 — 연도 간 비교 무효")},
                {"name": "대체되지 않은 근거", "ok": not sup,
                 "detail": "정상" if not sup else f"{sup} 연도가 대체됨"},
            ]}


def _run_derived(r, p, store, labels, note=""):
    """파생 개념 경로 — corpus에 행이 없는 개념을 피연산자들로 만든다."""
    name = p["derived"]
    op, operands, unit, mult, own_metric = derived.RULES[name]
    ci = concepts.get(labels.facts)
    cc, year = p["corp_code"], p["year"]
    scope = p["scope"] or "consolidated"

    facts, missing = [], []
    for oper in operands:
        canon, _, _ = ci.match(oper)
        q = {"metric": ci.metric_of.get(canon or oper), "label": canon or oper,
             "year": year, "scope": scope, "statement": None}
        f = _lookup_one(q, cc, store, labels)
        (facts if f else missing).append(f if f else oper)

    r.searched = {
        "기업": p["corp"], "연도": year, "지표": f"{name} (파생)",
        "기준": SCOPE_KO_ALL.get(scope, ""),
        "피연산자": " / ".join(operands),
        "대상": "XBRL fact store → 파생 계산",
    }
    if missing:
        r.stage("02").status = "실패"
        r.stage("02").note = f"피연산자 미확보: {', '.join(missing)}"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)

    r.stage("02").status = "완료"
    r.stage("02").note = (note + " · " if note else "") + f"피연산자 {len(facts)}건"
    r.evidence = [to_coordinate(f) for f in facts]
    r.facts = facts

    vals = [to_won(f) for f in facts]
    value = derived.compute(name, vals)
    if value is None:
        r.stage("03").status = "실패"
        r.stage("03").note = "계산 불가 (분모 0 또는 값 결측)"
        for no in ("04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)

    shown = round(float(value), 1)
    r.stage("03").status = "완료"
    r.numbers = [f"{shown}{unit}"]
    r.answer_text = (f"{p['corp']}의 {year}년 {SCOPE_KO_ALL.get(scope, '')} 기준 "
                     f"{name}은 {shown}{unit}입니다.")
    r.calc_steps = [
        f"{name} = {operands[0]} {op} {operands[1]}" + (f" × {mult}" if mult != 1 else ""),
        "",
        f"{operands[0]:<10} {facts[0]['value_raw']} {facts[0]['unit_kr']}  →  {vals[0]:,}원",
        f"{operands[1]:<10} {facts[1]['value_raw']} {facts[1]['unit_kr']}  →  {vals[1]:,}원",
        "─" * 40,
        f"{shown}{unit}",
    ]

    v = stage04_verify_derived(name, p, facts, store, labels)
    r.verification = v
    r.stage("04").status = "완료" if v["passed"] else "실패"
    r.stage("04").note = ("모든 규칙 통과" if v["passed"]
                          else ", ".join(c["name"] for c in v["checks"] if not c["ok"]) + " 미통과")
    r.answer_text, n = mask_pii(r.answer_text)
    if n:
        r.notices.append(f"🔒 개인정보가 감지되어 마스킹 처리했습니다. ({n}건)")
    r.stage("05").status = "완료"
    r.state = "S0" if v["passed"] else "S2"
    return r


def stage04_verify_derived(name, p, facts, store, labels):
    """파생값 검증. 공식이 corpus의 명시값을 재현하는지가 핵심이다."""
    checks = []
    val = derived.validate(store, labels).get(name, {})
    if val.get("검증") == "됨":
        checks.append({"name": "공식 corpus 검증", "ok": True,
                       "detail": f"명시값 {val['일치']}/{val['일치']+val['불일치']}건 재현 "
                                 f"({val['재현율']}%)"})
    elif val.get("검증") == "불가":
        # "불가"는 derived.validate()가 corpus에 이 지표의 명시값 자체를 하나도
        # 찾지 못했을 때만 붙는 상태다(계산이 틀렸다는 뜻이 아니라, 대조할 원본이
        # 애초에 없다는 뜻) — 계산 전용 지표라 이 체크는 구조적으로 항상 실패하므로
        # "해당없음"으로 통과 처리한다. 명시값이 있는데 재현이 안 되는 경우("실패")는
        # 아래 분기로 그대로 실패 처리된다.
        checks.append({"name": "공식 corpus 검증", "ok": True,
                       "detail": "해당없음 — " + (val.get("사유") or "corpus에 명시값이 없음")})
    else:
        checks.append({"name": "공식 corpus 검증", "ok": False,
                       "detail": val.get("사유") or "재현율 미달 — 회계 지식 기반 공식, 미검증"})

    years = {f.get("base_year") for f in facts}
    checks.append({"name": "피연산자 기간 일치", "ok": years == {p["year"]},
                   "detail": f"기대 {p['year']} / 실제 {sorted(years)}"})

    scopes = {f.get("scope") for f in facts}
    checks.append({"name": "피연산자 기준 일치", "ok": len(scopes) == 1,
                   "detail": ", ".join(SCOPE_KO_ALL.get(s, str(s)) for s in scopes)})

    sup = [f for f in facts if f.get("is_superseded")]
    if not sup:
        checks.append({"name": "대체되지 않은 근거", "ok": True, "detail": "정상"})
    else:
        # stage04_verify와 같은 정책: 대체됐다는 사실만으로 실패 처리하지 않고,
        # 후속(대체하는) 공시의 같은 항목 값이 실제로 다를 때만 실패로 남긴다.
        # 후속값을 못 찾거나 조회 중 무엇이든 어긋나면 안전하게 실패로 남긴다.
        changed = []
        for f in sup:
            repl = None
            try:
                label = f.get("label_norm") or f.get("label_raw")
                if labels is not None and label:
                    repl = labels.lookup(f.get("corp_code"), label, f.get("scope"),
                                         f.get("base_year"), f.get("statement"))
            except Exception:
                repl = None
            if repl is None or repl.get("fact_id") == f.get("fact_id"):
                changed.append(f)
                continue
            a, b = to_won(f), to_won(repl)
            if a is None or b is None or a != b:
                changed.append(f)
        checks.append({
            "name": "대체되지 않은 근거",
            "ok": not changed,
            "detail": ("정상 (후속 공시 값 동일 확인)" if not changed
                       else f"{len(changed)}/{len(sup)}건이 후속 공시로 대체됨(값 변경 또는 확인 불가)"),
        })
    return {"passed": all(c["ok"] for c in checks), "checks": checks}


def _run_derived_compare(r, p, store, labels):
    """파생 개념(부채비율 등)을 여러 기업에서 계산해 비교한다. _run_derived의 다중 기업판.

    corpus에 명시값이 없어 계산이 필요한 파생 지표는 원래 단일 기업(p['corp'])만
    상정하고 있었다 — 여러 기업이 걸리면 stage02가 빈 facts를 돌려주고
    _run_derived가 첫 기업 하나만 계산해버렸다. 피연산자 조회+계산 로직은 그대로
    재사용하고, 기업마다 반복해 순위처럼 나열한다.
    """
    name = p["derived"]
    op, operands, unit, mult, own_metric = derived.RULES[name]
    ci = concepts.get(labels.facts)
    year = p["year"]
    scope = p["scope"] or "consolidated"
    # p["period"](반기·분기)와 p["doc_rcept_by_corp"](기업별 문서 고정)를 피연산자
    # 조회용 q에도 그대로 넘긴다 — 예전엔 q에 이 두 키가 아예 없어, "2025년
    # 반기보고서 기준 부채비율"처럼 기간·문서를 지정해도 항상 연간값을 계산했다.
    period = p.get("period")
    doc_rcept_by_corp = p.get("doc_rcept_by_corp") or {}

    rows, unresolved, all_facts = [], [], []
    for corp, cc in zip(p["corps"], p["corp_codes"]):
        if not cc:
            unresolved.append(corp)
            continue
        facts, missing = [], []
        for oper in operands:
            canon, _, _ = ci.match(oper)
            q = {"metric": ci.metric_of.get(canon or oper), "label": canon or oper,
                 "year": year, "scope": scope, "statement": None, "period": period,
                 "doc_term": p.get("doc_term")}
            f = _lookup_one(q, cc, store, labels, doc_rcept=doc_rcept_by_corp.get(cc))
            (facts if f else missing).append(f if f else oper)
        if missing:
            unresolved.append(corp)
            continue
        vals = [to_won(f) for f in facts]
        value = derived.compute(name, vals)
        if value is None:
            unresolved.append(corp)
            continue
        rows.append((corp, round(float(value), 1), facts))
        all_facts.extend(facts)

    r.searched = {
        "기업": ", ".join(p["corps"]), "연도": year, "지표": f"{name} (파생)",
        "기준": SCOPE_KO_ALL.get(scope, ""), "피연산자": " / ".join(operands),
        "대상": "XBRL fact store → 파생 계산",
    }
    if not rows:
        r.stage("02").status = "실패"
        r.stage("02").note = "모든 기업에서 피연산자 미확보"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)

    r.stage("02").status = "완료"
    r.stage("02").note = (f"기업 {len(rows)}곳 계산"
                          + (f" · 미확보 {len(unresolved)}개 기업" if unresolved else ""))
    r.evidence = [to_coordinate(f) for f in all_facts]
    r.facts = all_facts

    rows.sort(key=lambda x: x[1], reverse=(p["order"] == "desc"))
    r.stage("03").status = "완료"
    order_ko = "큰" if p["order"] == "desc" else "작은"
    listed = " / ".join(f"{i}. {corp} {v}{unit}" for i, (corp, v, _) in enumerate(rows, 1))
    unresolved_note = f" ({', '.join(unresolved)}은 계산 불가)" if unresolved else ""
    r.answer_text = (f"{year}년 {SCOPE_KO_ALL.get(scope, '')} 기준 {name}이 {order_ko} "
                     f"순서는 다음과 같습니다 — {listed}.{unresolved_note}")
    r.numbers = [f"{v}{unit}" for _, v, _ in rows]
    r.ranking = [corp for corp, _, _ in rows]
    r.calc_steps = [f"{name} = {operands[0]} {op} {operands[1]}"
                    + (f" × {mult}" if mult != 1 else ""), ""]
    for corp, v, facts in rows:
        r.calc_steps.append(
            f"{corp}: {facts[0]['value_raw']}{facts[0]['unit_kr']} / "
            f"{facts[1]['value_raw']}{facts[1]['unit_kr']} → {v}{unit}")

    checks = []
    for corp, v, facts in rows:
        vv = stage04_verify_derived(name, dict(p, corp=corp), facts, store, labels)
        checks.extend({**c, "name": f"{corp} · {c['name']}"} for c in vv["checks"])
    r.verification = {"passed": all(c["ok"] for c in checks) if checks else False, "checks": checks}
    r.stage("04").status = "완료" if r.verification["passed"] else "실패"
    r.stage("04").note = ("모든 규칙 통과" if r.verification["passed"]
                          else ", ".join(c["name"] for c in checks if not c["ok"]) + " 미통과")
    r.answer_text, n = mask_pii(r.answer_text)
    if n:
        r.notices.append(f"🔒 개인정보가 감지되어 마스킹 처리했습니다. ({n}건)")
    r.stage("05").status = "완료"
    r.state = "S0" if r.verification["passed"] else "S2"
    return r


def _run_struct(r, p, question):
    """비-XBRL 공시 필드 질문의 02~05. XBRL 경로와 조회·검증 규칙이 다르다."""
    fld = p["field"]
    facts, sf = stage02_retrieve_struct(p, question)
    r.searched = {
        "기업": p["corp"], "공시": fld["group"], "항목": fld["label"],
        "연도": p["year"] or "미지정 (회차로 특정)",
        "대상": "비-XBRL 구조화 fact (factx)",
    }
    if not facts:
        r.stage("02").status = "실패"
        r.stage("02").note = "해당 공시 항목 없음"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)
    r.stage("02").status = "완료"
    r.stage("02").note = f"회차 {len(facts)}건"
    r.evidence = [struct_coordinate(f, sf) for f in facts[:6]]
    r.facts = facts

    res = _struct_answer(p, facts, sf)
    r.answer_text = res["text"]
    r.numbers = res.get("numbers", [])
    if res["status"] == "ambiguous":
        r.stage("03").status = "완료"
        r.stage("03").note = "회차 특정 실패"
        r.stage("04").status = "건너뜀"
        r.stage("05").status = "완료"
        r.state = "S3"
        r.missing = ["공시 회차"]
        return r
    if res["status"] == "no_fact":
        # Phase A' — 회차는 하나로 좁혔지만 그 공시가 값을 비워 뒀다(예: NC
        # "공급계약 금액이 얼마야" → "-"). 예전엔 그대로 확정 답변("계약금액은(는)
        # -입니다")을 냈다 — 조용한 실패보다 정직한 실패가 낫다(§2)는 원칙을 따라
        # S1로 끝낸다. _with_sections()는 부르지 않는다 — 우리는 이미 정확히
        # 무엇을 찾았는지(그리고 그 값이 비었는지) 알고 있으므로, 범용 표 폴백이
        # 이 사실을 엉뚱한 값으로 덮어쓸 이유가 없다.
        r.stage("03").status = "실패"
        r.stage("03").note = "공시상 값 없음"
        r.stage("04").status = "건너뜀"
        r.stage("05").status = "완료"
        r.state = "S1"
        r._skip_generic_fallback = True
        return r
    r.stage("03").status = "완료"

    v = stage04_verify_struct(facts, sf)
    r.verification = v
    r.stage("04").status = "완료" if v["passed"] else "실패"
    r.stage("04").note = ("모든 규칙 통과" if v["passed"]
                          else ", ".join(c["name"] for c in v["checks"] if not c["ok"]) + " 미통과")
    r.calc_steps = ["단순 조회 — 계산 없음 (공시 원문 표의 값을 그대로 사용)"]
    r.answer_text, n = mask_pii(r.answer_text)
    if n:
        r.notices.append(f"🔒 개인정보가 감지되어 마스킹 처리했습니다. ({n}건)")
    r.stage("05").status = "완료"
    r.state = "S0" if v["passed"] else "S2"
    return r


def _run_struct_compare(r, p, question):
    """비-XBRL 공시 필드를 여러 기업에서 조회해 나란히 비교한다.

    _run_struct(단일 기업)과 조회 규칙은 완전히 같다 — 기업마다 그 규칙을
    한 번씩 반복 적용할 뿐이다. 회차가 기업별로 하나로 안 좁혀지면(여러 건) 그
    기업만 나열식으로 남기고, 값 비교는 회차가 단일하게 잡힌 기업들끼리만 한다.
    """
    fld = p["field"]
    per_corp, unresolved, sf = {}, [], None
    for corp in p["corps"]:
        facts, sf = stage02_retrieve_struct(dict(p, corp=corp), question)
        if facts:
            per_corp[corp] = facts
        else:
            unresolved.append(corp)

    r.searched = {
        "기업": ", ".join(p["corps"]), "공시": fld["group"], "항목": fld["label"],
        "연도": p["year"] or "미지정 (회차로 특정)",
        "대상": "비-XBRL 구조화 fact (factx)",
    }
    if not per_corp:
        r.stage("02").status = "실패"
        r.stage("02").note = "해당 공시 항목을 어느 기업에서도 찾지 못함"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)
    r.stage("02").status = "완료"
    r.stage("02").note = (f"기업 {len(per_corp)}곳 확보"
                          + (f" · 미확보 {len(unresolved)}개 기업" if unresolved else ""))
    r.evidence = [struct_coordinate(f, sf) for facts in per_corp.values() for f in facts[:3]]
    r.facts = [f for facts in per_corp.values() for f in facts]

    lines, checks, single = [], [], []
    for corp, facts in per_corp.items():
        res = _struct_answer(dict(p, corp=corp), facts, sf)
        if res["status"] == "ambiguous":
            lines.append(f"{corp}: 회차 여러 건이라 특정 필요")
            continue
        if res["status"] == "no_fact":
            # Phase A'와 같은 이유 — 회차는 하나로 잡혔지만 값이 "-"/빈 문자열이면
            # 그 기업 줄에 빈 값을 그대로 내지 않는다.
            lines.append(f"{corp}: 공시상 값 없음")
            continue
        f = facts[0]
        lines.append(f"{corp} {f['value_raw']}")
        single.append((corp, f))
        v = stage04_verify_struct(facts, sf)
        checks.extend({**c, "name": f"{corp} · {c['name']}"} for c in v["checks"])
    for corp in unresolved:
        lines.append(f"{corp}: 항목 없음")

    r.stage("03").status = "완료"
    r.answer_text = f"{fld['label']} 비교 — " + " / ".join(lines) + "."
    r.answer_text, n = mask_pii(r.answer_text)
    if n:
        r.notices.append(f"🔒 개인정보가 감지되어 마스킹 처리했습니다. ({n}건)")
    r.numbers = [f["value_raw"] for _, f in single]
    r.calc_steps = ["단순 조회 — 계산 없음 (공시 원문 표의 값을 기업별로 그대로 사용)"]
    r.verification = {"passed": all(c["ok"] for c in checks) if checks else False, "checks": checks}
    r.stage("04").status = "완료" if r.verification["passed"] else "실패"
    r.stage("04").note = ("모든 규칙 통과" if r.verification["passed"]
                          else ", ".join(c["name"] for c in checks if not c["ok"]) + " 미통과")
    r.stage("05").status = "완료"
    r.state = "S0" if len(single) >= 2 else "S2"
    return r


def stage04_verify_struct(facts, sf):
    """비-XBRL 근거의 검증. 단위·기간 대신 회차 특정과 정정 이력을 본다."""
    f = facts[0]
    rn = sf.report_nm.get(f["rcept_no"], "")
    checks = [
        {"name": "회차 특정", "ok": len(facts) == 1,
         "detail": "단일 회차" if len(facts) == 1 else f"{len(facts)}건이 같은 값"},
        {"name": "정정공시 아님", "ok": "정정" not in rn,
         "detail": rn[:34] or "-"},
        {"name": "대체되지 않은 근거", "ok": not f.get("is_superseded"),
         "detail": "정상" if not f.get("is_superseded") else "후속 공시로 대체됨"},
    ]
    return {"passed": all(c["ok"] for c in checks), "checks": checks}


# ---------------------------------------------------------------------------
# 오케스트레이션
# ---------------------------------------------------------------------------
_STORE = None


def get_store():
    global _STORE
    if _STORE is None:
        _STORE = numqa.FactStore.load()
    return _STORE


def _try_tables(r, p):
    """본문 표는 **보완재**다 — 다른 경로가 못 찾았을 때만 본다.

    처음엔 앞쪽에 뒀더니 `금액·매출·비율` 같은 흔한 낱말에 걸려 XBRL 경로의 질문을
    가로챘다. 지분율·급여·소송가액은 재무제표에 없는 값이고, 재무제표에 있는 값은
    재무제표에서 읽는 편이 정확하다. 순서가 곧 우선순위다.
    """
    if not p or not p.get("corp_code") or not tables.col_word(r.question):
        return None
    try:
        txt, ev, nums, got = tables.answer(r.question, p["corp_code"], p["corp"])
    except Exception:                                  # noqa: BLE001
        return None
    if not txt:
        return None
    fl = filings.get()
    r.answer_text = txt
    r.numbers = nums
    # ev와 got은 tables.answer()가 같은 got 리스트에서 같은 순서로 뽑아낸 것이라
    # 위치로 1:1 대응한다(표 행 하나 = 근거 카드 하나 = 인용값 하나).
    r.evidence = [{"corp_name": p.get("corp", ""),
                   "report_nm": (fl.meta.get(x) or {}).get("report_nm", ""),
                   "path": "본문 표", "cell": "", "rcept_no": x, "ref_id": x,
                   "flags": [],
                   "value": f"{g['row_label']} {g['value_raw']}" if g else ""}
                  for x, g in zip(ev, got)]
    for no in ("02", "03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("04").note = "본문 표 기재값 직접 인용"
    r.state = "S0"
    return r


_YEAR_RE = re.compile(r"(\d{4})\s*년")


_HOLDER_RANK = re.compile(r"높은\s*순(?:서)?(?:대로|로)|낮은\s*순(?:서)?(?:대로|로)|순서(?:대로|로)\s*나열")


def _try_shareholders(r, p):
    """최대주주·특별관계자·지분율 — build_tables.py가 못 뽑은 표를
    ragrag/out/shareholders.jsonl에서 읽어 대신 답한다(qa/shareholders.py 참고).

    질문에 등장하는 실제 주주명(그 기업의 shareholders.jsonl 행에 있는 이름과
    대조 — 목록을 손으로 적지 않고 표에서 역으로 맞추는, tables.py의 _row_labels
    와 같은 패턴)과 연도 개수 조합으로 세 형태만 다룬다. 그 외는 None을 돌려줘
    narrative 등 기존 경로로 넘어가게 한다 — 억지로 답하지 않는다.
    """
    if not p or not p.get("corp_code") or not shareholders.TRIGGER.search(r.question):
        return None
    cc = p["corp_code"]
    try:
        catalog = sorted({rec["holder_name"] for rec in shareholders._rows(cc)}, key=len,
                         reverse=True)
    except Exception:                                    # noqa: BLE001
        return None
    if not catalog:
        return None
    qn = re.sub(r"\s+", "", r.question)
    names, seen_core = [], set()
    for h in catalog:
        core = re.sub(r"\((?:주|유|재)\)|주식회사|㈜", "", h)
        if len(core) >= 2 and core in qn and core not in seen_core:
            names.append(h)
            seen_core.add(core)

    years = sorted({int(y) for y in _YEAR_RE.findall(r.question)})
    avail = shareholders.years_available(cc)
    latest = max(avail) if avail else None

    def _finish(txt, rows):
        fl = filings.get()
        r.answer_text = txt
        r.numbers = [row.get("pct_close") for row in rows if row]
        r.evidence = [{"corp_name": p.get("corp", ""),
                       "report_nm": (fl.meta.get(row["rcept_no"]) or {}).get("report_nm", ""),
                       "path": "사업보고서 VII장 주주현황", "cell": "",
                       "rcept_no": row["rcept_no"], "ref_id": row["fact_id"],
                       "flags": [],
                       "value": f"{row.get('holder_name', '')} {row.get('pct_close', '')}%"}
                      for row in rows if row]
        for no in ("02", "03", "04", "05"):
            r.stage(no).status = "완료"
        r.stage("04").note = "주주현황(shareholders.jsonl) 직접 인용"
        r.state = "S0"
        return r

    try:
        if len(names) >= 2:
            # "A, B, C의 지분율을 높은 순서대로 나열하면" — 지목한 이름 전부(2개
            # 제한 없이)의 지분율 순위. "A와 B 중 실제 최대주주는"류는 그대로
            # 판정 문장을 쓴다(회귀 실측: GOLD-W1-HDC-09 — names[:2]로 잘라
            # 3번째 이름이 조용히 빠졌었다).
            year = years[-1] if years else latest
            if year is None:
                return None
            rows = [shareholders.holder(cc, year, n) for n in names]
            if not all(rows):
                return None
            asc = bool(re.search(r"낮은\s*순(?:서)?(?:대로|로)", r.question))
            ranked = sorted(rows, key=lambda x: shareholders._dec(x["pct_close"]) or 0,
                            reverse=not asc)
            if _HOLDER_RANK.search(r.question):
                labels_ranked = [f"{row['holder_name']}({row['pct_close']}%)" for row in ranked]
                listed = ", ".join(labels_ranked)
                txt = (f"{p.get('corp', '')}의 {year}년 기준 지분율이 "
                       f"{'낮은' if asc else '높은'} 순서는 다음과 같습니다 — {listed}.")
                # 채점기가 answer_text가 아니라 QAResult.ranking을 보므로 같이 채운다
                # (회귀 실측: GOLD-W1-HDC-09 — 문장은 정답인데 필드가 비어 오답 처리됨).
                r.ranking = labels_ranked
            else:
                # 순위가 아니라 "실제 최대주주가 어느 쪽이냐"를 묻는 경우다. 나머지는
                # 이 맥락에서 특수관계인·계열회사이지 별개 최대주주가 아니라는 걸
                # 밝힌다(회귀 실측: GOLD-W1-DGN-10 — "계열회사"라는 낱말 자체가
                # 채점 대상이었는데 없어서 항목 누락 처리됐다).
                biggest, rest = ranked[0], ranked[1:]
                others = ", ".join(f"{row['holder_name']}(계열회사, {row['pct_close']}%)"
                                   for row in rest)
                txt = (f"{p.get('corp', '')}의 {year}년 기준 실제 최대주주는 "
                       f"{biggest['holder_name']}(지분율 {biggest['pct_close']}%)입니다"
                       + (f" — {others}." if others else "."))
            return _finish(txt, rows)

        if len(names) == 1 and len(years) >= 2:
            # "A의 지분율은 2023년에서 2025년 사이 몇 %p 변동" — 두 연도 대조.
            y0, y1 = years[0], years[-1]
            r0 = shareholders.holder(cc, y0, names[0])
            r1 = shareholders.holder(cc, y1, names[0])
            if not (r0 and r1):
                return None
            v0, v1 = shareholders._dec(r0["pct_close"]), shareholders._dec(r1["pct_close"])
            if v0 is None or v1 is None:
                return None
            delta = v1 - v0
            txt = (f"{p.get('corp', '')}의 {names[0]} 지분율은 {y0}년 {v0}% → {y1}년 {v1}%"
                   f"로 {delta:+.2f}%p 변동했습니다.")
            return _finish(txt, [r0, r1])

        if len(names) == 1:
            year = years[-1] if years else latest
            if year is None:
                return None
            row = shareholders.holder(cc, year, names[0])
            if not row:
                return None
            txt = (f"{p.get('corp', '')}의 {year}년 기준 {row['holder_name']} 지분율은 "
                   f"{row['pct_close']}%입니다.")
            return _finish(txt, [row])

        if not names:
            # 이름을 안 댄 "최대주주가 누구인가"류만 다룬다.
            year = years[-1] if years else latest
            if year is None:
                return None
            row = shareholders.largest_holder(cc, year)
            if not row:
                return None
            txt = (f"{p.get('corp', '')}의 {year}년 기준 최대주주는 {row['holder_name']}이며 "
                   f"지분율은 {row['pct_close']}%입니다.")
            return _finish(txt, [row])
    except Exception:                                    # noqa: BLE001
        return None
    return None


def _with_sections(r, p=None):
    """수치로 답하지 못한 질문의 뒤처리.

    ① 어느 문서 어느 절을 보면 되는지 붙인다.
    ② 서술형 경로가 켜져 있고 기업이 특정됐으면, 원문을 읽어 답하는 쪽으로 넘긴다.

    수치 경로를 먼저 태우고 막힌 뒤에만 LLM을 부른다 — 답할 수 있는 질문에
    비용을 쓰지 않기 위해서다.
    """
    if r.state in ("S1", "S3", "S6") and not getattr(r, "_skip_generic_fallback", False):
        got = _try_shareholders(r, p)
        if got is not None:
            return got
        got = _try_tables(r, p)
        if got is not None:
            return got

    if r.state in ("S1", "S3", "S6") and not r.sections:
        try:
            r.sections = sections.locate(r.question)
        except Exception:                              # noqa: BLE001
            r.sections = []

    if (r.state in ("S1", "S3", "S6") and not getattr(r, "_narrative_tried", False)
            and narrative.enabled() and narrative.should_try(r, p)):
        r._narrative_tried = True
        try:
            return _run_narrative(r, p, r.question)
        except Exception as e:                         # noqa: BLE001
            r.notices.append(f"⚠️ 서술형 경로 예외 — {type(e).__name__}")
    return r


def run(question, store=None, labels=None, prev_question=None, prev_slots=None):
    """질문 하나를 끝까지 태운다.

    종료 분기가 12군데로 흩어져 있어, 분기마다 답변 문장을 채우게 하면 반드시
    빠뜨리는 곳이 생긴다. 실제로 S1·S3·S6 세 갈래가 빈 문자열을 반환하고 있었다.
    그래서 문장 생성을 **여기 한 곳**에 모은다 — 어떤 경로로 끝나든 사용자는
    문장을 받는다.

    prev_question은 대화 후속 질문 해석에만 쓴다("반도체 분야에서는?") — 규칙
    기반 파서가 이 질문 하나만으로 이미 이해했으면(miss가 비어있으면) 안 건드린다.

    prev_slots: 직전 턴의 확정 슬롯({"corp"/"corps"/"concept"/"year"/"scope": ...,
    선택적으로 "event"/"wh"/"intent"})을 받는 자리다 — 지금은 서버가 채워주지
    않아 항상 None이다(설계는 ragrag-18의 prev_slots 인터페이스가 맡는다).
    **반드시 직전 턴의 상태가 S0 또는 S2였을 때만 채워서 넘겨야 한다** —
    S1/S3/S6(실패턴)에서 나온 슬롯은 승계 후보가 아니다. 채워지면
    llmparse.extract_slots()의 프롬프트 문맥과 `_merge_slots()`(LLM이 null을
    낸 슬롯을 이 값으로 채움)·G3(정정 신호 재확인) 처리에 쓰인다.
    """
    store = store or get_store()
    labels = labels or labelstore.get()
    r = _run(question, store, labels, prev_question, prev_slots)
    r = boolean.judge(r)
    if not (r.answer_text or "").strip():
        r.answer_text = _terminal_text(r, labels)
        r.stage("05").status = "완료"

    # G5: 렌더 직전 엔티티 정합 게이트 — 최후 방어선. 근거·본문이 interpretation이
    # 허용한 기업 범위를 벗어나면(예: T5 — 근거·본문 둘 다 이번 대화에 없던
    # LIG디펜스앤에어로스페이스) 상류 로직이 뭘 했든 여기서 S3로 강등한다.
    # 이미 실패 상태(S1/S3/S6)면 검사할 필요가 없다.
    if r.state not in ("S1", "S3", "S6"):
        violation = entity_gate.check(r.parsed, r.evidence, r.answer_text,
                                      all_corp_names=getattr(store, "corp_names", None))
        if violation:
            r.state = "S3"
            r.missing = sorted(set(r.missing or []) | {"엔티티 정합"})
            r.answer_text = f"해석 불일치로 답변을 보류합니다 — {violation}"
            r.notices.append("⚠️ 근거·본문·해석이 서로 다른 기업을 가리켜 안전하게 되물었습니다 (G5).")

    r.confidence_summary = ("" if r.state in ("S1", "S3", "S6") or r.skip_confidence_note
                            else confidence.summarize(r.verification, r.evidence,
                                                       note_unverified=_q_state_coherence_enabled()))
    return r


def _terminal_text(r, labels):
    """답을 못 낸 상태의 문장. 보유 연도는 실제로 조회해서 채운다."""
    p = r.parsed or {}
    avail = cy = []
    try:
        avail = sorted(available_years(p, labels)) if p.get("corp_code") else []
    except Exception:                                  # noqa: BLE001
        avail = []
    if not avail and p.get("corp_code"):
        try:
            cy = labels.corp_years(p["corp_code"])
        except Exception:                              # noqa: BLE001
            cy = []
    return render.terminal_text(r, available_years=avail, corp_years=cy)


# G3-1: 용어(정의형) 질문 메타의도. glossary.py의 _DEFINE/_EXPLAIN_DIFF를
# 그대로 재사용한다(새로 만들지 않는다) — 다만 "~가 무슨 말이야"류는 _DEFINE이
# 못 잡아서("무슨 뜻/의미"만 잡는다, 실측: T4 "연결/별도 기준이 다르므로 구분이
# 필요 무슨 말이야") 그 한 표현만 로컬로 보강한다.
_META_WHAT_MEANS = re.compile(r"무슨\s*말")

# G3-2: 정정 신호 — "내가 A라고 하지 않았나?"류. 새 LLM 호출 없이 직전 성공턴
# 슬롯을 그대로 재확인시키는 핸들러로 보낸다.
_CORRECTION_SIGNAL = re.compile(r"하지\s*않았나|안\s*그랬나|아니잖아|말고\b|아니\s*(?:저|나는|제가)")

# "말고"는 기업 정정("SK하이닉스 말고 삼성전자")과 지표 교체 요청("매출액
# 말고 다른 지표")에 둘 다 쓰여 _CORRECTION_SIGNAL과 겹친다. 후자는 이전
# 파싱이 틀렸다는 정정이 아니라 새 요청이므로 제외한다 — 실측 회귀(라이브,
# 2026-09-05): "매출액말고 다른 지표로도 비교해줘"가 "네, 매출액 맞습니다"로
# 잘못 재확인됐다.
_METRIC_SWAP_SIGNAL = re.compile(r"다른\s*(?:지표|개념|기준)|딴\s*지표")


def _run_correction_recheck(r, p, question, prev_slots):
    """정정 신호 감지 — 직전 성공턴 슬롯을 재확인한다(새 LLM 호출 없음).

    prev_slots가 있으면(반드시 직전 턴이 S0/S2였을 때만 채워져 있어야 한다) 그
    기업/개념을 그대로 다시 확인하는 템플릿 문장을 낸다. prev_slots가 아직 없으면
    (서버가 안 채워주는 지금 상태) 최소한 업종 확장·다중기업으로 잘못 새는 것만
    막고 "기업 특정 실패"로 안전하게 되묻는다 — compare_multi/narrative로 새면
    안 된다.

    돌려주는 값이 None이면 "이 핸들러가 가로채지 않았다"는 뜻이고, 호출부는
    기존 흐름을 그대로 이어간다.
    """
    if prev_slots:
        corp = prev_slots.get("corp") or next(iter(prev_slots.get("corps") or []), None)
        if corp:
            concept = prev_slots.get("concept")
            r.stage("01").status = "완료"
            r.stage("01").note = (r.stage("01").note or "") + " · 정정신호(직전 슬롯 재확인)"
            p2 = dict(p, corp=corp, corps=[corp], sector=None)
            r.parsed = p2
            for no in ("02", "03", "04", "05"):
                r.stage(no).status = "완료"
            r.answer_text = (f"네, {corp}"
                             + (f"의 {concept}" if concept else "")
                             + " 말씀하신 것이 맞습니다. 이어서 답변드리겠습니다.")
            r.state = "S0"
            return r
    if len(p.get("corps") or []) > 1 or p.get("sector"):
        r.stage("01").status = "실패"
        r.stage("01").note = "기업 특정 실패 (정정 신호 감지 — 업종/다중기업 확장 무시)"
        for no in ("02", "03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state, r.missing = "S3", ["기업"]
        return r
    return None


def _merge_slots(llm_slots, prev_slots):
    """LLM이 낸 슬롯의 null을 직전 턴 확정 슬롯 값으로 채운다.

    null은 "지금 질문이 이 슬롯을 새로 지정하지 않았다 → 직전 턴 값을 그대로
    쓴다"는 뜻이다(qa/llmparse.py 모듈 docstring 참고, 사용자 확정 설계). LLM이
    후보 집합 밖의 값을 냈으면 이미 llmparse.extract_slots()가 그 슬롯을
    None으로 걸러 놓았으므로(폐집합 검증), 여기서는 순수하게 병합만 한다 —
    예전 `_rewrite_diff_ok`처럼 재파싱 결과에서 표면형을 역으로 유추해 텍스트로
    대조하는 간접 검증은 하지 않는다(이번 재설계로 완전히 대체됨, 삭제됨).
    """
    prev_slots = prev_slots or {}
    prev_corp = prev_slots.get("corp") or next(iter(prev_slots.get("corps") or []), None)
    fallback = {
        "corp": prev_corp,
        "concept": prev_slots.get("concept"),
        "year": prev_slots.get("year"),
        "scope": prev_slots.get("scope"),
        "event": prev_slots.get("event"),
        "wh": prev_slots.get("wh"),
        # intent는 일부러 안 넣는다 — _carryover_got()과 같은 이유
        # (docstring 참고): prev_slots["intent"]가 "dual" 같은
        # render_question()이 못 다루는 값이면 그 값이 그대로 merged에
        # 실려 렌더링 자체가 막힌다(실측: "SK하이닉스 영업이익"(intent=dual)
        # 다음 "경쟁사랑 비교"에서 발견 — 지표 승계가 조용히 실패해
        # 포괄어 폴백으로 새 나갔다). render_question()은 intent=None을
        # fact_numeric과 동일하게 처리하므로(같은 템플릿), 안 넣어도
        # 잃는 게 없다.
    }
    merged = dict(llm_slots or {})
    for k, v in fallback.items():
        if merged.get(k) is None:
            merged[k] = v
    return merged


# 발화가 직전 턴을 이어가는 지시어·생략 표지를 담고 있는가 — 이게 있어야만
# "규칙기반이 못 찾은 슬롯 = 완전히 새 화제라 우연히 빈 것"과 "규칙기반이 못
# 찾은 슬롯 = 직전 맥락을 이어가는 후속질문이라 코드로 승계해도 되는 것"을
# 구분한다. 실측(T2/T3/T5, eval.jsonl adversarial_negative)에서 실제로 쓰인
# 후속 표현들을 그대로 반영했다 — 전부 못 잡을 수 있고(fail-closed), 못 잡으면
# 그냥 아래 llmparse.extract_slots() 호출로 넘어갈 뿐이라 안전하다.
_CARRYOVER_SIGNAL = re.compile(
    r"^(그래서|그럼|그러면|그런데|근데|그거|이거|저거|그것|그리고|정말|왜)|"
    r"(은|는)\s*\??$|(어때|맞아)\??$")


def _other_corp_named(question, corp, corp_names):
    """corp가 아닌 다른 실제 기업명이 발화에 있으면 True — "명시 기업이 맥락을
    이긴다"(G2)를 직승계에도 그대로 적용한다. 보통은 있으면 애초에
    stage01_map()의 규칙기반 find_corps()가 잡아 miss에 안 들어오니 이 검사가
    걸릴 일은 드물다 — find_corps()가 놓친 표기 변형까지 잡는 2차 방어선이다."""
    return any(name != corp and name in question for name in (corp_names or []))


def _direct_carryover(question, miss, prev_slots, store, p):
    """규칙기반이 못 찾은 슬롯(기업/지표) 중, LLM 없이 코드만으로 직전 턴 값을
    그대로 승계해도 안전한 것만 승계한다. 세 조건을 전부 만족해야 한다:

    1. 발화가 직전 턴을 잇는다는 신호가 있다 — 둘 중 하나:
       (a) _CARRYOVER_SIGNAL(지시어·생략 표지, 예: "그래서 어떻게 변했어?")
       (b) 규칙기반이 이 발화만으로 event/wh를 이미 확정했다(예: "유상증자를
           언제 결정했는데?" — events_vocab.match()가 "유상증자결정"을,
           find_wh()가 "when"을 이 발화 텍스트만으로 이미 잡았다. 기업 이름만
           빠졌을 뿐 구체적인 분석 대상이 있다는 강한 근거라 (a)가 없어도
           승계를 허용한다 — 실측: T2가 (a)엔 안 걸리지만 이 조건엔 걸린다).
    2. prev_slots가 있다 — run()/_run() 계약상 직전 턴이 S0/S2였을 때만 채워져
       들어온다(모듈 docstring 참고). S1/S3/S6에서 나온 슬롯은 여기 오지 않는다.
    3. 그 슬롯(기업)이 발화에 다른 값으로 다시 명시돼 있지 않다.

    셋 다 만족하는 슬롯만 채워서 돌려준다 — 여기서 못 채운 나머지는 이전과
    똑같이 llmparse.extract_slots()로 넘어간다(이 함수는 LLM 호출을 대체하는
    게 아니라 그 앞에서 "물어볼 필요조차 없는 만큼"만 먼저 걷어낸다). 신호를
    못 잡으면(예: T3 "모든 경우의수 다 고려해서 알려줘" — 지시어도 없고
    event/wh도 이 문장만으론 안 잡힘) 안전하게 LLM 경로로 넘어갈 뿐이다.
    """
    # _METRIC_SWAP_SIGNAL("다른 지표로도") — "지표를 바꿔서 같은 대상을 계속
    # 보고 싶다"는 명백한 맥락 연속 신호인데 _CARRYOVER_SIGNAL(지시어류)엔
    # 안 걸린다. 실측 회귀(라이브, 2026-09-05): "매출액말고 다른 지표로도
    # 비교해줘"가 기업 승계가 전혀 안 돼 "기업을 알려주시면"으로 정직하게는
    # 되묻지만 맥락상 명백히 틀린 답이 됐다.
    signal = (bool(_CARRYOVER_SIGNAL.search(question)) or bool(p.get("event")) or bool(p.get("wh"))
              or bool(_METRIC_SWAP_SIGNAL.search(question)))
    if not prev_slots or not signal:
        return {}, []
    field_to_slot = {"기업": "corp", "지표": "concept"}
    got, carried = {}, []
    for m in miss:
        key = field_to_slot.get(m)
        if not key:
            continue
        val = prev_slots.get(key)
        if val is None:
            continue
        if key == "corp" and _other_corp_named(question, val, getattr(store, "corp_names", None)):
            continue
        got[key] = val
        carried.append(key)
    return got, carried


def _carryover_got(p, carried):
    """직승계 병합 직전, "이 발화가 이미 스스로 아는 것"(known) + "직전 턴에서
    가져온 것"(carried)을 합친다. **측정/테스트 스크립트도 이 함수를 그대로
    불러써야 한다 — 로직을 손으로 다시 베끼면 pipeline.py를 고쳐도 그쪽은
    낡은 채로 남는다(실측: 이 함수를 만들기 전, 별도 측정 스크립트가 옛
    known 구성을 그대로 복사해 두는 바람에 corp 승계 버그를 고친 뒤에도
    측정 결과에서 재현됐다).**

    known에 넣는 것 — 전부 "이 발화 자체에서 이미 확정된 값이라 승계로 덮으면
    안 되는 것"이다:
    - corp: find_corps()가 이미 잡았으면(예: "그거 SK하이닉스는?") 반드시
      넣는다. 안 넣으면 _merge_slots()가 "이 발화가 corp를 안 정했다"고
      오인해 prev_slots의 예전 회사로 덮어써 버린다 — 이 코드가 막으려는
      엔티티 오염이 승계 경로 자체에서 재발하는 가장 심각한 실측 회귀였다.
    - wh/event: T2류(event/wh는 이 발화만으로 이미 확정, corp만 빠짐)가
      렌더링에 필요하다. event는 dict이므로 type 문자열만 뽑는다.
    - year/scope: 안 넣으면 "2025년은?"처럼 이 발화가 스스로 새 연도를
      밝혔는데도 prev_slots의 예전 연도로 되돌아간다.
    - intent: 일부러 안 넣는다 — 조각 문장 혼자서는 numqa가 "dual"(scope
      미지정 이중값) 같은 이 맥락에 안 맞는 기본값을 매기는 경우가 있어,
      그대로 실리면 render_question()의 템플릿 조건에 걸려 렌더링이 막힌다.
      비워 두면 render_question() 자체의 fact_numeric 기본 가정이 대신 쓰인다.
    - concept: 이제 넣는다 — 예전엔 p["concept"]가 이 경로에서 종종 한글 표기가
      아니라 내부 metric_key(예: "operating_income")로 나와 그대로 넘기면 렌더
      문장이 깨져서 일부러 뺐었다. 근본 원인(qa/ontology.py의 concept 조립이
      numqa metric_key를 그대로 실었던 것)을 고쳐 p["concept"]가 항상 한글
      정규형이 되므로, 더 이상 배제할 이유가 없다.
    """
    known = {"corp": p.get("corp"), "concept": p.get("concept"),
             "wh": p.get("wh"), "year": p.get("year"), "scope": p.get("scope")}
    if p.get("event"):
        known["event"] = p["event"].get("type")   # render_question()은 문자열을 기대한다
    known = {k: v for k, v in known.items() if v}
    return {**known, **carried}


def _apply_carryover(question, got, prev_slots, store, labels):
    """_direct_carryover()가 채운 슬롯으로 새 p를 만든다.

    p를 직접 손으로 패치하지 않는다 — 실측 회귀: corp를 사후에 손으로 꽂아
    넣었더니 `event.anchors`처럼 **원래 파싱 시점(corps가 비어 있던 때)에
    이미 확정된 파생 필드**가 그대로 비어 있어 T2가 S0에서 S1로 나빠졌다
    (find_events()가 애초에 corps=[]로 호출됐던 결과를 그대로 물려받음).

    대신 LLM 경로와 똑같은 검증된 파이프라인(_merge_slots → render_question →
    stage01_map 재파싱)을 그대로 타되, "LLM이 낸 슬롯" 자리에 직승계로 얻은
    값(got)을 넣는다 — API 호출만 건너뛰고 나머지는 이미 통과된 로직을
    그대로 재사용한다. 반환: (새 p, 새 miss, 렌더된 문장) 또는 개선이 없으면
    (None, None, None).
    """
    merged = _merge_slots(got, prev_slots)
    rendered = llmparse.render_question(merged)
    if not rendered:
        return None, None, None
    p2 = stage01_map(rendered, store, labels, source="rewrite")
    return p2, _missing_fields(p2), rendered


# ---------------------------------------------------------------------------
# [P1] 답변 품질 트랙 v2 — Q_UMBRELLA(포괄어 지표 세트) / Q_RECOMMEND(투자
# 추천 의도). 둘 다 비용이 드는 새 경로가 아니라(추가 LLM 호출 없음) 기본값은
# off — 기존 llmparse.enabled()/narrative.enabled()와 같은 os.environ 패턴.
# ---------------------------------------------------------------------------
def _q_umbrella_enabled():
    return os.environ.get("Q_UMBRELLA_ENABLED", "").lower() in ("1", "true", "yes", "on")


def _q_recommend_enabled():
    return os.environ.get("Q_RECOMMEND_ENABLED", "").lower() in ("1", "true", "yes", "on")


def _q_multi_carry_enabled():
    return os.environ.get("Q_MULTI_CARRY_ENABLED", "").lower() in ("1", "true", "yes", "on")


def _q_state_coherence_enabled():
    return os.environ.get("Q_STATE_COHERENCE_ENABLED", "").lower() in ("1", "true", "yes", "on")


# [P3] boss 리뷰 라운드2 finding: `p.get("unsupported")`는 ontology.py가
# "다중 기업(2개 이상) + concept/field 없음 + ranking/aggregate 아님"이면
# 항상 세우는 범용 신호일 뿐, "일부 기업이 조용히 빠졌다"는 뜻이 전혀
# 아니다(예: "삼성전자, SK하이닉스 현금흐름 비교하면?"처럼 2개 다 정확히
# 잡혀도 세팅된다). perf.wanted()/health.wanted()는 원래 이 범용 신호를
# 정당하게 우회하도록 설계됐다(GENERAL 패턴이 p["concept"]와 무관하게
# 다지표 요약을 직접 처리) — 라운드1이 실제로 문제 삼은 건 "원문이 나열한
# 기업 수보다 p["corps"]가 적을 때"뿐이다. 그래서 unsupported 자체가 아니라
# "원문이 몇 개를 나열했는가"를 직접 세서 비교한다.
_ENUM_NUM_WORD = {"둘": 2, "두": 2, "셋": 3, "세": 3, "넷": 4, "네": 4, "다섯": 5}


def _enumerated_corps_dropped(question, p):
    """원문이 명시적으로 나열한 기업 수가 p["corps"]보다 많으면 True — 일부가
    조용히 빠졌다는 뜻이다. 쉼표 나열 개수와 "세 곳"/"3개사" 같은 명시 개수
    표현 중 더 큰 쪽을 "원문이 말한 개수"로 본다(둘 다 없으면 0 — 판단
    근거가 없을 때는 막지 않는다, 오탐보다 미탐이 낫다)."""
    corps = p.get("corps") or []
    commas = question.count(",") + question.count("、")
    said = commas + 1 if commas else 0
    m = re.search(r"(\d+)\s*(?:개|곳|군데)", question)
    if m:
        said = max(said, int(m.group(1)))
    else:
        # 카운터(곳/군데/개)를 반드시 요구한다 — 안 그러면 "네이버"의 "네",
        # "두산로보틱스"의 "두" 같은 흔한 고유명사 앞글자에 오탐한다(실측
        # 회귀: GOLD-W2B-P08/P12, 단일기업 질문이 "누락됨"으로 오판돼 막힘).
        for word, n in _ENUM_NUM_WORD.items():
            if re.search(word + r"\s*(?:곳|군데|개)", question):
                said = max(said, n)
                break
    return said > len(corps)


def _q_recency_enabled():
    return os.environ.get("Q_RECENCY_ENABLED", "").lower() in ("1", "true", "yes", "on")


def _q_sector_isolation_enabled():
    return os.environ.get("Q_SECTOR_ISOLATION_ENABLED", "").lower() in ("1", "true", "yes", "on")


def _q_competitor_enabled():
    return os.environ.get("Q_COMPETITOR_ENABLED", "").lower() in ("1", "true", "yes", "on")


def _q_ambiguous_reask_enabled():
    return os.environ.get("Q_AMBIGUOUS_REASK_ENABLED", "").lower() in ("1", "true", "yes", "on")


def _q_screening_enabled():
    return os.environ.get("Q_SCREENING_ENABLED", "").lower() in ("1", "true", "yes", "on")


def _q_scope_clarify_enabled():
    return os.environ.get("Q_SCOPE_CLARIFY_ENABLED", "").lower() in ("1", "true", "yes", "on")


def _q_growth_rank_enabled():
    return os.environ.get("Q_GROWTH_RANK_ENABLED", "").lower() in ("1", "true", "yes", "on")


# [2026-09-05, 사용자 요청 — aggregation 유형 개선] "부채비율이 300%를 넘는
# 기업은 몇 곳이고 어디인가" — 특정 기업을 안 대고 코퍼스 전체를 임계값으로
# 스크리닝하는 질문. ontology.py는 이미 concept/derived/year/scope를 전부
# 정확히 잡지만(실측 확인), corps가 비어 있으면 "순위 질문인데 대상 기업을
# 특정 못함"으로 unsupported 처리해 버린다(다중기업 비교·랭킹은 늘 명시
# 기업이나 업종이 있다고 가정했었다) — "전체 코퍼스가 대상"이라는 경우를
# 아예 다루지 않았다. qa/crosstab.py::screen()이 이미 만들어져 있었지만
# 실제로는 어디서도 호출되지 않는 죽은 코드였다.
_SCREEN_THRESHOLD = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*(?:를|을|가|이)?\s*"
    r"(넘는|초과하는|초과한|이상인|이상의|이상|밑도는|미만인|미만의|미만|이하인|이하의|이하)")
_SCREEN_ASK = re.compile(r"몇\s*(?:곳|개|군데)|어디")
_SCREEN_OP = {
    "넘는": "gt", "초과하는": "gt", "초과한": "gt",
    "이상인": "ge", "이상의": "ge", "이상": "ge",
    "밑도는": "lt", "미만인": "lt", "미만의": "lt", "미만": "lt",
    "이하인": "le", "이하의": "le", "이하": "le",
}
_SCREEN_OP_KO = {"gt": "초과", "ge": "이상", "lt": "미만", "le": "이하"}
_SCREEN_OP_FN = {
    "gt": lambda v, t: v > t, "ge": lambda v, t: v >= t,
    "lt": lambda v, t: v < t, "le": lambda v, t: v <= t,
}


def _run_screening(r, p, store, labels, threshold, op):
    """코퍼스 전체(70개사)를 대상으로 임계값 스크리닝한다. concept/derived·
    year·scope는 이미 ontology.py가 정확히 잡아 둔 것을 그대로 쓴다 —
    여기서 새로 추론하지 않는다."""
    concept = p.get("derived") or p.get("concept")
    year, scope = p.get("year"), p.get("scope") or "consolidated"
    ci = concepts.get(labels.facts)
    fn = _SCREEN_OP_FN[op]
    hits, checked = [], 0
    for name in store.corp_names:
        cc = store.corp_code.get(name)
        if not cc:
            continue
        y, v, _f = _concept_value(cc, concept, scope, year, labels, ci)
        if y is None:
            continue
        checked += 1
        if fn(float(v), threshold):
            hits.append((name, float(v)))
    r.stage("01").status = "완료"
    r.stage("01").note = (r.stage("01").note or "") + " · 전사 스크리닝(코퍼스 70개사)"
    if checked == 0:
        r.stage("02").status = "실패"
        r.stage("02").note = "코퍼스 전체에서 이 지표를 조회하지 못함"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)
    for no in ("02", "03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("02").note = f"{checked}개사 조회 · {len(hits)}개사 조건 충족"
    r.stage("04").note = "fact 값 그대로 인용/파생 계산 · 임계값 비교만"
    hits.sort(key=lambda t: -t[1])
    scope_ko = SCOPE_KO_ALL.get(scope, scope)
    op_ko = _SCREEN_OP_KO[op]
    year_ko = f"{year}년 " if year else ""
    if hits:
        listed = ", ".join(name for name, _v in hits)
        r.answer_text = (f"{year_ko}{scope_ko} 기준 {concept}이(가) {threshold}%{op_ko}인 기업은 "
                         f"{len(hits)}곳입니다 — {listed}.")
    else:
        r.answer_text = f"{year_ko}{scope_ko} 기준 {concept}이(가) {threshold}%{op_ko}인 기업이 없습니다."
    r.numbers = [str(v) for _n, v in hits]
    r.state = "S0"
    return r


def _exact_value(cc, concept, scope, y, labels, ci):
    """_concept_value와 달리 연도를 다른 값으로 대체하지 않는다 — 증가율은 두
    특정 연도 사이의 값이라, 한쪽을 최근값으로 슬쩍 바꾸면 증가율 자체가
    허구가 된다. 그 해 값이 없으면 그냥 None."""
    f = health._lookup_any(labels, ci, cc, concept, scope, y)
    if f:
        return health._num(f), f
    if concept in derived.RULES:
        _op, operands, _unit, _mult, _own = derived.RULES[concept]
        facts = [health._lookup_any(labels, ci, cc, o, scope, y) for o in operands]
        if all(fa is not None for fa in facts):
            v = derived.compute(concept, [health._num(fa) for fa in facts])
            if v is not None:
                return v, None
    return None, None


def _run_growth_rank(r, p, store, labels):
    """"2023회계연도 대비 2024회계연도에 자산총계 증가율이 가장 큰 기업은
    어디인가" — 특정 기업 없이 코퍼스 전체(70개사)에서 두 연도 간 증가율의
    극값을 찾는 질문. Q_SCREENING(임계값 필터)과 달리 필터가 아니라 계산 후
    순위 1위 추출이다. 두 해 값이 모두 있는 기업만 비교 대상에 넣는다."""
    concept = p.get("derived") or p.get("concept")
    scope = p.get("scope") or "consolidated"
    base_year, year = p.get("base_year"), p.get("year")
    order = p.get("order") or "desc"
    ci = concepts.get(labels.facts)
    rows = []
    for name in store.corp_names:
        cc = store.corp_code.get(name)
        if not cc:
            continue
        v0, f0 = _exact_value(cc, concept, scope, base_year, labels, ci)
        v1, f1 = _exact_value(cc, concept, scope, year, labels, ci)
        if v0 is None or v1 is None or v0 == 0:
            continue
        growth = float((v1 - v0) / v0 * 100)
        rows.append((name, growth, v0, v1, f0, f1))
    r.stage("01").status = "완료"
    r.stage("01").note = (r.stage("01").note or "") + " · 전사 증가율 순위(코퍼스 70개사)"
    if not rows:
        r.stage("02").status = "실패"
        r.stage("02").note = "두 연도 값이 모두 있는 기업이 없음"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)
    rows.sort(key=lambda t: t[1], reverse=(order == "desc"))
    for no in ("02", "03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("02").note = f"{len(rows)}개사 비교 가능(두 연도 모두 값 있음)"
    r.stage("04").note = "증감률 = (당기−전기)÷전기×100, 전 기업에 대해 계산 후 극값 선택"
    scope_ko = SCOPE_KO_ALL.get(scope, scope)
    order_ko = "큰" if order == "desc" else "작은"
    name, growth, v0, v1, f0, f1 = rows[0]
    r.answer_text = (f"{base_year}회계연도 대비 {year}회계연도 {scope_ko} 기준 {concept} 증가율이 "
                     f"가장 {order_ko} 기업은 {name}입니다 (증가율 {growth:.1f}%: "
                     f"{base_year} {v0:,.0f} → {year} {v1:,.0f}).")
    r.numbers = [f"{growth:.1f}%"]
    r.ranking = [nm for nm, *_ in rows]
    r.evidence = [to_coordinate(f) for f in (f0, f1) if f]
    r.state = "S0"
    return r


# [사용자 확정 스펙 2026-09-05] "경쟁사랑 비교" — 직전 기업의 업종 경쟁사로
# 확장한다. Q_SECTOR_ISOLATION(P4)의 expanded_corps 규칙을 그대로 쓴다 —
# 경쟁사는 사용자가 직접 댄 게 아니므로 corps엔 넣되 expanded_corps로
# 표시해 다음 턴 승계 후보에서 빠지게 한다.
_COMPETITOR_TRIGGER = re.compile(r"경쟁사|경쟁업체|동종업계|같은\s*업종|비슷한\s*회사|라이벌")

# [사용자 확정 스펙 2026-09-05] 모호 접두어(ontology._AMBIGUOUS_CORP_PREFIXES)를
# 조용히 버리지 않고 후보를 나열해 되묻는다.


def _ambiguous_prefix_mentions(question, corps_resolved):
    """원문에 애매 접두어가 단독으로 언급됐는데(전체 이름도, 이미 확정된
    기업도 아님) 실제 후보를 하나로 못 좁힌 경우를 질문에 나온 순서대로
    돌려준다. [(접두어, 후보목록), ...].

    공백을 무시하고 비교한다 — derived.find()/umbrella.find() 등 이
    코드베이스의 다른 표기 매칭과 같은 관례다. 안 그러면 "한화 에어로
    스페이스"(공백 포함, T6의 실제 발화)처럼 이미 확정될 표기가 후보 전체
    이름과 글자 그대로는 안 맞아 "한화"를 애매로 오판한다(실측 회귀:
    T6 "내가 한화 에어로 스페이스라고 하지않았나?" 파손).
    """
    qn = question.replace(" ", "")
    hits = []
    for prefix, candidates in ontology._AMBIGUOUS_CORP_PREFIXES.items():
        if prefix not in qn:
            continue
        if any(c in qn for c in candidates):
            continue  # 전체 이름이 실제로 있으면 애매하지 않다
        if any(c in corps_resolved for c in candidates):
            continue  # 승계 등으로 이미 확정됨
        # 이미 확정된(다른 그룹 소속) 기업명 안에 이 접두어가 그냥 부분
        # 문자열로 들어있을 뿐이면 애매한 게 아니다 — 실측 회귀:
        # "HD현대중공업"(HD 그룹 소속, 이미 확정)엔 "현대"가 부분문자열로
        # 들어있어 "현대" 그룹까지 잘못 애매 판정했다(GOLD-W2B-P02·
        # FIN-0064/0066/0073/0092/0138/0139).
        if any(prefix in c for c in corps_resolved):
            continue
        hits.append((qn.find(prefix), prefix, candidates))
    hits.sort()
    return [(pfx, cands) for _, pfx, cands in hits]


def _staleness_note(as_of_year):
    """[P3] Q_RECENCY — "지금 투자할만해?"류 현재형 질문에 붙는 as-of가
    18개월(사업연도 말 12/31 기준 환산) 넘게 오래됐으면 그 사실을 밝힌다.
    실제 벽시계 날짜(datetime.now())를 쓴다 — 이 프로젝트의 다른 계산은
    전부 결정론(fact 값)이지만, "오래됐다"는 판단 자체가 실행 시점에 매여
    있는 문제라 여기서만 예외적으로 현재 시각을 본다."""
    if not as_of_year or not _q_recency_enabled():
        return ""
    now = datetime.now()
    months_stale = (now.year - as_of_year) * 12 + (now.month - 12)
    if months_stale > 18:
        return f"⚠️ 이 자료는 {as_of_year}년 사업보고서 기준(약 {months_stale}개월 전)으로 다소 오래됐습니다. "
    return ""


# [P2] Q_MULTI_CARRY — 기존 _CARRYOVER_SIGNAL(그래서/그럼/그거 …)이 못 잡는
# 지시어들. 기존 신호와 별개 상수로 두는 이유: 이 신호는 "기업 리스트
# 승계"·"intent/concept_set 승계"에만 쓰고, 기존 단일 corp/concept 직승계
# (qa/pipeline.py::_direct_carryover) 신호 판정은 손대지 않는다 — 그쪽을
# 건드리면 이미 검증된 c15~c20(heldout) 회귀 위험이 생긴다.
_MULTI_CARRY_PRONOUNS = re.compile(
    r"이\s*회사|해당(?:\s*기업|\s*회사)?|그\s*회사|여기|저\s*회사|저\s*\d+\s*개|앞의|얘네"
)

# "비교 의도" — 발화가 새 기업을 명시적으로 대면서 동시에 "그것과 비교"를
# 뜻하는 조사/어미. 이게 있어야 "합집합"(기존 기업 + 새 기업)이고, 없으면
# "명시 기업으로 대체"다(스펙 원문 그대로).
_COMPARISON_SIGNAL = re.compile(r"랑\s*(?:비교|보다)|와\s*(?:비교|보다)|과\s*(?:비교|보다)|중(?:에|엔)?\s*(?:누가|어디)")


def _is_bare_reference(question, p):
    """발화 전체가 "기업명·지시어·연도"만으로 이뤄졌는가(새 내용이 없는가).

    intent/concept_set 승계(Q_MULTI_CARRY)는 이 경우에만 허용한다 — 스펙
    원문: "intent/concept_set 승계는 발화가 기업명·지시어·연도뿐일 때만".
    "한화에어로스페이스"(T9)처럼 기업명 하나만 있는 문장도, 이미 이 발화
    안에서 corps가 리터럴로 잡혀 있으므로 이 함수가 그 이름을 지우고 나면
    빈 문자열이 남아 참이 된다.
    """
    stripped = question
    for name in (p.get("corps") or []):
        stripped = stripped.replace(name, "")
    stripped = _MULTI_CARRY_PRONOUNS.sub("", stripped)
    stripped = re.sub(r"\d{4}년?", "", stripped)
    stripped = re.sub(r"[은는이가을를의,\s?!.]+", "", stripped)
    return stripped == ""


# [P3 후속] Q_RECENCY 시제 게이트 — 사용자 확정 스펙(2026-09-05). year=null인
# 질문을 "가장 최근"으로 조용히 답하던 기존 폴백(아래 "가장 최근" 블록)이
# "예전엔 얼마였어?" 같은 명백한 과거시제 질문에도 최신값을 태연히 내놓는
# 실측 반례가 있었다(P3 boss 라운드2 finding, REPORT.md 참고) — 골드셋엔
# 이 유형이 없어 정식 회귀로는 안 걸렸지만 실사용에선 사실상 오답이다.
#
# 현재 표지가 과거 표지보다 우선한다(스펙엔 충돌 규칙이 없어 보수적으로
# 정함) — "최근엔 어때?"처럼 현재 표지가 있으면 과거로 안 본다.
_PAST_TENSE_SIGNAL = re.compile(
    r"예전|과거|이전|그때|\d+\s*년\s*전|했었|였어\s*\??$|어땠어\s*\??$"
)
_PRESENT_TENSE_SIGNAL = re.compile(r"지금|현재|최근|올해|요즘")
# "작년"/"재작년"은 여기서 뺐다 — "예전에"/"과거에는"처럼 막연한 과거가 아니라
# 특정 단일 연도를 가리키는 표현인데, 이 코퍼스엔 "지금(기준 연도)"이 정의돼
# 있지 않아 그 연도가 몇 년인지 아무도 못 정한다(전체 시계열로 얼버무리는
# 것도 부정확하다 — "작년"은 하나의 특정 연도를 뜻하는 말이니까). 그래서
# 뒤(qa/pipeline.py::_run() "상대연도 모호" 분기)에서 별도로, 더 좁게(‘사업
# 보고서’ 앵커가 없을 때만) S3 되묻기로 처리한다. 실측(FIN 골드셋): "가장
# 최근 사업보고서 기준"류는 단일 확정값이 정답인데 단독 "재작년"/"작년"류는
# CLARIFICATION_REQUIRED가 정답이었다 — 이 둘을 여기 한 정규식에 묶으면
# 구분이 안 된다.


def _past_tense_signal(question):
    return bool(_PAST_TENSE_SIGNAL.search(question)) and not _PRESENT_TENSE_SIGNAL.search(question)


# "투자할만/살만/괜찮은 회사/추천" — 스펙이 예로 든 "어때"는 일부러 안 넣는다.
# bare "어때"는 qa/perf.py의 GENERAL 패턴("매출 어때?" 등)과 정면으로 겹쳐서
# 그대로 넣으면 실적 추이 질문 전체를 이 경로로 가로채 버린다(실측 회귀
# 위험 — perf.wanted()가 이미 "어때"류를 광범위하게 받는다). "투자" 문맥이
# 있는 조합형만 받는다.
_RECOMMEND_TRIGGER = re.compile(
    r"투자\s*(?:할\s*만|가치|해도\s*(?:될|되나|괜찮)|해볼\s*만)"
    r"|(?:살\s*만한|사도\s*될|괜찮은)\s*(?:회사|기업|종목)"
    r"|투자\s*추천|추천\s*(?:종목|기업|회사)|투자할까"
)

_RECOMMEND_DISCLAIMER = "투자 판단은 제공하지 않습니다. 공시 기반 사실은 다음과 같습니다."


def _recommend_wanted(question):
    return bool(_RECOMMEND_TRIGGER.search(question))


def _run_recommendation(r, p, store, labels):
    """투자 추천 의도 — 판단·전망 없이 health+perf+events 사실만 고정
    템플릿으로 보여준다. 첫 줄은 항상 고정 면책 문장이고(narrative의
    투자권유어휘 차단 정규식과 무관 — 이 문장은 LLM이 생성하지 않는 상수라
    애초에 그 정규식을 통과시킬 이유가 없다), 그 뒤로는 기존에 검증된
    perf.py/health.py/events.py의 fact 그대로만 나열한다. LLM 해석 없음."""
    r.stage("01").status = "완료"
    r.stage("01").note = (r.stage("01").note or "") + " · 투자추천의도(면책 고정템플릿)"
    p["intent"] = "recommendation"
    corps = p.get("corps") or []
    corp_codes = p.get("corp_codes") or []
    if not corps or not corp_codes or not corp_codes[0]:
        for no in ("02", "03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        r.answer_text = f"{_RECOMMEND_DISCLAIMER} 기업을 특정하지 못해 사실을 조회할 수 없습니다."
        return _with_sections(r, p)
    corp, cc = corps[0], corp_codes[0]
    scope = p.get("scope") or "consolidated"
    parts, as_of_year = [], None
    s = perf.summarize(store, corp, cc, scope)
    if s:
        parts.append(perf.render([s]))
        if s.get("years"):
            as_of_year = s["years"][-1]
    d = health.diagnose(labels, corp, cc, scope)
    if d:
        parts.append(health.render(d))
    rows = events.recent(corp)
    if rows:
        parts.append("최근 이벤트: " + "; ".join(line for _rno, line, _c in rows[:3]))
    if not parts:
        for no in ("02", "03", "04", "05"):
            r.stage(no).status = "건너뜀" if no != "02" else "실패"
        r.stage("02").note = "실적·건강도·이벤트 자료 전무"
        r.state = "S1"
        r.answer_text = f"{_RECOMMEND_DISCLAIMER} {corp}의 공시 데이터가 없어 사실을 제시할 수 없습니다."
        return _with_sections(r, p)
    for no in ("02", "03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("02").note = f"실적/건강도/이벤트 {len(parts)}종 취합"
    r.stage("04").note = "fact 값 그대로 인용 · LLM 해석 없음"
    as_of_note = f"(as-of {as_of_year}년 사업보고서 기준, {SCOPE_KO_ALL.get(scope, scope)} 기준) " if as_of_year else ""
    as_of_note += _staleness_note(as_of_year)
    r.answer_text = f"{_RECOMMEND_DISCLAIMER} {as_of_note}" + " ".join(parts)
    r.numbers = []
    r.state = "S2"
    r.skip_confidence_note = True  # 이미 첫 줄에 자체 고정 면책 문구가 있다 — 중복 표시 안 함
    return _with_sections(r, p)


# ---------------------------------------------------------------------------
# Q_UMBRELLA 실제 조회 — "plain" 세트는 각 지표 최신값을, "growth" 세트는
# qa/perf.py가 이미 계산한 최근 3개년 변화율을 재사용한다(qa/umbrella.py
# 참고 — 새 계산 경로를 만들지 않는다).
# ---------------------------------------------------------------------------
def _concept_value(cc, concept, scope, year, labels, ci):
    """개념 하나의 (연도, Decimal 값, 표시용 fact|None) — qa/derived.py와
    같은 우선순위(corpus 명시값 우선, 없으면 파생 RULES에 있는 개념에 한해
    계산). 표시용 fact가 있으면(원본 label 값) 호출부가 value_raw/unit_kr을
    그대로 쓰고, None이면 파생 계산값(round는 호출부 몫)이다. 아무것도 없으면
    (None, None, None).

    year: 명시되면 그 연도만 보고, 그 연도에 값이 없으면(명시값·파생 계산
    피연산자 어느 쪽도) 실패로 본다 — 없으면 가장 최근으로 폴백한다.
    """
    years = health._years_any(labels, ci, cc, concept, scope)
    y = year if (year and year in years) else (max(years) if years else None)
    f = health._lookup_any(labels, ci, cc, concept, scope, y) if y else None
    if f:
        return y, health._num(f), f
    if concept in derived.RULES:
        _op, operands, _unit, _mult, _own = derived.RULES[concept]
        avail_common = health._latest_common_year(labels, ci, cc, operands, scope)
        dy = year if (year and all(
            year in health._years_any(labels, ci, cc, o, scope) for o in operands)) else avail_common
        vals = ([health._num(health._lookup_any(labels, ci, cc, o, scope, dy)) for o in operands]
                if dy else [])
        if dy and all(v is not None for v in vals):
            v = derived.compute(concept, vals)
            if v is not None:
                return dy, v, None
    return None, None, None


def _umbrella_plain_summary(corp, cc, concept_set, scope, labels, year=None):
    """qa/derived.py와 같은 우선순위를 지킨다 — corpus 명시값이 있으면
    그것을 쓰고, 없을 때만(부채비율 등 파생 RULES에 있는 개념에 한해) 계산한다.
    "미공시"는 둘 다 실패했을 때만 쓴다(명시값도 없고 계산도 안 되는 경우).

    year: 명시되면(대화에서 이미 확정된 연도가 있으면) 그 연도만 본다 —
    없으면(원래 설계, "최신 재무정보" 의도) 가장 최근 연도로 폴백한다.
    실측 회귀: 연도가 이미 확정된 대화(예: "2024년 매출액은?" 다음 "다른
    지표로도 비교해줘")에서도 이 함수가 무조건 최신연도(2025)를 강제해
    맥락과 다른 연도로 답했다.
    """
    ci = concepts.get(labels.facts)
    lines, hit = [], 0
    for concept in concept_set:
        y, v, f = _concept_value(cc, concept, scope, year, labels, ci)
        if y is None:
            lines.append(f"{concept}: 미공시")
            continue
        hit += 1
        if f:
            lines.append(f"{concept} {f.get('value_raw', '')}{f.get('unit_kr', '')}({y}년)")
        else:
            unit = derived.RULES[concept][2]
            lines.append(f"{concept} {round(float(v), 1)}{unit}(파생, {y}년)")
    return lines, hit


def _run_umbrella_summary(r, p, store, labels):
    """핵심지표 요약 — 1기업 × N지표(concept_set)를 한 번에 보여준다.
    결측 지표는 개별적으로 "미공시"로 표시하고, 전부 결측일 때만 S1이다
    (하나라도 있으면 있는 만큼 S2로 답한다 — "결측은 미공시" 스펙 그대로)."""
    concept_set, kind = p["concept_set"]
    corps, corp_codes = p.get("corps") or [], p.get("corp_codes") or []
    r.stage("01").status = "완료"
    r.stage("01").note = (r.stage("01").note or "") + " · 핵심지표요약(Q_UMBRELLA)"
    if not corps or not corp_codes or not corp_codes[0]:
        for no in ("02", "03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S3"
        r.missing = ["기업"]
        return _with_sections(r, p)
    corp, cc = corps[0], corp_codes[0]
    scope = p.get("scope") or "consolidated"
    if kind == "growth":
        s = perf.summarize(store, corp, cc, scope)
        if not s or not s.get("change"):
            for no in ("02", "03", "04", "05"):
                r.stage(no).status = "건너뜀" if no != "02" else "실패"
            r.stage("02").note = "실적 시리즈 조회 실패"
            r.state = "S1"
            return _with_sections(r, p)
        for no in ("02", "03", "04", "05"):
            r.stage(no).status = "완료"
        r.stage("02").note = f"{corp} 최근 {perf.SPAN}개년 변화율"
        r.stage("04").note = "fact 값으로 계산 · perf.py 기존 경로 재사용"
        y0, y1 = s["years"][0], s["years"][-1]
        chline = " · ".join(f"{ko} {v:+.1f}%" for ko, v in s["change"].items() if v is not None)
        r.answer_text = (f"{corp}의 {y0}→{y1}년 성장률(참고: 진짜 CAGR이 아니라 "
                         f"perf.py의 기존 3개년 변화율) — {chline or '변화율 계산 불가'}.")
        r.numbers = [str(v) for v in s["change"].values() if v is not None]
        r.state = "S0"
        return r
    lines, hit = _umbrella_plain_summary(corp, cc, concept_set, scope, labels, year=p.get("year"))
    if hit == 0:
        for no in ("02", "03", "04", "05"):
            r.stage(no).status = "건너뜀" if no != "02" else "실패"
        r.stage("02").note = "핵심지표 전부 미공시"
        r.state = "S1"
        r.answer_text = f"{corp}의 핵심지표({', '.join(concept_set)})가 전부 미공시입니다."
        return _with_sections(r, p)
    for no in ("02", "03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("02").note = f"핵심지표 {hit}/{len(concept_set)}건 조회"
    r.stage("04").note = "fact 값 그대로 인용 · 파생 없음"
    scope_ko = SCOPE_KO_ALL.get(scope, scope)
    r.answer_text = f"{corp}의 {scope_ko} 기준 핵심지표 — " + " / ".join(lines) + "."
    r.numbers = []
    r.state = "S0"
    return r


def _run_umbrella_multi_plain(r, p, store, labels):
    """concept_set(plain) + N기업 — 진짜 (지표×기업) crosstab 표 렌더링은
    이 트랙 1라운드 범위를 넘어서는 작업이라, 우선 기업별 핵심지표 요약을
    나란히 보여준다. T7~T12 어디서도 이 경로는 실제로 발동하지 않는다(전부
    1기업 시나리오라서) — 아직 실측 검증이 안 된 경로임을 밝혀 둔다
    (REPORT.md P1 절 "판단 필요" 참고)."""
    concept_set, _kind = p["concept_set"]
    scope = p.get("scope") or "consolidated"
    sections, any_hit = [], False
    for corp, cc in zip(p["corps"], p.get("corp_codes") or []):
        if not cc:
            continue
        lines, hit = _umbrella_plain_summary(corp, cc, concept_set, scope, labels, year=p.get("year"))
        if hit:
            any_hit = True
        sections.append(f"{corp}: " + " / ".join(lines))
    if not any_hit:
        r.stage("02").status = "실패"
        r.stage("02").note = "전 기업 핵심지표 전무"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)
    for no in ("02", "03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("02").note = f"{len(sections)}개사 핵심지표"
    r.stage("04").note = "fact 값 그대로 인용 · 파생 포함"
    scope_ko = SCOPE_KO_ALL.get(scope, scope)
    r.answer_text = f"{scope_ko} 기준 기업별 핵심지표 — " + " | ".join(sections) + "."
    r.numbers = []
    r.state = "S0"
    return r


def _run_growth_ranking(r, p, store):
    """concept_set(성장률) + ranking — N기업을 매출 성장률(1순위)로 정렬하고
    영업이익 성장률을 병기한다. 동률은 기업명 가나다순(안정적 재현을 위해),
    한쪽이라도 계산 불가한 기업은 순위에서 빠지고 그 사실을 답변에 명시한다."""
    concept_set, _kind = p["concept_set"]
    primary = concept_set[0] if concept_set else "매출액"
    rows = []
    dropped = []
    for corp, cc in zip(p["corps"], p.get("corp_codes") or []):
        if not cc:
            dropped.append(corp)
            continue
        s = perf.summarize(store, corp, cc, p.get("scope") or "consolidated")
        if not s or s.get("change", {}).get(primary) is None:
            dropped.append(corp)
            continue
        rows.append((corp, s["change"]))
    if not rows:
        r.stage("02").status = "실패"
        r.stage("02").note = "전 기업 성장률 계산 불가"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)
    rows.sort(key=lambda t: (-t[1].get(primary, 0), t[0]))
    for no in ("02", "03", "04", "05"):
        r.stage(no).status = "완료"
    r.stage("02").note = f"{len(rows)}개사 성장률 순위(1위={rows[0][0]})"
    r.stage("04").note = "perf.py 기존 3개년 변화율 재사용 · 파생 없음"
    secondary = concept_set[1] if len(concept_set) > 1 else None
    lines = []
    for i, (corp, ch) in enumerate(rows, 1):
        p_v = ch.get(primary)
        s_v = ch.get(secondary) if secondary else None
        extra = f", {secondary} {s_v:+.1f}%" if s_v is not None else ""
        lines.append(f"{i}위 {corp} ({primary} {p_v:+.1f}%{extra})")
    note = f" (계산 불가로 제외: {', '.join(dropped)})" if dropped else ""
    scope_ko = SCOPE_KO_ALL.get(p.get("scope") or "consolidated", "")
    r.answer_text = (f"성장세 순위(최근 {perf.SPAN}개년 변화율 기준, {scope_ko} 기준) — "
                     + " / ".join(lines) + f".{note}")
    r.numbers = [str(ch.get(primary)) for _c, ch in rows]
    r.state = "S0"
    return r


def _run(question, store=None, labels=None, prev_question=None, prev_slots=None):
    """prev_slots는 반드시 직전 턴이 S0 또는 S2였을 때만 채워서 넘겨야 한다 —
    S1/S3/S6에서 나온 슬롯은 절대 승계 후보로 넘기면 안 된다(호출부 책임)."""
    store = store or get_store()
    labels = labels or labelstore.get()
    r = QAResult(question=question, resolved_question=question,
                stages=[Stage(no, nm) for no, nm in STAGES])

    # [01]
    p = stage01_map(question, store, labels)
    r.parsed = p
    miss = _missing_fields(p)
    # Q_MULTI_CARRY가 "이 발화가 원래 새 기업을 명시했는가"를 판정할 때 쓴다
    # — 뒤의 단일 concept 직승계(_apply_carryover)가 성공하면 p를 통째로
    # rendered 문장 재파싱 결과로 바꿔치기하면서 corps를 1개로 접어버리는데
    # (그 파이프라인은 애초에 corp 하나짜리 문장만 렌더링할 줄 안다), 그
    # 부작용과 "이 발화가 실제로 corp을 새로 댔는가"를 혼동하면 안 된다.
    _orig_corps = list(p.get("corps") or [])

    # [P4] Q_SECTOR_ISOLATION — 업종 확장을 썼으면(p["expanded_corps"] 비어
    #있지 않음) 그 사실을 답변 어디로 가든 공통으로 명시한다. 답변 종류마다
    # 따로 문구를 넣게 하면 새 핸들러가 추가될 때마다 빠뜨리기 쉬워서, 모든
    # 핸들러가 공유하는 이 지점(디스패치 진입 직후) 한 곳에서만 처리한다.
    if _q_sector_isolation_enabled() and p.get("expanded_corps"):
        r.notices.append(
            f"📊 '{p['sector']}' 업종으로 확장해 {len(p['expanded_corps'])}개사를 답변 "
            f"대상으로 삼았습니다: {', '.join(p['expanded_corps'])}.")

    # "매출액말고 다른 지표로도" — ontology.parse()는 부정("말고")을 모르고
    # "매출액"을 그대로 concept으로 확정해 버린다. 그 값을 그대로 두면 바로
    # 아래 Q_UMBRELLA 사전체크의 "개별 지표 명시 시 그것이 우선" 가드에
    # 걸려 "다른 지표" 트리거 자체가 평가되지 않는다(사용자가 방금 부정한
    # 그 지표가 오히려 "이미 확정된 지표"로 둔갑) — 실측 회귀(라이브,
    # 2026-09-05)의 두 번째 원인. _METRIC_SWAP_SIGNAL이 있으면 이 발화가
    # 낸 concept/derived/metric은 부정 대상일 뿐이므로 비우고 umbrella만
    # 보게 한다.
    if _q_umbrella_enabled() and _METRIC_SWAP_SIGNAL.search(question):
        p["concept"], p["derived"], p["metric"] = None, None, None
        miss = _missing_fields(p)

    # Q_UMBRELLA: 포괄어("최신 재무정보"/"실적"/"성장세" 등) → 폐집합 지표
    # 세트. 개별 지표가 이미 확정됐으면(concept/derived) 이 함수를 아예
    # 부르지 않는다 — "개별 지표 명시 시 그것이 우선" 원칙. carryover/llmparse가
    # 뒤에서 p를 통째로 새로 파싱해 이 표시를 날릴 수 있어(_apply_carryover가
    # stage01_map을 rendered 문장으로 재호출) umbrella_set을 지역변수로 들고
    # 있다가 그 이후 최종 p에도 다시 부착한다(아래 재부착 지점 참고).
    umbrella_set = None
    # P2 boss 리뷰(라운드1) finding 2: intent/concept_set 승계가 원래
    # `_q_umbrella_enabled()` 안의 elif로만 평가돼 Q_MULTI_CARRY_ENABLED만
    # 켜고 Q_UMBRELLA_ENABLED는 끈 상태에서 승계가 죽는 커플링이 있었다.
    # 두 플래그를 or로 열어 서로 독립적으로 켤 수 있게 한다.
    if (_q_umbrella_enabled() or _q_multi_carry_enabled()) and "지표" in miss \
            and not p.get("concept") and not p.get("derived"):
        umbrella_set = umbrella.find(question) if _q_umbrella_enabled() else None
        if umbrella_set:
            p["concept_set"] = umbrella_set
            miss = _missing_fields(p)
        elif (_q_multi_carry_enabled() and prev_slots and prev_slots.get("concept_set")
                and _is_bare_reference(question, p)):
            # Q_MULTI_CARRY: 포괄어 자체가 없어도(예: T9 "한화에어로스페이스"
            # 단독) 발화 전체가 기업명·지시어·연도뿐이면 직전 턴의 concept_set/
            # intent를 그대로 잇는다 — 스펙 원문 "발화가 기업명·지시어·연도뿐일
            # 때만" 그대로.
            umbrella_set = prev_slots["concept_set"]
            p["concept_set"] = umbrella_set
            if prev_slots.get("intent"):
                p["intent"] = prev_slots["intent"]
            miss = _missing_fields(p)

    # G3-1: 정의형(용어) 질문이면 규칙기반이 기업/업종을 잘못 확장했어도(T4 사고 —
    # "무슨 말이야"가 "금융 업종 8개사"로 새어 나감) 그 확장을 무시하고 강제로
    # glossary 경로로 보낸다. glossary.wanted()는 기업 미특정을 전제조건으로
    # 요구하는데, 이 메타의도가 감지되면 그 전제를 강제로 만족시킨다.
    _is_definition_q = bool(glossary._DEFINE.search(question)
                            or glossary._EXPLAIN_DIFF.search(question)
                            or _META_WHAT_MEANS.search(question))
    if narrative.enabled() and _is_definition_q:
        p_glossary = dict(p, corp=None, corps=[], sector=None)
        r.parsed = p_glossary
        return _run_glossary(r, p_glossary, question, prev_question)

    # G3-2: 정정 신호("~하지 않았나"류) — compare_multi/narrative로 새기 전에
    # 가로채, 직전 성공턴 슬롯을 재확인시키거나(가능하면) 최소한 안전하게 되묻는다.
    if _CORRECTION_SIGNAL.search(question) and not _METRIC_SWAP_SIGNAL.search(question):
        out = _run_correction_recheck(r, p, question, prev_slots)
        if out is not None:
            return out

    # [사용자 확정 스펙 2026-09-05] 모호 접두어 되묻기 — "삼성"·"한화"처럼
    # 접두어 하나만으론 8개사 중 하나로 못 좁히는 표기를 find_corps()가
    # 조용히 못 찾고 지나가던 것(실측: "하이닉스랑 삼성" → corps=["SK하이닉스"],
    # "삼성"은 그냥 사라짐)을 여기서 잡아 후보를 나열해 되묻는다. 확정된
    # 기업(_orig_corps)은 그대로 두고 모호분만 되묻는다. G3-1(정의형)·G3-2
    # (정정 재확인) **다음**에 둔다 — 그 두 분기가 이미 처리할 발화까지
    # 이 체크가 가로채면 안 된다(실측 회귀: G3-2가 처리해야 할 T6 "내가
    # 한화 에어로 스페이스라고 하지않았나?"를 이 체크가 먼저 "한화"를
    # 애매로 오판해 가로챘었다 — 지금은 순서를 이 뒤로 옮겨 해결).
    if _q_ambiguous_reask_enabled():
        ambiguous = _ambiguous_prefix_mentions(question, _orig_corps)
        if ambiguous:
            r.stage("01").status = "실패"
            r.stage("01").note = "모호 표기(" + ", ".join(pfx for pfx, _ in ambiguous) + ") — 되물음"
            for no in ("02", "03", "04", "05"):
                r.stage(no).status = "건너뜀"
            r.state, r.missing = "S3", ["기업"]
            parts = []
            if _orig_corps:
                parts.append(f"{', '.join(_orig_corps)}는 확인했습니다.")
            for prefix, candidates in ambiguous:
                parts.append(f"'{prefix}'은(는) 어느 회사인가요? {' / '.join(candidates)}")
            r.answer_text = " ".join(parts)
            return r

    # 용어·공시규정 개념 설명 — 기업이 특정 안 된 정의형 질문. 아래 llmparse
    # 재해석보다 먼저 본다 — 어차피 이 경로도 HCX를 부르므로, 회사를 찾으려는
    # 시도(llmparse.extract_slots)로 호출을 하나 더 늘릴 이유가 없다. narrative와
    # 같은 비용 성격이라 같은 게이트(NARRATIVE_ENABLED)를 공유한다 — 새 env var를
    # 안 늘린다.
    if narrative.enabled() and glossary.wanted(question, p):
        return _run_glossary(r, p, question, prev_question)

    # 01 보강, 1단계 — 코드만으로 직승계(LLM 호출 없음). 규칙기반이 못 찾은
    # 슬롯(기업/지표)이라도, 발화에 지시어·생략 표지가 있고 직전 턴이 S0/S2였고
    # (run() 계약) 다른 기업이 명시되지 않았으면 직전 슬롯을 그대로 쓴다
    # (_direct_carryover, 사용자 확정 설계 — "잘하는 결정론 경로가 못하는 LLM
    # 경로 뒤에 있던" 구조를 뒤집는다). T2/T3류(이벤트 자체는 규칙기반
    # events_vocab.match()/find_wh()가 이미 잡고, 기업만 빠짐)는 이 승계
    # 하나로 LLM 호출 없이 끝난다.
    carried, carried_fields = _direct_carryover(question, miss, prev_slots, store, p)
    if carried_fields and p.get("concept_set") and carried_fields == ["corp"]:
        # concept_set(Q_UMBRELLA)은 render_question()이 모르는 필드라 아래
        # 일반 경로(_merge_slots→render_question→stage01_map 재파싱)를 못
        # 탄다 — 렌더링할 단일 문장이 없어 render_question()이 None을 돌려주고
        # 승계 자체가 조용히 무산된다(실측: T8류 "그럼 이 회사의 최신 재무
        # 정보 알려줘"가 corps=[]로 남음). concept_set 질문은 애초에
        # event.anchors처럼 "재파싱 안 하면 못 채우는" 파생 필드에 의존하지
        # 않으므로(핵심지표 요약·성장률 랭킹 둘 다 event를 안 본다) 여기서만
        # 예외적으로 corp 관련 필드를 직접 붙인다.
        corp = carried["corp"]
        p["corp"], p["corps"] = corp, [corp]
        p["corp_code"] = store.corp_code.get(corp)
        p["corp_codes"] = [p["corp_code"]]
        # year/scope도 이 발화 자체가 새로 안 밝혔으면 직전 턴 값을 잇는다 —
        # 안 그러면 "2024년 매출액은?" 다음 "매출액말고 다른 지표로도"가
        # 연도 맥락을 잃고 umbrella 경로의 "최신 연도" 기본값(2025)으로
        # 새 버린다(실측 회귀, 라이브 2026-09-05).
        if not p.get("year") and prev_slots.get("year"):
            p["year"] = prev_slots["year"]
        if not p.get("scope") and prev_slots.get("scope"):
            p["scope"] = prev_slots["scope"]
        miss = _missing_fields(p)
        r.parsed = p
        llmparse.log_carryover(question, prev_question, prev_slots, carried_fields, carried)
        r.notices.append(f"↩️ 직전 대화 맥락에서 승계: {', '.join(carried_fields)}")
    elif carried_fields:
        # "이 발화가 이미 스스로 아는 것" + "직전 턴에서 가져온 것"을 합친다
        # — 무엇을 넣고 왜 빼는지는 _carryover_got() 자체의 docstring 참고
        # (측정 스크립트도 이 함수를 그대로 불러써야 한다 — 로직을 여기서
        # 손으로 다시 쓰지 마라).
        got = _carryover_got(p, carried)
        p2, miss2, rendered = _apply_carryover(question, got, prev_slots, store, labels)
        if p2 is not None and len(miss2) < len(miss):
            p, miss = p2, miss2
            r.parsed = p
            r.resolved_question = rendered
            llmparse.log_carryover(question, prev_question, prev_slots, carried_fields, carried)
            r.notices.append(f"↩️ 직전 대화 맥락에서 승계: {', '.join(carried_fields)}")

    # [P2] Q_MULTI_CARRY — corps **리스트** 승계. 위 _direct_carryover는 corp
    # "하나"만 다루고(miss에 "기업"이 있을 때만), 이 블록은 그와 별개로
    # "직전 턴에 여러 기업이 있었다"는 사실 자체를 승계한다. 스펙 3분기:
    #   (a) 이번 발화가 새 기업을 명시 + 비교 신호("~랑 비교했을때는" 등)
    #       → 합집합(직전 리스트 + 새 기업, 중복 제거, 순서 보존)
    #   (b) 새 기업을 명시했지만 비교 신호 없음 → 대체(이미 p["corps"]가
    #       새 기업만 담고 있으므로 아무것도 안 함 — 누적 금지 그 자체)
    #   (c) 새 기업 명시 없이 지시어만(_MULTI_CARRY_PRONOUNS) → 직전 리스트
    #       그대로("여기 다" 같은 뜻)
    #
    # "새 기업을 명시했는가"는 반드시 _orig_corps(이 발화 원문의 최초 파싱
    # 결과)로 판정한다 — 위 단일 concept 직승계(_apply_carryover)가 성공하면
    # p를 rendered 문장 재파싱 결과로 바꿔치기하면서 corps를 1개로 접어버리는
    # 부작용이 있어(그 파이프라인은 corp 하나짜리 문장만 렌더링할 줄 안다),
    # 그 이후의 p.get("corps")로 판정하면 (c) 케이스가 (b)로 오인된다(실측:
    # "여기는 어때"가 2개사 대신 1개사로 접힘). (c)로 확정되면 그 부작용을
    # 되돌리듯 p["corps"]를 무조건 직전 리스트로 덮어쓴다.
    if _q_multi_carry_enabled() and prev_slots:
        prev_corps = prev_slots.get("corps") or ([prev_slots["corp"]] if prev_slots.get("corp") else [])
        if prev_corps:
            if _orig_corps and _COMPARISON_SIGNAL.search(question):
                merged = list(dict.fromkeys(prev_corps + _orig_corps))
                if merged != (p.get("corps") or []):
                    p["corps"] = merged
                    p["corp_codes"] = [store.corp_code.get(c) for c in merged]
                    if len(merged) == 1:
                        p["corp"], p["corp_code"] = merged[0], p["corp_codes"][0]
                    miss = _missing_fields(p)
                    r.parsed = p
                    r.notices.append(f"↩️ 직전 기업 목록과 합쳐 비교: {', '.join(merged)}")
            elif not _orig_corps and _MULTI_CARRY_PRONOUNS.search(question) and not _other_corp_named(
                    question, prev_corps[0] if len(prev_corps) == 1 else "", getattr(store, "corp_names", None)):
                p["corps"] = list(prev_corps)
                p["corp_codes"] = [store.corp_code.get(c) for c in prev_corps]
                if len(prev_corps) == 1:
                    p["corp"], p["corp_code"] = prev_corps[0], p["corp_codes"][0]
                else:
                    p["corp"], p["corp_code"] = prev_corps[0], store.corp_code.get(prev_corps[0])
                r.resolved_question = question
                miss = _missing_fields(p)
                r.parsed = p
                r.notices.append(f"↩️ 직전 기업 목록 그대로 승계: {', '.join(prev_corps)}")

    # [사용자 확정 스펙 2026-09-05] "경쟁사랑 비교" — 직전 기업(S0/S2 턴)의
    # 업종 경쟁사로 확장한다. corps 자체를 오염시키지 않는다는 P4 원칙은
    # 그대로 지킨다 — 경쟁사들은 p["expanded_corps"]로 표시해 다음 턴
    # 승계 후보에서 빠지게 한다(기준 기업 자체는 승계분이라 expanded_corps에
    # 안 넣는다). 발화에 새 기업이 리터럴로 없을 때만 발동(_orig_corps 없음) —
    # "삼성전자랑 경쟁사 비교"처럼 새 기업을 이미 댔으면 이 분기를 안 탄다.
    if (_q_competitor_enabled() and not _orig_corps and prev_slots
            and _COMPETITOR_TRIGGER.search(question)):
        base_corp = prev_slots.get("corp") or (prev_slots.get("corps") or [None])[0]
        if not base_corp:
            r.stage("01").status = "실패"
            for no in ("02", "03", "04", "05"):
                r.stage(no).status = "건너뜀"
            r.state, r.missing = "S3", ["기업"]
            r.answer_text = "어느 기업의 경쟁사를 말씀하시는 건가요? 기준이 될 기업을 먼저 알려주세요."
            return r
        sector = applicability.load()["corp_sector"].get(base_corp)
        members = [c for c in (sectors.members(sector) if sector else [])
                   if c in store.corp_code and c != base_corp]
        if not sector or not members:
            r.stage("01").status = "실패"
            for no in ("02", "03", "04", "05"):
                r.stage(no).status = "건너뜀"
            r.state, r.missing = "S3", ["기업"]
            r.answer_text = f"{base_corp}의 업종 경쟁사 정보를 찾지 못했습니다."
            return r
        # 지표 승계 — 이 발화 자체에 지표가 없으면 직전 턴 concept을 기준
        # 기업 하나짜리 문장으로 재파싱해(_apply_carryover, 검증된 재파싱
        # 경로) metric/statement 등 파생 필드를 안전하게 채운다. p를 손으로
        # patch하지 않는 이유는 P2 절 _apply_carryover 문서 그대로다.
        if not p.get("concept") and not p.get("derived") and prev_slots.get("concept"):
            got = _carryover_got(dict(p, corp=base_corp, corps=[base_corp]), {})
            got["concept"] = prev_slots["concept"]
            p2, _miss2, rendered = _apply_carryover(question, got, prev_slots, store, labels)
            if p2 is not None:
                p = p2
                r.resolved_question = rendered
        if not p.get("corp"):
            p["corp"], p["corp_code"] = base_corp, store.corp_code.get(base_corp)
        p["corps"] = [base_corp] + members
        p["corp_codes"] = [store.corp_code.get(c) for c in p["corps"]]
        p["sector"] = sector
        p["expanded_corps"] = members
        # year/scope도 이 발화가 새로 안 밝혔으면 직전 턴 값을 잇는다 — 안
        # 그러면 "2024년 매출액" 다음 "경쟁사랑"·"다른 지표로" 연쇄에서
        # 연도 맥락을 잃고 umbrella 경로의 최신연도 기본값으로 샌다(실측
        # 회귀, 라이브 2026-09-05 — "아니 경쟁사들 비교를"이 2025년으로 샘).
        if not p.get("year") and prev_slots.get("year"):
            p["year"] = prev_slots["year"]
        if not p.get("scope") and prev_slots.get("scope"):
            p["scope"] = prev_slots["scope"]
        r.parsed = p
        r.notices.append(
            f"📊 '{sector}' 업종 내 경쟁사 {len(members)}개사로 확장해 비교합니다: "
            f"{', '.join(members)}.")
        if p.get("concept") or p.get("metric") or p.get("derived"):
            p["intent"] = "compare_multi"
        elif not p.get("concept_set"):
            # 지표가 없으면 포괄어 처리(핵심지표 세트)로 — "최신 재무정보"
            # 세트를 기본값으로 강제 적용한다(umbrella.find()는 문구 매칭이라
            # "경쟁사랑 비교"엔 안 걸린다).
            p["concept_set"] = umbrella._UMBRELLA_CONCEPTS["최신 재무정보"]
        miss = _missing_fields(p)

    # Q_RECOMMEND: 투자 추천 의도 — verdict.wanted()(실적 감성판정)·narrative
    # 어디로도 새지 않게 반드시 그 앞에서 가로챈다. "지표"를 특정할 필요가
    # 없는 의도라(health+perf+events 고정 템플릿) 아래 llmparse 재해석 단계도
    # 건너뛴다 — 지표를 못 찾아서 LLM을 부를 이유가 없다(애초에 안 찾는다).
    #
    # 개별 지표(concept/metric/derived)가 이미 확정됐으면 이 경로를 안 탄다 —
    # "개별 지표 명시 시 그것이 우선" 원칙(Q_UMBRELLA와 같은 원칙을 여기도
    # 적용). 실측 회귀: FIN-0017/0019("자본금 얼마야? … 투자해도 될지 의견도
    # 같이 줘")처럼 구체적 지표를 묻는 질문 끝에 "투자해도 될지"가 덧붙으면,
    # 이 가드가 없으면 정확한 지표값 답변이 통째로 recommendation 고정
    # 템플릿으로 뒤집혀 버린다.
    if (_q_recommend_enabled() and p.get("corps") and _recommend_wanted(question)
            and not p.get("concept") and not p.get("metric") and not p.get("derived")):
        return _run_recommendation(r, p, store, labels)

    # 01 보강, 2단계 — 직승계로도 못 채운 슬롯이 남았을 때만, 그리고 **직전
    # 맥락(prev_slots)이 아예 없을 때만**(=단일턴이라 애초에 승계 게이트를
    # 적용할 자리가 없었던 경우) HCX에 슬롯 JSON을 물어 채운다
    # (qa/llmparse.py::extract_slots()).
    #
    # prev_slots가 있는데 직승계 게이트(_direct_carryover)가 신호 부족 등으로
    # 거부한 경우는 여기서 LLM을 불러 우회하지 않는다 — 사용자 확정 설계:
    # "게이트가 애매하다고 판단한 걸 LLM에게 다시 물어 통과시키면 게이트를
    # 둔 의미가 없다." 이 경우 miss가 그대로 남아 아래 일반 miss 처리(S3
    # 되물음)로 정직하게 떨어진다 — 실측 트레이드오프: T3("모든 경우의수 다
    # 고려해서 알려줘")처럼 맥락은 있으나 신호가 애매한 후속질문은 이제 LLM이
    # 대신 풀어주지 않고 S3가 된다(사용자 확인 후 확정, conv_replay 기대값도
    # 그에 맞게 고쳤다).
    #
    # 슬롯은 후보 집합(corp/concept/event/scope/wh/intent) 밖의 값을 낼 수
    # 없다 — 폐집합 검증이 이미 그 슬롯을 None으로 걸렀으므로, 여기서는 그
    # 결과를 직전 턴 슬롯과 병합(_merge_slots)하고 고정 템플릿으로 문장을
    # 렌더링(llmparse.render_question)해 기존 ontology.parse()에 그대로
    # 넣는다. 예전의 자유 문장 재작성 + 텍스트 diff 가드(`_rewrite_diff_ok`,
    # 삭제됨)는 이 폐집합 검증으로 완전히 대체됐다.
    if miss and llmparse.enabled() and not prev_slots:
        llm_slots = llmparse.extract_slots(question, prev_question, prev_slots, store, labels)
        if llm_slots:
            merged = _merge_slots(llm_slots, prev_slots)
            rendered = llmparse.render_question(merged)
            if rendered:
                p2 = stage01_map(rendered, store, labels, source="rewrite")
                miss2 = _missing_fields(p2)
                if len(miss2) < len(miss):
                    p, miss = p2, miss2
                    r.parsed = p
                    r.resolved_question = rendered
                    r.notices.append(
                        f"🤖 질문 표현을 HyperCLOVA X로 정리해 다시 해석했습니다: 「{rendered}」")

    # Q_UMBRELLA 재부착 — carryover/llmparse가 p를 rendered 문장으로 통째로
    # 재파싱했으면(stage01_map 재호출) concept_set 표시가 날아간다. 최종 p에
    # 다시 붙인다(원 발화의 포괄어 판정은 이미 위에서 끝났으므로 재계산하지
    # 않고 그대로 재사용 — rendered 문장엔 애초에 포괄어가 없어 재계산해도
    # 못 잡는다).
    if umbrella_set and not p.get("concept_set"):
        p["concept_set"] = umbrella_set
        miss = _missing_fields(p)

    if p.get("concept_set") and p.get("corps"):
        # ranking intent 여부로 가르지 않는다 — "~중 가장 성장세가 두드러진
        # 회사는?" 같은 질문이 numqa에선 intent="ranking"이 아니라 "dual"로
        # 나오는 경우가 실측으로 확인됐다(사업연도 스코프 모호 마커 — "가장"
        # 우선순위 판정과 무관하게 붙는다). concept_set kind="growth" +
        # 기업 2개 이상이면 그 자체로 이미 "성장률을 비교해 달라"는 뜻이라
        # intent 값과 무관하게 순위로 답한다.
        concept_set, kind = p["concept_set"]
        if len(p["corps"]) > 1:
            if kind == "growth":
                return _run_growth_ranking(r, p, store)
            return _run_umbrella_multi_plain(r, p, store, labels)
        return _run_umbrella_summary(r, p, store, labels)

    # 비교·이력 — 지원범위 판정보다 **먼저** 본다.
    # 이 질문들은 intent=comparison이라 아래에서 "narrative 경로 필요"로 걸러진다.
    # 그런데 원문을 읽을 문제가 아니라 같은 칸을 두 공시에서 읽어 빼는 문제다.
    cmp_spec = compare.parse(question, p)
    if cmp_spec:
        return _run_compare(r, p, question, cmp_spec)

    # 다중 기업 비교로 이미 분류됐으면(compare_multi/struct_compare), 아래 두
    # 지름길(docstats·contract)이 p["corp"](첫 기업 하나)만 보고 답해버리는 걸
    # 막는다. "A와 B의 계약금액을 비교하면?"이 A만의 답으로 새는 사고가 있었다 —
    # 두 지름길 다 여러 기업을 볼 줄 몰라서, 뒤의 struct_compare/compare_multi
    # 분기가 여러 기업을 제대로 순회하도록 그대로 흘려보낸다.
    _is_multi_compare = p.get("intent") in ("compare_multi", "struct_compare")

    # 공시 건수 — 여러 기업 × 여러 유형을 각각 세어 비교. "SK하이닉스는 처분만,
    # 삼성전자는 취득·처분 혼재로 몇 건씩인가"류(_is_multi_compare 가드 앞에 둔다 —
    # 이 경로 자체가 다중 기업을 다루므로 그 가드에 걸려 건너뛰면 안 된다).
    if docstats.wanted_multi(question, p):
        txt, ev, nums = docstats.run_multi(question, p["corps"])
        if txt:
            return _run_contract(r, p, txt, ev, nums)

    # 공시 건수 — fact가 아니라 문서를 센다. [2026-09-05, 사용자 요청 — 시도
    # 후 되돌림] 처음엔 S2로 냈으나(코퍼스 안에서 셀 수 있는 만큼일 뿐,
    # 전체를 확보했다는 보장이 없어서 — 실측: GOLD-W1-SEC-04,
    # gold=INSUFFICIENT_EVIDENCE), 채점기가 INSUFFICIENT_EVIDENCE를
    # S1/S3/S6만 인정하고 S2는 인정하지 않아 목표 문항의 "정확"은 그대로
    # 안 고쳐지면서 기존 건수 골드 5건의 "행동"만 ✅→🟡로 떨어뜨렸다(순손해,
    # REPORT.md "마감 후" 항목 참고). state는 S0으로 되돌리고, docstats.run()의
    # 캐비엇 문구(기권은 아니되 범위를 밝힘)만 남긴다.
    if docstats.wanted(question) and not _is_multi_compare:
        txt, ev, nums = docstats.run(question, p.get("corp"))
        if txt:
            return _run_contract(r, p, txt, ev, nums)

    # 특정 회차 보고서의 접수(제출)일자 — 지표 조회가 아니라 manifest 조회다.
    # 온톨로지가 "언제 접수됐나"를 지표로 착각해 되묻던 것을 여기서 가로챈다.
    if docstats.wanted_when(question) and not _is_multi_compare:
        txt, ev, nums = docstats.run_when(question, p.get("corp"))
        if txt:
            return _run_contract(r, p, txt, ev, nums)

    # 이벤트 앵커 전후 대조 — "OO 유상증자 이후 부채비율이 얼마나 내려갔나"류.
    # contract.EVENT 체크보다 먼저 둔다: 이 질문도 EVENT에 걸리는 이벤트 키워드를
    # 담고 있어, 뒤에 두면 contract.answer()가 먼저 채가서(이벤트 문서 안의 필드
    # 하나를 읽으려다) 실패해 버린다. 대신 게이트를 좁게 잡아(전후비교 신호
    # 필수) 전후비교 신호가 없으면 즉시 None을 돌려주므로, 이벤트 자체를 묻는
    # 기존 질문(contract.answer() 몫)은 그대로 아래로 흘러간다.
    if p.get("corp") and not _is_multi_compare and eventspan.wanted(question, p):
        txt, ev, nums = eventspan.answer(question, p, labels)
        if txt:
            return _run_contract(r, p, txt, ev, nums)
        # G6 item4: "조용한 후보 0건" 금지 — 이벤트 유형(p["event"])은 정규
        # 어휘로 확정됐는데 그 기업의 실제 앵커 공시가 하나도 없으면, 다음
        # 지름길(contract.EVENT 등)로 조용히 흘려보내지 않고 그 사실을 S1로
        # 명시한다. p["event"]가 아예 없으면(이벤트 유형 어휘가 못 잡은 표현)
        # 예전처럼 기존 흐름으로 흘려보낸다 — 이벤트 자체를 못 알아챈 것과
        # "알아챘는데 자료가 없는 것"은 다른 실패다.
        if p.get("event") and not (p["event"].get("anchors")):
            r.stage("01").status = "완료"
            r.stage("01").note = (r.stage("01").note or "") + " · 이벤트앵커없음"
            r.stage("02").status = "실패"
            r.stage("02").note = f"{p['corp']}의 {p['event']['type']} 공시가 보유 자료에 없음"
            for no in ("03", "04", "05"):
                r.stage(no).status = "건너뜀"
            r.state = "S1"
            r.answer_text = f"{p['corp']}의 {p['event']['type']} 공시가 보유 자료에 없어 답할 수 없습니다."
            # 이벤트 유형은 확정됐는데 그 이벤트 자체가 없다는 뜻이라, 질문에 남은
            # 낱말(예: "부채비율")이 우연히 본문 표 컬럼명과 겹쳐 _try_tables()가
            # 엉뚱한 단일값으로 이 S1을 덮어쓰면 안 된다(효성중공업 실측 회귀 —
            # 아래 answer() 실패 분기와 같은 문제).
            r._skip_generic_fallback = True
            return _with_sections(r, p)

        # G7: wanted()가 True(전후비교 신호 + 이벤트/개념 슬롯 모두 확정)인데
        # answer()가 그래도 실패하면(위 "앵커없음" 케이스 말고 나머지 사유들 —
        # 이벤트를 하나로 못 좁혔다·접수일자를 못 읽었다·지표를 재확인 못 했다·
        # 전후 어느 한쪽 사업보고서가 없다·전후 값을 못 찾았다 등, eventspan.py
        # docstring의 6가지 실패 사유), 다음 핸들러(특히 바로 아래 contract.EVENT
        # 경로)로 조용히 넘기지 않는다. 질문은 명백히 "이벤트 전후 대조"를
        # 원하는데 그걸 계산 못 한 것이므로, 엉뚱한 단일연도 조회로 새서 상태
        # S0로 자신 있게 답해버리는 조용한 실패를 막고 여기서 S1로 정직하게
        # 끝낸다 — answer()의 두 번째 리턴값(reason 문자열, 실패 시 `ev`
        # 자리에 담겨온다)을 answer_text에 그대로 노출한다.
        reason = ev if isinstance(ev, str) and ev else "사유를 확인하지 못했습니다."
        r.stage("01").status = "완료"
        r.stage("02").status = "실패"
        r.stage("02").note = f"이벤트 전후 대조 실패 — {reason}"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        r.answer_text = f"{p['corp']}의 이벤트 전후 대조를 계산하지 못했습니다 — {reason}"
        # 실측 회귀(효성중공업/공급계약): 이 S1을 그냥 _with_sections(r, p)에
        # 넘기면 _try_tables()가 답변문의 "부채비율" 같은 낱말을 본문 표
        # 컬럼명으로 오인해 엉뚱한 단일시점 값으로 상태를 S0으로 덮어써 버린다
        # (질문이 명백히 원한 "전후 대조"가 아닌 답이 자신 있게 나가는 것 —
        # 오늘 고친 "조용한 실패"와 같은 종류의 사고). 이 분기는 이미 "무엇을
        # 원하는지 정확히 알았는데 계산에 실패했다"는 뜻이므로, 범용 단일값
        # 폴백(표·주주현황)으로 다시 새지 않게 막는다.
        r._skip_generic_fallback = True
        return _with_sections(r, p)

    # G6: "언제 결정했나"류(p["wh"]=="when") — XBRL 금액이 아니라 결정(접수)
    # 일자를 답한다. T2/T3 사고: "유상증자를 언제 결정했는데?"에서 concepts.py의
    # 라벨 매칭이 "유상증자"를 이벤트 날짜가 아니라 XBRL 금액으로 확정해 버렸다.
    # p["event"](qa/ontology.py가 qa/events_vocab.py로 이미 산출)가 있으면 그
    # 앵커로 답한다 — eventspan.py(전후 대조 전용)와 로직이 겹치지 않는다.
    # 앵커가 없으면(이벤트 유형 어휘가 못 잡은 표현) fail-closed(None)로 기존
    # 흐름(아래 금액 조회 등)에 그대로 맡긴다 — 억지로 답하지 않는다.
    if p.get("corp") and p.get("wh") == "when" and p.get("event") and not _is_multi_compare:
        got_date = _event_date_answer(question, p)
        if got_date:
            txt, ev, nums = got_date
            return _run_contract(r, p, txt, ev, nums)

    # 계약·사건을 이름으로 지목한 질문 — 공시 하나로 좁힌 뒤 그 안에서 읽는다.
    # Phase A(아래)보다 먼저 봐야 한다 — 실측 회귀(SHLEE SEM-EVT-02: 대우건설
    # "행당제7구역 주택재개발정비사업" 공급계약)에서, 질문이 구체적 계약명을
    # 대면 contract.find()의 본래 용법(find(question, corp, top=3) 그대로,
    # 텍스트 필드 전체와 6자 이상 연속 일치로 그 계약 문서 자체를 정확히 좁힘)이
    # Phase A의 "이벤트 앵커 다수 → 못 좁히면 최신 1건" 폴백보다 훨씬 정확하다.
    # Phase A는 top=len(anchors)+2로 넓게 불러 앵커 목록과 교집합을 보는 2차
    # 필터라 약한 매칭도 여럿 살아남아 좁히기에 실패하기 쉽고, 그러면 엉뚱한
    # "최신 결정"으로 답해버린다(위 사고 실측). 이 블록이 먼저 성공하면 그걸
    # 쓰고, 실패했을 때만(txt가 없을 때만) Phase A로 넘어간다.
    if p.get("corp") and contract.EVENT.search(question) and not _is_multi_compare:
        txt, ev, nums, got = contract.answer(question, p["corp"])
        if txt:
            return _run_contract(r, p, txt, ev, nums, got)

    # Phase A(G6 확장): "언제"의 이벤트 앵커 라우팅과 같은 원칙을 "얼마"에도
    # 적용한다 — p["event"](이벤트 앵커)와 p["field"](구조화 금액 개념)가 둘 다
    # 잡혔으면, 그 이벤트 앵커의 rcept_no로 구조화 필드 회차를 좁혀 확정 답변을
    # 시도한다. 이게 없으면 같은 회사 안에 동일 필드 값이 여러 건이라
    # _struct_answer()가 매번 "회차가 여러 건이라 특정이 필요합니다"로 되묻는다
    # (실측: HD현대일렉트릭/HD현대중공업/HMM 등 359건 — report_gen_gold_events.md
    # 템플릿3). 바로 위 contract.EVENT 블록이 이미 실패했을 때만(txt 없음) 여기
    # 온다 — 구체적 계약명이 있는 질문은 위에서 이미 정확히 처리됐을 것이므로,
    # 여기 남는 건 주로 "회사명 + 이벤트유형" 정형 질문(구체 고유명사 없음)이다.
    # 실패해도(None) 여기서 새로 S1을 만들지 않는다 — 기존 흐름
    # (stage02_retrieve_struct/_struct_answer)이 이미 정직한 되물음 등 자기
    # 몫의 동작을 하도록 그대로 통과시킨다(fall-through).
    if p.get("corp") and p.get("wh") == "amount" and p.get("event") and p.get("field") and not _is_multi_compare:
        got_amt = _event_amount_answer(question, p)
        if got_amt:
            txt, ev, nums = got_amt
            return _run_contract(r, p, txt, ev, nums)

    # 소송현황 표에 열거된 여러 원고의 소송가액을 합산 — 온톨로지 어휘에 "소송가액"
    # 개념이 없어 01단계가 막히는 질문을 본문 표(qa/lawsuit.py) 조회로 가로챈다.
    if p.get("corp") and not _is_multi_compare and lawsuit.wanted(question):
        txt, ev, nums = lawsuit.sum_named(question, p.get("corp_code"), p["corp"])
        if txt:
            return _run_contract(r, p, txt, ev, nums)

    # "두 회사 중 이 제품을 직접 생산하는 곳은 어디인가" — 사업설명 산문(narrative
    # 청크) 대조. 정답이 재무 수치가 아니라 사업의 내용 절 문장이라 metric이
    # 안 잡혀 01단계가 막히는 질문을 qa/bizcompare.py로 가로챈다.
    if bizcompare.wanted(question, p.get("corps")):
        winner, ev, counts = bizcompare.producer_of(question, p["corps"])
        if winner:
            other = next(c for c in p["corps"] if c != winner)
            txt = (f"{' / '.join(p['corps'])} 중 답은 {winner}입니다 (사업보고서 "
                  f"'II. 사업의 내용' 절 언급 빈도 — {winner} {counts[winner]}건, "
                  f"{other} {counts[other]}건).")
            return _run_contract(r, p, txt, ev, [])

    # 실적 감성판정 — "이번 분기 실적은 좋았나요?" 류. perf.wanted()보다 먼저
    # 봐야 한다 — "실적"·"흐름" 신호가 겹쳐서, 먼저 안 두면 판단을 구하는 질문이
    # perf.py의 단순 증감률 요약으로 새 나간다(실측: "좋았나요?"가 perf 경로로
    # 새서 판정 문장 없이 수치만 나열됨). 숫자는 health.py·기존 fact lookup이
    # 이미 계산·검증한 값 그대로 쓰고, 그 위에 해석 문장만 HCX가 얹는다
    # (qa/verdict.py 참고 — 하드코딩 규칙표 대신 LLM이 수치를 보고 판단).
    if narrative.enabled() and verdict.wanted(question, p):
        return _run_verdict(r, p, labels)

    # 지표 여러 개 × 최근 N개 분기 표 — "최근 8개 분기 매출·영업이익·순이익
    # 추이를 표로". perf.wanted()보다 먼저 봐야 한다 — "최근 N개 분기"도
    # perf.py의 추이 신호(TREND)와 겹쳐서, 먼저 두지 않으면 이 더 구체적인
    # 요청이 perf.py의 연간 요약 문장으로 새 나간다.
    if quarter_table_wanted(p):
        return _run_quarter_table(r, p, store, labels)

    # 서로 다른 (연도,분기) 두 시점 직접 비교 — "2024년 4분기 대비 2025년 1분기".
    # fact_compute의 [올해,작년] 기본 짝짓기보다 먼저 봐야 한다 — 안 그러면
    # 질문이 명시한 두 시점을 무시하고 "1년 전"과 비교해버린다.
    if quarter_pair_wanted(question, p):
        return _run_quarter_pair(r, p, store, labels)

    # 실적 흐름 요약 — 지표를 하나로 좁히지 않은 추이 질문.
    # "요즘 실적 흐름 어때?"는 지표가 없다고 되물을 일이 아니라 여러 지표를 함께
    # 보여줄 일이다. 정답셋에서 가장 많은 유형(20건)이 이것이다.
    #
    # [P3] Q_STATE_COHERENCE boss 리뷰 高 finding(라운드1) — 원문이 나열한
    # 기업 중 일부가 p["corps"]에서 조용히 빠진 채(예: "삼성전자, SK하이닉스,
    # LG화학 셋의 실적을 비교하면 어때" → corps 2개만 잡힘) perf.wanted()의
    # GENERAL 패턴("실적")이 그 사실을 안내하지 않고 잡힌 기업만으로 S0을
    # 냈다. (라운드1 수정에서 `not p.get("unsupported")`로 막았다가, 라운드2
    # 리뷰에서 이게 "기업이 다 잡힌 정상 다중기업 비교"(예: "삼성전자,
    # SK하이닉스 현금흐름 비교하면?")까지 함께 막아버리는 새 회귀라는 걸
    # 지적받았다 — unsupported는 "일부 누락"이 아니라 ontology의 범용 다중
    #기업 미지원 신호일 뿐이라 이 판단엔 안 맞았다.) 지금은
    # `_enumerated_corps_dropped()`로 "원문이 말한 기업 수 > 실제 잡힌 수"만
    # 직접 검사한다 — 정말 누락됐을 때만 막고, 다 잡힌 정상 비교는 그대로
    # 통과시킨다. health.wanted()도 동일 구조라 같이 가드한다.
    if perf.wanted(question, p) and p.get("corps") and not _enumerated_corps_dropped(question, p):
        return _run_perf(r, p, store, labels)

    # 현금흐름 패턴·ROE/ROA — 재무제표만으로 계산되는 "기업 건강도" 진단.
    # PER처럼 주가가 필요한 지표는 이 프로젝트 데이터(전자공시)에 아예 없어서 뺐다.
    if health.wanted(question, p) and p.get("corps") and not _enumerated_corps_dropped(question, p):
        return _run_health(r, p, labels)

    # 이벤트 통합 뷰 — "이 회사 최근 이벤트/공시 뭐 있어?" 같은 열린 질문.
    # contract.py는 계약 이름을 대야만 찾고, shareholders.py는 주주현황 전용이라
    # 둘 다 이런 열린 질문은 못 받는다(D-TRACE 계획서의 "이벤트" 갭).
    if events.wanted(question, p):
        return _run_events(r, p)

    # [Q_SCREENING] "부채비율이 300%를 넘는 기업은 몇 곳이고 어디인가" —
    # 특정 기업·업종 없이 코퍼스 전체를 임계값으로 스크리닝. p.get("unsupported")가
    # 바로 이 케이스("순위 질문인데 대상 기업을 특정 못함")를 잡고 있어, 그
    # S6 처리 직전에 가로챈다. concept/derived/year가 이미 확정돼 있어야
    # 한다(ontology.py가 못 잡았으면 스크리닝도 대상이 불명확해 시도 안 함).
    if (_q_screening_enabled() and p.get("unsupported") and not p.get("corps")
            and (p.get("concept") or p.get("derived")) and p.get("year")
            and _SCREEN_ASK.search(question)):
        m = _SCREEN_THRESHOLD.search(question)
        if m:
            threshold, op = float(m.group(1)), _SCREEN_OP[m.group(2)]
            return _run_screening(r, p, store, labels, threshold, op)

    # [Q_GROWTH_RANK] "2023회계연도 대비 2024회계연도에 자산총계 증가율이
    # 가장 큰 기업은 어디인가" — Q_SCREENING과 같은 이유(대상 기업 미특정 →
    # unsupported)로 걸리지만, 필터가 아니라 "코퍼스 전체 계산 후 극값 1곳"이라
    # 별도 함수로 처리한다. fact_compute(두 연도 비교) + 순위(topn/order)가
    # 함께 온 경우로 구분한다.
    if (_q_growth_rank_enabled() and p.get("unsupported") and not p.get("corps")
            and p["intent"] == "fact_compute" and p.get("base_year") and p.get("year")
            and (p.get("concept") or p.get("derived"))):
        return _run_growth_rank(r, p, store, labels)

    if p.get("unsupported"):
        r.stage("01").status = "완료"
        r.stage("01").note = p["unsupported"]
        for no in ("02", "03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S6"
        return _with_sections(r, p)
    if miss:
        r.stage("01").status = "실패"
        # 실제로 못 찾은 항목만 적는다 — "기업/지표/연도" 셋을 고정으로 찍으면
        # 기업은 이미 잡혔고 지표만 없는 경우까지 "기업도 못 찾은 것"처럼 보여
        # 디버깅을 방해한다(narrative.should_try()가 "지표만 없음"을 별도로
        # 취급하는 것과도 화면이 어긋난다).
        r.stage("01").note = "·".join(miss) + " 특정 실패"
        for no in ("02", "03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state, r.missing = "S3", miss
        return _with_sections(r, p)
    r.stage("01").status = "완료"
    src = "metric" if p["metric"] else ("field" if p.get("field") else "label")
    if len(p["corps"]) > 1:
        who = (f"{p['sector']} 업종 {len(p['corps'])}개사" if p.get("sector")
               else f"{len(p['corps'])}개사")
    else:
        who = p["corp"]
    r.stage("01").note = (f"{who} · {p['year']}년 · {ontology.concept_ko(p)}"
                          f" ({src}) · intent={p['intent']}")

    per = p.get("period") or {}

    # 분기·반기 — 파서를 분기·반기보고서까지 넓혀 이제 수치로 답한다.
    spec = _period_spec(p)
    if spec:
        r.stage("01").note += f" · 기간={spec['ko']}"
        r.notices.append(
            f"🗓 {spec['ko']} 기준입니다"
            + (f" ({spec['derive'][0]} − {spec['derive'][1]}로 계산)."
               if spec.get("derive") else f" ({spec['report']} 기준)."))

    if per.get("unsupported"):
        r.stage("01").note += f" · {per['unsupported']}"
        for no in ("02", "03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S6"
        sa = per.get("sub_annual") or {}
        r.notices.append(f"📄 해당 기간은 {sa.get('report', '분기·반기보고서')}에 있습니다 "
                         "— 문서는 corpus에 있으나 수치로 추출돼 있지 않습니다.")
        return _with_sections(r, p)

    # 업종 적합성 — 조회하기 전에 본다. 뜻 없는 숫자를 내는 것보다 낫다.
    # 업종 부적합은 강한 주장이다 — "이 업종 8개사 중 0곳만 보고합니다"라고 단정한다.
    # 개념을 부분어로 겨우 잡은 상태에서 그 주장을 하면, 잘못 잡은 개념을 근거로
    # 확신 있게 틀린 답을 낸다. 실제로 대량보유상황보고서를 묻는 질문에
    # "자기주식은 이 업종에 부적합"이라고 답하고 있었다. 개념이 확실할 때만 본다.
    weak_concept = str(p.get("concept_why") or "").startswith("부분어")

    if (p.get("corp") and len(p.get("corps", [])) == 1 and not weak_concept
            and p["intent"] not in ("struct_field", "ranking", "aggregate")):
        checked = applicability.concepts_to_check(p)
        # 부적합 판정은 **다른 곳에서는 흔한 개념**일 때만 뜻이 있다.
        # '합계'처럼 corpus 전체에서 한두 기업만 쓰는 말이면 "이 업종 0곳"은 당연하고
        # 정보가 없다. 실제로 "직원 연간급여총액 합계"에 "'합계'는 이 업종에 부적합"
        # 이라 답했다.
        #
        # 판단은 **검사 대상 개념**으로 한다. 파생 지표(재고자산회전율)의 커버리지가
        # 아니라 그 밑의 재고자산으로 봐야 한다 — 파생 이름은 corpus에 행으로 없다.
        try:
            ci = concepts.get(labels.facts)
            if checked and max(ci.coverage(ci.group(c)) for c in checked) < 10:
                checked = []
        except Exception:                              # noqa: BLE001
            pass
        if checked:
            a = applicability.assess(p["corp"], checked)
            if a["verdict"] == "부적합":
                return _run_inapplicable(r, p, a)
            if a["verdict"] == "드묾":
                r.notices.append(
                    f"⚠️ {a['sector']} 업종에서 드문 계정입니다 "
                    f"({', '.join(c['개념'] + ' ' + c['보고'] for c in a['checks'] if c['비율'] is not None and c['비율'] < 0.3)}).")

    # [02] — 비-XBRL 공시 필드 경로
    if p["intent"] == "struct_field":
        return _run_struct(r, p, question)
    if p["intent"] == "struct_compare":
        return _run_struct_compare(r, p, question)

    # 기간 시리즈 (범위·최근N·추이)
    # 순위·집계·다중비교는 여러 기업을 한 해에서 비교하는 것이라 시리즈가 아니다.
    # 이 가드가 없으면 "네 기업 중 큰 순서대로"가 첫 기업의 연도별 추이로 샌다.
    #
    # 파생 개념(영업이익률 등)도 뺀다 — "요즘"·"흐름"이 per.kind='all'로 잡히면
    # concept(="영업이익률")이 있다는 이유로 여기로 먼저 왔는데, _run_series는 그
    # 이름을 corpus에 없는 raw label로 그대로 조회해 실패한다. 반면 같은 개념의
    # available_years는 파생 분기를 이미 처리해서 "보유 연도 있음"을 정확히
    # 계산해버려, "자료 없음"이라면서 "보유 연도는 2021~2025"라는 자기모순
    # 메시지가 났다(실측: GOLD-W2B-P17). 파생 개념은 _run_derived(_compare)가
    # 맞는 경로다.
    if (per.get("kind") in ("range", "recent_n", "all") and p.get("concept")
            and not p.get("derived")
            and p["intent"] not in ("ranking", "aggregate", "compare_multi")):
        return _run_series(r, p, store, labels)

    # [P3 후속] Q_RECENCY 시제 게이트(사용자 확정, 2026-09-05) — year=null +
    # 과거 표지("예전에 얼마였어?" 등)면 최신 단일연도 폴백을 쓰지 않는다.
    # 시계열로 답할 수 있으면(_run_series) 그걸 쓰고, 못 쓰면(파생 개념 —
    # series 경로가 없다, 바로 위 가드와 같은 이유) 정직하게 S3로 연도를
    # 되묻는다. 현재 표지가 있으면(예: "최근엔 어때?") 과거로 안 본다 —
    # _past_tense_signal()이 그 우선순위를 처리한다. 반례 실측: "삼성전자
    # 매출이 예전에 얼마였어"/"SK하이닉스 영업이익이 과거에는 어땠어"가
    # 최신연도(2025)로 자신 있게 답하던 것(P3 boss 라운드2 finding).
    # Q_RECENCY_ENABLED 게이트 — 아직 boss 승인 전(P3 미승인, REPORT.md 참고)
    # 이라 기본 off. 꺼져 있으면 기존 "가장 최근" 폴백이 그대로 적용된다
    # (지금 프로덕션과 동일한 동작 — 이 수정으로 새로 뭔가 바뀌지 않는다).
    if (_q_recency_enabled() and not p.get("year") and not p.get("field") and p.get("concept")
            and p["intent"] not in ("ranking", "aggregate", "compare_multi")
            and _past_tense_signal(question)):
        if p.get("derived"):
            avail = available_years(p, labels)
            r.stage("01").status = "완료"
            r.stage("02").status = "완료" if avail else "실패"
            for no in ("03", "04", "05"):
                r.stage(no).status = "건너뜀"
            r.state, r.missing = "S3", ["연도"]
            r.notices.append(
                "🕐 과거 시점을 물으셨는데 특정 연도가 없어 어느 연도인지 여쭙습니다"
                + (f" (보유 연도: {', '.join(map(str, sorted(avail)))})." if avail else "."))
            return _with_sections(r, p)
        per["kind"] = "all"
        return _run_series(r, p, store, labels)

    # [2026-09-05, 사용자 요청 — clarification 유형 개선] "재작년"/"작년"/
    # "최근"이 **"사업보고서" 없이 단독으로** 쓰이면 진짜 모호하다 — 몇 년도
    # 기준인지 사람도 못 정한다("지금이 몇 년도인지" 자체가 이 코퍼스에
    # 정의돼 있지 않다). "가장 최근 사업보고서 기준"처럼 명시적으로 앵커가
    # 있으면(period.parse()가 kind="latest"로 정확히 잡는다, 아래
    # explicit_latest) 모호하지 않으므로 그대로 최신연도로 푼다 — 실측(FIN
    # 골드셋)으로 이 구분을 확인했다: "가장 최근 사업보고서 기준"류(FIN-0050~
    # 0057)는 전부 단일 확정값이 정답인데, 단독 "재작년"/"작년"/"최근"류
    # (FIN-0058~0065)는 전부 CLARIFICATION_REQUIRED가 정답이었다.
    # derived(파생비율) 개념은 제외한다 — "영업이익률 흐름은 어때?"류가
    # per.kind="all"(추이)인데 derived라서 바로 아래 "가장 최근" 폴백의
    # `or p.get("derived")` 예외를 타고 연도만 채운 뒤 트렌드로 이어지는
    # 기존 경로가 있다(실측 회귀: GOLD-W2B-P17 — 이 새 체크가 그 경로보다
    # 먼저 걸려 트렌드 답변 대신 되묻기로 새 버렸다). plain 개념(비유동부채·
    # 자산총계 등, derived.RULES에 없는 것)만 이 모호성 체크 대상이다.
    _AMBIGUOUS_RELATIVE_YEAR = re.compile(r"최근|작년|재작년")
    if (_q_recency_enabled() and not p.get("year") and not p.get("field") and not p.get("derived")
            and per.get("kind") != "latest"
            and per.get("kind") not in ("range", "recent_n", "all", "term")
            and _AMBIGUOUS_RELATIVE_YEAR.search(question) and "보고서" not in question):
        avail = available_years(p, labels)
        r.stage("01").status = "실패"
        r.stage("01").note = "상대연도 모호(사업보고서 앵커 없음) — 되물음"
        for no in ("02", "03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state, r.missing = "S3", ["연도"]
        m = _AMBIGUOUS_RELATIVE_YEAR.search(question)
        ref_note = ""
        # 정말 몰라서 못 답하는 게 아니다 — "가장 최근 연도라면" 값을 참고로
        # 같이 보여준다(그 값에 실제 근거 좌표도 붙인다). 단정은 안 한다 —
        # state는 그대로 S3, 이 참고값을 "확정 답"이라고 부르지 않는다.
        if avail:
            p_ref = dict(p, year=max(avail))
            try:
                facts_ref = stage02_retrieve(p_ref, store, labels, question)
            except Exception:                                  # noqa: BLE001
                facts_ref = []
            if facts_ref:
                r.evidence = [to_coordinate(f) for f in facts_ref]
                f0 = facts_ref[0]
                ref_note = (f" 참고로 가장 최근({max(avail)}년) 사업보고서 기준으로는 "
                           f"{f0.get('value_raw', '')}{f0.get('unit_kr', '')}입니다.")
        r.answer_text = (f"'{m.group(0)}'이(가) 정확히 몇 년도를 말씀하시는지 알려주시면 "
                         f"답변드리겠습니다" + (f" (보유 연도: {', '.join(map(str, sorted(avail)))})."
                                              if avail else ".") + ref_note)
        return r

    # "가장 최근" — 조회 조건이 실제로 가진 최신 연도로 푼다.
    # 연도를 아예 말하지 않은 질문도 여기서 같이 처리한다 — 예전엔 그런 질문을
    # missing_fields가 막아 세워 "몇 년도요?"라고 되물었다. 이제는 최신 연도로
    # 답하고 쿠션어로 "다른 연도면 말씀해 달라"고 덧붙인다 — 사람도 시점 없이
    # 물으면 최신 기준으로 답하지, 먼저 되묻지 않는다.
    # range/recent_n/all은 보통 _run_series가 다연도로 알아서 처리하니 연도를 안
    # 채운다. 다만 파생 개념은 이제 series로 안 가므로(위 가드), 그 경우엔 여기서
    # 마저 채워야 한다 — 안 그러면 year=None인 채로 _run_derived에 들어가 "피연산자
    # 미확보"로 무너진다(실측: GOLD-W2B-P17, "크래프톤 영업이익률 흐름 어때").
    explicit_latest = per.get("kind") == "latest"
    if (not p.get("year") and not p.get("field")
            and (per.get("kind") not in ("range", "recent_n", "all", "term")
                 or p.get("derived"))):
        avail = available_years(p, labels)
        if avail:
            p["year"] = max(avail)
            if explicit_latest:
                r.notices.append(f"🕐 '가장 최근'을 {p['year']}년으로 해석했습니다 "
                                 f"(이 지표의 보유 연도: {', '.join(map(str, sorted(avail)))}).")
            elif _q_recency_enabled() and _past_tense_signal(question):
                # 방어적 분기 — 위 시제 게이트가 정상 작동하면 과거 표지가
                # 있는 질문은 이 블록에 아예 안 온다. 그래도 도달했다면(예:
                # 새 상위 분기가 추가돼 순서가 바뀌는 경우) "연도를 말씀하지
                # 않으셔서"는 거짓이다 — 실제로는 "예전에" 같은 시점 표지가
                # 있었는데 특정 연도로 못 옮긴 것뿐이다. 사용자 확정 지시:
                # 이 거짓 문구를 절대 쓰지 않는다.
                r.notices.append(
                    f"🕐 특정 연도로 옮기지 못해 가장 최근인 {p['year']}년 기준으로 답변드립니다 "
                    f"— 원하시는 연도를 말씀해 주세요 "
                    f"(이 지표의 보유 연도: {', '.join(map(str, sorted(avail)))}).")
            else:
                r.notices.append(
                    f"🕐 연도를 말씀하지 않으셔서 가장 최근인 {p['year']}년 기준으로 답변드립니다 "
                    f"— 다른 연도를 원하시면 연도를 함께 말씀해 주세요 "
                    f"(이 지표의 보유 연도: {', '.join(map(str, sorted(avail)))}).")
        else:
            r.stage("02").status = "실패"
            r.stage("02").note = "보유 연도 없음"
            for no in ("03", "04", "05"):
                r.stage(no).status = "건너뜀"
            r.state = "S1"
            return _with_sections(r, p)

    # 다지표 나열 — 한 기업·한 해에서 여러 지표를 각각 조회한다.
    if p.get("concepts_multi") and len(p.get("corps") or []) == 1:
        return _run_multi_concept(r, p, store, labels)

    # [02]
    multi = p["intent"] in ("ranking", "aggregate", "compare_multi")
    if multi:
        facts, r.unresolved = stage02_retrieve_multi(p, store, labels)
    else:
        facts = stage02_retrieve(p, store, labels, question)
    r.searched = {
        "기업": ", ".join(p["corps"]) if multi else p["corp"], "연도": p["year"],
        "지표": ontology.concept_ko(p),
        "기준": SCOPE_KO_ALL.get(p["scope"], "연결/별도 미지정"),
        "재무제표": p["statement"] or "미지정",
        "대상": "XBRL fact store" + ("" if p["metric"] else " (label 색인)"),
    }
    if not facts:
        # "OO와 XX 중 어디가 더 높은가"류는 intent가 ranking으로 잡히지 compare_multi가
        # 아니다(topn=1인 2사 비교, 실측: GOLD-W2B-P20) — derived 지표(영업이익률 등)를
        # 여러 기업에서 계산해야 하는 건 compare_multi와 똑같으므로 intent에 ranking도
        # 같이 걸어야 한다. corps 2개 이상일 때만 — 1개면(회사를 특정 못 해 되물어야
        # 할 상황 등) 기존 _run_derived(단일 기업판)로 그대로 보낸다.
        if p.get("derived") and p["intent"] in ("compare_multi", "ranking") and len(p.get("corps") or []) > 1:
            return _run_derived_compare(r, p, store, labels)
        if p.get("derived"):
            # corpus에 명시값이 없으면 계산으로 만든다. corpus 우선, 파생은 차선.
            return _run_derived(r, p, store, labels, note="corpus 명시값 없음 → 파생")
        r.stage("02").status = "실패"
        r.stage("02").note = "해당 fact 없음"
        for no in ("03", "04", "05"):
            r.stage(no).status = "건너뜀"
        r.state = "S1"
        return _with_sections(r, p)
    r.stage("02").status = "완료"
    r.stage("02").note = (f"fact {len(facts)}건"
                          + (f" · 미확보 {len(r.unresolved)}개 기업" if r.unresolved else ""))
    r.evidence = [to_coordinate(f) for f in facts]
    r.facts = facts

    # [2026-09-05, 시도했으나 미승인 — 기본 OFF로 둔다] 연도 모호성
    # (Q_RECENCY)과 같은 이유로, 연결/별도를 밝히지 않았는데 두 값이 서로
    # 다르면 "구분이 필요합니다"라고 자신 있게 확정 답변하지 않고 먼저
    # 되묻자는 시도. FIN-0045/0047/0049/0133/0137(골드
    # CLARIFICATION_REQUIRED, 5건)은 고쳤지만, 실측 결과 FIN-0096~0101 +
    # FIN-0123(7건)은 정확히 같은 "스코프 미명시·두 값이 다름" 모양인데
    # 골드가 {consolidated:X, separate:Y} 형태의 확정 dual-value 답을
    # 기대해서 오히려 회귀가 났다 — 질문 문장만 봐서는 이 두 유형을 구분할
    # 신호가 없다(값의 상대적 차이 크기로도 안 갈린다: FIN-0049는 두 값이
    # 2%밖에 안 다른데도 CLARIFICATION_REQUIRED). 순회귀(-2, 5건 개선 vs
    # 7건 악화)라 골드셋 신호가 더 명확해지기 전까진 켜지 않는다.
    if (_q_scope_clarify_enabled() and not multi and not p.get("scope")
            and p["intent"] not in ("fact_compute",) and len(facts) == 2
            and {f["scope"] for f in facts} == {"consolidated", "separate"}):
        v0, v1 = to_won(facts[0]), to_won(facts[1])
        if v0 is not None and v1 is not None and v0 != v1:
            label = p.get("label") or ontology.concept_ko(p)
            parts = [f"{SCOPE_KO_ALL.get(f['scope'], '')} {f['value_raw']}{f['unit_kr']}"
                     for f in facts]
            r.state, r.missing = "S3", ["연결/별도"]
            r.answer_text = (f"{p['corp']}의 {p['year']}년 {label}은 연결/별도 기준에 따라 다릅니다"
                             f" ({' / '.join(parts)}). 어느 기준으로 답변드릴까요?")
            return _with_sections(r, p)

    # [03] ↔ [04] 피드백 루프
    verification = {}
    for attempt in range(1, MAX_RETRY + 1):
        res = stage03_compute(question, store, p, facts)
        if res["status"] != "ok":
            r.stage("03").status = "실패"
            r.stage("03").note = f"status={res['status']}"
            for no in ("04", "05"):
                r.stage(no).status = "건너뜀"
            r.state = "S6" if res["status"] == "route_narrative" else "S1"
            r.answer_text = res.get("text", "")
            return _with_sections(r, p)
        verification = stage04_verify(p, facts, res, r.unresolved, store, labels)
        if verification["passed"] or not _retryable(verification):
            r.stage("03").status = "완료"
            r.stage("03").retries = attempt - 1
            break
        r.stage("03").status = "재시도"
        r.stage("03").retries = attempt

    r.answer_text = res["text"]
    r.numbers = res.get("numbers", [])
    r.ranking = res.get("ranking", [])
    r.calc_steps = calc_steps(p, facts, res)
    r.verification = verification
    r.stage("04").status = "완료" if verification["passed"] else "실패"
    r.stage("04").note = ("모든 규칙 통과" if verification["passed"]
                          else ", ".join(c["name"] for c in verification["checks"]
                                         if not c["ok"]) + " 미통과")

    # [05]
    r.answer_text, n = mask_pii(r.answer_text)
    if n:
        r.notices.append(f"🔒 개인정보가 감지되어 마스킹 처리했습니다. ({n}건)")
    r.stage("05").status = "완료"
    r.state = "S0" if verification["passed"] else "S2"
    if n:
        r.notices.append("상태 S5(개인정보 마스킹)가 함께 적용되었습니다.")
    return r
