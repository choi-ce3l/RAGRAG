#!/usr/bin/env python3
"""온톨로지 오버핏 측정 — 기업 단위 교차검증.

온톨로지를 corpus에서 뽑았으니, 그 corpus의 기업들에는 잘 맞는 게 당연하다.
문제는 **처음 보는 기업에도 통하느냐**다. 이걸 재려면 기업을 갈라야 한다.

  train 기업들의 표기로만 온톨로지를 만든다  →  test 기업 질문으로 평가

fact store는 전부 남겨둔다. 데이터가 없어서 못 맞히는 것과 어휘가 없어서 못 맞히는
것은 다른 문제이고, 여기서 재려는 것은 후자다.

    python scripts/holdout.py [--folds 5] [--dataset <경로>]
"""

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluation import dataset, scorer                  # noqa: E402
from qa import concepts, labelstore, ontology, pipeline, sectors   # noqa: E402


def attribute(rows, store, labels):
    """각 질문이 어느 기업에 대한 것인지 — 온전한 온톨로지로 판정한다."""
    out = []
    for rec in rows:
        p = ontology.parse(rec["question"], store, labels)
        out.append((rec, tuple(p["corps"])))
    return out


def run_subset(pairs, store, labels):
    return [scorer.score(rec, pipeline.run(rec["question"], store=store, labels=labels), 0.0)
            for rec, _ in pairs]


def summarize(scored):
    if not scored:
        return {"n": 0}
    graded = [r for r in scored if r["정확"] != "➖"]
    stage1 = sum(1 for r in scored if r["오류단계"] == "[01]")
    recog = sum(1 for r in scored if r["state"] not in ("S3",))
    return {
        "n": len(scored),
        "정확": round(100 * sum(1 for r in graded if r["정확"] == "✅") / len(graded), 1) if graded else None,
        "채점가능": len(graded),
        "행동": round(100 * sum(1 for r in scored if r["행동"] == "✅") / len(scored), 1),
        "01실패": stage1,
        "개념인식": round(100 * recog / len(scored), 1),
    }


def vocab_recall(train_corps, test_corps, facts):
    """train 어휘가 test 기업의 개념을 얼마나 덮는가.

    질문 분포에 의존하지 않는 지표다. 골드셋이 널리 쓰이는 계정만 묻는다면
    질문 기반 평가는 오버핏을 못 본다 — 이건 어휘 자체를 직접 본다.
    """
    from qa.concepts import canonical
    tr, te = set(), collections.Counter()
    for f in facts:
        lab = f.get("label_norm") or f.get("label_raw")
        c = canonical(lab) if lab else None
        if not c:
            continue
        if f["corp_name"] in train_corps:
            tr.add(c)
        elif f["corp_name"] in test_corps:
            te[c] += 1
    if not te:
        return None
    types_hit = sum(1 for c in te if c in tr)
    tokens_hit = sum(n for c, n in te.items() if c in tr)
    return {"개념종류": round(100 * types_hit / len(te), 1),
            "사용량가중": round(100 * tokens_hit / sum(te.values()), 1),
            "test개념수": len(te), "미보유": len(te) - types_hit}


def sweep(single, store, labels, fracs=(0.9, 0.7, 0.5, 0.3, 0.15, 0.07)):
    """train 기업 비율을 줄여가며 어디서 무너지는지 본다."""
    all_corps = sorted({f["corp_name"] for f in labels.facts if f.get("corp_name")})
    print("\n" + "=" * 74)
    print("train 기업 비율을 줄여가며 — 어디서 무너지는가")
    print(f"{'train비율':>9}{'기업':>6}{'개념':>8}{'어휘재현(종류)':>14}{'어휘재현(가중)':>14}{'질문정확':>10}")
    out = []
    for fr in fracs:
        k = max(2, int(len(all_corps) * fr))
        train = set(all_corps[:k])              # 결정적 분할 (이름 정렬 기준)
        test = set(all_corps) - train
        sub = [f for f in labels.facts if f.get("corp_name") in train]
        idx = concepts.ConceptIndex(sub)
        vr = vocab_recall(train, test, labels.facts) or {}
        prev_i, prev_s = concepts.set_index(idx), sectors.restrict(train)
        try:
            qs = [(rec, c) for rec, c in single if c in test]
            sc = summarize(run_subset(qs, store, labels)) if qs else {"정확": None, "n": 0}
        finally:
            concepts.set_index(prev_i)
            sectors.restrict(prev_s)
        row = {"train비율": fr, "기업": k, "개념": len(idx.surfaces),
               "어휘재현_종류": vr.get("개념종류"), "어휘재현_가중": vr.get("사용량가중"),
               "질문정확": sc.get("정확"), "질문수": sc.get("n")}
        out.append(row)
        print(f"{int(fr*100):>8}%{k:>6}{len(idx.surfaces):>8,}"
              f"{str(vr.get('개념종류'))+'%':>14}{str(vr.get('사용량가중'))+'%':>14}"
              f"{str(sc.get('정확'))+'%' if sc.get('정확') is not None else '-':>10}"
              f"  (n={sc.get('ن', sc.get('n'))})")
    return out


