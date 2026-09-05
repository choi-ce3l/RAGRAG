#!/usr/bin/env python3
"""서술형 프롬프트의 한국어 토큰 비율 실측 — 1회 호출.

## 왜 따로 만드나

`keytest.py`는 한 문장(27토큰)을 보낸다. 키가 도는지 보기엔 충분하지만 **비율을
재기엔 표본이 틀렸다.** 우리가 실제로 보낼 것은 공시 원문 청크 4개가 붙은
6,300자짜리 프롬프트다. 짧은 문장과 긴 공시문은 토큰화 밀도가 다르다.

## 무엇을 재나

실제 `narrative.retrieve()` → `build_context()`로 만든 프롬프트를 그대로 보내고,
응답의 `prompt_tokens`를 읽어 **문자/토큰 비율**을 확정한다. 이 비율 하나면
98건이든 40건이든 곱셈으로 총량이 나온다.

비용을 묶는 장치:
  - 호출 1회, 재시도 없음
  - `max_tokens=16` — 출력은 최소로. 입력 토큰은 어차피 전액 과금되므로
    측정에는 지장이 없다.

    python scripts/tokenprobe.py          # 보낼 프롬프트와 예상치만 출력
    python scripts/tokenprobe.py --run    # 실제 호출 (과금)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qa import labelstore, narrative, pipeline          # noqa: E402

QUESTION = ("삼성전자가 2025년 7월 체결한 반도체 위탁생산 공급계약과 관련하여, "
            "최초 공시 시점과 정정 공시 시점 사이에 계약상대방 표기가 어떻게 달라졌는가?")
MAX_TOKENS = 16


def main():
    ap = argparse.ArgumentParser(description="서술형 프롬프트 토큰 비율 실측")
    ap.add_argument("--run", action="store_true", help="실제로 호출한다 (과금 발생)")
    ap.add_argument("--question", default=QUESTION)
    a = ap.parse_args()

    st, ls = pipeline.get_store(), labelstore.get()
    p = pipeline.stage01_map(a.question, st, ls)
    chunks = narrative.retrieve(a.question, p, top=narrative.MAX_CHUNKS)
    ctx = narrative.build_context(chunks)
    chars = len(narrative.SYSTEM) + len(ctx) + len(a.question)

    print(f"질문      : {a.question[:60]}…")
    print(f"청크      : {len(chunks)}개")
    print(f"프롬프트  : system {len(narrative.SYSTEM)}자 + 컨텍스트 {len(ctx):,}자 "
          f"+ 질문 {len(a.question)}자 = {chars:,}자")
    print(f"출력 상한 : max_tokens={MAX_TOKENS}")
    if not a.run:
        print("\n(예상만 출력했습니다. 실제 호출은 --run 을 붙이세요.)")
        return

    key = narrative._api_key() if hasattr(narrative, "_api_key") else None
    from openai import OpenAI
    import os
    key = key or os.environ.get("CLOVA_API_KEY")
    if not key:
        for q in (Path.cwd(), Path(__file__).resolve().parent.parent,
                  Path(__file__).resolve().parent.parent.parent):
            f = q / ".env"
            if f.is_file():
                for line in f.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("CLOVA_API_KEY="):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            if key:
                break
    cli = OpenAI(api_key=key, base_url=narrative.BASE_URL)
    r = cli.chat.completions.create(
        model=narrative.MODEL,
        messages=[{"role": "system", "content": narrative.SYSTEM},
                  {"role": "user", "content": f"{ctx}\n\n질문: {a.question}"}],
        max_tokens=MAX_TOKENS, temperature=0.2)
    u = r.usage
    print(f"\n실측      : 입력 {u.prompt_tokens:,} + 출력 {u.completion_tokens} "
          f"= 합계 {u.total_tokens:,} 토큰")
    print(f"비율      : {chars / u.prompt_tokens:.2f} 자 = 1 토큰")
    print(f"\n이 비율로 환산한 총량")
    for nm, n, ch in (("전체 폴백", 98, 618_804), ("좁은 게이트", 40, 258_676)):
        tin = ch / (chars / u.prompt_tokens)
        print(f"  {nm:<10} 호출 {n:>3}건 · 입력 {tin:>9,.0f} 토큰 "
              f"· 출력 최대 {n * 400:>6,} 토큰 · 합 {tin + n * 400:>9,.0f} 토큰")


if __name__ == "__main__":
    main()
