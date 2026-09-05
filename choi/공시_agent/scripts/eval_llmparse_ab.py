#!/usr/bin/env python3
"""qa/llmparse.py::extract_slots()의 "현재 프롬프트 버전(A)" vs "미래 튜닝
버전(B)"을 같은 held-out 세트(data/llmparse_tuning/eval.jsonl)로 비교하는 하네스.

## 절대 규칙 — 기본 실행은 CLOVA API를 한 번도 부르지 않는다

이 스크립트를 인자 없이 그냥 실행하면(`python scripts/eval_llmparse_ab.py`)
**가짜 예측기 두 개**(완벽한 예측기 / 항상 틀리는 예측기)로만 채점 로직을
단위 테스트하고, conv_replay 하네스도 tests/conv_replay/test_conv_replay.py와
똑같은 하드코딩 mock(FAKE_SLOTS)으로만 돌린다 — 네트워크 호출이 전혀 없다.

실제 `qa.llmparse.extract_slots`(CLOVA_API_KEY 필요)로 돌리려면 `--live`를
줘야 하고, 그마저도 `LLMPARSE_AB_LIVE_CONFIRM=1` 환경변수가 없으면 즉시
중단한다 — 사용자 승인 없이 API 비용이 나가는 걸 막는 이중 게이트다.
이번 세션에서는 `--live`를 한 번도 주지 않았다(아래 "무엇을 안 돌렸는가" 참고).

## 측정 지표

1. 슬롯 정확도 — 전체 exact-match율 + 슬롯별(corp/concept/year/scope/event/wh/intent) 정확도
2. 가드 거부율 — `_validate_slot()`이 실제로 값을 버린 비율. adversarial_negative
   카테고리에서 특히 "모델이 애초에 null을 냈는지"(validation=="null") vs
   "모델이 오답을 냈는데 가드가 잡았는지"(validation=="rejected_*")를 구분한다.
   이건 data/llmparse_log.jsonl(extract_slots()가 매 호출마다 남기는 로그)의
   `validation` 필드로만 가능하다 — extract_slots()의 반환값 자체는 이미
   검증을 통과한 값만 담고 있어 이 구분이 없어진 뒤이기 때문이다.
3. conv_replay 통과율 — tests/conv_replay/test_conv_replay.py의 T1~T6를 실제
   호출(모킹 없이)로 돌린 통과율. B 자리가 생기면 모델 이름만 바꿔 끼우면 된다.

    python scripts/eval_llmparse_ab.py                 # 가짜 예측기 자체 테스트만 (API 없음)
    python scripts/eval_llmparse_ab.py --live           # 실제 A 실행 (승인 게이트 필요)
"""

import argparse
import copy
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

# --live 실측(2026-09-04): 141건을 딜레이 없이 연속 호출했더니 127/141이
# 429(Too many requests)로 실패했다(data/llmparse_log.jsonl의 reason 필드로
# 확인). 호출 사이에 이 만큼 쉰다 — CLOVA Studio의 정확한 초당 한도를 몰라서
# 넉넉하게 잡았다(과소평가해서 또 rate limit 맞는 것보다 좀 느린 게 낫다).
LIVE_CALL_DELAY_SEC = 3.0

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qa import labelstore, llmparse, narrative, pipeline   # noqa: E402

EVAL_PATH = ROOT / "data" / "llmparse_tuning" / "eval.jsonl"
HELDOUT_PATH = ROOT / "data" / "llmparse_tuning" / "heldout.jsonl"
SLOT_KEYS = llmparse._SLOT_KEYS


