"""diagnose.py — 진단(구현 검증 겸).

이식 전 jin/router/diagnose.py는 bare `import parse`가 choi의 문서 파서(rag.py가 내부에서
기대하는 것)와 이름이 겹쳐서 importlib.util.spec_from_file_location으로 별도 이름
("router_intent_parse")에 우회 바인딩했다(jin/router/README.md "choi와의 경계" 참고).
이식 시 jin의 parse.py를 intent_parse.py로 리네임하고 choi 쪽(ragrag/pipeline/rag.py)도
Step 2에서 이미 상대import(`from . import parse`)로 전환됐으므로, ragrag.pipeline.parse와
ragrag.router.intent_parse는 정식 패키지 경로가 달라 더 이상 이름이 충돌하지 않는다 —
그래서 이 우회는 정식 import로 대체했다(jin/INTEGRATION_PLAN.md §2-2 제안대로).

실행:
  conda run -n RAGRAG python3 -m ragrag.router.diagnose              # LLM 계측 없이 1~3절만
  USE_LOCAL_LLM=1 conda run -n RAGRAG python3 -m ragrag.router.diagnose   # 4절(로컬 LLM 캘리브레이션) 포함
"""
import collections
import json
import os
import time

from ragrag.pipeline import numqa as choi_numqa  # noqa: E402

from . import router      # noqa: E402
from . import execute     # noqa: E402
from . import compose     # noqa: E402
from . import resolver    # noqa: E402
from . import intent_parse as ip  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_GOLD_B = os.path.normpath(os.path.join(_HERE, "..", "goldsets", "layerB"))
_GOLD_A = os.path.normpath(os.path.join(_HERE, "..", "goldsets", "layerA"))
_OUT = os.path.join(_HERE, "out")
os.makedirs(_OUT, exist_ok=True)


# ---------------------------------------------------------------------------
# 골드셋 로드
# ---------------------------------------------------------------------------
SLICE_TO_INTENT = {"fact_numeric": "fact_numeric", "dual": "dual", "compute": "compute",
                   "restatement": "comparison"}


def load_gold_items():
    items = []
    for name in ("fact_numeric", "dual", "compute"):
        path = os.path.join(_GOLD_B, f"goldB_{name}.jsonl")
        for line in open(path, encoding="utf-8"):
            d = json.loads(line)
            d["_layer"] = "B"
            d["_expected_intent"] = SLICE_TO_INTENT[d["slice"]]
            items.append(d)
    path = os.path.join(_GOLD_A, "goldA_restatement.jsonl")
    for line in open(path, encoding="utf-8"):
        d = json.loads(line)
        d["_layer"] = "A"
        d["_expected_intent"] = SLICE_TO_INTENT[d["slice"]]
        items.append(d)
    return items


