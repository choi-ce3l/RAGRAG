#!/usr/bin/env python3
"""이벤트 앵커 커버리지 측정 도구 — corpus 전수 기준, 순수 측정 전용.

## 왜 필요한가

이벤트 앵커 novelty 카드(전후 대조)는 지금까지 하드코딩 사례 3개(삼성SDI 유상
증자, 알테오젠, 한화에어로스페이스)로만 확인됐다. "corpus에 실제로 존재하는
(기업×이벤트유형) 조합 전체에서 실제로 이 정도 비율로 맞는다"고 말하려면 전수
질문을 만들어 실제 `qa.pipeline.run()`으로 채점해야 한다 — 함수 단위 호출이나
수작업 산수로는 안 된다(이번 세션에서 확립된 규칙).

## 절대 규칙

이 스크립트는 `qa/*.py`를 한 글자도 고치지 않는다. `qa/events_vocab.py`·
`qa/filings.py`·`qa/eventspan.py`·`qa/ontology.py`는 읽기 전용 — 여기서
버그로 보이는 게 나와도 "발견된 갭"으로만 기록하고 고치지 않는다. 새 LLM
호출도 없다 — `NARRATIVE_ENABLED`/`LLMPARSE_ENABLED` 둘 다 이 스크립트에서
설정하지 않는다(기본값 off를 그대로 둔다).

## (기업, 이벤트유형) 조합과 "복수 앵커" 처리

`find_events(corp, event_type)`는 그 조합의 발생 이력을 최신순으로 준다.
같은 유형이 여러 번 결정된 회사(예: 한화에어로스페이스의 "단일판매ㆍ공급계약
체결" 29건)에는 앵커가 여럿이다. 우리 템플릿 질문은 거래상대방·프로젝트명 같은
구체 고유명사를 넣지 않으므로(정형 템플릿이라 그럴 수 없다), `contract.find()`
2차 필터가 하나로 좁힐 근거가 없다 — pipeline.py::_event_date_answer()와
eventspan.py::answer() 둘 다 이 경우 **문서화된 폴백**(주석: "못 좁히면 최신
1건이 기본 앵커다")을 그대로 따른다. 그래서 이 스크립트도 같은 문서화된 규칙을
따라 `anchors[0]`(find_events()가 이미 최신순으로 정렬해 준 것)의 rcept_dt를
기대값으로 삼는다 — 별도의 판단 로직을 새로 만드는 게 아니라 파이프라인 자신의
주석에 적힌 계약을 그대로 옮겨 쓰는 것이다. 이게 어긋나면(예: contract.find가
실제로 다른 걸 골라버리면) 그 자체가 실패로 잡히고 "흥미로운 갭"으로 보고된다.

## 템플릿별 채점 기준

1. "{corp} {alias} 언제 결정했어" — `state=="S0"`이고 답변 텍스트에 기대 날짜
   문자열("2025년 2월 4일" 형식)이 포함되면 합격.
2. "{corp} {alias} 이후 부채비율 얼마나 변했어" — 세 갈래로 분류한다.
   - `s0_computed`: state=="S0"이고 답변에 "이전(...)…→…이후(...)" 비교 문형이
     있다 (eventspan.answer()의 실제 문장 템플릿과 동일한 마커로 판별한다).
   - `explicit_fail`: state가 S0이 아니다(S1/S2/S3/S6) — 명시적으로 못 했다고
     밝힌 것이므로 "정직한 실패"로 합격 처리한다.
   - `silent_fail`: state=="S0"인데 비교 문형이 없다 — 이벤트 전후 대조가 속으로
     실패했는데 겉으로는 성공(S0)한 다른 답(예: 특정 연도 단일 값)으로 샌 것이다.
     **이게 유일한 진짜 실패다.**
   부채비율 자체가 이 corpus에 전혀 없는 회사(부채총계·자본총계 라벨이 어느
   해에도 없음)는 스킵한다(합격/불합격 집계에서 뺀다).
3. "{corp} {alias} 금액이 얼마야" — wh=amount 전용 라우팅이 아직 없다는 걸
   이미 알고 있으므로(알려진 한계), 합격/불합격 판정 없이 실제 상태(S0/S1/S2/
   S3/S6) 분포만 있는 그대로 기록한다.

## 성능

70개사 × 이벤트유형(corpus에 실제로 존재하는 만큼) 조합마다 pipeline.run()을
3번(템플릿마다 한 번) 부른다. store/labels/filings는 각각 모듈 전역 캐시가 있어
(`pipeline.get_store()`/`labelstore.get()`/`filings.get()`) 프로세스 안에서 한 번만
로드된다 — 새 캐싱 로직을 추가하지 않고 기존 캐시를 그대로 재사용한다.
"""

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qa import derived, events_vocab, filings, labelstore, pipeline  # noqa: E402