# ---------------------------------------------------------------------------
# eval.jsonl 로딩 — (system,user,output) 3필드에서 extract_slots() 호출에 필요한
# (question, prev_question, prev_slots)를 되살린다. user는 llmparse._build_user_prompt()가
# 만든 고정 형식이라(이 스크립트가 그 포맷을 새로 정의한 게 아니라 그쪽 포맷을
# 그대로 따르는 것이므로) 역파싱이 안전하다.
# ---------------------------------------------------------------------------
def parse_user_prompt(user_text):
    q, prev_q, prev_slots = None, None, None
    for line in user_text.splitlines():
        if line.startswith("지금 질문: "):
            q = line[len("지금 질문: "):]
        elif line.startswith("직전 질문: "):
            prev_q = line[len("직전 질문: "):]
        elif line.startswith("직전 확정 슬롯: "):
            prev_slots = json.loads(line[len("직전 확정 슬롯: "):])
    return q, prev_q, prev_slots


def load_eval_set(path=EVAL_PATH):
    out = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            q, prev_q, prev_slots = parse_user_prompt(r["user"])
            out.append({
                "question": q, "prev_question": prev_q, "prev_slots": prev_slots,
                "gold": json.loads(r["output"]), "category": r["category"], "note": r["note"],
            })
    return out


# ---------------------------------------------------------------------------
# 채점 — predictor(question, prev_question, prev_slots, store, labels) -> dict|None
# 반환 형식은 llmparse.extract_slots()와 정확히 같다(검증까지 끝난 슬롯 dict).
# 이래야 A(현재 프롬프트로 부른 extract_slots 그 자체)와 B(나중에 모델 이름만
# 바꾼 같은 함수)를 같은 채점 코드로 비교할 수 있다.
# ---------------------------------------------------------------------------
def run_predictor(predictor, eval_set, store, labels, delay=0.0):
    """delay>0이면 호출 사이에 그만큼 쉰다(초) — 실제 CLOVA 호출에서 rate limit을
    피하려는 용도. 가짜 예측기(self-test) 호출에는 delay=0을 쓴다(API가 아니라
    필요 없다)."""
    preds = []
    for i, ex in enumerate(eval_set):
        preds.append(predictor(ex["question"], ex["prev_question"], ex["prev_slots"], store, labels))
        if delay and i < len(eval_set) - 1:
            time.sleep(delay)
    return preds


def score_predictions(eval_set, preds):
    assert len(preds) == len(eval_set)
    n = len(eval_set)
    per_slot = {k: [0, 0] for k in SLOT_KEYS}
    by_cat = defaultdict(lambda: [0, 0])
    exact = 0
    rows = []
    for ex, pred in zip(eval_set, preds):
        gold = ex["gold"]
        p = pred or {k: None for k in SLOT_KEYS}
        ok_all = True
        for k in SLOT_KEYS:
            per_slot[k][1] += 1
            match = p.get(k) == gold.get(k)
            per_slot[k][0] += int(match)
            ok_all = ok_all and match
        exact += int(ok_all)
        cat = ex["category"]
        by_cat[cat][1] += 1
        by_cat[cat][0] += int(ok_all)
        rows.append({"category": cat, "question": ex["question"], "gold": gold, "pred": p, "exact": ok_all})
    report = {
        "n": n,
        "exact_match_rate": exact / n if n else 0.0,
        "per_slot_accuracy": {k: (v[0] / v[1] if v[1] else 0.0) for k, v in per_slot.items()},
        "per_category_exact_match": {c: (v[0] / v[1] if v[1] else 0.0) for c, v in by_cat.items()},
    }
    return report, rows


# ---------------------------------------------------------------------------
# heldout.jsonl 채점 — 사용자가 손으로 쓴 40건(캐주얼 20 + 적대적 20).
# eval.jsonl(생성기 산출, 7슬롯 전부 채워진 완전한 정답)과 다르게, 각 레코드는
# expected(일부 슬롯만)·acceptable(슬롯별 허용값 집합)·forbidden(corp 절대
# 금지값 목록)을 따로 갖는다 — build_heldout.py가 이미 (named 매크로 해석,
# wh 한글→내부 토큰, event 문자 정규화, 스키마 밖 필드 제거)를 다 끝내 둔
# 파일이라 여기서는 채점만 한다.
# ---------------------------------------------------------------------------
def load_heldout_set(path=HELDOUT_PATH):
    out = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            out.append(r)
    return out


