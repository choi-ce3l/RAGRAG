#!/usr/bin/env python3
"""공시 QA 에이전트 평가 파이프라인 (기능 2) — 진입점.

    python evaluation/evaluate.py --dataset <데이터셋 경로>

원본 뼈대 `scripts/evaluate.py`는 그대로 두고 이 폴더에 실구현을 둔다.

요구사항 스펙:
  - 실행 전 데이터셋 형식 검증 → 문제가 있으면 시작하지 않고 위치를 알린다 (상태 E1)
  - 문항 실행 중 에러/타임아웃 → 즉시 중단하고 어떤 문항/어떤 오류인지 표시 (상태 E2)
  - 정확도 / 서브에이전트 단계별 오류 / 근거 좌표 정확성을 측정
  - 요약 + 문항별 상세 + 시각화를 리포트로 저장
"""

import argparse
import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluation import dataset, report, scorer         # noqa: E402
from qa import pipeline                                # noqa: E402

REPORT_DIR = Path(__file__).resolve().parent.parent / "data" / "eval_reports"


class Timeout(Exception):
    pass


def _alarm(signum, frame):                             # noqa: ARG001
    raise Timeout("문항 처리 시간 초과")


def run_one(rec, store, timeout):
    """단일 문항 실행. 예외는 그대로 올려 evaluate()가 fail-fast 하도록 둔다."""
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(timeout)
    t0 = time.time()
    try:
        result = pipeline.run(rec["question"], store=store)
    finally:
        signal.alarm(0)
    return result, time.time() - t0


def evaluate(dataset_path, timeout=30, table_limit=30, expand_limit=10):
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── 형식 검증 (E1) — 실패하면 한 문항도 실행하지 않는다 ──────────
    try:
        rows = dataset.load(dataset_path)
    except dataset.DatasetError as e:
        print()
        print(report.block_e1(dataset_path, str(e)))
        print()
        return 1

    store = pipeline.get_store()
    scored = []

    # ── 문항 실행 (E2) — 에러/타임아웃이면 즉시 중단 ─────────────────
    for rec in rows:
        try:
            result, elapsed = run_one(rec, store, timeout)
        except Exception as e:                          # noqa: BLE001
            print()
            print(report.block_e2(rec["qid"], rec["question"],
                                  "01~05 중 (파이프라인 예외)", f"{type(e).__name__}: {e}"))
            print()
            return 1
        scored.append(scorer.score(rec, result, elapsed))

    # ── 리포트 ──────────────────────────────────────────────────────
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(dataset_path).stem
    md_path = REPORT_DIR / f"report_{stem}.md"
    json_path = REPORT_DIR / f"details_{stem}.json"
    paths = {"요약 리포트": md_path, "문항별 상세": json_path,
             "시각화": "report.md 안에 포함 (ASCII)"}
    meta = {"dataset": dataset_path, "n": len(rows), "started": started, "status": "완료"}

    print()
    print(report.render(meta, scored, paths, table_limit, expand_limit))
    print()

    md_path.write_text(report.render_markdown(meta, scored, paths), encoding="utf-8")
    json_path.write_text(json.dumps(scored, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    return 0


def main():
    ap = argparse.ArgumentParser(description="공시 QA 에이전트 평가 파이프라인")
    ap.add_argument("--dataset", required=True, help="평가 데이터셋 파일 경로")
    ap.add_argument("--timeout", type=int, default=30, help="문항당 제한 시간(초)")
    ap.add_argument("--table-limit", type=int, default=30,
                    help="터미널에 찍을 표 행 수 (전체는 report.md)")
    ap.add_argument("--expand-limit", type=int, default=10,
                    help="터미널에 펼칠 오답 상세 건수")
    a = ap.parse_args()
    return evaluate(a.dataset, a.timeout, a.table_limit, a.expand_limit)


if __name__ == "__main__":
    sys.exit(main())