def by_sector(single, store, labels):
    """업종 하나를 통째로 빼고 그 업종 질문을 평가한다.

    기업 단위 무작위 홀드아웃은 어휘가 업종 전반에 공유되기 때문에 잘 안 문다.
    업종을 통째로 빼면 그 업종 특유 계정(보험 책임준비금 등)이 어휘에서 사라진다.
    "업종 밖 기업"에 대한 우려를 가진 데이터로 재는 가장 가까운 방법이다.
    """
    idx_sec = sectors.load()["sector"]
    all_corps = {f["corp_name"] for f in labels.facts if f.get("corp_name")}
    print("\n" + "=" * 78)
    print("업종을 통째로 빼면 — 그 업종 특유 계정이 어휘에서 사라진다")
    print(f"{'업종':<16}{'기업':>5}{'질문':>5}{'어휘재현(종류)':>14}{'어휘재현(가중)':>14}{'질문정확':>10}")
    out = []
    for sec, members in sorted(idx_sec.items(), key=lambda x: -len(x[1])):
        test = set(members) & all_corps
        if len(test) < 2:
            continue
        train = all_corps - test
        sub = [f for f in labels.facts if f.get("corp_name") in train]
        idx = concepts.ConceptIndex(sub)
        vr = vocab_recall(train, test, labels.facts) or {}
        prev_i, prev_s = concepts.set_index(idx), sectors.restrict(train)
        try:
            qs = [(rec, c) for rec, c in single if c in test]
            sc = summarize(run_subset(qs, store, labels)) if qs else {"n": 0, "정확": None}
        finally:
            concepts.set_index(prev_i)
            sectors.restrict(prev_s)
        row = {"업종": sec, "기업": len(test), "질문": sc["n"],
               "어휘재현_종류": vr.get("개념종류"), "어휘재현_가중": vr.get("사용량가중"),
               "미보유개념": vr.get("미보유"), "정확": sc.get("정확")}
        out.append(row)
        print(f"{sec:<16}{len(test):>5}{sc['n']:>5}"
              f"{str(vr.get('개념종류'))+'%':>14}{str(vr.get('사용량가중'))+'%':>14}"
              f"{(str(sc.get('정확'))+'%') if sc.get('정확') is not None else '-':>10}")
    return out


def main():
    ap = argparse.ArgumentParser(description="온톨로지 오버핏 측정")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--dataset", default="/home/dslab/RAGRAG/choi/benchmarks/dataset/qa_gold.jsonl")
    ap.add_argument("-o", "--out", default="data/eval_reports/holdout.json")
    a = ap.parse_args()

    store, labels = pipeline.get_store(), labelstore.get()
    rows = dataset.load(a.dataset)
    pairs = attribute(rows, store, labels)

    # 단일 기업 질문만 쓴다. 여러 기업이 걸린 질문은 train/test가 섞여 누수가 생긴다.
    single = [(rec, corps[0]) for rec, corps in pairs if len(corps) == 1]
    corps_all = sorted({c for _, c in single})
    print(f"전체 {len(rows)}문항 중 단일 기업 질문 {len(single)}건 · 기업 {len(corps_all)}개")

    # ── 기준선: 온전한 온톨로지 ──────────────────────────────
    base = run_subset([(r, None) for r, _ in single], store, labels)
    print(f"\n기준선(온전한 온톨로지)  {summarize(base)}")

    # ── 폴드별: train 기업으로만 온톨로지를 만들고 test 질문 평가 ──
    folds = [corps_all[i::a.folds] for i in range(a.folds)]
    all_corps = {f["corp_name"] for f in labels.facts if f.get("corp_name")}
    held, per_fold = [], []

    for i, test_corps in enumerate(folds, 1):
        test_set = set(test_corps)
        train_corps = all_corps - test_set
        sub = [f for f in labels.facts if f.get("corp_name") in train_corps]
        idx = concepts.ConceptIndex(sub)

        prev_idx = concepts.set_index(idx)
        prev_sec = sectors.restrict(train_corps)
        try:
            qs = [(rec, c) for rec, c in single if c in test_set]
            scored = run_subset(qs, store, labels)
        finally:
            concepts.set_index(prev_idx)
            sectors.restrict(prev_sec)

        held += scored
        s = summarize(scored)
        per_fold.append(s)
        print(f"  fold {i}: test 기업 {len(test_corps):>2}개 · 질문 {s['n']:>3}건 "
              f"· 개념 {len(idx.surfaces):,}개 → {s}")

    print(f"\n홀드아웃 종합            {summarize(held)}")

    b, h = summarize(base), summarize(held)
    print("\n" + "=" * 58)
    print(f"{'지표':<12}{'온전':>10}{'홀드아웃':>12}{'차이':>12}")
    for k, unit in (("정확", "%"), ("행동", "%"), ("개념인식", "%"), ("01실패", "건")):
        bv, hv = b.get(k), h.get(k)
        if bv is None or hv is None:
            continue
        d = round(hv - bv, 1)
        print(f"{k:<12}{bv:>9}{unit}{hv:>11}{unit}{d:>+11}{unit}")

    sw = sweep(single, store, labels)
    bs = by_sector(single, store, labels)

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(
        {"base": b, "holdout": h, "folds": per_fold, "sweep": sw, "by_sector": bs,
         "n_companies": len(corps_all)},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