def grade_heldout_record(rec, pred):
    """expected/acceptable은 "최종 해석값"(사용자가 원하는 완결된 의미)을 적어 둔
    것이다. 그런데 extract_slots()의 실제 스키마에서는 그 슬롯이 직전 턴과 값이
    같아 새로 지정되지 않았으면 null을 내는 게 정답이다(null=승계, 병합은
    _merge_slots()가 한다) — null을 내든 그 값을 그대로 반복하든 최종 해석은
    똑같다. 그래서 prev_slots[k]가 이미 expected/acceptable 값과 같으면 null도
    정답으로 인정한다. (실측 확인: c18에서 이 예외가 없으면 모델이 정확히
    "바뀐 corp는 명시, 안 바뀐 concept/year는 null"이라는 교과서적으로 맞는
    행동을 했는데도 오답으로 잘못 채점됐다.)
    """
    pred = pred or {k: None for k in SLOT_KEYS}
    expected = rec.get("expected") or {}
    acceptable = rec.get("acceptable") or {}
    forbidden = set(rec.get("forbidden") or [])
    prev_slots = rec.get("prev_slots") or {}

    forbidden_hit = pred.get("corp") in forbidden and pred.get("corp") is not None

    graded = {}
    for k in SLOT_KEYS:
        if k in expected:
            gold_vals = expected[k] if isinstance(expected[k], list) else [expected[k]]
        elif k in acceptable:
            gold_vals = acceptable[k] if isinstance(acceptable[k], list) else [acceptable[k]]
        else:
            continue  # 이 레코드에선 이 슬롯을 채점하지 않는다(작성자가 의견 없음)
        if k in prev_slots and prev_slots[k] in gold_vals and None not in gold_vals:
            gold_vals = gold_vals + [None]   # 안 바뀐 슬롯은 null(승계)도 정답
        correct = pred.get(k) in gold_vals
        graded[k] = correct

    ok_all = all(graded.values()) and not forbidden_hit
    return graded, ok_all, forbidden_hit


def score_heldout(heldout_set, preds):
    assert len(preds) == len(heldout_set)
    per_slot = defaultdict(lambda: [0, 0])          # 채점된 것만 분모에 들어간다
    per_cat = defaultdict(lambda: [0, 0])
    null_stats = {"expected_null_total": 0, "expected_null_correct": 0}   # "없음" 선택률
    incident_rows = []
    fail_rows = []
    rows = []
    for rec, pred in zip(heldout_set, preds):
        graded, ok_all, forbidden_hit = grade_heldout_record(rec, pred)
        for k, correct in graded.items():
            per_slot[k][1] += 1
            per_slot[k][0] += int(correct)
            exp_v = rec.get("expected", {}).get(k, rec.get("acceptable", {}).get(k))
            if exp_v is None or exp_v == [None]:
                null_stats["expected_null_total"] += 1
                null_stats["expected_null_correct"] += int(correct)
        cat = rec["category"]
        per_cat[cat][1] += 1
        per_cat[cat][0] += int(ok_all)
        row = {"id": rec["id"], "category": cat, "question": rec["question"],
               "prev_slots": rec.get("prev_slots"), "pred": pred, "expected": rec.get("expected"),
               "acceptable": rec.get("acceptable"), "forbidden_hit": forbidden_hit,
               "ok_all": ok_all, "graded": graded}
        rows.append(row)
        if rec.get("incident_reproduction"):
            incident_rows.append(row)
        if not ok_all:
            fail_rows.append(row)
    report = {
        "n": len(heldout_set),
        "per_slot_accuracy": {k: (v[0] / v[1] if v[1] else None) for k, v in per_slot.items()},
        "per_slot_n": {k: v[1] for k, v in per_slot.items()},
        "per_category_pass_rate": {c: (v[0] / v[1] if v[1] else 0.0) for c, v in per_cat.items()},
        "negative_null_selection_rate": (
            null_stats["expected_null_correct"] / null_stats["expected_null_total"]
            if null_stats["expected_null_total"] else None),
        "negative_null_n": null_stats["expected_null_total"],
        "n_fail": len(fail_rows),
    }
    return report, rows, incident_rows, fail_rows


