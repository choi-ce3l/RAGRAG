#!/usr/bin/env python3
"""공시 QA 에이전트 (기능 1) 직접 실행용 진입점.

    python scripts/ask.py "삼성전자의 2025년 연결 매출액은 얼마인가?"

Claude Code 안에서는 자연어 질문이 자동 인식되어 01~05 서브에이전트가 도는 것이
기본 경로이고, 이 스크립트는 같은 파이프라인을 터미널에서 바로 돌려보기 위한 것이다.
현재 지원 경로는 XBRL 수치 질문이며 API 키를 쓰지 않는다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qa import pipeline, render                       # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    question = " ".join(sys.argv[1:])
    result = pipeline.run(question)
    print()
    print(render.render(result))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