REPORT_DIR = ROOT / "data" / "eval_reports"
REPORT_MD = REPORT_DIR / "report_gen_gold_events.md"
REPORT_JSON = REPORT_DIR / "details_gen_gold_events.json"


def fmt_date_ko(dt):
    """pipeline.py::_event_date_answer()와 정확히 같은 포맷 규칙(월/일 0채움 없음)."""
    return f"{dt[:4]}년 {int(dt[4:6])}월 {int(dt[6:8])}일"


def alias_map():
    """이벤트 유형 → 구어체 별칭(있는 것만). 여러 별칭이 같은 유형을 가리키면
    _EVENT_ALIASES에 먼저 등록된 것을 쓴다(딕셔너리 삽입 순서)."""
    rev = {}
    for alias, name in events_vocab._EVENT_ALIASES.items():
        rev.setdefault(name, alias)
    return rev


def debt_ratio_available(labels, cc):
    """이 기업에 부채비율(부채총계÷자본총계, 연결)을 계산할 재료가 어느 해든
    하나라도 있는가. 없으면 템플릿2를 스킵한다(합격/불합격 집계 제외)."""
    if not cc:
        return False
    op1, op2 = derived.RULES["부채비율"][1]
    for y in labels.corp_years(cc):
        if labels.lookup(cc, op1, "consolidated", y) and labels.lookup(cc, op2, "consolidated", y):
            return True
    return False


def build_combos():
    """corpus에 실제로 존재하는 (기업, 이벤트유형) 조합 전수 — find_events()가
    실제로 앵커를 돌려준 것만 포함한다(우연이 아니라 전수 기준)."""
    fl = filings.get()
    types = events_vocab.event_types()
    aliases = alias_map()
    corps = sorted({m.get("corp_name") for m in fl.meta.values()})
    combos = []
    for etype in sorted(types):
        for corp in corps:
            anchors = fl.find_events(corp, etype)
            if not anchors:
                continue
            combos.append({
                "corp": corp,
                "type": etype,
                "alias": aliases.get(etype),
                "term": aliases.get(etype, etype),
                "n_anchors": len(anchors),
                "latest_anchor": anchors[0],   # find_events()는 최신순(내림차순)으로 준다
            })
    return combos


def grade_when(r, expected_ko):
    return r.state == "S0" and expected_ko in (r.answer_text or "")


def grade_eventspan(r):
    txt = r.answer_text or ""
    is_comparison = all(s in txt for s in ("이전(", "이후(", "→"))
    if r.state == "S0" and is_comparison:
        return "s0_computed"
    if r.state != "S0":
        return "explicit_fail"
    return "silent_fail"