# ---------------------------------------------------------------------------
# 가짜 예측기 두 개 — API 호출 전혀 없음. 채점 로직 자체를 검증하는 용도.
# ---------------------------------------------------------------------------
def make_perfect_predictor(eval_set):
    """eval.jsonl의 정답을 그대로 돌려준다 → exact_match_rate가 정확히 1.0이어야 한다."""
    gold_by_q = {(ex["question"], ex["prev_question"]): ex["gold"] for ex in eval_set}

    def _predict(question, prev_question, prev_slots, store, labels):
        return dict(gold_by_q[(question, prev_question)])
    return _predict


_WRONG_FIXED_GUESS = {"corp": "삼성전자", "concept": "매출액", "year": 2021,
                      "scope": "separate", "event": None, "wh": "who", "intent": "existence"}


def always_wrong_predictor(question, prev_question, prev_slots, store, labels):
    """무엇을 묻든 항상 같은 고정값을 낸다 — "아무 값이나 내는" 나쁜 예측기.

    일부 슬롯은 우연히 정답(대부분 null)과 맞을 수 있으나, 대부분의 골드가
    구체적인 값이거나(예: adversarial_negative의 corp:B) 이 고정값과 다른 값이라
    exact_match_rate가 완벽한 예측기보다 뚜렷하게 낮게 나온다(아래 self_test에서
    실측 확인).
    """
    return dict(_WRONG_FIXED_GUESS)


def self_test():
    """가짜 예측기 두 개로 채점 로직 자체를 검증한다. API 호출 없음."""
    eval_set = load_eval_set()
    perfect = make_perfect_predictor(eval_set)
    preds_perfect = [perfect(ex["question"], ex["prev_question"], None, None, None) for ex in eval_set]
    report_perfect, _ = score_predictions(eval_set, preds_perfect)

    preds_wrong = [always_wrong_predictor(ex["question"], ex["prev_question"], None, None, None)
                   for ex in eval_set]
    report_wrong, _ = score_predictions(eval_set, preds_wrong)

    print(f"[self-test] eval.jsonl {len(eval_set)}건")
    print(f"  완벽한 예측기 exact_match_rate = {report_perfect['exact_match_rate']:.4f} (기대: 1.0000)")
    print(f"  항상틀림 예측기 exact_match_rate = {report_wrong['exact_match_rate']:.4f} (기대: 훨씬 낮음)")
    assert report_perfect["exact_match_rate"] == 1.0, "완벽한 예측기가 100%가 아님 — 채점 로직 버그"
    assert report_wrong["exact_match_rate"] < 0.5, "항상틀림 예측기가 너무 정확함 — 채점 로직 버그 의심"
    print("  → 채점 로직 자체 검증 통과 (assert 2건)\n")
    return report_perfect, report_wrong


# ---------------------------------------------------------------------------
# 가드 거부율 — "모델이 애초에 null을 냈다" vs "모델이 오답을 냈는데 가드가
# 잡아 결과적으로 null이 됐다"를 구분한다. extract_slots()의 반환값(검증 후)
# 만으로는 이 구분이 사라지므로, data/llmparse_log.jsonl의 raw validation 기록을
# 봐야 한다(qa/llmparse.py::_log_slots_call 포맷). 순수 함수라 API 없이도
# 합성 로그 항목으로 단위 테스트할 수 있다.
# ---------------------------------------------------------------------------
def guard_rejection_breakdown(log_entries, slot="corp"):
    """log_entries: llmparse._log_slots_call()이 쓰는 것과 같은 dict의 리스트.

    반환: {"native_null": N, "guarded_reject": N, "ok": N, "total": N}
    - native_null: 모델이 처음부터 이 슬롯에 null을 냄(validation[slot]=="null")
    - guarded_reject: 모델이 후보 밖 값·범위 밖 값을 냈다가 가드에 걸림
      (validation[slot]이 "rejected_"로 시작)
    - ok: 후보 안의 값을 내고 그대로 통과
    """
    out = Counter()
    for e in log_entries:
        v = (e.get("validation") or {}).get(slot)
        if v == "null":
            out["native_null"] += 1
        elif v and v.startswith("rejected_"):
            out["guarded_reject"] += 1
        elif v == "ok":
            out["ok"] += 1
        out["total"] += 1
    return dict(out)


