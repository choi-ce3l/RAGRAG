#!/usr/bin/env python3
"""공시 필드 387종에 사람 말 붙이기 — 빌드타임 1회.

## 왜 필요한가

구조화 공시 fact는 `field_key`(DART 서식 코드)로 저장돼 있다. 키는 유일하고 뜻이
있지만(`HLD`=보유, `OSTK`=보통주식, `LMT`=한도), 한국어 라벨은 표 머리글이 모든
칸에 찍혀 있어 앵커로 쓸 수 없다 — `보통주식` 하나가 58개 키에, `보고자`가 9개 키에
붙어 있다.

그래서 "보고자(flr_nm)가 누구야"가 `RPT_RSP_NM`(신고 주체)이 아니라
`SPC_NM`(특별관계자)으로 갔고, 씨제이(주) 대신 강신호를 답했다.

## 무엇을 만드나

키마다 **정규 이름 · 사용자가 쓸 말 · 단독 질의에서 이길 자격**을 붙인 표
(`data/field_aliases.json`). 질의 시점에는 이 표를 딕셔너리로 볼 뿐 LLM이
개입하지 않는다 — 재현 가능하고 공짜다.

## 안전장치

- `group`·`kind`는 데이터에서 온 값이다. 모델이 바꿔 오면 그 레코드는 버린다.
- `bare_terms`(단독으로 이 키를 뜻하는 말)가 두 키에서 겹치면 **양쪽 모두에서
  제거한다.** 조용히 하나를 고르지 않고 되묻게 둔다.
- 뜻이 불분명하면 지어내지 말고 `"confidence": "low"`로 표시하게 한다. 낮은 것은
  별칭을 쓰지 않고 사람이 검토한다.

    python scripts/label_fields.py            # 프롬프트와 예상 토큰만 출력
    python scripts/label_fields.py --run      # 실제 호출 (과금)
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 배치 40종 · max_tokens 2000으로 처음 돌렸더니 JSON이 중간에 잘려 10배치 중 6개가
# 0~1종만 회수됐다(387종 중 42종). 출력 한 건이 60~120토큰이라 40종은 담기지 않는다.
BATCH = 15
OUT = Path("data/field_aliases.json")

SYSTEM = """당신은 한국 기업공시(DART) 서식을 잘 아는 데이터 사서입니다.
공시 서식의 필드 코드에 사람이 쓰는 말을 붙이는 일을 합니다.

규칙:
1. 주어진 key·labels·group·kind·values만 근거로 판단하십시오. 외부 지식으로 추측하지 마십시오.
2. group과 kind는 입력값을 그대로 되돌려 주십시오. 바꾸지 마십시오.
3. 뜻이 분명하지 않으면 지어내지 말고 confidence를 "low"로 두십시오.
4. values는 **뜻을 알아내기 위한 근거일 뿐입니다.** 값 자체를 aliases에 넣지 마십시오.
   ("02-214****", "국민연금공단", "202-81-45975" 같은 것은 별칭이 아닙니다.)
5. aliases에는 사용자가 질문에 실제로 쓸 법한 표현을 넣으십시오(구어 포함).
   같은 라벨을 여러 필드가 공유할 때가 많으므로("보통주식"이 58개 키에 붙어 있습니다),
   **무엇이 다른지 드러나게** 수식을 붙이십시오
   (예: "보유 보통주식", "1일 매수주문 한도 보통주식", "처분 보통주식").

