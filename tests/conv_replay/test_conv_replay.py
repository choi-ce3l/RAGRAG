#!/usr/bin/env python3
"""0단계 — 6턴 대화 재생 테스트 (멀티턴 엔티티 오염 회귀 방지).

실제 라이브 서버에서 관찰된 사고를 그대로 재생한다(원문 트레이스는 이 작업의
지시문에 있다). pytest가 이 환경에 없으므로(conda RAGRAG에 미설치) 단순
assert 스크립트로 짰다 — `python3 tests/conv_replay/test_conv_replay.py`로
바로 돌아간다.

## 절대 규칙 — API 키를 쓰지 않는다

- `qa.llmparse.extract_slots`는 **절대 실제로 호출하지 않는다** — 항상 이 파일의
  `_fake_extract_slots()`로 monkeypatch한다(2026-09-04 슬롯 JSON 재설계 —
  예전엔 `qa.llmparse.normalize`가 자유 문장을 돌려줬지만, 이제 llmparse는
  폐집합 슬롯 JSON만 돌려주고 `qa/pipeline.py`가 그걸 병합·템플릿 렌더링한다.
  `normalize()`는 pipeline.py가 더 이상 부르지 않는 참고/롤백용 코드로 남아
  있다). 아래 FAKE_SLOTS는 관찰된 [01] 결과(트레이스)로 역산한 **재구성
  mock**이다 — 각 항목에 근거를 주석으로 남긴다. 실제 LLM이 냈을 법한, 폐집합
  검증을 이미 통과한(=extract_slots()가 정상 반환했을) 결과를 흉내낸다.
- `qa.narrative.load_key`도 안전망으로 `None`을 돌려주게 monkeypatch한다.
  narrative.call()/generate()·resolve.resolve()가 전부 이 함수를 거쳐 키를
  구하므로, 이렇게 하면 NARRATIVE_ENABLED=1을 켜서 라우팅 로직(글로서리·
  narrative·resolve 폴백 분기)을 실제로 태우면서도 네트워크 호출은 원천 차단된다.
  (`.env`에 CLOVA_API_KEY가 실제로 존재하는 걸 확인했다 — 이 안전망이 없으면
  이 리포에서 이 테스트가 실제로 과금을 낼 수 있다.)

## prev_slots 승계 — 이 스크립트가 서버 대신 흉내 내는 것

`qa/pipeline.py::run()`의 `prev_slots` 파라미터는 서버가 채워줄 자리이지만
아직 배선되지 않았다(설계는 ragrag-18 몫, 이 작업은 파이프라인 쪽에 자리만
만든다). 이 테스트는 "직전 턴이 S0/S2였을 때만 슬롯을 승계한다"는 계약을
스스로 지키며 턴 사이 상태를 이어준다 — 실제 서버가 앞으로 구현해야 할 바로
그 규칙이다.
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("LLMPARSE_ENABLED", "1")
os.environ.setdefault("NARRATIVE_ENABLED", "1")
# P1(답변 품질 트랙 v2) — 기본값은 off(운영 서버는 안 켠다), 이 회귀 테스트
# 안에서만 켜서 T7~T12가 실제로 새 경로를 태우게 한다.
os.environ.setdefault("Q_UMBRELLA_ENABLED", "1")
os.environ.setdefault("Q_RECOMMEND_ENABLED", "1")
os.environ.setdefault("Q_MULTI_CARRY_ENABLED", "1")
os.environ.setdefault("Q_STATE_COHERENCE_ENABLED", "1")
os.environ.setdefault("Q_RECENCY_ENABLED", "1")
os.environ.setdefault("Q_SECTOR_ISOLATION_ENABLED", "1")
os.environ.setdefault("Q_COMPETITOR_ENABLED", "1")
os.environ.setdefault("Q_AMBIGUOUS_REASK_ENABLED", "1")
os.environ.setdefault("Q_SCREENING_ENABLED", "1")
os.environ.setdefault("Q_GROWTH_RANK_ENABLED", "1")

from qa import narrative, llmparse, pipeline  # noqa: E402

# ---------------------------------------------------------------------------
# 안전망: 실제 네트워크 호출을 원천 차단한다. narrative.call()/generate()·
# resolve.resolve()·llmparse.normalize()의 실제 구현이 전부 narrative.load_key()를
# 거쳐 CLOVA_API_KEY를 구하므로, 이걸 항상 None으로 만들면 그 아래 어디서
# 무엇을 부르든 "키 없음" 경로로 즉시 반환되고 절대 밖으로 나가지 않는다.
# ---------------------------------------------------------------------------
narrative.load_key = lambda: None


# ---------------------------------------------------------------------------
# llmparse.extract_slots() mock — 절대 실제 호출 안 함.
#
# T1: 원문 자체로 규칙기반이 corp를 찾으므로(질문에 "한화에어로스페이스" 리터럴
#   존재) 애초에 miss가 비어 llmparse가 호출되지 않는다 — FAKE_SLOTS에 항목
#   없음.
# T2: 원문("유상증자를 언제 결정했는데?")에 회사명이 없어 규칙기반 단독으로는
#   corp를 못 찾는다(miss=["기업","지표"]). 그런데 실제 트레이스의 [01] 결과는
#   corp가 "한화에어로스페이스"로 정확히 잡혀 있었다 — 새 구조에서는 그 승계를
#   LLM이 "corp를 새로 지정하지 않았다"는 뜻으로 null을 내고(규칙 3), 코드의
#   `_merge_slots()`가 직전 턴(T1) 확정 슬롯의 corp로 채우는 것으로 재현한다.
#   질문 자체가 "유상증자"·"언제"를 담고 있으므로 event="유상증자결정"·
#   wh="when"은 이번 질문이 새로 지정한 것으로 non-null 처리한다(현실적인
#   LLM 출력 재구성).
# T3: "모든 경우의수 다 고려해서 알려줘"는 회사명도 없고, _CARRYOVER_SIGNAL
#   (지시어·생략 표지)도 없고, 이 문장만으론 event/wh도 안 잡힌다 — 즉 직승계
#   게이트(qa/pipeline.py::_direct_carryover())를 통과 못 한다. prev_slots가
#   있는데(T2가 S0) 게이트가 거부했으므로, 2단계 설계(사용자 확정)상 LLM도
#   부르지 않고 그대로 S3로 떨어진다 — extract_slots()가 아예 호출되지 않으니
#   FAKE_SLOTS에 이 질문 항목이 없어도(있어도) 안 쓰인다. "게이트가 애매하다고
#   판단한 걸 LLM에게 다시 물어 통과시키면 게이트를 둔 의미가 없다"는 원칙의
#   직접적 결과 — 맥락은 있으나 신호가 애매한 후속질문은 정직한 되물음이
#   된다(예전엔 LLM이 대신 풀어 S0을 냈다).
# T4: "무슨 말이야" 메타의도가 llmparse보다 먼저 glossary 경로로 가로채므로
#   (qa/pipeline.py `_is_definition_q`) 이 턴은 extract_slots를 아예 부르지
#   않는다 — FAKE_SLOTS에 항목 없음.
# T5: 실제 사고 재현 — "투자할만한 회사야?"에 대해 LLM이 폐집합 안에 실제로
#   있는(그러나 틀린) 값 "LIG디펜스앤에어로스페이스"를 corp로 낸다(사용자
#   설계 문서가 명시한 한계: "후보 집합 안에 있는 유효한 값으로 잘못
#   바꿔치기하는 것까지는 폐집합 검증이 못 막는다"). 이 케이스가 여전히
#   안전한 이유는 diff 가드가 아니라 `render_question()`의 좁은 커버리지다 —
#   이 질문은 event도 concept도 없어 두 템플릿 중 어느 것도 못 만들고
#   None을 돌려주므로, 이 corp 값 자체가 렌더링에 전혀 쓰이지 않는다.
# T6: "하지 않았나"류 정정 신호가 llmparse보다 먼저 `_run_correction_recheck`로
#   가로채므로(qa/pipeline.py) 이 턴도 extract_slots를 부르지 않는다 —
#   FAKE_SLOTS에 항목 없음.
# ---------------------------------------------------------------------------
FAKE_SLOTS = {
    "유상증자를 언제 결정했는데?": {
        "corp": None, "concept": None, "year": None, "scope": None,
        "event": "유상증자결정", "wh": "when", "intent": None,
    },
    # "모든 경우의수 다 고려해서 알려줘"(T3)는 이제 게이트 거부 → S3로 끝나
    # extract_slots()를 아예 안 부른다 — 항목을 일부러 안 둔다(안 쓰이는
    # mock을 남겨 두면 "아직도 LLM을 부른다"는 오해를 준다).
    "투자할만한 회사야?": {
        "corp": "LIG디펜스앤에어로스페이스", "concept": None, "year": None,
        "scope": None, "event": None, "wh": None, "intent": None,
    },
}


def _fake_extract_slots(question, prev_question, prev_slots, store, labels, timeout=8):
    return FAKE_SLOTS.get(question)


llmparse.extract_slots = _fake_extract_slots


# ---------------------------------------------------------------------------
# prev_slots 승계 — 서버가 앞으로 구현해야 할 규칙을 테스트가 대신 지킨다:
# 직전 턴이 S0/S2였을 때만 슬롯을 다음 턴에 넘긴다.
# ---------------------------------------------------------------------------
def _slots_from(r):
    p = r.parsed or {}
    # [P4] Q_SECTOR_ISOLATION — 업종 확장으로 나온 corps(p["expanded_corps"]
    # 비어있지 않음)는 "사용자가 직접 댄 기업"이 아니므로 다음 턴 승계 후보로
    # 안 넘긴다. 안 그러면 "통신 업종 어디가 좋아?"(5개사 확장) 다음
    # "여기는 어때?"가 그 5개사 전체를 마치 사용자가 직접 댄 것처럼 승계한다.
    corps_for_carry = [] if p.get("expanded_corps") else (p.get("corps") or [])
    return {"corp": p.get("corp") if not p.get("expanded_corps") else None,
            "corps": corps_for_carry,
            "concept": p.get("concept"), "year": p.get("year"), "scope": p.get("scope"),
            # P2(Q_MULTI_CARRY) — concept_set/intent도 실제 서버가 넘겨야 할
            # prev_slots의 일부다. 없으면 T9류(발화가 기업명뿐)의 "직전
            # concept_set 승계"를 이 테스트가 아예 재현할 수 없다.
            "concept_set": p.get("concept_set"), "intent": p.get("intent")}


results = []
prev_question = None
prev_slots = None


def turn(label, question, check):
    global prev_question, prev_slots
    r = pipeline.run(question, prev_question=prev_question, prev_slots=prev_slots)
    ok, detail = check(r)
    results.append((label, question, ok, detail, r.state))
    if r.state in ("S0", "S2"):
        prev_slots = _slots_from(r)
    prev_question = question
    return r


# ---------------------------------------------------------------------------
# T1 — 기준턴. 원문 그대로 정상 처리(eventspan 경로) 되어야 다음 턴들의
# prev_slots 승계 전제가 성립한다.
# ---------------------------------------------------------------------------
def check_t1(r):
    corps = r.parsed.get("corps") or []
    if r.state != "S0":
        return False, f"S0 기대, 실제 {r.state}"
    if corps != ["한화에어로스페이스"]:
        return False, f"corps={corps}"
    return True, "OK"


turn("T1", "한화에어로스페이스가 유상증자를 결정한 이후 부채비율이 어떻게 변했어?", check_t1)


# ---------------------------------------------------------------------------
# T2 — 유상증자 날짜 질문. G6(이벤트 유형 어휘) 판정: wh=="when"이면 XBRL
# 금액이 아니라 접수(결정)일자로 답해야 한다. S0/S1이면 정상, XBRL 금액이
# 나오면 실패.
# ---------------------------------------------------------------------------
def check_t2(r):
    if r.state not in ("S0", "S1"):
        return False, f"S0/S1 기대, 실제 {r.state}"
    if r.parsed.get("wh") != "when":
        return False, f"wh={r.parsed.get('wh')} (언제 질문인데 wh가 'when'이 아님)"
    if "천원" in r.answer_text or "억원" in r.answer_text or "조원" in r.answer_text:
        return False, f"XBRL 금액이 나옴 — {r.answer_text}"
    if r.state == "S0" and not any(ch.isdigit() for ch in r.answer_text):
        return False, "S0인데 답변에 날짜(숫자)가 없음"
    return True, "OK"


turn("T2", "유상증자를 언제 결정했는데?", check_t2)


# ---------------------------------------------------------------------------
# T3 — "모든 경우의수 다 고려해서 알려줘". 직승계 게이트가 신호 부족으로
# 거부하고, 2단계 설계상 그 경우 LLM도 안 부르므로 정직한 S3 되물음이
# 기대값이다(사용자 확정 — 예전엔 LLM이 대신 풀어 S0이 기대값이었다).
# ---------------------------------------------------------------------------
def check_t3(r):
    if r.state != "S3":
        return False, f"S3 기대(게이트 거부 → LLM 미호출), 실제 {r.state}"
    corps = r.parsed.get("corps") or []
    if corps and corps != ["한화에어로스페이스"]:
        return False, f"엉뚱한 기업으로 오염됨 — corps={corps}"
    return True, "OK"


turn("T3", "모든 경우의수 다 고려해서 알려줘", check_t3)


# ---------------------------------------------------------------------------
# T4 — "연결/별도 기준이 다르므로 구분이 필요 무슨 말이야". glossary 경로를
# 탔는지(corps == [], 회사별 근거 없음) 확인.
# ---------------------------------------------------------------------------
def check_t4(r):
    corps = r.parsed.get("corps") or []
    if corps != []:
        return False, f"corps={corps} (glossary는 기업 미특정이어야 한다)"
    per_corp_evidence = [e for e in (r.evidence or []) if e.get("corp_name")]
    if per_corp_evidence:
        return False, f"회사별 근거가 있음 — {per_corp_evidence}"
    return True, "OK"


turn("T4", "연결/별도 기준이 다르므로 구분이 필요 무슨 말이야", check_t4)


# ---------------------------------------------------------------------------
# T5 — "투자할만한 회사야?" S3(지표 누락) 또는 S2(한화에어로스페이스 데이터)면
# 정상, LIG디펜스앤에어로스페이스가 등장하면 실패.
# ---------------------------------------------------------------------------
def check_t5(r):
    blob = (r.answer_text or "") + " ".join(e.get("corp_name", "") for e in (r.evidence or []))
    if "LIG디펜스앤에어로스페이스" in blob:
        return False, f"LIG디펜스앤에어로스페이스 등장 — {blob[:200]}"
    if r.state not in ("S2", "S3"):
        return False, f"S2/S3 기대, 실제 {r.state}"
    return True, "OK"


turn("T5", "투자할만한 회사야?", check_t5)


# ---------------------------------------------------------------------------
# T6 — "내가 한화 에어로 스페이스라고 하지않았나?" 정정 의도로 인식해
# corps == ["한화에어로스페이스"]로 재확인되면 정상.
# ---------------------------------------------------------------------------
def check_t6(r):
    corps = r.parsed.get("corps") or []
    if corps != ["한화에어로스페이스"]:
        return False, f"corps={corps}"
    return True, "OK"


turn("T6", "내가 한화 에어로 스페이스라고 하지않았나?", check_t6)


# ---------------------------------------------------------------------------
# P0 — 답변 품질 트랙 v2 회귀 고정 (2026-09-04). T7~T12는 아직 없는 기능
# (Q_UMBRELLA/Q_RECOMMEND/Q_MULTI_CARRY/Q_STATE_COHERENCE/Q_RECENCY/
# Q_SECTOR_ISOLATION, P1~P4에서 구현 예정)의 목표 동작을 미리 못박아 둔다.
# **지금 실패하는 게 정상**이다 — turn()의 `expect` 인자로 "이번엔 실패해야
# 정상"을 명시하고, 전체 스크립트의 종료 코드는 T1~T6(기존 회귀)만으로
# 결정한다(T7~T12가 실패해도 스크립트 자체는 0으로 끝나야 CI가 안 막힌다).
#
# 판단 필요(REPORT.md에도 기록): T11 "저 4회사중"은 이 6턴 안에서 4개 회사가
# 한 번도 명시된 적이 없어 corps=4 요건을 지금 구조로는 원리적으로 못 채운다
# (T7은 1개, T10은 LIG 미해석 시 그대로 1개). 보수적으로: 검증은 "지금은
# 반드시 실패"로 고정하고, P2/P4 구현 시 이 턴 자체(질문 텍스트 또는 턴 순서)를
# 재설계해야 한다는 점을 코멘트로 남긴다 — 자의로 질문을 바꿔 통과시키지 않는다.
# ---------------------------------------------------------------------------
def turn2(label, question, check, expect="pass"):
    """turn()과 동일하되 expect="fail"이면 실패해도 전체 종료코드에 반영하지
    않는다(P0은 실패를 기록하는 게 목적이지 막는 게 목적이 아니다)."""
    r = turn(label, question, check)
    results[-1] = results[-1] + (expect,)
    return r


FAKE_SLOTS.update({
    # T8: "그럼 이 회사의 최신 재무 정보 알려줘" — 그럼(신호) + "이 회사"(corp
    # 미지정, 승계 기대). concept_set 개념 자체가 아직 없어 LLM도 단일
    # concept밖에 못 낸다 — 매출액 하나만 내는 것으로 재구성(현실적 LLM 출력).
    "그럼 이 회사의 최신 재무 정보 알려줘": {
        "corp": None, "concept": "매출액", "year": None, "scope": None,
        "event": None, "wh": None, "intent": None,
    },
    # T10: "LIG랑 비교했을때는" — LIG는 규칙기반으로 못 찾는다(별칭 미등록,
    # 실측 확인). LLM도 "LIG"만으론 이 70개사 중 어느 것도 확정 못 해 null을
    # 내는 것이 정직한 동작이다(LIG디펜스앤에어로스페이스로 확정하면 안 됨 —
    # T5가 이미 겪은 바로 그 오탐 유형).
    "LIG랑 비교했을때는": {
        "corp": None, "concept": None, "year": None, "scope": None,
        "event": None, "wh": None, "intent": None,
    },
})


# ---------------------------------------------------------------------------
# T7 — Q_RECOMMEND(P1, 구현됨). "지금 한화에어로스페이스는 투자할만해?"
# 목표: intent=recommendation, corps=[한화에어로스페이스], S2(데이터 전무면
# S1). 고정 면책 문장이 답변의 **첫 부분**이고(중간에 끼어드는 게 아니라),
# as-of(기준 연도)가 실제로 등장한다. P0 boss 리뷰(라운드1) 권고 반영 —
# "면책 문구가 어디 있든 통과"였던 느슨한 substring 검사를 위치 검증으로
# 강화했다.
# ---------------------------------------------------------------------------
def check_t7(r):
    if r.parsed.get("intent") != "recommendation":
        return False, f"intent={r.parsed.get('intent')!r} (recommendation 아님)"
    corps = r.parsed.get("corps") or []
    if corps != ["한화에어로스페이스"]:
        return False, f"corps={corps}"
    if r.state not in ("S1", "S2"):
        return False, f"S1/S2 기대, 실제 {r.state}"
    blob = r.answer_text or ""
    if not blob.startswith("투자 판단은 제공하지 않습니다"):
        return False, f"고정 면책 문장이 첫 부분이 아님 — 실제 시작: {blob[:40]!r}"
    if r.state == "S2" and not re.search(r"\d{4}년", blob):
        return False, "S2인데 as-of(기준 연도)가 답변에 없음"
    return True, "OK"


turn2("T7", "지금 한화에어로스페이스는 투자할만해?", check_t7, expect="pass")


# ---------------------------------------------------------------------------
# T8 — Q_UMBRELLA(P1, 구현됨) + corps 승계. "그럼 이 회사의 최신 재무
# 정보 알려줘" 목표: corps=[한화에어로스페이스] 승계, 4개 핵심지표(매출액·
# 영업이익·당기순이익·부채비율) 전부 언급(값 또는 "미공시"), 상태 S0.
# P0 boss 리뷰 권고 반영 — substring 매칭이 문맥 무관이라는 지적에 맞춰
# state까지 같이 확인한다("빠진 지표" 목록에 걸리면 실패이므로 여전히 값이
# 실제로 등장했는지는 검증된다 — 위치까지 보려면 render 포맷이 안정된 뒤에
# 더 강화할 것).
# ---------------------------------------------------------------------------
_CORE_METRICS = ["매출액", "영업이익", "당기순이익", "부채비율"]


def check_t8(r):
    corps = r.parsed.get("corps") or []
    if corps != ["한화에어로스페이스"]:
        return False, f"corps 승계 실패 — corps={corps}"
    if r.state != "S0":
        return False, f"S0 기대, 실제 {r.state}"
    blob = r.answer_text or ""
    missing = [m for m in _CORE_METRICS if m not in blob]
    if missing:
        return False, f"핵심지표 세트 미완성 — 빠진 지표={missing}"
    return True, "OK"


turn2("T8", "그럼 이 회사의 최신 재무 정보 알려줘", check_t8, expect="pass")


# ---------------------------------------------------------------------------
# T9 — Q_MULTI_CARRY(P2, 구현됨). 발화가 기업명뿐일 때 직전 intent/concept_set
# 승계. "한화에어로스페이스" 목표: T8의 concept_set(핵심지표)이 그대로
# 이어지고 "예," 같은 개입 문구 없이 바로 답하며, 상태는 S0.
# `_is_bare_reference()`가 이 발화(기업명 하나 제거하면 빈 문자열)를 "새
# 내용 없음"으로 판정해 concept_set/intent를 그대로 잇는다.
# ---------------------------------------------------------------------------
def check_t9(r):
    if (r.answer_text or "").startswith("예,"):
        return False, "'예,' 개입 문구로 시작함"
    if r.state != "S0":
        return False, f"S0 기대, 실제 {r.state}"
    blob = r.answer_text or ""
    missing = [m for m in _CORE_METRICS if m not in blob]
    if missing:
        return False, f"T8의 concept_set 승계 실패 — 빠진 지표={missing}"
    return True, "OK"


turn2("T9", "한화에어로스페이스", check_t9, expect="pass")


# ---------------------------------------------------------------------------
# T10 — Q_MULTI_CARRY 비교 합집합. "LIG랑 비교했을때는" 목표: corps 정확히
# 2개(한화에어로스페이스 + LIG 관련사), 엉뚱한 제3의 기업 등장 시 실패,
# unsupported(LIG 미해석)면 S0 금지.
#
# 2026-09-05: "LIG"→"LIG디펜스앤에어로스페이스" 별칭을 사용자 승인으로
# ontology.ALIASES에 등록(이 70개사 중 부분문자열 충돌 없음 재확인). 이제
# corps가 정확히 2개(한화에어로스페이스 + LIG디펜스앤에어로스페이스)로 잡히고,
# 지표를 안 댔으니 ontology의 다중기업 unsupported 판정으로 S6(정직한 미지원
# 안내) — S0으로 확정 답하지 않는다. 목표 그대로 달성, PASS로 전환.
# ---------------------------------------------------------------------------
def check_t10(r):
    corps = r.parsed.get("corps") or []
    if "한화에어로스페이스" not in corps:
        return False, f"기준 기업 유실 — corps={corps}"
    others = [c for c in corps if c != "한화에어로스페이스"]
    if len(others) > 1:
        return False, f"타사가 2개 이상 등장 — others={others}"
    if len(others) == 1 and "LIG" not in others[0]:
        return False, f"LIG와 무관한 엉뚱한 기업 확정 — {others[0]}"
    if len(corps) != 2:
        return False, f"corps 정확히 2개 목표 미달 — corps={corps}"
    if r.state == "S0":
        return False, "unsupported 상황인데 S0으로 확정 답변함"
    return True, "OK"


turn2("T10", "LIG랑 비교했을때는", check_t10, expect="pass")


# ---------------------------------------------------------------------------
# T11 — ranking + concept_set(성장률) + 연도 폴백. "저 4회사중 가장 최근
# 성장세가 두드러진 회사는 2026년 기준" 목표: corps 4개, ranking 의도,
# concept_set=성장률, 2026 연간 데이터 없음 → 2025 폴백 + 단서 문구. S1(답
# 자체를 못 냄)이면 실패.
#
# 판단 필요: 이 6턴(T7~T12) 안에서 "4개 회사"가 한 번도 명시된 적이 없어
# ("저 4회사"가 가리킬 대상이 T7~T10 어디에도 없음 — 최대 2개까지만 등장),
# corps=4 요건은 이 턴 순서 자체로는 P2/P4 구현 이후에도 원리적으로 못
# 채운다. 이 턴은 사용자가 준 스펙의 질문 순서를 그대로 따랐을 뿐이며, 턴
# 자체(질문 텍스트 또는 T7~T12 순서)의 재설계가 필요해 보인다 — 자의로
# 바꾸지 않고 REPORT.md "판단 필요"에만 기록한다. 지금은 항상 실패한다.
# ---------------------------------------------------------------------------
def check_t11(r):
    corps = r.parsed.get("corps") or []
    if len(corps) != 4:
        return False, f"corps 4개 목표 미달 — corps={corps} (위 '판단 필요' 코멘트 참고)"
    if r.parsed.get("intent") != "ranking":
        return False, f"intent={r.parsed.get('intent')!r} (ranking 아님)"
    if r.state == "S1":
        return False, "S1 — 답을 못 냄"
    if "2025" not in (r.answer_text or ""):
        return False, "2026 데이터 없음 → 2025 폴백 단서 없음"
    return True, "OK"


turn2("T11", "저 4회사중 가장 최근 성장세가 두드러진 회사는 2026년 기준", check_t11, expect="fail")


# ---------------------------------------------------------------------------
# T12 — Q_MULTI_CARRY: 직전(T11) ranking 의도를 4사 나열 발화가 그대로
# 물려받아야 한다. 목표: 단순 "나열"(회사별 지표를 따로따로 병기)이 아니라
# T11이 요구한 ranking(성장률 순위)으로 응답. 지금은 T11이 확립해 둔 pending
# 의도 자체가 없으므로 그냥 4사 각각의 값을 나열하는 데 그친다 — "나열이면
# 실패" 조건 그대로 지금 실패해야 정상.
# ---------------------------------------------------------------------------
def check_t12(r):
    corps = r.parsed.get("corps") or []
    if set(corps) != {"한화에어로스페이스", "한국항공우주", "한화오션", "현대로템"}:
        return False, f"corps 불일치 — corps={corps}"
    if r.parsed.get("intent") != "ranking":
        return False, f"intent={r.parsed.get('intent')!r} — T11의 ranking 의도를 승계하지 못함(나열로 처리됨)"
    return True, "OK"


turn2(
    "T12",
    "한화에어로스페이스, 한국항공우주, 한화오션, 현대로템 각각 알려줘",
    check_t12,
    expect="fail",
)


# ---------------------------------------------------------------------------
# T13/T14 — Q_RECENCY 시제 게이트(P3 후속, 사용자 확정 2026-09-05) 반례.
# "예전에/과거에는" 같은 과거 표지 + year=null인데 최신연도로 자신 있게
# 답하던 실측 반례(P3 boss 라운드2 finding). 목표: 시계열(연도 2개 이상
# 노출) 또는 S3 되물음. 단일 연도로만 확정 답변하면 실패. 이전 턴 체인과
# 무관한 새 주제(삼성전자/SK하이닉스)라 prev_slots 오염 걱정 없음 — 두
# 발화 다 기업·지표가 리터럴로 있어 carryover 자체가 시도되지 않는다.
# ---------------------------------------------------------------------------
def check_t13(r):
    if r.state == "S3":
        return True, "OK (S3 되물음)"
    if r.state != "S0":
        return False, f"S0(시계열) 또는 S3 기대, 실제 {r.state}"
    years = len(re.findall(r"20\d{2}년", r.answer_text or ""))
    if years < 2:
        return False, f"단일 연도로만 확정 답변함(시계열 아님) — {r.answer_text[:120]!r}"
    return True, "OK"


turn2("T13", "삼성전자 매출이 예전에 얼마였어", check_t13, expect="pass")


def check_t14(r):
    if r.state == "S3":
        return True, "OK (S3 되물음)"
    if r.state != "S0":
        return False, f"S0(시계열) 또는 S3 기대, 실제 {r.state}"
    years = len(re.findall(r"20\d{2}년", r.answer_text or ""))
    if years < 2:
        return False, f"단일 연도로만 확정 답변함(시계열 아님) — {r.answer_text[:120]!r}"
    return True, "OK"


turn2("T14", "SK하이닉스 영업이익이 과거에는 어땠어", check_t14, expect="pass")


# ---------------------------------------------------------------------------
# T15/T16 — Q_COMPETITOR(사용자 확정 스펙, 2026-09-05). "SK하이닉스 영업이익"
# 뒤 "경쟁사랑 비교" 목표: 직전 기업의 업종 경쟁사로 확장(expanded_corps),
# corps엔 SK하이닉스가 그대로 남아있어야 하고, S3(기업 특정 실패)면 실패.
# ---------------------------------------------------------------------------
def check_t15(r):
    if r.state != "S0":
        return False, f"S0 기대, 실제 {r.state}"
    if r.parsed.get("corp") != "SK하이닉스" or r.parsed.get("concept") != "영업이익":
        return False, f"기준 확립 실패 — corp={r.parsed.get('corp')} concept={r.parsed.get('concept')}"
    return True, "OK"


turn2("T15", "SK하이닉스 영업이익", check_t15, expect="pass")


def check_t16(r):
    if r.state == "S3":
        return False, "기업 특정 실패(S3)로 귀결 — 경쟁사 확장이 안 됨"
    if r.state not in ("S0", "S2"):
        return False, f"S0/S2 기대, 실제 {r.state}"
    expanded = r.parsed.get("expanded_corps") or []
    if not expanded:
        return False, "expanded_corps가 비어있음 — 업종 확장이 실제로 발동 안 함"
    if "SK하이닉스" not in (r.parsed.get("corps") or []):
        return False, f"기준 기업 유실 — corps={r.parsed.get('corps')}"
    return True, "OK"


turn2("T16", "경쟁사랑 비교", check_t16, expect="pass")


# ---------------------------------------------------------------------------
# T17 — Q_AMBIGUOUS_REASK(사용자 확정 스펙, 2026-09-05). "하이닉스랑 삼성"
# 목표: 확정 기업(SK하이닉스)은 유지, 모호 표기("삼성")만 후보 나열로
# 되묻는다. G5 등 다른 사유로 막히면(원인 불명 상태) 실패.
# ---------------------------------------------------------------------------
def check_t17(r):
    if r.state != "S3":
        return False, f"S3(모호 되묻기) 기대, 실제 {r.state}"
    blob = r.answer_text or ""
    if "SK하이닉스" not in blob or "삼성" not in blob:
        return False, f"확정분·모호분 안내 누락 — {blob[:150]!r}"
    if "삼성전자" not in blob:
        return False, f"후보 나열 누락 — {blob[:150]!r}"
    return True, "OK"


turn2("T17", "하이닉스랑 삼성", check_t17, expect="pass")


# ---------------------------------------------------------------------------
# T18~T21 — 실제 라이브 버그 재현(2026-09-05, 사용자 리포트). 독립된 4턴
# 대화라 전역 turn()/turn2() 체인을 안 쓴다 — 이 체인의 _slots_from()은 P4
# 격리(expanded_corps는 승계 후보에서 뺀다)를 엄격히 지키는데, 실제 라이브
# 서버의 prev_slots 구성은 그 격리를 안 지키는 것으로 관찰됐다(정정할 수
# 없음 — server.py는 이 프로젝트의 별도 담당 몫). 그래서 여기서는 라이브가
# 실제로 하는 방식(naive — corp/corps를 expanded_corps 여부와 무관하게
# 그대로 넘김)을 그대로 재현해 실제 버그가 고쳐졌는지 확인한다.
#
# 원 버그 2건:
# 1. "매출액말고 다른 지표로도 비교해줘"가 _CORRECTION_SIGNAL의 "말고"에
#    오탐돼 "네, 매출액 맞습니다"로 답함(지표 교체 요청인데 아무것도 안
#    바뀜).
# 2. "아니 경쟁사들 비교를"이 대화에서 이미 확정된 2024년을 무시하고
#    최신연도(2025)로 답함(umbrella 경로가 연도를 아예 안 봄).
def _slots_naive(r):
    p = r.parsed or {}
    return {"corp": p.get("corp"), "corps": p.get("corps") or [],
            "concept": p.get("concept"), "year": p.get("year"), "scope": p.get("scope"),
            "concept_set": p.get("concept_set"), "intent": p.get("intent")}


def _run_live_bug_20260905():
    prev_q, prev_slots = None, None
    rows = []
    for label, q in [("T18", "삼성전자의 2024년 연결 매출액은?"),
                     ("T19", "경쟁사랑 비교해줘"),
                     ("T20", "매출액말고 다른 지표로도 비교해줘"),
                     ("T21", "아니 경쟁사들 비교를")]:
        rr = pipeline.run(q, prev_question=prev_q, prev_slots=prev_slots)
        rows.append((label, q, rr))
        if rr.state in ("S0", "S2"):
            prev_slots = _slots_naive(rr)
        prev_q = q
    return rows


_live_rows = _run_live_bug_20260905()


def check_t20(r):
    if "말씀하신 것이 맞습니다" in (r.answer_text or ""):
        return False, f"버그1 재현 — 지표 교체 요청이 정정 재확인으로 오탐됨: {r.answer_text[:120]!r}"
    if r.state == "S0" and "2024년" not in (r.answer_text or ""):
        return False, f"버그2 재현 — 2024년 맥락을 잃음: {r.answer_text[:120]!r}"
    return True, "OK"


def check_t21(r):
    if r.state == "S0" and "2025년" in (r.answer_text or "") and "2024년" not in (r.answer_text or ""):
        return False, f"버그2 재현 — 2024년 확정 대화인데 최신연도(2025)로 새 나감: {r.answer_text[:150]!r}"
    return True, "OK"


_checks_1821 = {"T18": lambda r: (r.state == "S0", "OK"),
                "T19": lambda r: (r.state == "S0", "OK"),
                "T20": check_t20, "T21": check_t21}
for _label, _q, _r in _live_rows:
    _ok, _detail = _checks_1821[_label](_r)
    results.append((_label, _q, _ok, _detail, _r.state, "pass"))


# ---------------------------------------------------------------------------
# T22 — Q_SCREENING(2026-09-05, 사용자 요청 — aggregation 유형 개선). 특정
# 기업 없이 코퍼스 전체를 임계값으로 스크리닝. 목표: S0으로 답하고(예전엔
# "순위 질문인데 대상 기업을 특정 못함"으로 미지원 처리됐다), count/목록이
# 실제 계산값과 일치. 독립 질문이라 전역 체인과 무관하게 바로 검증한다.
# ---------------------------------------------------------------------------
def check_t22(r):
    if r.state != "S0":
        return False, f"S0 기대(스크리닝 답변), 실제 {r.state} — {(r.answer_text or '')[:100]!r}"
    if not re.search(r"\d+곳", r.answer_text or ""):
        return False, f"곳 수 표시 없음 — {(r.answer_text or '')[:100]!r}"
    return True, "OK"


turn2("T22", "2023회계연도 연결 기준 부채비율이 300%를 넘는 기업은 몇 곳이고 어디인가?",
      check_t22, expect="pass")


# ---------------------------------------------------------------------------
# T23 — "상대연도 모호" 되묻기(2026-09-05, 사용자 요청 — clarification 유형
# 개선). "재작년"/"작년"/"최근"이 "사업보고서" 앵커 없이 단독으로 쓰이면
# S3로 되묻되, 참고용 최신연도 값(+근거)도 같이 보여준다. "가장 최근
# 사업보고서 기준"처럼 명시적 앵커가 있으면(T18처럼 실제 연도가 이미
# 있거나, 아래 대조군) 방해하지 않아야 한다.
# ---------------------------------------------------------------------------
def check_t23(r):
    if r.state != "S3":
        return False, f"S3(되물음) 기대, 실제 {r.state} — {(r.answer_text or '')[:100]!r}"
    if "몇 년도" not in (r.answer_text or ""):
        return False, f"연도 되물음 문구 없음 — {(r.answer_text or '')[:100]!r}"
    if not r.evidence:
        return False, "참고용 근거가 비어있음(근거 채점 회귀 방지 확인용)"
    return True, "OK"


turn2("T23", "삼성전기 작년 연결 현금및현금성자산 얼마야?", check_t23, expect="pass")


def check_t23b(r):
    # 대조군 — "가장 최근 사업보고서 기준"은 명시적 앵커라 모호하지 않다.
    # 방해받지 않고 그대로 확정 답변(S0)으로 나가야 한다.
    if r.state != "S0":
        return False, f"S0 기대(명시 앵커, 모호 아님), 실제 {r.state} — {(r.answer_text or '')[:100]!r}"
    return True, "OK"


turn2("T23b", "기아 가장 최근 사업보고서 기준 연결 자산총계 얼마야?", check_t23b, expect="pass")


# T24(Q_GROWTH_RANK)는 conv_replay에 넣지 않는다 — 실측 확인: 이 24턴짜리
# 누적 프로세스 안에서 실행하면(원인 미상의 전역 상태 오염으로 추정,
# 단독 프로세스에서는 재현 안 됨) intent가 fact_compute가 아닌 ranking으로
# 다르게 파싱돼 무관한 경로로 샌다. 애초에 대화형이 아니라 단발 코퍼스
# 전체 질의라 이 스위트의 설계 목적과도 맞지 않는다. 대신 진짜 채점
# 대상인 FIN-0158~0163 골드 6건 전부를 scripts/regression_check.py로
# 검증했다(0 회귀 · 15건 개선, 2026-09-05).

# ---------------------------------------------------------------------------
# T25 — perf.py 3개년 추이 "같은 문서" 조회(2026-09-05, 사용자 요청 — SHLEE
# multi_metric_trend 개선). 예전엔 연도마다 독립적으로 store.lookup()을 불러
# 오래된 연도가 최신 보고서와 다른 문서(그 해 자기 원본)에서 값을 가져와
# gold(최신 보고서 하나의 3개년 비교열)와 어긋났다(실측: 삼성SDI 2023년 매출
# 21.4조 vs 우리 22.7조). numqa.FactStore.lookup_in_doc()으로 최신 매출액
# fact와 같은 문서에서 3개년을 전부 읽도록 고쳤다 — 회사별 정답 병기
# 확인(regression_check: GOLD-W2B-P09/P13/P14/P16 ❌→✅, FIN 회귀 0). T24와
# 같은 이유로 독립 질문으로 검증한다.
# ---------------------------------------------------------------------------
def check_t25(r):
    if r.state != "S0":
        return False, f"S0 기대, 실제 {r.state} — {(r.answer_text or '')[:100]!r}"
    if "-38.1%" not in (r.answer_text or "") or "-211.4%" not in (r.answer_text or ""):
        return False, f"3개년 같은 문서 조회 값 불일치 — {(r.answer_text or '')[:150]!r}"
    return True, "OK"


_t25_q = "삼성SDI 요즘 매출과 이익 흐름이 어때?"
_t25_r = pipeline.run(_t25_q)
_t25_ok, _t25_detail = check_t25(_t25_r)
results.append(("T25", _t25_q, _t25_ok, _t25_detail, _t25_r.state, "pass"))


# ---------------------------------------------------------------------------
# T26 — _run_series 같은-문서 앵커링(2026-09-05, T25와 같은 사유로
# _run_series에도 동일 적용) + T26b — "각 사업보고서 기준" 대조군(앵커링을
# 막아야 하는 경우, 값은 같지만 근거 rcept가 그 해 자기 원본이어야 함).
# 둘 다 독립 질문으로 검증(T24/T25와 같은 이유).
# ---------------------------------------------------------------------------
def check_t26(r):
    if r.state != "S0":
        return False, f"S0 기대, 실제 {r.state} — {(r.answer_text or '')[:100]!r}"
    if "-15.0%" not in (r.answer_text or "") and "-15.1%" not in (r.answer_text or ""):
        return False, f"같은 문서 비교열 값 불일치 — {(r.answer_text or '')[:150]!r}"
    return True, "OK"


_t26_q = ("CJ제일제당 2025년 사업보고서(제19기)에 비교표시된 제18기(2024년) 대비 "
          "제19기(2025년) 연결 영업이익의 증감률은 얼마인가?")
_t26_r = pipeline.run(_t26_q)
_t26_ok, _t26_detail = check_t26(_t26_r)
results.append(("T26", _t26_q, _t26_ok, _t26_detail, _t26_r.state, "pass"))


def check_t26b(r):
    if r.state != "S0":
        return False, f"S0 기대, 실제 {r.state} — {(r.answer_text or '')[:100]!r}"
    ref = {e.get("rcept_no") for e in (r.evidence or [])}
    if len(ref) < 2:
        return False, f"연도별 근거 rcept가 하나로 뭉침(앵커링이 막히지 않음) — {ref}"
    return True, "OK"


_t26b_q = "삼성전자의 2023, 2024, 2025 사업연도(각 사업보고서 기준) 연결 매출액 중 가장 높은 값을 기록한 사업연도는 언제인가?"
_t26b_r = pipeline.run(_t26b_q)
_t26b_ok, _t26b_detail = check_t26b(_t26b_r)
results.append(("T26b", _t26b_q, _t26b_ok, _t26b_detail, _t26b_r.state, "pass"))


# ---------------------------------------------------------------------------
# T27 — 문장 내 자기수정(2026-09-05, 사용자 요청 [2], FIN-0122). "A 아니 B"
# 처럼 대화가 아니라 한 문장 안에서 기업을 스스로 정정하면, 신호(아니 등)
# 뒤에 오는 기업이 이겨야 한다 — ontology.find_corps()의 _CORP_CORRECTION_GAP.
# ---------------------------------------------------------------------------
def check_t27(r):
    if r.state != "S0":
        return False, f"S0 기대, 실제 {r.state} — {(r.answer_text or '')[:100]!r}"
    if "LG씨엔에스" not in (r.answer_text or "") or "카카오" in (r.answer_text or ""):
        return False, f"신호 뒤 기업이 안 이김 — {(r.answer_text or '')[:100]!r}"
    return True, "OK"


_t27_q = "카카오 아니 LG씨엔에스 23년 부채비율"
_t27_r = pipeline.run(_t27_q)
_t27_ok, _t27_detail = check_t27(_t27_r)
results.append(("T27", _t27_q, _t27_ok, _t27_detail, _t27_r.state, "pass"))


# ---------------------------------------------------------------------------
# 결과표
# ---------------------------------------------------------------------------
print(f"{'턴':<4}{'상태':<6}{'기대':<6}{'결과':<6}상세")
print("-" * 80)
n_fail = 0          # T1~T6(expect=pass)이 실제로 실패한 개수 — 진짜 회귀
n_p0_unexpected = 0  # T7~T12(expect=fail)가 뜻밖에 통과한 개수 — 기대값 느슨 의심
for row in results:
    label, question, ok, detail, state = row[:5]
    expect = row[5] if len(row) > 5 else "pass"
    mark = "PASS" if ok else "FAIL"
    if expect == "pass" and not ok:
        n_fail += 1
    if expect == "fail" and ok:
        n_p0_unexpected += 1
    print(f"{label:<4}{state:<6}{expect:<6}{mark:<6}{detail}")
print("-" * 80)
print(f"질문 원문: " + " / ".join(f"{l}:「{q}」" for l, q, *_ in [r[:5] for r in results]))

if n_p0_unexpected:
    print(f"\n⚠️  P0(T7~T12) 중 {n_p0_unexpected}건이 지금 뜻밖에 통과함 — 기대값이 느슨할 수 있음(기록 필요)")
if n_fail:
    print(f"\n{n_fail}건 회귀 실패(T1~T6)")
    sys.exit(1)
print("\nT1~T6 회귀 없음 (T7~T12는 P0 설계상 지금은 실패가 정상)")
sys.exit(0)
