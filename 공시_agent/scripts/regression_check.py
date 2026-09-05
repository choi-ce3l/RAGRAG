#!/usr/bin/env python3
"""qa/*.py · evaluation/*.py 수정 회귀 체크 스크립트.

수동으로 반복하던 절차(수정 전 두 골드셋 평가 → 수정 → 다시 평가 → qid별
정확/근거/행동 diff)를 자동화한다. 이 스크립트는 evaluation/evaluate.py를
그대로 호출만 할 뿐, qa/*.py·evaluation/*.py 로직은 건드리지 않는다.

사용법:
    conda run -n RAGRAG python3 scripts/regression_check.py --baseline
        현재 코드로 두 골드셋(SHLEE + FIN)을 평가해 baseline으로 저장한다.
        (버그를 고치기 전에 한 번 찍어 둔다)

    conda run -n RAGRAG python3 scripts/regression_check.py --check
        다시 평가해 baseline과 비교한다. qid별 정확/근거/행동 중
        ✅ → ❌ 또는 ✅ → ➖ 로 바뀐 항목을 회귀로 보고한다.
        (➖→✅, ❌→✅ 등은 개선/중립으로 별도 표시)
        회귀가 있으면 종료 코드 1, 없으면 0.

baseline은 data/regression_baseline/ 에 저장되며, evaluate.py가 매 실행마다
덮어쓰는 data/eval_reports/ 와는 분리되어 있다 — 이 스크립트를 실행해도
data/eval_reports/의 report.md·details.json은 평소처럼 최신 실행 결과로
덮인다(그건 evaluate.py 본연의 동작이고, baseline은 별도 스냅샷일 뿐이다).
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from evaluation import evaluate as evaluate_mod  # noqa: E402

REPORT_DIR = PROJECT_ROOT / "data" / "eval_reports"
BASELINE_DIR = PROJECT_ROOT / "data" / "regression_baseline"

DATASETS = {
    "SHLEE": Path("/home/dslab/RAGRAG/SHLEE/정답 DATASET 제작/지시파일/gold/qa_gold_final.json"),
    "FIN": Path("/home/dslab/RAGRAG/choi/benchmarks/dataset/qa_gold.jsonl"),
}

FIELDS = ("정확", "근거", "행동")


def run_dataset(path):
    """evaluate.py의 evaluate()를 그대로 호출해 최신 채점 결과를 읽어온다."""
    stem = path.stem
    rc = evaluate_mod.evaluate(str(path))
    if rc != 0:
        raise RuntimeError(
            f"{path.name} 평가가 실패했습니다(E1/E2) — 위 출력을 확인하세요. "
            "회귀 체크를 진행할 수 없습니다."
        )
    details_path = REPORT_DIR / f"details_{stem}.json"
    rows = json.loads(details_path.read_text(encoding="utf-8"))
    return stem, rows


def extract(rows):
    return {r["qid"]: {f: r.get(f) for f in FIELDS} for r in rows}


def classify(old, new):
    """(old, new) 필드값 쌍을 회귀/개선/중립/무변화로 분류한다."""
    if old == new:
        return None
    if old == "✅" and new != "✅":
        return "regression"
    if new == "✅" and old != "✅":
        return "improvement"
    return "neutral"


def cmd_baseline():
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    for name, path in DATASETS.items():
        print(f"\n▶ {name} 평가 중... ({path})")
        stem, rows = run_dataset(path)
        snapshot = extract(rows)
        out = {
            "dataset_name": name,
            "dataset_path": str(path),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "n": len(snapshot),
            "qids": snapshot,
        }
        out_path = BASELINE_DIR / f"baseline_{stem}.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ baseline 저장: {name} ({stem}) — {len(snapshot)}건 → {out_path}")
    return 0


def cmd_check():
    had_setup_error = False
    found_regression = False

    for name, path in DATASETS.items():
        baseline_path = BASELINE_DIR / f"baseline_{path.stem}.json"
        if not baseline_path.exists():
            print(f"\n⚠️  {name} baseline이 없습니다 — 먼저 --baseline을 실행하세요 ({baseline_path})")
            had_setup_error = True
            continue

        print(f"\n▶ {name} 평가 중... ({path})")
        stem, rows = run_dataset(path)
        current = extract(rows)
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["qids"]

        regressions, improvements, neutrals = [], [], []
        for qid, base_vals in baseline.items():
            if qid not in current:
                continue
            cur_vals = current[qid]
            for f in FIELDS:
                kind = classify(base_vals.get(f), cur_vals.get(f))
                item = (qid, f, base_vals.get(f), cur_vals.get(f))
                if kind == "regression":
                    regressions.append(item)
                elif kind == "improvement":
                    improvements.append(item)
                elif kind == "neutral":
                    neutrals.append(item)

        missing = [qid for qid in baseline if qid not in current]
        new_qids = [qid for qid in current if qid not in baseline]

        print(f"\n🔎 회귀 체크 — {name} ({stem}, {len(current)}qid)")
        if missing:
            print(f"   ⚠️  baseline에는 있었지만 이번엔 없는 qid {len(missing)}건: {missing[:10]}")
        if new_qids:
            print(f"   ℹ️  baseline에 없던 신규 qid {len(new_qids)}건 (회귀 판정 제외)")
        print(f"   회귀 {len(regressions)}건 · 개선 {len(improvements)}건 · 중립 변화 {len(neutrals)}건")

        if regressions:
            found_regression = True
            print("   ❌ 회귀 목록:")
            for qid, f, old, new in regressions:
                print(f"      ❌ [{qid}] {f}: {old} → {new}")
        if improvements:
            print("   ⬆️  개선 목록:")
            for qid, f, old, new in improvements[:20]:
                print(f"      ⬆️  [{qid}] {f}: {old} → {new}")
            if len(improvements) > 20:
                print(f"      ... 외 {len(improvements) - 20}건")

    print()
    if had_setup_error:
        print("⚠️  일부 데이터셋은 baseline이 없어 비교하지 못했습니다.")
        return 2
    if found_regression:
        print("❌ 회귀가 발견되었습니다 (위 목록 참고).")
        return 1
    print("✅ 회귀 없음.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--baseline", action="store_true", help="현재 상태를 baseline으로 저장")
    g.add_argument("--check", action="store_true", help="현재 상태를 baseline과 비교")
    a = ap.parse_args()
    return cmd_baseline() if a.baseline else cmd_check()


if __name__ == "__main__":
    sys.exit(main())
