"""멀티턴 후속 질문 슬롯 추출 — 01단계 규칙기반 파서가 기업/지표/연도 중 하나라도
막혔을 때만, HCX에 "지금 질문이 새로 지정한 슬롯"을 JSON으로 물어 채운다.

## 경계
narrative.py와 같은 원칙이다: LLM은 숫자·지표를 직접 판단하지 않는다. 여기서 하는
일은 슬롯 값을 폐집합(corpus 유래 후보 목록) 중에서 고르는 것뿐이고, 그 값들을
합쳐 고정 템플릿으로 만든 문장을 그대로 기존 `ontology.parse()`가 재해석한다 —
회사명 인식, 지표 매칭, intent 분류는 전부 기존의 검증된 규칙 기반 로직을 그대로
통과한다.

## 왜 자유 문장 재작성(이전 방식)이 아닌가
이전 `normalize()`는 "질문 문장을 다시 써라"였다. LLM이 문장 자체는 그럴듯하게
써도 그 안에 원문·직전턴 어디에도 없는 실체(예: 다른 회사명)를 자연스럽게 섞어
넣으면, 재파싱이 그 실체를 표면형으로 못 뽑아내 텍스트 diff 가드(`_rewrite_diff_ok`,
이제 삭제됨)를 그냥 통과해버릴 수 있었다 — 간접적 검증의 구조적 한계다(실측:
T2~T6 멀티턴 오염 사고, "한화에어로스페이스" 다음 "LIG디펜스앤에어로스페이스"로
답을 낸 것).

`extract_slots()`는 자유 문장이 아니라 슬롯 JSON을 받는다. 각 슬롯은 명시적으로
제공된 후보 집합의 원소 또는 null만 허용한다(qa/resolve.py와 완전히 같은 폐집합
원칙) — 후보 밖 값은 그 자리에서 null로 걸러진다("존재하지 않는 값을 지어내는
것"은 이 구조적 검증만으로 막힌다). null은 "지금 질문이 이 슬롯을 새로 지정하지
않았다 → 직전 턴 값을 그대로 쓴다"는 뜻이고, 그 병합은 호출부(`qa/pipeline.py`)가
한다. "후보 집합 안에 있는 유효한 값으로 잘못 바꿔치기하는 것"까지 완전히 막지는
못한다 — 이건 프롬프트/추후 튜닝 품질의 몫으로 남겨 둔다(2026-09-04 설계 결정).

`render_question()`이 병합된 슬롯을 고정 패턴 한국어 문장으로 렌더링하고, 그
문장을 `ontology.parse()`에 그대로 넣는다 — 파싱 로직 자체는 여기서 한 글자도
건드리지 않는다.

## 트리거
규칙 기반 파서가 기업/지표/연도 중 하나라도 특정하지 못했을 때(miss)만 부른다.
잘 파싱되는 질문에는 비용을 쓰지 않는다 — narrative.py의 should_try()와 같은 이유다.

## 비용
호출당 입력 몇백~천 토큰 수준(후보 목록 + 질문 한두 줄)이라 narrative(청크 포함,
~3,000토큰)보다 가볍다. 기본적으로 꺼져 있고, LLMPARSE_ENABLED로 명시적으로
켜야 동작한다.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .narrative import BASE_URL, MODEL, load_key

MAX_OUTPUT_TOKENS = 200

_HERE = Path(__file__).resolve().parent
LOG_PATH = _HERE.parent / "data" / "llmparse_log.jsonl"


def enabled():
    """켜져 있는가. 비용이 드는 경로라 명시적으로 켜야 한다."""
    return os.environ.get("LLMPARSE_ENABLED", "").lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# [폐기 예정 · 참고/롤백용으로만 남김] 자유 문장 재작성 — pipeline.py는 더 이상
# 이 함수를 부르지 않는다. 아래 SYSTEM/normalize()는 손대지 않았다.
# ---------------------------------------------------------------------------
SYSTEM = (
    "당신은 한국 기업 공시 QA 시스템의 질문 정규화기입니다. "
    "사용자의 캐주얼한(또는 직전 질문에 이어지는) 질문을 시스템이 바로 이해할 수 있는 "
    "완결된 독립 질문 한 문장으로 다시 씁니다.\n"
    "규칙:\n"
    "1. 회사명은 '등록된 회사명' 목록에 있는 표기와 정확히 일치하도록 띄어쓰기만 고치십시오 "
    "(예: 'SK 텔레콤' → 'SK텔레콤'). 목록에 없는 이름은 손대지 마십시오.\n"
    "2. 특정 지표 없이 전반적인 실적·재무상태를 캐주얼하게 묻는 질문"
    "(예: '~어때', '~어떤가요', '~궁금해', '~알려줘')이면 문장 끝에 '실적 흐름 추이'를 덧붙이십시오.\n"
    "3. '직전 질문'이 함께 주어지면, 지금 질문이 그 직전 질문에 대한 후속·변형인지 먼저 "
    "판단하십시오. 후속이면(예: 직전 '건설사에서 매출 1위는?' 다음 '반도체 분야에서는?') "
    "직전 질문의 틀(무엇을 묻는지)은 유지하고, 지금 질문이 새로 지정한 부분(업종·기업·연도 "
    "등)만 바꿔 끼워서 완결된 새 질문 하나로 합치십시오 — 이때 직전 질문에 있던 기업·업종은 "
    "지금 질문이 새로 지정한 것으로 완전히 대체하고, 남겨두지 마십시오. **지금 질문이 기업· "
    "업종을 전혀 새로 지정하지 않았으면(예: 직전 '한화에어로스페이스 부채비율은?' 다음 "
    "'그래서 어떻게 변했어?'), 직전 질문에 있던 기업·업종을 정확히 그대로 유지하십시오 — "
    "같은 업종의 다른 회사나 그럴듯한 다른 이름으로 절대 바꿔치기하지 마십시오. 확신이 없어도 "
    "직전 질문의 기업·업종을 임의로 다른 것으로 대체하는 것보다는 그대로 유지하는 쪽을 "
    "택하십시오.** 지금 질문이 직전 질문과 무관한 새 주제이면 직전 질문은 참고하지 말고 지금 "
    "질문만 그대로 정규화하십시오.\n"
    "4. 그 외 원문의 의미·숫자·연도·지표 표현은 그대로 보존하십시오. 새로운 사실을 추가하거나 "
    "질문에 답하지 마십시오 — 오직 질문 문장만 다시 씁니다.\n"
    "5. 정규화된 질문 한 줄만 출력하십시오. 설명·따옴표를 붙이지 마십시오."
)


def normalize(question, corp_names, timeout=8, prev_question=None):
    """[더 이상 pipeline.py에서 호출되지 않음 — 참고/롤백용] 질문 표현을 정리해
    재파싱용 문장을 돌려준다. 실패하거나 바뀐 게 없으면 None.

    이 함수가 안고 있던 구조적 한계(간접적 diff 가드로만 검증 가능)는
    `extract_slots()`가 폐집합 슬롯 검증으로 대체했다 — 모듈 docstring 참고.
    """
    key = load_key()
    if not key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None

    names_block = ", ".join(corp_names) if corp_names else "(없음)"
    user = f"등록된 회사명 목록: {names_block}\n"
    if prev_question:
        user += f"직전 질문: {prev_question}\n"
    user += f"지금 질문: {question}"
    try:
        cli = OpenAI(api_key=key, base_url=BASE_URL, timeout=timeout, max_retries=0)
        r = cli.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": user}],
            max_tokens=MAX_OUTPUT_TOKENS, temperature=0.0)
    except Exception:                                          # noqa: BLE001
        return None
    out = (r.choices[0].message.content or "").strip().strip('"').strip("'")
    return out if out and out != question else None


# ---------------------------------------------------------------------------
# Step 1 — 호출 로그 (jsonl, append-only)
# ---------------------------------------------------------------------------
def _log_slots_call(*, question, prev_question, prev_slots, candidates_size,
                     raw_model_output, parsed_slots, validation, accepted, reason):
    """매 extract_slots() 호출마다(성공/실패/거부 전부) 한 줄 JSON을 남긴다.

    거부된 것도 남긴다 — 나중에 hard negative 학습 데이터로 쓴다. 로그 쓰기
    실패(디스크 문제 등)가 파이프라인 응답 자체를 막으면 안 되므로 항상
    try/except로 감싼다 — 실패해도 정상 응답은 그대로 나간다.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "question": question,
        "prev_question": prev_question,
        "prev_slots": prev_slots,
        "candidates_size": candidates_size,
        "raw_model_output": raw_model_output,
        "parsed_slots": parsed_slots,
        "validation": validation,
        "accepted": accepted,
        "reason": reason,
    }
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:                                          # noqa: BLE001
        pass