JSON 배열만 출력하십시오. 설명 문장을 덧붙이지 마십시오."""

# bare_terms·confidence는 첫 실행에서 걷어냈다. bare_terms는 지시를 "구별되는 문자열"로
# 읽고 값 표본을 그대로 베꼈고(30%), confidence는 42종 전부 high로 나와 변별력이 없었다.
SCHEMA_HINT = """출력 형식 (배열의 각 원소):
{
  "key": "RPT_RSP_NM",
  "name": "보고자(신고인)",
  "aliases": ["신고인", "제출인", "보고 주체", "대량보유 신고한 곳"],
  "group": "holding",
  "kind": "text",
  "note": "대량보유상황보고서를 제출한 주체. 특별관계자(SPC_NM)와 구분된다."
}"""


def batches(records, n=BATCH):
    for i in range(0, len(records), n):
        yield records[i:i + n]


def build_prompt(chunk):
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in chunk)
    return f"{SCHEMA_HINT}\n\n다음 {len(chunk)}개 필드에 대해 위 형식으로 답하십시오.\n\n{body}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="실제로 호출한다 (과금 발생)")
    ap.add_argument("--infile", default="/tmp/keyinfo.json")
    ap.add_argument("--show", type=int, default=0, help="N번째 배치 프롬프트를 그대로 출력")
    a = ap.parse_args()

    recs = list(json.load(open(a.infile, encoding="utf-8")).values())
    recs.sort(key=lambda r: -r["n"])
    chunks = list(batches(recs))
    chars = sum(len(SYSTEM) + len(build_prompt(c)) for c in chunks)
    print(f"필드 {len(recs)}종 · 배치 {len(chunks)}회 (배치당 {BATCH}종)")
    print(f"입력 {chars:,}자 ≈ {chars / 2.3:,.0f} 토큰 · 출력 배치당 최대 2,000 토큰"
          f" → 총 ≈ {chars / 2.3 + len(chunks) * 2000:,.0f} 토큰")

    if a.run:
        return run(chunks, recs)

    if a.show is not None:
        print("\n" + "=" * 74)
        print("[SYSTEM]")
        print(SYSTEM)
        print("\n[USER] — 배치 " + str(a.show) + " (앞 6종만 표시)")
        print(build_prompt(chunks[a.show][:6]))
        print("=" * 74)
        print("\n(예상만 출력했습니다. 실제 호출은 --run 을 붙이세요.)")


def _client():
    import os
    from openai import OpenAI
    from qa import narrative
    key = os.environ.get("CLOVA_API_KEY")
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
    return OpenAI(api_key=key, base_url=narrative.BASE_URL), narrative.MODEL


def _parse(text):
    """모델 출력에서 JSON 배열만 건져낸다. 설명 문장이 섞여도 견디게."""
    t = text.strip()
    i, j = t.find("["), t.rfind("]")
    if i < 0 or j < 0:
        return []
    try:
        return json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        rows = []
        for line in t[i:j + 1].splitlines():
            line = line.strip().rstrip(",")
            if line.startswith("{") and line.endswith("}"):
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return rows


def run(chunks, recs):
    cli, model = _client()
    src = {r["key"]: r for r in recs}
    got, usage_in, usage_out = {}, 0, 0
    for i, ch in enumerate(chunks, 1):
        try:
            r = cli.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": build_prompt(ch)}],
                max_tokens=3000, temperature=0.1)
        except Exception as e:                              # noqa: BLE001
            print(f"  [{i}/{len(chunks)}] 실패: {type(e).__name__}: {e}")
            continue
        usage_in += r.usage.prompt_tokens
        usage_out += r.usage.completion_tokens
        rows = _parse(r.choices[0].message.content or "")
        for x in rows:
            if isinstance(x, dict) and x.get("key") in src:
                got[x["key"]] = x
        print(f"  [{i}/{len(chunks)}] 회수 {len(rows):>2}개 · 누적 {len(got)}/{len(src)}"
              f" · 토큰 {usage_in + usage_out:,}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"raw": got, "source": {k: src[k] for k in got}},
              open("/tmp/field_labels_raw.json", "w"), ensure_ascii=False, indent=1)
    print(f"\n원본 응답 {len(got)}종 → /tmp/field_labels_raw.json")
    print(f"토큰 합계: 입력 {usage_in:,} + 출력 {usage_out:,} = {usage_in + usage_out:,}")


if __name__ == "__main__":
    main()