def safe_run(store, labels, question):
    """pipeline.run()을 감싸 예외를 잡는다 — 크래시도 실패로 세야 한다(측정
    도구가 죽으면 커버리지를 잴 수 없다)."""
    try:
        r = pipeline.run(question, store, labels)
        return r, None
    except Exception as e:                      # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                     help="디버그용 — 처음 N개 조합만 돈다(전체 결과로 쓰면 안 됨)")
    args = ap.parse_args()

    t_start = time.time()
    store = pipeline.get_store()
    labels = labelstore.get()

    combos = build_combos()
    if args.limit:
        combos = combos[: args.limit]

    print(f"조합 {len(combos)}건 (기업 × 이벤트유형, corpus 전수) — 템플릿 3종 × 각 1건 = "
          f"최대 pipeline.run() {len(combos) * 3}회")

    debt_ok_cache = {}

    rows_t1, rows_t2, rows_t3 = [], [], []
    n_done = 0
    for combo in combos:
        corp, etype, term = combo["corp"], combo["type"], combo["term"]
        anchor = combo["latest_anchor"]
        cc = store.corp_code.get(corp)

        # ---- 템플릿1: 언제 ----
        q1 = f"{corp} {term} 언제 결정했어"
        r1, err1 = safe_run(store, labels, q1)
        expected_ko = fmt_date_ko(anchor["rcept_dt"]) if len(anchor.get("rcept_dt") or "") == 8 else None
        if err1 or not expected_ko:
            ok1 = False
        else:
            ok1 = grade_when(r1, expected_ko)
        rows_t1.append({
            "corp": corp, "type": etype, "term": term, "n_anchors": combo["n_anchors"],
            "question": q1, "expected_date": expected_ko,
            "state": (r1.state if r1 else None), "answer": (r1.answer_text if r1 else None),
            "error": err1, "pass": bool(ok1),
        })

        # ---- 템플릿2: 이후 부채비율 ----
        if cc not in debt_ok_cache:
            debt_ok_cache[cc] = debt_ratio_available(labels, cc)
        if debt_ok_cache[cc]:
            q2 = f"{corp} {term} 이후 부채비율 얼마나 변했어"
            r2, err2 = safe_run(store, labels, q2)
            if err2:
                verdict2 = "silent_fail"  # 크래시도 진짜 실패로 센다
            else:
                verdict2 = grade_eventspan(r2)
            rows_t2.append({
                "corp": corp, "type": etype, "term": term, "n_anchors": combo["n_anchors"],
                "question": q2, "state": (r2.state if r2 else None),
                "answer": (r2.answer_text if r2 else None), "error": err2,
                "verdict": verdict2,
            })
        else:
            rows_t2.append({
                "corp": corp, "type": etype, "term": term, "n_anchors": combo["n_anchors"],
                "question": None, "state": None, "answer": None, "error": None,
                "verdict": "skip_no_debt_ratio",
            })

        # ---- 템플릿3: 금액 (현황 기록만, 합격/불합격 없음) ----
        q3 = f"{corp} {term} 금액이 얼마야"
        r3, err3 = safe_run(store, labels, q3)
        rows_t3.append({
            "corp": corp, "type": etype, "term": term, "n_anchors": combo["n_anchors"],
            "question": q3, "state": (r3.state if r3 else None),
            "answer": (r3.answer_text if r3 else None), "error": err3,
        })

        n_done += 1
        if n_done % 50 == 0:
            elapsed = time.time() - t_start
            print(f"  {n_done}/{len(combos)} 조합 처리 — 경과 {elapsed:.1f}초"
                  f" (평균 {elapsed / n_done:.2f}초/조합)")

    elapsed_total = time.time() - t_start
    print(f"완료 — 조합 {len(combos)}건, 총 {elapsed_total:.1f}초"
          f" (평균 {elapsed_total / max(len(combos), 1):.2f}초/조합)")

    write_report(combos, rows_t1, rows_t2, rows_t3, elapsed_total)
    return 0


