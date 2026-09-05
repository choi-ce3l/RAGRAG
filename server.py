#!/usr/bin/env python3
"""공시 QA 에이전트 — 평가용 HTTP API 서버.

대회 제출물 중 하나가 '평가용 API 서버(End-point URL + 요청/응답 JSON 스키마)'다.
평가는 화면이 아니라 API 응답으로만 이뤄지므로, 답변뿐 아니라 **근거 좌표와 검증 상태**가
JSON에 그대로 실리도록 했다.

로직은 새로 짠 것이 없다. `qa.pipeline.run()` 결과를 사람이 읽는 화면(`qa.render`) 대신
기계가 읽는 JSON으로 바꿔 내보내는 껍데기다.

    uvicorn server:app --host 0.0.0.0 --port 8000

  POST /ask     질문 → 답변 · 근거 · 검증
  GET  /health  준비 상태 (색인 로드 완료 여부)
  GET  /docs    자동 생성된 API 명세서 + 브라우저 테스트
"""

import json
import threading
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from qa import evidence_pack, labelstore, pipeline, render

_READY = {"ok": False, "facts": 0, "load_ms": 0}

# [2026-09-05, 대회 API 안전망] pipeline.run() 주위에 예외 처리가 없었다 —
# 우리가 못 본 문항 하나가 예외를 던지면 그 요청은 500으로 죽어 부분점수도
# 못 받는다(대회 타임아웃 300초는 널널해 레이턴시는 문제 아니지만, 크래시는
# 전혀 다른 문제다). 예외를 삼키지는 않는다 — 사용자에겐 안전한 S6로 보이게
# 하되, 원인은 파일로 남겨 나중에 고칠 수 있게 한다.
_ERROR_LOG = Path(__file__).resolve().parent / "data" / "server_errors.jsonl"
_ERROR_ANSWER = "처리 중 오류가 발생해 답변을 생성하지 못했습니다"


def _log_server_error(question, exc):
    try:
        _ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "question": question,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 로깅 자체가 실패해도 사용자 응답 흐름은 막지 않는다


@asynccontextmanager
async def lifespan(app: FastAPI):
    """색인을 서버 시작 때 미리 올린다.

    첫 요청에서 로드하면 11초가 걸려 평가자가 타임아웃을 볼 수 있다.
    로드가 끝나기 전에는 /health 가 ready=false 를 돌려준다.
    """
    t0 = time.time()
    store = pipeline.get_store()
    ls = labelstore.get()
    pipeline.run("워밍업")                      # 경로를 한 번 태워 지연 로딩을 모두 끝낸다
    _READY.update(ok=True, facts=len(ls.facts),
                  corps=len(store.corp_names),
                  load_ms=int((time.time() - t0) * 1000))
    yield


