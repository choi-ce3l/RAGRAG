#!/usr/bin/env python3
"""CLOVA Studio API 키 동작 확인 — 최소 비용 1회 호출.

무엇이 오가는지 눈으로 보기 위한 것이다. 요청 헤더·본문과 응답 원문,
그리고 실제 소비 토큰 수를 그대로 찍는다.

비용을 묶는 장치:
  - 프롬프트를 한 문장으로 고정
  - max_tokens=40 으로 출력 상한을 강제
  - 호출 1회, 재시도 없음

    python scripts/keytest.py            # 예상 소비량만 출력 (호출 안 함)
    python scripts/keytest.py --run      # 실제 호출
"""

import argparse
import json
import os
import sys
from pathlib import Path

BASE_URL = "https://clovastudio.stream.ntruss.com/v1/openai"
MODEL = "HCX-005"
PROMPT = "한 문장으로만 답하세요. 사업보고서는 무엇인가요?"
MAX_TOKENS = 40


def load_key():
    """.env를 찾아 CLOVA_API_KEY를 읽는다 (rag.py와 같은 탐색 순서)."""
    if os.environ.get("CLOVA_API_KEY"):
        return os.environ["CLOVA_API_KEY"], "환경변수"
    here = Path(__file__).resolve().parent
    for p in [Path.cwd() / ".env", here / ".env", here.parent / ".env",
              here.parent.parent / ".env", here.parent.parent.parent / ".env"]:
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("CLOVA_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'"), str(p)
    return None, None


def main():
    ap = argparse.ArgumentParser(description="CLOVA 키 동작 확인")
    ap.add_argument("--run", action="store_true", help="실제로 호출한다 (과금 발생)")
    a = ap.parse_args()

    key, src = load_key()
    print(f"키 출처   : {src or '(없음)'}")
    print(f"키 형태   : {(key[:6] + '…' + key[-4:]) if key else '(없음)'}  길이 {len(key) if key else 0}")
    print(f"엔드포인트: POST {BASE_URL}/chat/completions")
    print(f"모델      : {MODEL}   출력 상한 max_tokens={MAX_TOKENS}")
    print()
    print("보낼 요청 (헤더 + 본문):")
    print(f"  Authorization: Bearer {(key[:6] + '…') if key else 'nv-…'}")
    print("  " + json.dumps({"model": MODEL,
                             "messages": [{"role": "user", "content": PROMPT}],
                             "max_tokens": MAX_TOKENS, "temperature": 0.2},
                            ensure_ascii=False))
    print()
    est_in = len(PROMPT)          # 한글은 대략 글자수 ≈ 토큰수 수준으로 보수적 추정
    print(f"예상 소비 : 입력 ≈ {est_in} 토큰 + 출력 ≤ {MAX_TOKENS} 토큰"
          f"  →  합계 ≤ {est_in + MAX_TOKENS} 토큰 (0.1K 미만)")

    if not a.run:
        print("\n(예상만 출력했습니다. 실제 호출은 --run 을 붙이세요.)")
        return 0
    if not key:
        print("\n키를 찾지 못했습니다.")
        return 1

    from openai import OpenAI
    cli = OpenAI(api_key=key, base_url=BASE_URL)
    r = cli.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": PROMPT}],
        max_tokens=MAX_TOKENS, temperature=0.2)
    print("\n" + "─" * 60)
    print("응답 본문:")
    print("  " + (r.choices[0].message.content or "").strip())
    u = r.usage
    print("\n실제 소비 토큰:")
    print(f"  입력 {u.prompt_tokens} + 출력 {u.completion_tokens} = 합계 {u.total_tokens}")
    print(f"  = {u.total_tokens/1000:.4f} K 토큰")
    return 0


if __name__ == "__main__":
    sys.exit(main())
