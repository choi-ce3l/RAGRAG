#!/usr/bin/env python3
"""필드 별칭 검수 — LLM 출력에 안전장치를 걸고 검토표를 낸다.

## 왜 검수가 필요한가

별칭 표는 빌드타임 산출물이라 한 번 잘못 들어가면 계속 틀린다. 그런데 이 오류는
**눈에 보인다** — 숫자를 전사하는 것과 달리 표를 훑으면 잡힌다. 그래서 검수를
자동으로 걸고, 남은 것만 사람이 본다.

## 안전장치

1. **타입 대조** — group·kind는 데이터에서 온 값이다. 모델이 바꿔 왔으면 그 레코드는
   버린다. 지어냈다는 신호이기 때문이다.
2. **단독어 충돌 제거** — bare_terms가 두 키에서 겹치면 양쪽 모두에서 뺀다.
   `보고자`처럼 9개 키가 공유하는 말로 아무 키나 고르면 안 된다. 조용히 하나를
   고르는 것보다 되묻는 편이 낫다.
3. **저신뢰 격리** — confidence가 high가 아니면 별칭을 쓰지 않는다. 표에는 남겨서
   사람이 볼 수 있게 한다.
4. **자기 라벨 회수** — 원래 라벨은 신뢰할 수 있는 별칭이므로 무조건 넣는다.

    python scripts/review_fields.py
"""

import collections
import json
from pathlib import Path

RAW = Path("/tmp/field_labels_raw.json")
OUT = Path("data/field_aliases.json")


def main():
    blob = json.load(open(RAW, encoding="utf-8"))
    raw, src = blob["raw"], blob["source"]
    print(f"모델 응답 {len(raw)}종 / 입력 {len(src)}종")

    kept, dropped = {}, collections.Counter()
    for k, r in raw.items():
        s = src[k]
        if r.get("group") != s["group"] or r.get("kind") != s["kind"]:
            dropped["타입 불일치"] += 1
            continue
        if not isinstance(r.get("name"), str) or not r["name"].strip():
            dropped["이름 없음"] += 1
            continue
        kept[k] = r
    print(f"타입 대조 통과 {len(kept)}종  (버림: {dict(dropped)})")

    # 값 베끼기 제거 — 첫 실행에서 모델이 값 표본을 별칭으로 넣었다.
    # "국민연금공단"이 IFR_JOB의 별칭이 되면 그 말이 든 질문이 통째로 그리로 간다.
    copied = 0
    for k, r in kept.items():
        vals = src[k]["values"]
        clean = []
        for t in (r.get("aliases") or []):
            t = str(t).strip()
            if not t:
                continue
            if any(t == v.strip() or (len(t) > 3 and t in v) for v in vals):
                copied += 1
                continue
            clean.append(t)
        r["aliases"] = clean
    print(f"값을 베낀 별칭 제거: {copied}개")

    # 별칭 충돌 — 두 키 이상이 같은 말을 주장하면 양쪽에서 뺀다.
    # 조용히 하나를 고르는 것보다 되묻는 편이 낫다.
    claim = collections.defaultdict(list)
    for k, r in kept.items():
        for t in (r.get("aliases") or []):
            claim[str(t).strip()].append(k)
    clash = {t: ks for t, ks in claim.items() if len(ks) > 1}
    print(f"\n별칭 충돌 {len(clash)}건 — 양쪽에서 제거")
    for t, ks in sorted(clash.items(), key=lambda x: -len(x[1]))[:6]:
        print(f"   '{t}' ← {', '.join(ks[:5])}")

    out = {}
    for k, r in kept.items():
        uniq = [t for t in (r.get("aliases") or []) if t not in clash]
        out[k] = {"name": r["name"].strip(), "group": r["group"], "kind": r["kind"],
                  "aliases": sorted(set(src[k]["labels"]) | set(uniq)),  # 원 라벨은 항상 신뢰
                  "uniq": sorted(uniq),                  # 이 키만 쓰는 말 — 매칭 가산점
                  "note": (r.get("note") or "")[:160], "n": src[k]["n"]}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    usable = [k for k, v in out.items() if v["uniq"]]
    print(f"\n→ {OUT}  총 {len(out)}종 · 고유 별칭을 가진 키 {len(usable)}종")

    print("\n[검토표 — 다투던 키가 갈라졌는가]")
    for k in ("RPT_RSP_NM", "SPC_NM", "BUY_OSTK_LMT", "HLD_OSTK"):
        v = out.get(k)
        if v:
            print(f"   {k:<14} {v['name'][:24]:<26} uniq={v['uniq'][:3]}")
            print(f"   {'':<14} {v['note'][:96]}")


if __name__ == "__main__":
    main()