def log_carryover(question, prev_question, prev_slots, carried_fields, result_slots):
    """qa/pipeline.py::_direct_carryover()가 LLM 없이 코드로 슬롯을 승계했을
    때마다 남긴다 — 같은 로그 파일에 origin="carryover"로 구분해서 쌓는다.

    나중에 이 로그로 (a) 전체 호출 중 승계로 끝난 비율(=LLM 호출 절감량),
    (b) 승계된 슬롯이 실제로 맞았는지(사람이 나중에 표본 검토)를 각각
    측정한다 — extract_slots() 실제 LLM 호출과 같은 파일에 섞이므로, 이
    origin 필드로만 갈라서 집계하면 된다.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "origin": "carryover",
        "question": question,
        "prev_question": prev_question,
        "prev_slots": prev_slots,
        "carried_fields": carried_fields,
        "result_slots": result_slots,
        "accepted": True,
        "reason": "carryover",
    }
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:                                          # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Step 2 — 슬롯 JSON 추출
# ---------------------------------------------------------------------------
_SLOT_KEYS = ("corp", "concept", "year", "scope", "event", "wh", "intent")

# 고정 후보 — qa/ontology.py::find_wh()가 실제로 돌려주는 값 전체, 그리고
# qa/ontology.py·code_chunkingandparsing/src/numqa.py가 쓰는 전체 intent
# 문자열(2026-09-04 코드로 재확인). scope는 qa/pipeline.py::SCOPE_KO_ALL의
# 키와 동일하다(순환 임포트를 피하려 값만 여기 다시 적는다 — pipeline.py가
# 이미 llmparse를 임포트하므로 반대 방향 임포트는 안 된다).
SCOPE_CANDIDATES = ["consolidated", "separate"]
WH_CANDIDATES = ["when", "amount", "who", "why"]
INTENT_CANDIDATES = [
    "fact_numeric", "fact_compute", "comparison", "existence", "dual",
    "ranking", "aggregate", "derived", "struct_field", "struct_compare",
    "compare_multi",
]

SYSTEM_SLOTS = (
    "당신은 한국 기업 공시 QA 시스템의 슬롯 추출기입니다.\n"
    "아래 슬롯 스키마와 각 슬롯의 후보 목록을 보고, 지금 질문(+직전 질문+직전 "
    "확정 슬롯)이 가리키는 슬롯 값을 JSON으로만 출력하십시오.\n"
    "슬롯: corp(기업명) · concept(재무제표 지표명) · year(정수 연도) · "
    "scope(consolidated 또는 separate) · event(공시 이벤트 유형) · "
    "wh(when/amount/who/why — 질문이 묻는 것의 종류) · intent(질문 의도).\n"
    "규칙:\n"
    "1. corp·concept·event·scope·wh·intent는 반드시 그 슬롯의 '후보 목록'에 "
    "있는 표기와 정확히 일치하는 값 또는 null만 출력하십시오. year는 후보 "
    "목록 대신 유효 범위로 주어지며, 그 범위 밖이거나 확신이 없으면 null을 "
    "출력하십시오. 목록에 없는 새 값을 만들어내지 마십시오 — 조금이라도 "
    "확신이 없으면 null을 고르십시오.\n"
    "2. **지금 질문이 그 슬롯을 새로 지정하지 않았으면 반드시 null을 "
    "출력하십시오.** 직전 값을 임의로 그대로 베끼거나, 그럴듯한 다른 값으로 "
    "바꿔치기하지 마십시오 — null이 나오면 그 슬롯은 이 결과를 받는 코드가 "
    "직전 턴 값으로 채웁니다. 그 판단은 당신이 아니라 코드가 합니다.\n"
    "3. 지금 질문이 명시적으로 다른 기업·지표·연도 등을 지정했으면(직전 "
    "질문과 다른 대상이면) 그 새 값으로 채우십시오 — 이때는 직전 값을 "
    "유지하지 않습니다.\n"
    "4. 출력은 아래 7개 키를 모두 포함한 JSON 객체 하나뿐입니다. 설명· "
    "따옴표·코드블록을 붙이지 마십시오.\n"
    '{"corp": null, "concept": null, "year": null, "scope": null, '
    '"event": null, "wh": null, "intent": null}'
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def _year_range(labels):
    """corpus에 실제로 존재하는 연도 범위 (min, max). 없으면 (None, None).

    새로 전수 스캔을 하지 않는다 — `LabelStore.years_of`가 __init__에서 이미
    전체 fact를 훑어 (기업,개념,기준) → 연도 집합으로 인메모리에 담아 둔 것을
    그대로 재사용한다(qa/labelstore.py 참고).
    """
    years = {y for ys in labels.years_of.values() for y in ys if y}
    return (min(years), max(years)) if years else (None, None)


def _build_candidates(store, labels):
    """폐집합 후보 조립 — 전부 corpus 유래다(손으로 적은 목록 없음).

    - corp: store.corp_names (numqa.FactStore가 이미 만들어 둔 것)
    - concept: concepts.py::ConceptIndex.llm_candidates (qa/ontology.py:346이
      resolve.py 호출에 쓰는 바로 그 리스트 — coverage로 이미 추려져 있다)
    - event: events_vocab.llm_candidates() (이번 작업에서 새로 추가, 위와 같은
      coverage 방식)

    실측 버그(2026-09-04): 위 세 소스는 내부적으로 set/dict를 거쳐 만들어져
    프로세스마다(해시 시드마다) 순서가 달라진다 — 튜닝 데이터 생성 스크립트가
    한 번 부른 `_build_candidates()`의 순서와, 실제 런타임에 매 호출마다 새로
    부르는 순서가 달라서 **같은 질문·같은 슬롯인데도 프롬프트 텍스트가 매번
    바뀌는** 문제가 있었다(학습 데이터의 input과 실제 서비스 input이 불일치).
    여기서 명시적으로 안정 정렬해 프로세스와 무관하게 항상 같은 순서가 나오게
    한다 — concept/event는 원래 의도(흔한 것 먼저)를 살리되 동점일 때 이름
    알파벳순으로 타이브레이크만 추가한다.
    """
    from . import concepts, derived, events_vocab
    ci = concepts.get(labels.facts)
    # 파생비율(부채비율 등)은 concepts.py 어휘(재무제표 행)에 없다 — 계산으로
    # 만드는 값이라 corpus에 그 이름의 행 자체가 없기 때문이다(qa/derived.py
    # 모듈 docstring 참고). 그래서 ci.llm_candidates만 쓰면 "부채비율이 뭐야?"
    # 류 질문에서 concept 후보가 구조적으로 없어 모델이 절대 이 값을 낼 수
    # 없었다(실측: heldout 6건이 이 이유로 실패). derived.RULES의 이름은 전부
    # 이미 한글 정규형이라(딕셔너리 키 자체가 "부채비율" 등) 그대로 더한다 —
    # coverage 개념이 없어 XBRL 후보들 뒤에 알파벳순으로 붙인다.
    concept_cands = sorted(ci.llm_candidates, key=lambda c: (-ci.coverage(c), c))
    concept_cands += sorted(k for k in derived.RULES if k not in ci.llm_candidates)
    return {
        "corp": sorted(store.corp_names or []),
        "concept": concept_cands,
        "event": sorted(events_vocab.llm_candidates(),
                        key=lambda t: (-events_vocab.event_types()[t]["corps"], t)),
        "scope": SCOPE_CANDIDATES,
        "wh": WH_CANDIDATES,
        "intent": INTENT_CANDIDATES,
    }


def _build_user_prompt(question, prev_question, prev_slots, candidates, year_range):
    lo, hi = year_range
    lines = [
        f"기업 후보: {', '.join(candidates['corp']) if candidates['corp'] else '(없음)'}",
        f"지표 후보: {', '.join(candidates['concept']) if candidates['concept'] else '(없음)'}",
        f"이벤트 유형 후보: {', '.join(candidates['event']) if candidates['event'] else '(없음)'}",
        f"scope 후보: {', '.join(candidates['scope'])}",
        f"wh 후보: {', '.join(candidates['wh'])}",
        f"intent 후보: {', '.join(candidates['intent'])}",
        (f"year 유효 범위: {lo}~{hi}" if lo is not None else "year 유효 범위: (알 수 없음 — 항상 null)"),
    ]
    if prev_question:
        lines.append(f"직전 질문: {prev_question}")
    if prev_slots:
        lines.append(f"직전 확정 슬롯: {json.dumps(prev_slots, ensure_ascii=False)}")
    lines.append(f"지금 질문: {question}")
    return "\n".join(lines)


def _parse_json_object(text):
    """모델 원문에서 JSON 객체 하나를 뽑는다. 코드블록·잡담이 섞여도 시도한다."""
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    m = _JSON_OBJECT.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _validate_slot(key, value, candidates, year_range):
    """폐집합 검증(qa/resolve.py와 같은 원칙) — 후보 밖/범위 밖이면 (None, 사유).

    resolve.py의 `if resolved not in candidates: resolved = None`과 동일한
    패턴이다. 이 검증만으로 "존재하지 않는 값을 지어내는 것"은 막힌다 —
    "후보 집합 안에 있는 유효한 값으로 잘못 바꿔치기하는 것"까지는 못 막는다
    (모듈 docstring 참고, 사용자 설계 결정).
    """
    if value is None:
        return None, "null"
    if key == "year":
        lo, hi = year_range
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return None, "rejected_not_in_candidates"
        if lo is None or not (lo <= iv <= hi):
            return None, "rejected_out_of_range"
        return iv, "ok"
    cand = candidates.get(key) or []
    if value not in cand:
        return None, "rejected_not_in_candidates"
    return value, "ok"


def extract_slots(question, prev_question, prev_slots, store, labels, timeout=8):
    """지금 질문(+직전 질문+직전 확정 슬롯)이 새로 지정한 슬롯 값을 JSON으로 받는다.

    반환: {"corp","concept","year","scope","event","wh","intent"} 딕셔너리
    (각 값은 검증된 값 또는 None) 또는 호출 자체가 실패했으면 None.
    각 슬롯은 명시적으로 제공된 후보 집합의 원소 또는 null만 허용한다 — 모델이
    후보 밖 값을 냈으면 그 슬롯만 None으로 걸러지고(전체 실패가 아니다), 어느
    슬롯이 왜 걸러졌는지는 data/llmparse_log.jsonl에 전부 남는다.

    null은 "지금 질문이 이 슬롯을 새로 지정하지 않았다 → 직전 턴 값을 그대로
    쓴다"는 뜻이다 — 그 병합은 이 함수가 아니라 호출부(qa/pipeline.py)가 한다.
    """
    candidates = _build_candidates(store, labels)
    year_range = _year_range(labels)
    candidates_size = {"corp": len(candidates["corp"]), "concept": len(candidates["concept"]),
                        "event": len(candidates["event"])}
    null_validation = {k: "null" for k in _SLOT_KEYS}

    def _finish(raw_output, parsed, validation, accepted, reason):
        _log_slots_call(question=question, prev_question=prev_question,
                         prev_slots=prev_slots, candidates_size=candidates_size,
                         raw_model_output=raw_output, parsed_slots=parsed,
                         validation=validation, accepted=accepted, reason=reason)

    key = load_key()
    if not key:
        _finish(None, None, null_validation, False, "no_api_key")
        return None
    try:
        from openai import OpenAI
    except ImportError:
        _finish(None, None, null_validation, False, "openai_not_installed")
        return None

    user = _build_user_prompt(question, prev_question, prev_slots, candidates, year_range)
    try:
        cli = OpenAI(api_key=key, base_url=BASE_URL, timeout=timeout, max_retries=0)
        r = cli.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM_SLOTS},
                      {"role": "user", "content": user}],
            max_tokens=MAX_OUTPUT_TOKENS, temperature=0.0)
    except Exception as e:                                     # noqa: BLE001
        _finish(None, None, null_validation, False, f"{type(e).__name__}: {e}")
        return None

    raw = (r.choices[0].message.content or "").strip()
    parsed = _parse_json_object(raw)
    if parsed is None:
        _finish(raw, None, null_validation, False, "invalid_json")
        return None

    out, validation = {}, {}
    for k in _SLOT_KEYS:
        v, why = _validate_slot(k, parsed.get(k), candidates, year_range)
        out[k], validation[k] = v, why

    _finish(raw, out, validation, True, "parsed")
    return out


# ---------------------------------------------------------------------------
# Step 2 — 결정론 템플릿 렌더
# ---------------------------------------------------------------------------
# scope 한국어 표기 — qa/pipeline.py::SCOPE_KO_ALL과 같은 값(순환 임포트를
# 피하려 값만 다시 적는다; pipeline.py가 이미 이 모듈을 임포트한다).
_SCOPE_KO = {"consolidated": "연결", "separate": "별도"}


def render_question(slots):
    """병합된 슬롯(LLM 슬롯의 null을 직전 턴 값으로 채운 것)을 고정 패턴
    한국어 질문 문장으로 렌더링한다. 그 문장은 그대로 ontology.parse()가
    다시 읽는다 — "무엇을 렌더링할지"는 슬롯이 결정하고, "어떻게 문장으로
    만들지"는 여기 고정 템플릿이 결정한다.

    자신 있게 만들 수 있는 조합만 다루고, 그 외에는 None을 돌려줘 호출부가
    "재작성 실패 → 원문 그대로 진행"으로 자연스럽게 폴백하게 한다 — 확신
    없으면 억지 템플릿을 만들지 않는다(이 코드베이스 전체의 원칙).

    지금 커버하는 조합(이번 세션 T1~T6 사고와 직접 관련된, 가장 흔하고
    위험도 높은 경우만 — 전체 11개 intent를 다 커버하려 하지 않는다):
      1) wh in ("when", "amount") + event + corp → 이벤트 기준 문장
      2) intent in ("fact_numeric", "fact_compute", "derived")
         + corp + concept + year → XBRL 조회 문장(+scope 있으면 반영)
    그 외(ranking/aggregate/compare_multi/struct_compare/dual/comparison/
    existence, 또는 위 조합에 필요한 슬롯이 부족한 경우)는 전부 None이다 —
    다음 단계(학습 데이터 생성)의 스코프를 정하는 것이 이 목록이다.
    """
    corp = slots.get("corp")
    if not corp:
        return None

    wh = slots.get("wh")
    event = slots.get("event")
    if wh in ("when", "amount") and event:
        if wh == "when":
            return f"{corp} {event} 언제 결정했어"
        return f"{corp} {event} 금액이 얼마야"

    intent = slots.get("intent")
    concept = slots.get("concept")
    year = slots.get("year")
    # intent가 null이어도(모델이 다른 슬롯은 다 채우고 intent만 "이 발화가
    # 새로 지정한 게 아니다"로 비웠을 때 — 실측 흔함, LLM/직승계 둘 다) concept+
    # year가 있으면 fact_numeric으로 본다. 틀려도 안전하다 — 이 문장은
    # ontology.parse()가 처음부터 다시 읽어 자기 규칙으로 intent를 새로
    # 정하므로, 여기서는 "그럴듯한 조회 문장 하나를 만든다"는 역할뿐이다.
    if intent is None or intent in ("fact_numeric", "fact_compute", "derived"):
        if concept and year:
            scope_ko = _SCOPE_KO.get(slots.get("scope"), "")
            scope_part = f"{scope_ko} 기준 " if scope_ko else ""
            return f"{corp}의 {year}년 {scope_part}{concept}은(는) 얼마인가?"

    return None