def _guard_breakdown_self_test():
    """합성 로그 항목(API 호출 없음)으로 guard_rejection_breakdown()을 검증한다."""
    synthetic = [
        {"validation": {"corp": "null"}},                        # 모델이 처음부터 null
        {"validation": {"corp": "null"}},
        {"validation": {"corp": "rejected_not_in_candidates"}},   # 모델이 틀렸는데 가드가 잡음
        {"validation": {"corp": "ok"}},                           # 정상 통과
    ]
    out = guard_rejection_breakdown(synthetic, "corp")
    assert out == {"native_null": 2, "guarded_reject": 1, "ok": 1, "total": 4}, out
    print("[self-test] guard_rejection_breakdown 합성 로그 검증 통과:", out, "\n")


# ---------------------------------------------------------------------------
# conv_replay 통과율 — tests/conv_replay/test_conv_replay.py의 6턴을 그대로
# 재생하되, extract_slots 구현을 인자로 갈아끼울 수 있게 만든다(그 파일은
# 임포트되는 순간 자기 mock으로 6턴을 즉시 실행해버리는 스크립트라 그대로
# 재사용할 수 없다 — 체크 함수만 이 파일 것과 동일하게 재구성했다).
# ---------------------------------------------------------------------------
def _slots_from(r):
    p = r.parsed or {}
    return {"corp": p.get("corp"), "corps": p.get("corps") or [],
            "concept": p.get("concept"), "year": p.get("year"), "scope": p.get("scope")}


def _check_t1(r):
    corps = r.parsed.get("corps") or []
    if r.state != "S0":
        return False, f"S0 기대, 실제 {r.state}"
    if corps != ["한화에어로스페이스"]:
        return False, f"corps={corps}"
    return True, "OK"


def _check_t2(r):
    if r.state not in ("S0", "S1"):
        return False, f"S0/S1 기대, 실제 {r.state}"
    if r.parsed.get("wh") != "when":
        return False, f"wh={r.parsed.get('wh')}"
    if "천원" in r.answer_text or "억원" in r.answer_text or "조원" in r.answer_text:
        return False, f"XBRL 금액이 나옴 — {r.answer_text}"
    if r.state == "S0" and not any(ch.isdigit() for ch in r.answer_text):
        return False, "S0인데 답변에 날짜(숫자)가 없음"
    return True, "OK"


def _check_t4(r):
    corps = r.parsed.get("corps") or []
    if corps != []:
        return False, f"corps={corps}"
    per_corp_evidence = [e for e in (r.evidence or []) if e.get("corp_name")]
    if per_corp_evidence:
        return False, f"회사별 근거가 있음 — {per_corp_evidence}"
    return True, "OK"


def _check_t5(r):
    blob = (r.answer_text or "") + " ".join(e.get("corp_name", "") for e in (r.evidence or []))
    if "LIG디펜스앤에어로스페이스" in blob:
        return False, f"LIG디펜스앤에어로스페이스 등장 — {blob[:200]}"
    if r.state not in ("S2", "S3"):
        return False, f"S2/S3 기대, 실제 {r.state}"
    return True, "OK"


def _check_t6(r):
    corps = r.parsed.get("corps") or []
    if corps != ["한화에어로스페이스"]:
        return False, f"corps={corps}"
    return True, "OK"


