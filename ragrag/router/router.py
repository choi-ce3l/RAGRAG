"""router.py — 경로 선택(가산점 규칙 방식, P1 §4.2).

parse.py도 자체 if/elif cascade로 intent를 정하지만(그 결과는 frame.intent에 이미
들어있다), 이 모듈은 그것과 별개로 frame의 슬롯 완성도 + 원문 트리거 어휘를 다시
독립적으로 채점해 경로를 정한다. 두 메커니즘이 보통은 같은 결론에 도달해야 하고,
diagnose.py가 실제로 둘이 얼마나 일치하는지 비교한다 — 불일치하면 parse.py 규칙이나
이 채점 규칙 중 하나(또는 둘 다)가 놓친 게 있다는 신호다.

동점 시 우선순위: fact_numeric > dual > compute > comparison > existence > narrative >
clarification(지시사항의 "fact > dual > narrative"를 전체 7개 경로로 확장한 것).
필수 슬롯 미충족이면 최고점 경로라도 clarification으로 강등한다.
"""
import json
import os

from . import vocab
from .frame import Intent

ROUTES = ["fact_numeric", "dual", "compute", "comparison", "existence", "narrative", "clarification"]
PRIORITY = {"fact_numeric": 6, "dual": 5, "compute": 4, "comparison": 3,
            "existence": 2, "narrative": 1, "clarification": 0}

# 경로별 필수 슬롯 — 없으면 그 경로가 최고점이어도 clarification으로 강등.
REQUIRED_SLOTS = {
    "fact_numeric": ("corp_code", "metric", "period", "scope"),
    "dual": ("corp_code", "metric", "period"),
    "compute": ("corp_code", "metric", "period"),
    "comparison": ("corp_code", "metric", "period"),
    "existence": ("corp_code",),
    "narrative": (),
    "clarification": (),
}

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG_PATH = os.path.join(_HERE, "out", "router_decisions.jsonl")


def _slot_ok(frame, slot):
    if slot == "corp_code":
        return bool(frame.corp_code)
    if slot == "metric":
        return bool(frame.metric)
    if slot == "period":
        return bool(frame.period.year or frame.period.as_of_date)
    if slot == "scope":
        return bool(frame.scope)
    return True


def score_routes(frame):
    """frame -> (scores dict, rules_fired list). 규칙 이름 + 대상 경로 + 점수를 남긴다."""
    q = frame.raw_question or ""
    scores = {r: 0.0 for r in ROUTES}
    rules_fired = []

    def bump(route, pts, name):
        scores[route] += pts
        rules_fired.append({"rule": name, "route": route, "points": pts})

    has_corp = bool(frame.corp_code)
    has_metric = bool(frame.metric)
    has_period = bool(frame.period.year or frame.period.as_of_date)
    has_scope = bool(frame.scope)

    if has_corp and has_metric and has_period and has_scope:
        bump("fact_numeric", 3, "R_SLOTS_FULL_WITH_SCOPE")
    if has_corp and has_metric and has_period and not has_scope:
        bump("dual", 3, "R_SLOTS_FULL_NO_SCOPE")

    # 트리거 규칙은 슬롯-완전성 규칙(3점)보다 항상 높은 점수(5점)를 준다 — 그래야
    # "슬롯이 다 찼다"는 사실만으로 fact_numeric이 이겨버리는 걸 막는다. 실제로 겪은 버그:
    # "…전년 대비 몇 % 증가했는가?"는 corp/metric/period/scope가 전부 채워지는 동시에
    # compute 트리거도 걸리는데, 둘 다 3점이면 동점 tie-break(fact_numeric 우선순위 6 > compute
    # 4)가 compute를 눌러버려 parse.py의 판단(compute)과 router.py가 어긋났다(진단 §2에서
    # 실측 99/694건). 점수 비대칭으로 고쳤다 — 자세한 내용은 DIAGNOSIS.md 참고.
    for term in vocab.COMPUTE_TERMS:
        if term in q:
            bump("compute", 5, f"R_COMPUTE_TRIGGER:{term}")
            break
    for term in vocab.VERSION_PAIR_TERMS:
        if term in q:
            bump("comparison", 5, f"R_COMPARISON_TRIGGER:{term}")
            break
    for term in vocab.EXISTENCE_TERMS:
        if term in q:
            bump("existence", 5, f"R_EXISTENCE_TRIGGER:{term}")
            break

    # 버전 신호(정정 전/후 등)는 comparison 쪽에 약한 가점 — existence/compute 트리거가
    # 이미 우세하면 뒤집지 않도록 점수를 작게 준다.
    for term in vocab.VERSION_ORIGINAL_TERMS + vocab.VERSION_CORRECTED_TERMS:
        if term in q:
            bump("comparison", 1, f"R_VERSION_SIGNAL_WEAK:{term}")
            break

    if has_metric and not (has_corp and has_period):
        bump("clarification", 2, "R_METRIC_BUT_INCOMPLETE")
    if has_corp and not has_metric:
        bump("narrative", 1, "R_CORP_ONLY_LEANS_NARRATIVE")
    if not has_corp and not has_metric:
        bump("clarification", 0.5, "R_NO_ANCHOR_WEAK_SIGNAL")

    return scores, rules_fired


def select_route(frame, log_path=None):
    """frame -> (route:str, decision:dict). log_path가 주어지면 JSONL에 append."""
    scores, rules_fired = score_routes(frame)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], -PRIORITY[kv[0]]))
    top_route, top_score = ranked[0]
    if top_score <= 0:
        top_route = "narrative" if frame.corp_code else "clarification"

    required = REQUIRED_SLOTS.get(top_route, ())
    missing = [s for s in required if not _slot_ok(frame, s)]
    final_route = "clarification" if missing else top_route

    decision = {
        "question": frame.raw_question, "frame": frame.to_dict(),
        "scores": scores, "rules_fired": rules_fired,
        "top_route": top_route, "missing_slots": missing, "selected_route": final_route,
        "agrees_with_parse_intent": final_route == (
            frame.intent.value if isinstance(frame.intent, Intent) else frame.intent),
    }
    if log_path:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(decision, ensure_ascii=False) + "\n")
    return final_route, decision