def write_report(combos, rows_t1, rows_t2, rows_t3, elapsed_total):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    n1 = len(rows_t1)
    n1_pass = sum(1 for r in rows_t1 if r["pass"])
    fails_t1 = [r for r in rows_t1 if not r["pass"]]

    t2_counts = Counter(r["verdict"] for r in rows_t2)
    n2_scored = sum(v for k, v in t2_counts.items() if k != "skip_no_debt_ratio")
    silent_fails_t2 = [r for r in rows_t2 if r["verdict"] == "silent_fail"]

    t3_state_counts = Counter(r["state"] for r in rows_t3)

    # 유형별 집계(템플릿1 기준 — 어느 유형에서 "언제" 라우팅이 계속 깨지는지)
    by_type = defaultdict(lambda: {"n": 0, "pass": 0})
    for r in rows_t1:
        by_type[r["type"]]["n"] += 1
        by_type[r["type"]]["pass"] += int(r["pass"])

    lines = []
    lines.append("# 이벤트 앵커 커버리지 리포트 (corpus 전수)\n")
    lines.append(f"생성 시각: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"조합(기업×이벤트유형) 전수: **{len(combos)}건** "
                 f"(corpus에 `find_events()`가 실제로 1건 이상 돌려준 조합만)\n")
    lines.append(f"전체 실행 시간: **{elapsed_total:.1f}초** "
                 f"(pipeline.run() 총 {n1 + len(rows_t2) - t2_counts['skip_no_debt_ratio'] * 0 + len(rows_t3)}회 근처, "
                 f"평균 {elapsed_total / max(len(combos), 1):.2f}초/조합)\n")

    lines.append("\n## 요약\n")
    lines.append("| 템플릿 | 문항수 | 결과 |")
    lines.append("|---|---|---|")
    lines.append(f"| 1. 언제 결정했어 | {n1} | S0 정답 {n1_pass}건 / 실패 {len(fails_t1)}건 "
                 f"({100 * n1_pass / max(n1, 1):.1f}% S0 정답) |")
    lines.append(f"| 2. 이후 부채비율 | {len(rows_t2)} (스킵 {t2_counts['skip_no_debt_ratio']}건 제외 시 {n2_scored}건 채점) | "
                 f"계산됨(S0) {t2_counts['s0_computed']}건 · 명시적실패(S1등) {t2_counts['explicit_fail']}건 · "
                 f"**조용한실패 {t2_counts['silent_fail']}건** |")
    lines.append(f"| 3. 금액이 얼마야 (현황 기록만) | {len(rows_t3)} | 상태 분포: "
                 + ", ".join(f"{k}={v}" for k, v in sorted(t3_state_counts.items(), key=lambda kv: -kv[1])) + " |")

    lines.append("\n## 템플릿1 — \"언제 결정했어\" 상세\n")
    lines.append(f"S0 정답 {n1_pass}/{n1}건 ({100 * n1_pass / max(n1, 1):.1f}%)\n")
    if fails_t1:
        lines.append(f"\n### 실패 사례 ({len(fails_t1)}건)\n")
        lines.append("| 기업 | 유형 | 질문 | 기대 날짜 | 실제 state | 실제 답변(앞부분) | 오류 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in fails_t1[:200]:
            ans = (r["answer"] or "")[:120].replace("|", "/").replace("\n", " ")
            lines.append(f"| {r['corp']} | {r['type']} | {r['question']} | {r['expected_date']} | "
                         f"{r['state']} | {ans} | {r['error'] or ''} |")
        if len(fails_t1) > 200:
            lines.append(f"\n(그 외 {len(fails_t1) - 200}건 생략 — 전체는 details_gen_gold_events.json 참고)\n")
    else:
        lines.append("\n실패 사례 없음.\n")

    lines.append("\n### 유형별 \"언제\" 정답률 (전체 " + str(len(by_type)) + "개 유형)\n")
    lines.append("| 이벤트 유형 | 문항수 | S0 정답 | 정답률 |")
    lines.append("|---|---|---|---|")
    for t, agg in sorted(by_type.items(), key=lambda kv: (-kv[1]["n"], kv[0])):
        rate = 100 * agg["pass"] / max(agg["n"], 1)
        lines.append(f"| {t} | {agg['n']} | {agg['pass']} | {rate:.0f}% |")

    lines.append("\n## 템플릿2 — \"이후 부채비율 얼마나 변했어\" 상세\n")
    lines.append(f"채점 대상 {n2_scored}건 (부채비율 데이터 자체가 없어 스킵한 조합 "
                 f"{t2_counts['skip_no_debt_ratio']}건 제외)\n")
    lines.append(f"- 계산됨(S0, 실제 전후 비교 문형 확인): {t2_counts['s0_computed']}건\n"
                 f"- 명시적 실패(S0 아님 — S1/S2/S3/S6): {t2_counts['explicit_fail']}건\n"
                 f"- **조용한 실패(S0인데 전후 비교가 아닌 다른 답으로 샘)**: {t2_counts['silent_fail']}건\n")
    if silent_fails_t2:
        lines.append(f"\n### 조용한 실패 사례 ({len(silent_fails_t2)}건) — 진짜 실패\n")
        lines.append("| 기업 | 유형 | 질문 | 실제 state | 실제 답변 |")
        lines.append("|---|---|---|---|---|")
        for r in silent_fails_t2[:200]:
            ans = (r["answer"] or "")[:200].replace("|", "/").replace("\n", " ")
            lines.append(f"| {r['corp']} | {r['type']} | {r['question']} | {r['state']} | {ans} |")
        if len(silent_fails_t2) > 200:
            lines.append(f"\n(그 외 {len(silent_fails_t2) - 200}건 생략 — 전체는 details_gen_gold_events.json 참고)\n")
    else:
        lines.append("\n조용한 실패 사례 없음.\n")

    lines.append("\n## 템플릿3 — \"금액이 얼마야\" (합격/불합격 아님, 현황 기록)\n")
    lines.append("wh=amount 전용 라우팅이 아직 없다는 걸 이미 알고 있으므로, 실제로 나온 "
                 "상태(state)만 있는 그대로 기록한다. **이 표는 커버리지 측정이지 판정이 아니다.**\n")
    lines.append("| state | 건수 |")
    lines.append("|---|---|")
    for k, v in sorted(t3_state_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} |")
    lines.append("\n### 표본 (앞 30건)\n")
    lines.append("| 기업 | 유형 | 질문 | state | 답변(앞부분) |")
    lines.append("|---|---|---|---|---|")
    for r in rows_t3[:30]:
        ans = (r["answer"] or "")[:120].replace("|", "/").replace("\n", " ")
        lines.append(f"| {r['corp']} | {r['type']} | {r['question']} | {r['state']} | {ans} |")

    lines.append("\n## 한계와 확신 없는 부분 (정직하게 명시)\n")
    lines.append(
        "- **복수 앵커 처리**: 같은 (기업, 유형) 조합에 결정이 여러 번 있으면(예: "
        "위 유형별 표에서 문항수=1이지만 실제 사건은 여럿인 경우), 우리 템플릿 질문에는 "
        "거래상대방·프로젝트명 같은 구체 고유명사가 없어 `contract.find()`가 하나로 못 좁힌다. "
        "이 스크립트는 파이프라인 자신의 문서화된 폴백 규칙(주석: \"못 좁히면 최신 1건이 "
        "기본 앵커다\")을 그대로 따라 `find_events()`가 준 최신 앵커(anchors[0])의 날짜를 "
        "기대값으로 삼았다. 이건 회귀 판정 기준이 아니라 커버리지 측정을 위한 결정론적 "
        "선택이며, 실제로 여러 건이 있는 조합에서 개별 앵커별 채점은 하지 않는다(정형 질문 "
        "문구만으로는 애초에 구분할 수 없기 때문).\n"
    )
    lines.append(
        "- **부채비율 스킵 기준**: `qa/derived.py`의 부채비율 규칙(부채총계÷자본총계, 연결)이 "
        "쓰는 두 라벨이 그 기업의 어느 사업연도에도 없으면 템플릿2를 스킵으로 뺐다. 이 corpus의 "
        "70개사를 실측한 결과 스킵 대상은 0건이었다(금융지주사도 부채총계/자본총계 라벨이 "
        "있었다) — \"금융사라 없을 것\"이라는 추정은 이 corpus에서는 틀렸다.\n"
    )
    lines.append(
        "- **이벤트 유형 어휘의 긴 꼬리**: `events_vocab.event_types()`가 돌려주는 유형 중 "
        "다수(특히 \"투자판단관련주요경영사항 (...)\")는 report_nm의 괄호 안 세부 설명이 "
        "그대로 유형 이름이 되어 사실상 1건짜리 고유 유형이다. 이런 유형은 별칭이 없어 "
        "질문에 유형명 원문을 그대로 넣었다 — 사람이 실제로 물을 법한 문장은 아니지만, "
        "corpus 전수라는 이번 작업의 정의를 그대로 따른 것이다.\n"
    )

    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    detail = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_combos": len(combos),
        "elapsed_seconds": elapsed_total,
        "template1_when": rows_t1,
        "template2_eventspan": rows_t2,
        "template3_amount": rows_t3,
    }
    REPORT_JSON.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"리포트 저장: {REPORT_MD}")
    print(f"상세 JSON 저장: {REPORT_JSON}")


if __name__ == "__main__":
    sys.exit(main())