# ---------------------------------------------------------------------------
# 1) parse_intent 슬롯별 성공률 + intent 일치율 + 기존 numqa.parse_intent 대비 비교
# ---------------------------------------------------------------------------
def diagnose_parse(gold_items):
    old_store = choi_numqa.FactStore.load(path=choi_numqa.FACTS_PATH)

    slot_ok = collections.Counter()
    slot_total = collections.Counter()
    intent_match = collections.Counter()
    intent_total = collections.Counter()
    old_intent_match = collections.Counter()
    old_corp_match = collections.Counter()
    new_corp_match = collections.Counter()
    confusion = collections.Counter()   # (expected, got_new)
    rows = []

    for it in gold_items:
        q = it["question"]
        f = ip.parse_intent(q)
        old_p = choi_numqa.parse_intent(q, old_store)

        exp_corp = it.get("corp_code")
        slot_total["corp"] += 1
        slot_ok["corp"] += int(f.corp_code == exp_corp)
        new_corp_match[f.corp_code == exp_corp] += 1
        old_corp_match[old_p.get("corp_code") == exp_corp] += 1

        exp_metric = it.get("metric_key")
        if exp_metric:
            slot_total["metric"] += 1
            slot_ok["metric"] += int(f.metric == exp_metric)

        exp_year = it.get("base_year") or it.get("fiscal_year")
        if exp_year:
            slot_total["period_year"] += 1
            slot_ok["period_year"] += int(f.period.year == exp_year)

        exp_scope = it.get("scope")
        if exp_scope:
            slot_total["scope"] += 1
            slot_ok["scope"] += int(f.scope == exp_scope)

        exp_intent = it["_expected_intent"]
        intent_total[exp_intent] += 1
        got_new = f.intent.value
        intent_match[exp_intent] += int(got_new == exp_intent)
        old_intent_norm = {"fact_numeric": "fact_numeric", "fact_compute": "compute",
                            "comparison": "comparison"}.get(old_p.get("intent"), old_p.get("intent"))
        old_intent_match[exp_intent] += int(old_intent_norm == exp_intent)
        confusion[(exp_intent, got_new)] += 1

        rows.append({"qid": it["qid"], "layer": it["_layer"], "question": q,
                     "expected_intent": exp_intent, "new_intent": got_new,
                     "old_intent": old_intent_norm, "new_confidence": f.confidence,
                     "fired_rules": f.fired_rules})

    report = {
        "n_total": len(gold_items),
        "slot_success_rate": {k: round(slot_ok[k] / slot_total[k], 4) for k in slot_total},
        "slot_counts": {k: {"ok": slot_ok[k], "total": slot_total[k]} for k in slot_total},
        "intent_match_rate_new": {k: round(intent_match[k] / intent_total[k], 4) for k in intent_total},
        "intent_match_rate_old": {k: round(old_intent_match[k] / intent_total[k], 4) for k in intent_total},
        "intent_totals": dict(intent_total),
        "corp_match_new": dict(new_corp_match),
        "corp_match_old": dict(old_corp_match),
        "confusion_expected_vs_new": {f"{k[0]}->{k[1]}": v for k, v in confusion.items()},
    }
    with open(os.path.join(_OUT, "diag_parse.json"), "w", encoding="utf-8") as fo:
        json.dump(report, fo, ensure_ascii=False, indent=2)
    with open(os.path.join(_OUT, "diag_parse_rows.jsonl"), "w", encoding="utf-8") as fo:
        for r in rows:
            fo.write(json.dumps(r, ensure_ascii=False) + "\n")
    return report


# ---------------------------------------------------------------------------
# 2) 층B 라우터 통과 채점 + superseded 필터 전후 차이
# ---------------------------------------------------------------------------
def _tok_in(tok, text):
    return tok in text


def diagnose_layerB_routed(gold_items_b):
    unfiltered_store = choi_numqa.FactStore.load(path=choi_numqa.FACTS_PATH)  # §6.2-(1) 미방어 상태 재현

    n = pas = strict_pass = 0
    changed_vs_unfiltered = []
    fails = []
    router_route_mismatch = 0
    decisions_path = os.path.join(_OUT, "router_decisions.jsonl")
    if os.path.exists(decisions_path):
        os.remove(decisions_path)

    for it in gold_items_b:
        q = it["question"]
        f = ip.parse_intent(q)
        route, decision = router.select_route(f, log_path=decisions_path)
        if route != (f.intent.value):
            router_route_mismatch += 1
        result = execute.run(f)
        composed = compose.compose(f, result)
        text = composed["text"]

        mc = it.get("must_contain", [])
        mnc = it.get("must_not_contain", [])
        mc_hit = any(_tok_in(t, text) for t in mc)
        mnc_hit = any(_tok_in(t, text) for t in mnc)
        ok = (mc_hit or not mc) and not mnc_hit
        if it["slice"] == "dual":
            gv = it.get("gold_values", {})
            strict = all(choi_numqa._digit(v) in text for v in gv.values()) and not mnc_hit
        else:
            strict = ok
        n += 1
        pas += ok
        strict_pass += strict
        if not strict:
            fails.append({"qid": it["qid"], "slice": it["slice"], "question": q,
                          "status": result.get("status"), "text": text[:150]})

        # superseded 필터 전후 비교: "값"(numbers)만 비교한다 — text 전체를 비교하면
        # execute.py와 numqa.answer()의 문장 템플릿 차이(예: "매출액" vs "수익(매출액)"
        # 라벨 표기)가 노이즈로 섞여 진짜 defense 효과(값 자체가 바뀌는 경우)를 가린다.
        try:
            old_res = choi_numqa.answer(q, unfiltered_store)
            new_numbers = result.get("numbers", [])
            old_numbers = old_res.get("numbers", [])
            if old_res.get("text") and new_numbers != old_numbers:
                changed_vs_unfiltered.append({
                    "qid": it["qid"], "slice": it["slice"], "question": q,
                    "filtered_numbers": new_numbers, "unfiltered_numbers": old_numbers,
                    "filtered_text": text, "unfiltered_text": old_res["text"],
                    "filtered_status": result.get("status"), "unfiltered_status": old_res.get("status"),
                    "filtered_sources": result.get("sources"),
                })
        except Exception as e:  # noqa: BLE001
            changed_vs_unfiltered.append({"qid": it["qid"], "error": str(e)})

    report = {
        "n": n, "pass": pas, "strict_pass": strict_pass,
        "router_route_vs_frame_intent_mismatch": router_route_mismatch,
        "n_changed_vs_unfiltered_numqa": len(changed_vs_unfiltered),
    }
    with open(os.path.join(_OUT, "diag_layerB_routed.json"), "w", encoding="utf-8") as fo:
        json.dump({"report": report, "fails": fails[:40],
                   "changed_vs_unfiltered": changed_vs_unfiltered}, fo, ensure_ascii=False, indent=2)
    return report, changed_vs_unfiltered, fails


