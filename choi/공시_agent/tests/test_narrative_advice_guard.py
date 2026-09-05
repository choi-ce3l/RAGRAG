#!/usr/bin/env python3
"""[P1] narrative.py의 투자 권유 어휘 차단 정규식 — 단위 테스트.

API 호출 없음(순수 함수 테스트). 두 가지를 확인한다:
1. 실제로 투자 권유성 어휘("추천합니다"·"유망합니다"·"매수하세요" 등)가
   섞인 narrative 산출물을 차단·대체하는가.
2. **이 프로젝트가 스스로 만드는 고정 문장**(qa/pipeline.py의 recommendation
   템플릿 첫 줄)은 "투자"라는 낱말을 담고 있어도 이 정규식에 걸리지 않는가
   — 걸리면 recommendation 답변 자체가 통째로 안전 문구로 뒤집혀 버린다.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qa import narrative, pipeline  # noqa: E402

cases_should_block = [
    "이 회사는 실적이 좋아 투자를 추천합니다.",
    "향후 전망이 유망합니다.",
    "지금 매수하세요.",
    "긍정적으로 전망됩니다.",
]
cases_should_pass = [
    pipeline._RECOMMEND_DISCLAIMER,
    "매출액이 전년 대비 12.3% 증가했습니다.",
    "이 회사의 부채비율은 45.2%입니다.",
]

n_fail = 0
for text in cases_should_block:
    out = narrative._block_investment_advice(text, "test")
    ok = out == narrative._SAFE_FALLBACK
    print(f"[차단 기대] {'PASS' if ok else 'FAIL'} — {text!r} → {out!r}")
    if not ok:
        n_fail += 1

for text in cases_should_pass:
    out = narrative._block_investment_advice(text, "test")
    ok = out == text
    print(f"[통과 기대] {'PASS' if ok else 'FAIL'} — {text!r} → {out!r}")
    if not ok:
        n_fail += 1

if n_fail:
    print(f"\n{n_fail}건 실패")
    sys.exit(1)
print("\n전부 통과")
sys.exit(0)