app = FastAPI(
    title="공시 QA 에이전트",
    version="1.0.0",
    description=(
        "공시 데이터에 자연어로 질의하면 답변과 **근거 좌표**(접수번호·재무제표·셀)를 함께 돌려준다.\n\n"
        "수치는 LLM이 만들지 않는다 — 조회는 XBRL fact store에서, 계산은 Decimal로 한다."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 스키마 — 이 정의가 그대로 /openapi.json 명세서가 된다
# ---------------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000,
                          description="자연어 질문",
                          examples=["삼성전자의 2024년 연결 매출액은?"])
    verbose: bool = Field(False, description="계산 과정·파이프라인 단계·검증 상세를 함께 반환")
    prev_question: str | None = Field(None, max_length=1000,
                          description="직전 질문 — 대화 후속 질문 해석에만 쓴다"
                                      "(예: '건설사에서 매출 1위는?' 다음 '반도체 분야에서는?')")
    prev_slots: dict[str, Any] | None = Field(None, description=(
        "직전 턴의 확정 슬롯 — 직전 /ask 응답의 slots 필드를 그대로 돌려보내면 된다. "
        "직전 턴이 S0/S2가 아니었으면 서버가 slots를 안 돌려주므로 자연히 None이 되고, "
        "그럴 땐 그 전 성공턴의 slots를 계속 들고 있다가 보내는 편이 낫다(정정 신호 "
        "'아까 그거 아니고'류가 실패턴 하나 끼어도 맥락을 잃지 않게)."))


class Evidence(BaseModel):
    corp: str = Field(..., description="기업명")
    report: str = Field(..., description="공시 문서명")
    path: str = Field(..., description="문서 내 위치 (재무제표 > 계정)")
    cell: str = Field("", description="셀 위치 또는 필드 코드")
    rcept_no: str = Field(..., description="DART 접수번호")
    fact_id: str = Field(..., description="수치의 고유 식별자")
    flags: list[str] = Field(default_factory=list,
                             description="⚠️정정 · ⛔대체됨 등 근거의 주의 표시")
    value: str = Field("", description="공시 원문에 실제로 적힌 값/문구 그대로의 인용")
    correction_history: list[dict] | None = Field(None, description=(
        "이 근거 값의 정정 이력. 이 rcept_no 이후 같은 사건으로 이어진 정정본 중 "
        "이 값이 실제로 바뀐 건만, 정정된 순서대로 담는다. 각 원소는 "
        "{rcept_no, rcept_dt, report_nm, before, after, before_dec, after_dec}. "
        "정정 이력이 없으면(또는 확인할 수 없으면) null — 빈 배열과 구분한다."))


class AskResponse(BaseModel):
    question: str
    resolved_question: str = Field("", description=(
        "llmparse가 대명사·후속질문을 풀어 쓴 문장(변경 없으면 question과 동일). "
        "다음 요청의 prev_question으로 이 값을 넘겨야 대화가 3턴째부터도 안 끊긴다 — "
        "매번 사용자가 방금 친 원문만 넘기면, 그 원문 자체가 이미 대명사형일 때 "
        "직전 맥락을 못 살린다."))
    state: str = Field(..., description=(
        "S0 정상 · S1 데이터 없음 · S2 검증 미흡 · S3 모호(되물음) · S6 미지원"))
    answer: str = Field("", description=(
        "자연어 답변. 답할 수 없는 경우에도 비우지 않고 그 이유·되물을 항목을 담는다"))
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: str = Field("", description=(
        "교차검증·근거 flags를 옮겨 적은 신뢰도 한 줄 요약 (예: "
        "'✅ 3개 교차검증 통과' · '⚠️ 2/4 검증 실패 — 근거 재확인 권장'). "
        "답을 못 낸 경우(S1/S3/S6)는 빈 문자열."))
    table: dict[str, Any] | None = Field(
        None, description="질문이 표 형태를 요청했을 때만. {columns: [...], rows: [[...], ...]}")
    slots: dict[str, Any] | None = Field(None, description=(
        "이번 턴이 S0/S2로 확정됐을 때만 채워지는 기업/지표/연도/기준 슬롯 "
        "({corp, corps, concept, year, scope} 중 값이 있는 것만). 다음 요청의 "
        "prev_slots로 그대로 돌려보내면 후속 질문(3턴↑ 멀티턴, 정정 신호)이 이 턴의 "
        "확정된 맥락을 이어받는다. S1/S3/S6에서는 항상 null — 실패턴의 슬롯은 "
        "승계 후보가 아니다."))
    elapsed_ms: int
    detail: dict[str, Any] | None = Field(
        None, description="verbose=true일 때만. 계산 과정·단계·검증·해석 결과")


class Health(BaseModel):
    status: str
    ready: bool
    facts: int = 0
    corps: int = 0
    load_ms: int = 0


# ---------------------------------------------------------------------------
def _evidence(c):
    return Evidence(corp=c.get("corp_name") or "", report=c.get("report_nm") or "",
                    path=c.get("path") or "", cell=c.get("cell") or "",
                    rcept_no=c.get("rcept_no") or "", fact_id=c.get("ref_id") or "",
                    flags=c.get("flags") or [], value=c.get("value") or "",
                    correction_history=evidence_pack.correction_info(
                        c.get("rcept_no"), c.get("diff_key")))


def _slots(r):
    """pipeline.run()의 prev_slots 인터페이스가 요구하는 형태로 이번 턴 슬롯을 뽑는다.

    S0/S2에서만 채운다 — pipeline.py 쪽 docstring이 명시하듯 실패턴(S1/S3/S6)의
    슬롯은 승계 후보가 아니다. 이 게이트를 클라이언트가 아니라 여기 한 곳에서만
    판단해서, 클라이언트는 그냥 받은 걸 그대로 돌려보내기만 하면 되게 한다.
    """
    if r.state not in ("S0", "S2"):
        return None
    p = r.parsed or {}
    out = {k: p.get(k) for k in ("corp", "corps", "concept", "year", "scope") if p.get(k)}
    return out or None


def _detail(r):
    p = r.parsed or {}
    return {
        "pipeline": [{"stage": s.no, "name": s.name, "status": s.status, "note": s.note}
                     for s in r.stages],
        "interpretation": {k: p.get(k) for k in
                           ("intent", "corps", "year", "scope", "concept", "sector",
                            "derived", "unsupported", "concept_why")},
        "calculation": r.calc_steps,
        "verification": r.verification,
        "ranking": r.ranking,
        "series": [{"year": y, "value": f.get("value_raw"), "unit": f.get("unit_kr")}
                   for y, f in r.series],
        "cagr_pct": r.cagr,
        "clarification_needed": r.missing,
        "searched_scope": r.searched,
        "suggested_sections": r.sections,
        "notices": r.notices,
    }


@app.post("/ask", response_model=AskResponse, summary="질문에 답한다",
          response_description="답변과 근거 좌표")
def ask(req: AskRequest):
    """자연어 질문 하나를 받아 답변과 근거를 돌려준다.

    답할 수 없는 경우에도 **HTTP 200**이며 `state`로 구분한다 —
    데이터가 없는 것(S1), 질문이 모호한 것(S3), 아직 지원하지 않는 것(S6)은
    서로 다른 상황이고 오류가 아니기 때문이다.
    """
    t0 = time.time()
    try:
        r = pipeline.run(req.question, prev_question=req.prev_question,
                         prev_slots=req.prev_slots)
    except Exception as e:                                    # noqa: BLE001
        _log_server_error(req.question, e)
        return AskResponse(
            question=req.question, resolved_question=req.question,
            state="S6", answer=_ERROR_ANSWER, evidence=[], confidence="",
            table=None, slots=None, elapsed_ms=int((time.time() - t0) * 1000),
            detail=None,
        )
    return AskResponse(
        question=req.question, resolved_question=r.resolved_question or req.question,
        state=r.state, answer=render.summary_text(r),
        evidence=[_evidence(c) for c in r.evidence],
        confidence=r.confidence_summary,
        table=r.table or None,
        slots=_slots(r),
        elapsed_ms=int((time.time() - t0) * 1000),
        detail=_detail(r) if req.verbose else None,
    )


class EvalAnswer(BaseModel):
    question_id: str
    question: str
    retrieved_context: str = Field(..., description="답변 생성에 참고한 검색 문서")
    think_trace: str = Field(..., description="사고·추론·도구 사용 과정")
    answer: str = Field(..., description="최종 생성 답변")


def _retrieved_context(r):
    """대회 스키마의 retrieved_context — 근거(r.evidence)를 사람이 읽을 텍스트로 편다.

    /ask의 evidence는 구조화된 배열이지만 이 스키마는 문자열 한 개를 요구한다 —
    새 근거를 만드는 게 아니라 같은 근거를 다른 그릇에 담는 것뿐이다.
    """
    if not r.evidence:
        return ""
    lines = []
    for e in r.evidence:
        corp = e.get("corp_name") or ""
        report = e.get("report_nm") or ""
        path = e.get("path") or ""
        value = e.get("value") or ""
        rcept = e.get("rcept_no") or ""
        lines.append(f"[{corp} · {report} · 접수번호 {rcept}] {path}: {value}".strip())
    return "\n".join(lines)


def _think_trace(r):
    """대회 스키마의 think_trace — 파이프라인 단계·계산 과정·검증 특이사항을 순서대로.

    새로 추론 로그를 만드는 게 아니라, /ask의 verbose=true detail이 이미 갖고 있는
    r.stages/r.calc_steps/r.notices를 한 줄짜리 텍스트로 직렬화한다.
    """
    lines = []
    for s in r.stages:
        if s.status in ("건너뜀", "대기"):
            continue
        line = f"[{s.no}] {s.name}: {s.status}"
        if s.note:
            line += f" — {s.note}"
        lines.append(line)
    if r.calc_steps:
        lines.append("계산 과정:")
        lines.extend(f"  {c}" for c in r.calc_steps)
    if r.notices:
        lines.extend(r.notices)
    return "\n".join(lines)


# [2026-09-05, 대회 멀티턴 대비] `/answer` 스키마엔 prev_question/prev_slots를
# 실어 보낼 필드가 없다 — 대회 쪽이 세션을 명시적으로 넘길 방법 자체가 없다.
# 그런데 문항이 "하나씩 sequential하게" 들어온다고 했고, 우리 골드셋에도
# "그럼 부채총계는?" 같이 직전 문항에 이어지는 후속질문이 실제로 있다
# (FIN-0124~0126) — 즉 대회 문항도 그런 후속질문을 섞어 낼 가능성이 있다.
# /ask처럼 클라이언트가 매번 넘겨줄 수 없으니, 서버가 직전 성공턴(S0/S2)의
# question·slots를 프로세스 전역에 들고 있다가 다음 호출에 자동으로 물려준다.
# /ask에는 이 자동 승계를 넣지 않는다 — 거긴 호출자가 자기 대화의
# prev_slots를 명시적으로 관리하므로, 전역 상태를 섞으면 동시 접속한
# 서로 다른 대화끼리 맥락이 오염된다. /answer는 애초에 세션 개념이 없어
# "직전 호출 = 같은 흐름"으로 가정하는 것 말고는 대안이 없다.
_last_turn_lock = threading.Lock()
_last_turn = {"question": None, "slots": None}


@app.get("/answer", response_model=EvalAnswer, summary="평가용 API — 대회 지정 스키마",
         response_description="question_id·retrieved_context·think_trace·answer")
def answer_eval(question_id: str, question: str):
    """대회 평가용 엔드포인트. `GET /answer?question_id=...&question=...` 고정 스키마.

    로직은 `/ask`와 완전히 같다 — `pipeline.run()` 결과를 대회가 요구하는 필드
    이름({question_id, question, retrieved_context, think_trace, answer})으로
    다시 포장할 뿐, 별도 파이프라인이 아니다.
    """
    with _last_turn_lock:
        prev_question, prev_slots = _last_turn["question"], _last_turn["slots"]
    try:
        r = pipeline.run(question, prev_question=prev_question, prev_slots=prev_slots)
    except Exception as e:                                    # noqa: BLE001
        _log_server_error(question, e)
        return EvalAnswer(question_id=question_id, question=question,
                          retrieved_context="", think_trace="", answer=_ERROR_ANSWER)
    if r.state in ("S0", "S2"):
        # 실패턴(S1/S3/S6)에서는 갱신하지 않는다 — pipeline.run()의 문서화된
        # 계약과 동일하게, 실패턴의 slots는 승계 후보가 아니라 이전 성공턴을
        # 계속 들고 있는 편이 낫다(/ask의 AskRequest.prev_slots 설명과 같은 이유).
        with _last_turn_lock:
            _last_turn["question"] = r.resolved_question or question
            _last_turn["slots"] = _slots(r)
    return EvalAnswer(
        question_id=question_id, question=question,
        retrieved_context=_retrieved_context(r),
        think_trace=_think_trace(r),
        answer=render.summary_text(r),
    )


_WEB = Path(__file__).resolve().parent / "web" / "index.html"


@app.get("/", include_in_schema=False)
def ui():
    """데모·확인용 웹 화면. 같은 서버가 /ask 를 그대로 호출한다."""
    return FileResponse(_WEB)


@app.get("/health", response_model=Health, summary="준비 상태")
def health():
    """색인 로드가 끝났는지 알려준다. ready=false면 아직 요청을 보내지 말 것."""
    return Health(status="ok" if _READY["ok"] else "loading", ready=_READY["ok"],
                  facts=_READY["facts"], corps=_READY.get("corps", 0),
                  load_ms=_READY["load_ms"])