# ---------------------------------------------------------------------------
# 3) 층A 103 comparison 라우팅률
# ---------------------------------------------------------------------------
def diagnose_layerA_comparison(gold_items_a):
    n = routed_comparison = solved_ok = used_fallback = 0
    fails = []
    for it in gold_items_a:
        q = it["question"]
        f = ip.parse_intent(q)
        n += 1
        if f.intent.value == "comparison":
            routed_comparison += 1
            result = execute.run(f)
            if result.get("status") == "ok":
                solved_ok += 1
                if result.get("used_restatement_fallback"):
                    used_fallback += 1
            else:
                fails.append({"qid": it["qid"], "question": q, "status": result.get("status")})
        else:
            fails.append({"qid": it["qid"], "question": q, "got_intent": f.intent.value})
    report = {"n": n, "routed_comparison": routed_comparison,
              "solved_ok": solved_ok, "used_restatement_fallback": used_fallback}
    with open(os.path.join(_OUT, "diag_layerA.json"), "w", encoding="utf-8") as fo:
        json.dump({"report": report, "fails": fails[:40]}, fo, ensure_ascii=False, indent=2)
    return report, fails


# ---------------------------------------------------------------------------
# 4) (USE_LOCAL_LLM=1) 계측기 캘리브레이션
# ---------------------------------------------------------------------------
def diagnose_local_llm_calibration(gold_items_b, n_correct=5, n_wrong=5, n_runs=3):
    """정답 5 + "그럴듯한" 오답 5(연도 바꿈/scope 뒤바뀜 — 실제 다른 fact 값을 그대로
    가져다 쓴 silent-wrong / 지어낸 수치)로 로컬 LLM judge의 검출률을 잰다.

    year_swap/scope_swap 오답은 실제 FactStore 조회로 만든다(그냥 gold와 같은 문자열을
    "오답"이라 우기면 judge가 맞혀도 의미가 없다 — candidate 텍스트 자체가 gold와
    달라야 진짜 검출 테스트다). 조회가 안 되는 항목은 그 kind를 건너뛰고 fabricated로 채운다.
    """
    from . import llm_local

    picks = [it for it in gold_items_b if it["slice"] == "fact_numeric"][:max(n_correct, n_wrong) * 3]
    cases = []
    for it in picks[:n_correct]:
        gold = it.get("gold_value") or next(iter(it.get("gold_values", {}).values()), "")
        cases.append({"qid": it["qid"] + "-correct", "question": it["question"],
                      "gold": gold, "candidate": gold, "expected_verdict": "CORRECT"})

    wrong_kinds = ["year_swap", "scope_swap", "fabricated"]
    n_made = 0
    for i, it in enumerate(picks):
        if n_made >= n_wrong:
            break
        gold = it.get("gold_value") or next(iter(it.get("gold_values", {}).values()), "")
        kind = wrong_kinds[n_made % len(wrong_kinds)]
        cand_val = None
        if kind == "year_swap" and it.get("corp_code") and it.get("metric_key") and it.get("scope"):
            f_prev = execute.build_store().lookup(it["corp_code"], it["metric_key"], it["scope"],
                                                   it.get("base_year", 0) - 1)
            if f_prev and f_prev["value_raw"] != gold:
                cand_val = f_prev["value_raw"] + it.get("unit_kr", "")
        elif kind == "scope_swap" and it.get("corp_code") and it.get("metric_key") and it.get("scope"):
            other_scope = "separate" if it["scope"] == "consolidated" else "consolidated"
            f_other = execute.build_store().lookup(it["corp_code"], it["metric_key"], other_scope,
                                                    it.get("base_year"))
            if f_other and f_other["value_raw"] != gold:
                cand_val = f_other["value_raw"] + it.get("unit_kr", "")
        if cand_val is None:
            kind = "fabricated"
            cand_val = "999,999,999,999(지어낸 값)"
        cases.append({"qid": it["qid"] + f"-wrong-{kind}", "question": it["question"],
                      "gold": gold, "candidate": cand_val, "expected_verdict": "INCORRECT",
                      "wrong_kind": kind})
        n_made += 1

    runs = []
    for run_idx in range(n_runs):
        verdicts = {}
        for c in cases:
            v, raw = llm_local.judge(c["question"], c["gold"], c["candidate"])
            verdicts[c["qid"]] = v
        n_ok = sum(1 for c in cases if verdicts[c["qid"]] == c["expected_verdict"])
        runs.append({"run": run_idx, "verdicts": verdicts, "accuracy": n_ok / len(cases)})

    # run 간 편차: 같은 qid가 run마다 다른 판정을 냈는지
    flips = 0
    for c in cases:
        vs = {runs[r]["verdicts"][c["qid"]] for r in range(n_runs)}
        if len(vs) > 1:
            flips += 1
    undetected_wrong_kinds = collections.Counter()
    for c in [c for c in cases if c["expected_verdict"] == "INCORRECT"]:
        if all(runs[r]["verdicts"][c["qid"]] != "INCORRECT" for r in range(n_runs)):
            undetected_wrong_kinds[c["wrong_kind"]] += 1

    report = {
        "n_cases": len(cases), "n_runs": n_runs,
        "accuracy_per_run": [r["accuracy"] for r in runs],
        "n_qid_with_flip_across_runs": flips,
        "undetected_wrong_kinds": dict(undetected_wrong_kinds),
        "cases": [{"qid": c["qid"], "expected": c["expected_verdict"], "wrong_kind": c.get("wrong_kind")}
                  for c in cases],
    }
    with open(os.path.join(_OUT, "diag_local_llm_calibration.json"), "w", encoding="utf-8") as fo:
        json.dump({"report": report, "runs": runs}, fo, ensure_ascii=False, indent=2)
    return report


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    t0 = time.time()
    gold = load_gold_items()
    gold_b = [g for g in gold if g["_layer"] == "B"]
    gold_a = [g for g in gold if g["_layer"] == "A"]
    print(f"골드셋 로드: B={len(gold_b)} A={len(gold_a)} ({time.time()-t0:.1f}s)")

    # resolver의 manifest+supersede map을 먼저 1회 구축(캐시 워밍 — 이후 매 질문마다 재사용).
    t1 = time.time()
    resolver._load_state()
    print(f"resolver 상태 구축: {time.time()-t1:.1f}s")

    t1 = time.time()
    r1 = diagnose_parse(gold)
    print(f"[1] parse 진단 완료 {time.time()-t1:.1f}s -> {json.dumps(r1['intent_match_rate_new'], ensure_ascii=False)}")

    t1 = time.time()
    r2, changed, fails2 = diagnose_layerB_routed(gold_b)
    print(f"[2] 층B 라우팅 채점 완료 {time.time()-t1:.1f}s -> {r2}")

    t1 = time.time()
    r3, fails3 = diagnose_layerA_comparison(gold_a)
    print(f"[3] 층A comparison 라우팅 완료 {time.time()-t1:.1f}s -> {r3}")

    if os.environ.get("USE_LOCAL_LLM") == "1":
        t1 = time.time()
        r4 = diagnose_local_llm_calibration(gold_b)
        print(f"[4] 로컬 LLM 캘리브레이션 완료 {time.time()-t1:.1f}s -> {r4}")
    else:
        print("[4] USE_LOCAL_LLM!=1 — 스킵")

    print(f"전체 소요 {time.time()-t0:.1f}s")
