"""execute.py — 경로별 실행. 기존 choi 모듈(facts/numqa)에 위임하되, 두 가지 known
defense를 이 레이어에서 건다(numqa.py 원본은 수정하지 않는다):

  (1) FactStore 인덱스 구축 시 is_superseded=True를 제외 — CHOI_상태파악.md §6.2-(1)
      ("numqa.FactStore가 superseded fact를 걸러내지 않아 인덱스 키의 37.2%가 정정으로
      대체된 필링의 fact로 해결된다")의 방어. numqa.FactStore.load()는 안 쓰고, facts
      리스트를 우리가 먼저 필터링한 뒤 numqa.FactStore(filtered_list)로 넘긴다 — 생성자
      자체는 choi 원본 그대로 재사용.
  (2) compute에 frame.scope를 명시적으로 전달 — §6.2-(2)("fact_compute가 scope를
      항상 consolidated로 하드코딩해 '별도' 질문에도 연결 값을 반환하고 라벨은 '연결'이라
      표기") 방어. frame.scope가 None이면 연결·별도 둘 다 계산해 병기한다.

numqa.answer(q, store)는 쓰지 않는다 — 그 함수가 q 텍스트를 다시 자체 parse_intent로
파싱해버려서, 우리 frame(위 두 방어가 반영된)을 무시하게 된다. 대신 numqa.FactStore의
lookup()/lookup_in_doc()과 facts.compute()를 직접 호출해 numqa와 같은 계산 로직을
재사용하되 입력을 frame이 정한다.
"""
import os

from ragrag.pipeline import facts as choi_facts  # noqa: E402
from ragrag.pipeline import numqa as choi_numqa  # noqa: E402

from . import resolver  # noqa: E402  (jin/router 형제 모듈)

SCOPE_KO = {"consolidated": "연결", "separate": "별도"}
BOTH_SCOPES = ("consolidated", "separate")

_ALL_FACTS = None


def _load_all_facts():
    global _ALL_FACTS
    if _ALL_FACTS is None:
        _ALL_FACTS = choi_facts.load_facts()   # out/facts.jsonl, choi 원본 함수
    return _ALL_FACTS


def build_store(doc_ids=None):
    """doc_ids가 주어지면 그 안의 fact만, 아니면 is_superseded=False 기본 필터로
    numqa.FactStore를 만든다. numqa.FactStore.__init__ 자체는 choi 원본 그대로 재사용 —
    우리가 손대는 건 "무엇을 넘기는가"뿐이다."""
    facts_list = _load_all_facts()
    if doc_ids is not None:
        filtered = [f for f in facts_list if f.get("metric_key") and f["doc_id"] in doc_ids]
    else:
        filtered = [f for f in facts_list if f.get("metric_key") and not f["is_superseded"]]
    return choi_numqa.FactStore(filtered)


def _fact_phrase(f):
    return choi_numqa._fact_phrase(f)   # choi 헬퍼 재사용(문장 조각 하나, import만)


def _digit(v):
    return choi_numqa._digit(v)


# ---------------------------------------------------------------------------
def run(frame):
    """frame.intent에 따라 분기. 반환은 항상 dict(status, text, numbers, sources, ...)."""
    dispatch = {
        "fact_numeric": fact_numeric, "dual": dual, "compute": compute,
        "comparison": comparison, "existence": existence, "narrative": narrative,
    }
    fn = dispatch.get(frame.intent.value if hasattr(frame.intent, "value") else frame.intent)
    if fn is None:
        return {"status": "clarification", "text": _clarify_text(frame),
                "numbers": [], "sources": []}
    return fn(frame)


def _clarify_text(frame):
    if frame.missing_slots:
        return "질문에서 다음을 특정하지 못했습니다: " + ", ".join(frame.missing_slots)
    return "질문을 더 구체화해 주세요."