_TURNS = [
    ("T1", "한화에어로스페이스가 유상증자를 결정한 이후 부채비율이 어떻게 변했어?", _check_t1),
    ("T2", "유상증자를 언제 결정했는데?", _check_t2),
    ("T3", "모든 경우의수 다 고려해서 알려줘", _check_t2),
    ("T4", "연결/별도 기준이 다르므로 구분이 필요 무슨 말이야", _check_t4),
    ("T5", "투자할만한 회사야?", _check_t5),
    ("T6", "내가 한화 에어로 스페이스라고 하지않았나?", _check_t6),
]


def run_conv_replay(extract_slots_fn, narrative_load_key_fn=lambda: None, delay=0.0):
    """T1~T6를 주어진 extract_slots 구현으로 재생한다.

    extract_slots_fn 자리에 llmparse.extract_slots(진짜, CLOVA_API_KEY 필요)를
    넣으면 실제 통과율이 나온다 — 이번 세션은 넣지 않는다(아래 main() 참고).
    안전망으로 narrative.load_key도 갈아끼운다(test_conv_replay.py와 동일한
    이유 — narrative/resolve 폴백이 실제로 켜져도 네트워크로 못 나가게).
    delay>0이면 턴 사이에 그만큼 쉰다(실제 API 호출 시 rate limit 회피용).
    """
    orig_extract, orig_load_key = llmparse.extract_slots, narrative.load_key
    llmparse.extract_slots = extract_slots_fn
    narrative.load_key = narrative_load_key_fn
    os.environ.setdefault("LLMPARSE_ENABLED", "1")
    os.environ.setdefault("NARRATIVE_ENABLED", "1")
    try:
        results = []
        prev_question, prev_slots = None, None
        for i, (label, question, check) in enumerate(_TURNS):
            r = pipeline.run(question, prev_question=prev_question, prev_slots=prev_slots)
            ok, detail = check(r)
            results.append((label, question, ok, detail, r.state))
            if r.state in ("S0", "S2"):
                prev_slots = _slots_from(r)
            prev_question = question
            if delay and i < len(_TURNS) - 1:
                time.sleep(delay)
        return results
    finally:
        llmparse.extract_slots = orig_extract
        narrative.load_key = orig_load_key


# 실제 트레이스를 역산해 재구성한 mock — tests/conv_replay/test_conv_replay.py의
# FAKE_SLOTS를 그대로 가져온다(그 파일의 주석에 각 값의 근거가 있다).
_FAKE_SLOTS = {
    "유상증자를 언제 결정했는데?": {
        "corp": None, "concept": None, "year": None, "scope": None,
        "event": "유상증자결정", "wh": "when", "intent": None,
    },
    "모든 경우의수 다 고려해서 알려줘": {
        "corp": None, "concept": None, "year": None, "scope": None,
        "event": "유상증자결정", "wh": "when", "intent": None,
    },
    "투자할만한 회사야?": {
        "corp": "LIG디펜스앤에어로스페이스", "concept": None, "year": None,
        "scope": None, "event": None, "wh": None, "intent": None,
    },
}


def _mock_extract_slots(question, prev_question, prev_slots, store, labels, timeout=8):
    return _FAKE_SLOTS.get(question)


def conv_replay_self_test():
    """mock(API 호출 없음)으로 run_conv_replay() 하네스 자체를 검증한다."""
    results = run_conv_replay(_mock_extract_slots)
    print("[self-test] conv_replay (mock, API 호출 없음)")
    n_fail = 0
    for label, question, ok, detail, state in results:
        mark = "PASS" if ok else "FAIL"
        n_fail += int(not ok)
        print(f"  {label:<4}{state:<6}{mark:<6}{detail}")
    print(f"  → {len(results) - n_fail}/{len(results)} 통과 (mock 기준)\n")
    return results


