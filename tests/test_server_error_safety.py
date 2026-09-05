#!/usr/bin/env python3
"""server.py — pipeline.run() 예외 안전망 테스트.

/ask, /answer 둘 다 pipeline.run() 주위에 try/except가 없었다(2026-09-05
발견) — 우리가 못 본 문항 하나가 예외를 던지면 그 요청은 500으로 죽어
부분점수도 못 받는다. pipeline.run을 강제로 raise하게 몽키패치해서, 두
엔드포인트 모두 여전히 200을 내고 state="S6"·정상 스키마 키를 갖춘 응답을
내는지 확인한다. (pytest 없이 이 저장소 다른 테스트들과 같은 스크립트
방식으로 작성.)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402


def _boom(*args, **kwargs):
    raise RuntimeError("의도적으로 발생시킨 테스트용 예외")


n_fail = 0


def check(label, cond, detail=""):
    global n_fail
    print(f"[{label}] {'PASS' if cond else 'FAIL'}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        n_fail += 1


_orig_run = server.pipeline.run
try:
    with TestClient(server.app) as client:   # 워밍업(lifespan)은 진짜 pipeline.run으로 먼저 통과시킨다
        server.pipeline.run = _boom
        r_ask = client.post("/ask", json={"question": "삼성전자 매출액은?"})
        check("ask 상태코드", r_ask.status_code == 200, str(r_ask.status_code))
        body_ask = r_ask.json()
        check("ask state=S6", body_ask.get("state") == "S6", str(body_ask.get("state")))
        check("ask answer=안전문구", body_ask.get("answer") == server._ERROR_ANSWER)
        check("ask evidence=[]", body_ask.get("evidence") == [])
        for key in ("question", "resolved_question", "state", "answer",
                    "evidence", "confidence", "table", "slots", "elapsed_ms", "detail"):
            check(f"ask 스키마 키 {key}", key in body_ask)

        r_answer = client.get("/answer", params={"question_id": "Q1", "question": "삼성전자 매출액은?"})
        check("answer 상태코드", r_answer.status_code == 200, str(r_answer.status_code))
        body_answer = r_answer.json()
        check("answer answer=안전문구", body_answer.get("answer") == server._ERROR_ANSWER)
        for key in ("question_id", "question", "retrieved_context", "think_trace", "answer"):
            check(f"answer 스키마 키 {key}", key in body_answer)
finally:
    server.pipeline.run = _orig_run

if n_fail:
    print(f"\n{n_fail}건 실패")
    sys.exit(1)
print("\n전부 통과")
sys.exit(0)