# ---------------------------------------------------------------------------
def fact_numeric(frame):
    res = resolver.resolve(frame)
    store = build_store(res.doc_ids if res.doc_ids else None)
    f = store.lookup(frame.corp_code, frame.metric, frame.scope, frame.period.year)
    if not f:
        return {"status": "no_fact", "text": "해당 fact를 찾지 못했습니다.",
                "numbers": [], "sources": [], "resolver": res.to_dict()}
    return {
        "status": "ok",
        "text": f"{frame.corp_name}의 {frame.period.year}년 {SCOPE_KO[frame.scope]} 기준 "
                f"{f['label_norm']}은 {_fact_phrase(f)}입니다.",
        "numbers": [_digit(f["value_raw"])],
        "sources": [{"scope": frame.scope, "fact_id": f["fact_id"], "rcept_no": f["rcept_no"],
                     "aclass": f["aclass_xbrl_code"], "row": f["label_raw"],
                     "version": res.method or "latest_effective",
                     "supersede_method": res.method}],
        "resolver": res.to_dict(),
    }


def dual(frame):
    res = resolver.resolve(frame)
    store = build_store(res.doc_ids if res.doc_ids else None)
    cf = store.lookup(frame.corp_code, frame.metric, "consolidated", frame.period.year)
    sf = store.lookup(frame.corp_code, frame.metric, "separate", frame.period.year)
    found = [(SCOPE_KO[s], f) for s, f in (("consolidated", cf), ("separate", sf)) if f]
    if not found:
        return {"status": "no_fact", "text": "해당 지표 fact를 찾지 못했습니다.",
                "numbers": [], "sources": [], "resolver": res.to_dict()}
    parts = [f"{sk} {_fact_phrase(f)}" for sk, f in found]
    note = " — 연결/별도 기준이 다르므로 구분이 필요합니다." if len(found) == 2 else ""
    return {
        "status": "ok",
        "text": f"{frame.corp_name}의 {frame.period.year}년 {found[0][1]['label_norm']}은 " +
                " / ".join(parts) + note,
        "numbers": [_digit(f["value_raw"]) for _, f in found],
        "sources": [{"scope": sk_en, "fact_id": f["fact_id"], "rcept_no": f["rcept_no"],
                     "aclass": f["aclass_xbrl_code"], "version": res.method or "latest_effective",
                     "supersede_method": res.method}
                    for sk_en, (sk_ko, f) in zip(("consolidated", "separate")[:len(found)], found)],
        "resolver": res.to_dict(),
    }


def _compute_one_scope(store, frame, scope):
    cur = store.lookup(frame.corp_code, frame.metric, scope, frame.period.year)
    prev = store.lookup_in_doc(cur["doc_id"], frame.metric, scope, frame.period.year - 1) if cur else None
    if not (cur and prev):
        return None
    op = frame.operation or "growth"
    r = choi_facts.compute(op, [cur, prev])
    pct = round(float(r["value"]), 1)
    return {
        "scope": scope, "pct": pct, "cur": cur, "prev": prev, "formula_result": r,
        "text": (f"{SCOPE_KO[scope]} {cur['label_norm']}은 전년({frame.period.year - 1}년) 대비 "
                 f"{pct}% {'증가' if pct >= 0 else '감소'}했습니다 "
                 f"({cur['value_raw']} vs {prev['value_raw']}, 단위 {cur['unit_kr']})."),
    }


def compute(frame):
    """§6.2-(2) 방어: frame.scope가 있으면 그 scope만, None이면 연결·별도 둘 다 계산해 병기."""
    res = resolver.resolve(frame)
    store = build_store(res.doc_ids if res.doc_ids else None)
    scopes = [frame.scope] if frame.scope else list(BOTH_SCOPES)
    results = {s: _compute_one_scope(store, frame, s) for s in scopes}
    ok_results = {s: r for s, r in results.items() if r}
    if not ok_results:
        return {"status": "no_fact", "text": "계산에 필요한 fact를 찾지 못했습니다.",
                "numbers": [], "sources": [], "resolver": res.to_dict()}
    text = f"{frame.corp_name}의 {frame.period.year}년 " + " ".join(r["text"] for r in ok_results.values())
    sources = []
    for s, r in ok_results.items():
        sources.append({"scope": s, "fact_id": r["cur"]["fact_id"], "rcept_no": r["cur"]["rcept_no"],
                         "version": res.method or "latest_effective", "supersede_method": res.method})
        sources.append({"scope": s, "fact_id": r["prev"]["fact_id"], "rcept_no": r["prev"]["rcept_no"],
                         "version": res.method or "latest_effective", "supersede_method": res.method})
    return {
        "status": "ok", "text": text,
        "numbers": [f"{r['pct']}%" for r in ok_results.values()],
        "sources": sources, "scopes_computed": list(ok_results.keys()),
        "resolver": res.to_dict(),
    }