# ---------------------------------------------------------------------------
# --live: 실제 A(현재 프롬프트) 실행. 이중 게이트 — 플래그 + 환경변수.
# ---------------------------------------------------------------------------
def run_live_a(dataset="eval"):
    if not os.environ.get("LLMPARSE_AB_LIVE_CONFIRM"):
        print("--live 요청됐지만 LLMPARSE_AB_LIVE_CONFIRM=1 환경변수가 없어 중단합니다 "
              "(CLOVA API 비용 발생 — 사용자 승인 없이 실행 금지).")
        sys.exit(1)
    if not narrative.load_key():
        print("CLOVA_API_KEY를 찾을 수 없어 중단합니다.")
        sys.exit(1)
    store = pipeline.get_store()
    labels = labelstore.get()

    def real_predictor(question, prev_question, prev_slots, store, labels):
        return llmparse.extract_slots(question, prev_question, prev_slots, store, labels)

    if dataset == "heldout":
        heldout_set = load_heldout_set()
        n_calls = len(heldout_set)
        print(f"[--live --dataset heldout] 실제 extract_slots() 호출 {n_calls}건 시작 "
              f"(CLOVA API 실사용) ...")
        preds = run_predictor(
            real_predictor,
            [{"question": r["question"], "prev_question": r.get("prev_question"),
              "prev_slots": r.get("prev_slots")} for r in heldout_set],
            store, labels, delay=LIVE_CALL_DELAY_SEC)
        report, rows, incident_rows, fail_rows = score_heldout(heldout_set, preds)
        print(json.dumps(report, ensure_ascii=False, indent=2))

        print(f"\n=== 사고 재현 4건 (a10, c11, c12, c18) ===")
        for row in incident_rows:
            mark = "PASS" if row["ok_all"] else "FAIL"
            print(f"  [{row['id']}] {mark} — Q: {row['question']}")
            print(f"      prev_slots: {row['prev_slots']}")
            print(f"      예측: {row['pred']}")
            print(f"      기대(expected): {row['expected']} / 허용(acceptable): {row['acceptable']}"
                  + (" / forbidden 위반!" if row["forbidden_hit"] else ""))

        print(f"\n=== 실패 {len(fail_rows)}건 상세 (원문·모델출력·정답슬롯) ===")
        for row in fail_rows:
            print(f"  [{row['id']}/{row['category']}] Q: {row['question']}")
            print(f"      prev_slots: {row['prev_slots']}")
            print(f"      모델 출력: {row['pred']}")
            print(f"      expected: {row['expected']} / acceptable: {row['acceptable']}"
                  + (" / forbidden 위반!" if row["forbidden_hit"] else ""))
        print(f"\n실제 API 호출 수(이번 run_live_a 호출): {n_calls}")
        return report, rows

    eval_set = load_eval_set()
    n_calls = len(eval_set)
    print(f"[--live --dataset eval] 실제 extract_slots() 호출 {n_calls}건 시작 (CLOVA API 실사용) ...")
    preds = run_predictor(real_predictor, eval_set, store, labels, delay=LIVE_CALL_DELAY_SEC)
    report, rows = score_predictions(eval_set, preds)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    time.sleep(LIVE_CALL_DELAY_SEC)
    print("\nconv_replay (실제 extract_slots, 모킹 없음):")
    results = run_conv_replay(llmparse.extract_slots, delay=LIVE_CALL_DELAY_SEC)
    for label, question, ok, detail, state in results:
        print(f"  {label:<4}{state:<6}{'PASS' if ok else 'FAIL':<6}{detail}")
    n_calls += len(results)
    print(f"\n실제 API 호출 수(이번 run_live_a 호출, eval {len(eval_set)}건 + conv_replay {len(results)}건): {n_calls}")
    return report, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="실제 CLOVA API로 A(현재 프롬프트)를 돌린다 — 승인 게이트 필요")
    ap.add_argument("--dataset", choices=["eval", "heldout"], default="eval",
                    help="--live와 함께 쓴다 — eval.jsonl(생성기, 141건) 또는 "
                         "heldout.jsonl(사용자 수기 40건)")
    args = ap.parse_args()
    if args.live:
        run_live_a(dataset=args.dataset)
        return
    self_test()
    _guard_breakdown_self_test()
    conv_replay_self_test()
    print("전부 API 호출 없이 완료 — --live를 주지 않으면 이 스크립트는 네트워크에 나가지 않는다.")


if __name__ == "__main__":
    main()
