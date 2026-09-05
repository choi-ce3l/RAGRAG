"""공시 QA 에이전트 (기능 1) — 01~05 서브에이전트 파이프라인.

현재 지원 경로: 수치 fact-path (XBRL 재무 수치). API 키를 쓰지 않는다.
narrative 경로(서술형 질문)는 아직 붙지 않았다 — 상태 S6으로 안내만 한다.

기존 자산(`code_chunkingandparsing/src/numqa.py` 등)을 import 하기 위한 경로 설정을
여기서 한다. 하위 모듈이 어떤 순서로 import 되어도 numqa를 찾을 수 있어야 한다.
"""

import sys
from pathlib import Path as _Path

_SRC = _Path(__file__).resolve().parent.parent / "code_chunkingandparsing" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