def _restatement_scan(frame, scope):
    """resolver의 supersede-pair가 실패했을 때의 폴백.

    중요한 구분(diagnose.py에서도 다시 언급): goldset_layerA의 "comparison/restatement"는
    같은 필링을 재제출한 [기재정정](supersede 체인)이 아니라, 서로 다른 두 연차보고서가
    같은 회계연도 수치를 다르게 보고하는 "재작성"(예: 사업부 재분류·전기 비교표시 수정)이다.
    resolver.resolve(pair)는 supersede 체인만 보므로 이 케이스에서 항상 no_correction_history로
    실패한다 — 그래서 여기서는 supersede와 무관하게 (corp,metric,scope,year)로 전체
    facts.jsonl을 스캔해, 값이 다른 서로 다른 필링이 2개 이상이면 그 중 최초 필링과
    최신 필링을 "비교 쌍"으로 삼는다. is_superseded=True는 제외(gen_goldA_restate.py와
    동일 정책). rcept_no는 접수일 기반이라 문자열 정렬이 곧 시간순이다.
    """
    all_facts = _load_all_facts()
    cands = [f for f in all_facts
             if f["corp_code"] == frame.corp_code and f.get("metric_key") == frame.metric
             and f["scope"] == scope and f["base_year"] == frame.period.year
             and not f["is_superseded"]]
    by_doc = {}
    for f in cands:
        by_doc.setdefault(f["doc_id"], f)   # 문서당 1개(중복 행이면 첫 값)
    if len(by_doc) < 2:
        return None, None
    ordered = sorted(by_doc.values(), key=lambda f: f["rcept_no"])
    if ordered[0]["value_decimal"] == ordered[-1]["value_decimal"] and len(by_doc) == 2:
        # 값이 같은 2건은 비교할 "재작성"이 없다는 뜻 — 그래도 답은 반환(same=True로).
        pass
    return ordered[0], ordered[-1]


# ---------------------------------------------------------------------------
def comparison(frame):
    """resolver의 supersede pair(원공시·정정본)를 우선 시도하고, 정정 이력이 없으면
    _restatement_scan()으로 폴백한다(위 독스트링 참고 — 이게 goldset_layerA의 실제 다수 케이스)."""
    res = resolver.resolve(frame)
    scope = frame.scope or "consolidated"
    method = res.method
    low_conf = res.low_confidence
    fallback_used = False

    if res.original_doc_id and res.corrected_doc_id:
        all_facts = _load_all_facts()

        def _find(doc_id):
            cands = [f for f in all_facts if f["doc_id"] == doc_id
                     and f.get("metric_key") == frame.metric
                     and f["scope"] == scope and f["base_year"] == frame.period.year]
            return cands[0] if cands else None

        orig_f, corr_f = _find(res.original_doc_id), _find(res.corrected_doc_id)
    else:
        orig_f, corr_f = None, None

    if not (orig_f and corr_f):
        orig_f, corr_f = _restatement_scan(frame, scope)
        if orig_f and corr_f:
            fallback_used = True
            method = "restatement_scan"
            low_conf = True   # 재작성 스캔은 검증된 체인이 아니라 값 비교 휴리스틱 — 저신뢰로 표기

    if not (orig_f and corr_f):
        return {"status": "no_pair", "resolver": res.to_dict(),
                "text": "정정 전/후 쌍이나 재작성 비교 대상을 찾지 못했습니다."}

    diff = choi_facts.compute("diff", [corr_f, orig_f])
    same = orig_f["value_decimal"] == corr_f["value_decimal"]
    diff_note = "값이 일치합니다" if same else f"값이 다릅니다(차이 {diff['value']})"
    label = "정정 전/후" if not fallback_used else "초기 필링/최신 필링"
    text = (f"{frame.corp_name}의 {frame.period.year}년 {SCOPE_KO[scope]} {orig_f['label_norm']}은 "
            f"{label} 각각 {orig_f['value_raw']} / {corr_f['value_raw']}({diff_note}).")
    return {
        "status": "ok", "text": text, "numbers": [orig_f["value_raw"], corr_f["value_raw"]],
        "same_value": same, "used_restatement_fallback": fallback_used,
        "sources": [
            {"which": "early", "fact_id": orig_f["fact_id"], "rcept_no": orig_f["rcept_no"],
             "version": "original" if not fallback_used else "earliest_filing",
             "supersede_method": method},
            {"which": "late", "fact_id": corr_f["fact_id"], "rcept_no": corr_f["rcept_no"],
             "version": "corrected" if not fallback_used else "latest_filing",
             "supersede_method": method},
        ],
        "resolver": res.to_dict(), "low_confidence": low_conf,
    }


