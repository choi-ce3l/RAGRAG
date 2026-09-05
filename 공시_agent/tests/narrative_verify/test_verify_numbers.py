#!/usr/bin/env python3
"""narrative.verify_numbers()의 G5(근거 문서 정합) 확장 회귀 테스트.

실제 라이브에서 관찰된 사고를 합성 데이터로 고정한다: "두산로보틱스 회사합병
금액이 얼마야"에 narrative가 "1,544,278,681원(사업결합 관련 취득 직접원가)"을
확신 있게 답했는데, 그 숫자는 실제 "회사합병결정" 공시가 아니라 무관한
사업보고서(Doosan Robotics Americas, Inc. 지분 취득 관련 주석)에서 온 것이었다.
숫자 자체는 원문에 진짜 있어 기존 숫자-원문 대조만으로는 못 잡는다.

이 테스트가 쓰는 rcept_no 두 개는 지어낸 값이 아니라 실제 corpus에서 조회한
값이다(실행 로그는 이 파일이 아니라 작업 보고에 남긴다):
  - 이벤트 앵커(진짜 "회사합병결정" 공시): qa.filings.get().find_events(
    "두산로보틱스", "회사합병결정") → 8건 중 최신 rcept_no="20241210000312"
  - 무관한 문서(숫자가 실제로 나오는 곳): qa.chunkstore.get()에서
    corp_name="두산로보틱스"이고 텍스트에 "1,544,278,681"이 포함된 레코드
    → rcept_no="20260318001562" (사업보고서 (2025.12), "Doosan Robotics
    Americas, Inc." 지분 취득 관련 사업결합 주석 — 회사합병결정과 무관)

실제 LLM을 부르지 않는다 — narrative.generate()는 아예 호출하지 않고
verify_numbers()라는 순수 함수만 직접 검증한다.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from qa import narrative  # noqa: E402

ANCHOR_RCEPT = "20241210000312"        # 실제 회사합병결정 공시(최신 앵커)
UNRELATED_RCEPT = "20260318001562"     # 무관한 사업보고서(같은 숫자가 실제로 등장)

ANSWER = ("두산로보틱스의 회사합병과 관련한 취득 직접원가는 1,544,278,681원"
          "(사업결합 관련 취득 직접원가)입니다.")

P = {
    "corp": "두산로보틱스",
    "event": {
        "type": "회사합병결정",
        "anchors": [
            {"corp": "두산로보틱스", "rcept_no": ANCHOR_RCEPT,
             "rcept_dt": "20241210", "report_nm": "[기재정정]주요사항보고서(회사합병결정)",
             "is_correction": False},
        ],
    },
}

MISMATCHED_CHUNKS = [
    {"corp_name": "두산로보틱스", "report_nm": "사업보고서 (2025.12)",
     "rcept_no": UNRELATED_RCEPT,
     "text": ("사업결합과 관련하여 발생한 취득관련 직접원가 1,544,278,681원은 "
              "모두 발생시점에 비용 처리하였습니다.")},
]

MATCHED_CHUNKS = [
    {"corp_name": "두산로보틱스", "report_nm": "[기재정정]주요사항보고서(회사합병결정)",
     "rcept_no": ANCHOR_RCEPT,
     "text": ("사업결합과 관련하여 발생한 취득관련 직접원가 1,544,278,681원은 "
              "모두 발생시점에 비용 처리하였습니다.")},
]

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))


# ---------------------------------------------------------------------------
# 1) 회귀로 고정할 실제 사고 — 인용 숫자는 원문에 있으나(mismatched chunk),
#    그 청크의 rcept_no가 이벤트 anchors와 다르다 → 실패(ok=False)해야 한다.
# ---------------------------------------------------------------------------
r1 = narrative.verify_numbers(ANSWER, MISMATCHED_CHUNKS, p=P)
check("G5-mismatch → ok False", r1["ok"] is False, r1)
check("G5-mismatch → detail에 근거 문서 불일치 표시", "불일치" in r1["detail"], r1["detail"])

# ---------------------------------------------------------------------------
# 2) 같은 숫자가 실제 이벤트 앵커 문서(rcept_no 일치)에서 나온 경우는 통과해야
#    한다 — G5가 "이벤트 질문에는 무조건 실패"가 아니라 문서 정합만 본다는 것.
# ---------------------------------------------------------------------------
r2 = narrative.verify_numbers(ANSWER, MATCHED_CHUNKS, p=P)
check("G5-matched anchor → ok True", r2["ok"] is True, r2)

# ---------------------------------------------------------------------------
# 3) 하위호환 — p를 안 주면(기존 호출부) 예전처럼 숫자-원문 대조만 본다.
#    mismatched 청크라도 p 없이는 통과해야 한다(범위를 좁게 잡는다).
# ---------------------------------------------------------------------------
r3 = narrative.verify_numbers(ANSWER, MISMATCHED_CHUNKS)
check("p 없음 → 기존 동작(숫자만 대조, ok True)", r3["ok"] is True, r3)

# ---------------------------------------------------------------------------
# 4) p는 있지만 이벤트/anchors가 없는 일반 서술형 질문 — G5 검사를 건너뛴다.
# ---------------------------------------------------------------------------
p_no_event = {"corp": "두산로보틱스"}
r4 = narrative.verify_numbers(ANSWER, MISMATCHED_CHUNKS, p=p_no_event)
check("event 없음 → G5 건너뜀(ok True)", r4["ok"] is True, r4)

p_empty_anchors = {"corp": "두산로보틱스", "event": {"type": "회사합병결정", "anchors": []}}
r5 = narrative.verify_numbers(ANSWER, MISMATCHED_CHUNKS, p=p_empty_anchors)
check("anchors 빈 리스트 → G5 건너뜀(ok True)", r5["ok"] is True, r5)

# ---------------------------------------------------------------------------
# 5) 원문에 아예 없는 숫자(예전 검사) — p 유무와 무관하게 여전히 잡혀야 한다.
# ---------------------------------------------------------------------------
bad_answer = "두산로보틱스의 취득 직접원가는 9,999,999,999원입니다."
r6 = narrative.verify_numbers(bad_answer, MATCHED_CHUNKS, p=P)
check("원문에 없는 숫자 → 여전히 ok False", r6["ok"] is False, r6)

# ---------------------------------------------------------------------------
print(f"{'테스트':<45}{'결과':<6}상세")
print("-" * 90)
n_fail = 0
for label, ok, detail in results:
    mark = "PASS" if ok else "FAIL"
    if not ok:
        n_fail += 1
    print(f"{label:<45}{mark:<6}{detail}")
print("-" * 90)

if n_fail:
    print(f"\n{n_fail}건 실패")
    sys.exit(1)
print("\n전부 통과")
sys.exit(0)
