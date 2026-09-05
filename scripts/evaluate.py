#!/usr/bin/env python3
"""
공시 QA 에이전트 평가 파이프라인 (뼈대)

로컬에 이미 존재하는 code_chunkingandparsing/src/eval.py 를 기반으로 한다:
  - evaluate(search_name, k)  : hit@1/5/10 (검색이 gold 섹션을 잡는지)
  - answer_eval()             : must_contain / must_not_contain 규칙 채점 (LLM-judge 아님)

이 스크립트는 그 위에 요구사항 문서(공시_agent_요구사항.md)의 기능 2 스펙
(정확도 + 서브에이전트 단계별 오류 추적 + 근거 좌표 정확성)을 얹기 위한 뼈대다.
아직 서브에이전트 단계별 오류 추적은 구현되지 않음 (서버의 numqa.py/factx.py 이관 후
02~04 에이전트가 실제로 분리되면 각 단계 성공/실패를 별도로 기록할 수 있음).

실행 방법:
    python scripts/evaluate.py --dataset data/eval_datasets/<파일명>

TODO:
    - [ ] 데이터셋 로드 및 형식 검증 (validate_dataset) — 질문/정답/근거좌표 필드 확인
    - [ ] QA 에이전트 파이프라인 호출 연결 (run_qa_pipeline)
          -> 우선은 code_chunkingandparsing/src/rag.py 의 answer()/eval.py 의
             evaluate()/answer_eval() 재사용, 이후 01~05 서브에이전트가 실제 분리되면 교체
    - [ ] 문항별 채점 로직: 정확도(answer_eval의 must_contain 방식 참고) /
          서브에이전트 단계별 오류(현재는 미구현 — 파이프라인이 아직 단일 rag.answer() 호출) /
          근거 좌표 정확성(eval.py의 hit@k 방식 참고)
    - [ ] 에러/타임아웃 발생 시 즉시 중단 (fail-fast) 처리
    - [ ] 리포트 생성 (요약 + 상세 + 시각화) -> data/eval_reports/
"""

import argparse
import sys
from pathlib import Path

# code_chunkingandparsing/src 를 import 경로에 추가해 기존 rag.py/eval.py 재사용
# (공시_agent와 code_chunkingandparsing은 형제 디렉토리 — README.md "저장소 구성" 참고)
_AGENT_ROOT = Path(__file__).resolve().parent.parent
_LEGACY_SRC = _AGENT_ROOT / "code_chunkingandparsing" / "src"
if str(_LEGACY_SRC) not in sys.path:
    sys.path.insert(0, str(_LEGACY_SRC))


def validate_dataset(dataset_path: Path) -> bool:
    """데이터셋 파일 형식을 검증한다. (Placeholder)

    문제가 있으면 문제 위치를 출력하고 False를 반환해야 한다.
    """
    if not dataset_path.exists():
        print(f"[오류] 데이터셋 파일을 찾을 수 없습니다: {dataset_path}")
        return False
    # TODO: 필수 필드(질문, 정답, 근거 좌표 등) 검증 로직 구현
    print(f"[TODO] 데이터셋 형식 검증 미구현: {dataset_path}")
    return True


def run_qa_pipeline(question: str) -> dict:
    """단일 질문에 대해 QA 에이전트 파이프라인(01~05 서브에이전트)을 실행한다. (Placeholder)

    현재는 01(온톨로지 매핑)~04(도메인 전문가 검증)가 로컬에 없으므로,
    임시로 legacy rag.answer()를 호출하는 것으로 대체할 수 있다:

        import rag
        ans, hits = rag.answer(question, verbose=False)

    에러/타임아웃 발생 시 예외를 그대로 올려서 evaluate()가
    fail-fast로 중단할 수 있도록 한다.
    """
    raise NotImplementedError(
        "QA 파이프라인 연결 필요 (.claude/agents/01~05, 임시로는 legacy rag.answer() 대체 가능)"
    )


def evaluate(dataset_path: Path, report_dir: Path) -> None:
    """데이터셋 전체를 순회하며 평가하고 리포트를 생성한다. (Placeholder)

    - 특정 문항에서 오류 발생 시 즉시 중단한다 (fail-fast).
    - 정확도 / 오류 단계 / 근거 좌표 정확성을 측정한다.
    - 요약 리포트 + 문항별 상세 결과 + 시각화를 report_dir에 저장한다.
    """
    print(f"[TODO] 평가 실행 미구현. dataset={dataset_path}, report_dir={report_dir}")
    print("[참고] legacy eval.py의 evaluate()/answer_eval()을 이 함수 안에서 재사용하는 것으로 시작 가능")


def main() -> int:
    parser = argparse.ArgumentParser(description="공시 QA 에이전트 평가 파이프라인")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("data/eval_datasets"),
        help="평가 데이터셋 파일 경로",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("data/eval_reports"),
        help="평가 결과 리포트 저장 경로",
    )
    args = parser.parse_args()

    if not validate_dataset(args.dataset):
        return 1

    evaluate(args.dataset, args.report_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