def existence(frame):
    """신규: FactStore 조회 -> 없으면 manifest(문서 존재 여부)로 폴백해 "확인 범위"를
    명시한 부재 응답을 만든다. 최신 default로 임의 값을 지어내지 않는다."""
    if not frame.corp_code:
        return {"status": "unparsed", "text": "질문에서 기업을 특정하지 못했습니다.",
                "numbers": [], "sources": []}
    res = resolver.resolve(frame) if frame.period.year else None
    store = build_store(res.doc_ids if (res and res.doc_ids) else None)
    if frame.metric and frame.period.year:
        f = store.lookup(frame.corp_code, frame.metric, frame.scope, frame.period.year) if frame.scope \
            else (store.lookup(frame.corp_code, frame.metric, "consolidated", frame.period.year) or
                  store.lookup(frame.corp_code, frame.metric, "separate", frame.period.year))
        if f:
            return {"status": "ok", "text": f"존재합니다: {_fact_phrase(f)}.",
                    "numbers": [_digit(f["value_raw"])],
                    "sources": [{"fact_id": f["fact_id"], "rcept_no": f["rcept_no"]}]}
    docs = resolver.periodic_docs_exist(frame.corp_code, frame.period.year)
    scope_desc = f"{frame.period.year}년" if frame.period.year else "전체 연도"
    if docs:
        text = (f"{frame.corp_name}의 {scope_desc} 사업보고서류 문서는 {len(docs)}건 존재하지만, "
                f"{'지정한 지표(' + frame.metric + ')' if frame.metric else '요청한 항목'}의 fact는 "
                f"찾지 못했습니다. (확인 범위: periodic facts.jsonl, corp_code={frame.corp_code})")
        status = "no_fact"
    else:
        text = (f"{frame.corp_name}의 {scope_desc} periodic 문서 자체가 코퍼스에 없습니다. "
                f"(확인 범위: manifest periodic, corp_code={frame.corp_code})")
        status = "absent"
    return {"status": status, "text": text, "numbers": [], "sources": [],
            "checked_scope": {"doc_group": "periodic", "corp_code": frame.corp_code,
                               "year": frame.period.year, "n_docs_found": len(docs)}}


# ---------------------------------------------------------------------------
def narrative(frame):
    """USE_LOCAL_LLM=1이면 llm_local 경유 실제 호출, 아니면 라우팅 결과+파라미터 스텁."""
    if os.environ.get("USE_LOCAL_LLM") == "1":
        from . import llm_local   # 지연 import — 안 쓰는 경로에서 openai/urllib 의존 안 만듦
        return llm_local.answer_narrative(frame)
    return {
        "status": "stub", "text": "",
        "routing": {"path": "narrative", "would_call": "rag.answer(question)",
                    "note": "CLOVA API 비용 승인 전 — 통합 지점만 명확히 함. "
                            "USE_LOCAL_LLM=1이면 llm_local 경유 로컬 생성으로 대체된다."},
        "params": {"question": frame.raw_question, "corp_name": frame.corp_name,
                   "corp_code": frame.corp_code, "scope": frame.scope,
                   "period": frame.period.__dict__},
        "sources": [],
    }


if __name__ == "__main__":
    import json
    import sys

    from . import intent_parse as ip

    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:])
        f = ip.parse_intent(q)
        out = run(f)
        print(json.dumps({"frame": f.to_dict(), "result": out}, ensure_ascii=False, indent=2))
    else:
        print(__doc__)
