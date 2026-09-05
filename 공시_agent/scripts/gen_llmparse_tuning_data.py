#!/usr/bin/env python3
"""qa/llmparse.py::extract_slots() 파인튜닝용 학습 데이터 생성기.

## 절대 규칙 — 이 스크립트는 CLOVA API를 한 번도 부르지 않는다

`extract_slots()` 자체를 호출하지 않는다 — output(정답 슬롯)은 전부 이미 알려진
정답(goldset/이벤트 템플릿/conv_replay 재구성)에서 결정론적으로 만든다.
`llmparse._build_user_prompt()`·`llmparse._validate_slot()`·`llmparse._build_candidates()`·
`llmparse._year_range()`는 순수 함수라서(네트워크 호출 없음) 그대로 재사용한다 —
학습 데이터의 user 텍스트가 런타임 실제 입력과 한 글자도 다르면 안 되기 때문이다.
`qa/pipeline.get_store()`·`qa/labelstore.get()`도 로컬 캐시(facts.db·labelstore.jsonl)만
읽는 순수 로딩이라 API 키가 없어도 동작한다.

캐주얼 변주는 전부 regex 치환 기반 결정론 변형이다 — 또 다른 LLM 호출로 만들지
않는다(재현 가능성·비용 0 보장).

## 출력

data/llmparse_tuning/train.jsonl, eval.jsonl — 각 레코드:
    {"system": ..., "user": ..., "output": "<json>", "category": ..., "note": ...}

Clova Studio가 정확히 어떤 업로드 스키마를 요구하는지는 이번 세션에서 확인되지
않았다 — 이 (system,user,output) 포맷은 범용 형태이고, 실제 튜닝 job을 넣기
전에 NCP Clova Studio 문서 기준으로 포맷 변환이 한 번 더 필요할 수 있다.

    python scripts/gen_llmparse_tuning_data.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qa import concepts, events_vocab, labelstore, llmparse, pipeline, sectors   # noqa: E402

GOLD_ROOT = ROOT.parent / "code_chunkingandparsing"
EVENTS_JSON = ROOT / "data" / "eval_reports" / "details_gen_gold_events.json"
OUT_DIR = ROOT / "data" / "llmparse_tuning"

SLOT_KEYS = llmparse._SLOT_KEYS
_SCOPE_KO = llmparse._SCOPE_KO


# ---------------------------------------------------------------------------
# 0) 실 코퍼스에서 후보 집합 조립 — extract_slots()가 실제로 쓰는 것과 동일한
#    _build_candidates()/_year_range()를 그대로 호출한다. API 호출 없음(순수
#    로컬 함수 — store/labels는 facts.db·labelstore.jsonl 캐시 로딩일 뿐이다).
# ---------------------------------------------------------------------------
def load_context():
    store = pipeline.get_store()
    labels = labelstore.get()
    candidates = llmparse._build_candidates(store, labels)
    year_range = llmparse._year_range(labels)
    ci = concepts.get(labels.facts)
    return store, labels, candidates, year_range, ci


def build_metric_to_concept(ci, candidates):
    """metric_key → concept 표면형. qa/ontology.py:417의 역매핑 예시와 같은 방식
    (`next((c for c, m in ci.metric_of.items() if m == metric), None)`)이되,
    후보 집합(candidates['concept'] = ci.llm_candidates)에 실제로 있는 값만
    받는다 — 아니면 학습 데이터 자체가 _validate_slot()에서 걸린다."""
    cand = set(candidates["concept"])
    out = {}
    for c, mk in ci.metric_of.items():
        if c not in cand:
            continue
        cur = out.get(mk)
        if cur is None or ci.coverage(c) > ci.coverage(cur):
            out[mk] = c
    return out


def full_slots(**kw):
    d = {k: None for k in SLOT_KEYS}
    d.update(kw)
    return d


def validate_output(slots, candidates, year_range):
    """레코드의 output이 _validate_slot()을 그대로 통과하는 값들로만 됐는지.

    통과 = 각 슬롯을 다시 검증했을 때 값이 바뀌지 않는 것(=이미 폐집합 안).
    """
    for k in SLOT_KEYS:
        v = slots.get(k)
        vv, _why = llmparse._validate_slot(k, v, candidates, year_range)
        if vv != v:
            return False, k
    return True, None


def make_record(question, prev_question, prev_slots, output_slots, candidates,
                 year_range, category, note):
    user = llmparse._build_user_prompt(question, prev_question, prev_slots,
                                        candidates, year_range)
    return {
        "system": llmparse.SYSTEM_SLOTS,
        "user": user,
        "output": json.dumps(output_slots, ensure_ascii=False),
        "category": category,
        "note": note,
    }


# ---------------------------------------------------------------------------
# 캐주얼 변주 — 규칙 기반 결정론 변형만 쓴다(LLM 호출 없음).
# ---------------------------------------------------------------------------
_ENDING_RULES = [
    (re.compile(r"은 얼마인가\?$"), ["은 얼마야?", " 얼마임?", " 얼마인지 알려줘", " 얼마나 돼?"]),
    (re.compile(r"는 얼마인가\?$"), ["는 얼마야?", " 얼마임?", " 얼마인지 알려줘", " 얼마나 돼?"]),
    (re.compile(r"몇 % 증가했는가\?$"),
     ["몇 % 늘었어?", "몇 프로 증가한거야?", "얼마나 증가했어?", "몇 % 정도 늘었어?"]),
    (re.compile(r"값이 일치하는가\?$"), ["값이 같아?", "일치해?", "서로 같아?", "똑같은거야?"]),
]


def casualize(question, idx):
    """어미 교체 + (idx 기반) 조사 생략. 전부 결정론(idx로만 갈라진다)."""
    out = question
    for pat, options in _ENDING_RULES:
        if pat.search(out):
            out = pat.sub(options[idx % len(options)], out)
            break
    if idx % 10 < 3:
        out = re.sub(r"의(\s|$)", r"\1", out, count=1)  # "~의" 조사 30% 생략
    return out


def drop_year_token(question, year):
    """연도 리터럴을 질문에서 전부 지운다(멀티턴: 연도는 prev_slots로만 승계).

    goldA_restatement류 질문은 같은 연도(fiscal_year)가 "OO년 연결 매출액"과
    "OO년 사업보고서" 두 자리에 반복해서 나온다 — 한 번만 지우면(count=1) 두
    번째 자리에 연도가 그대로 남아 "질문이 연도를 명시했는데 정답은 null"이라는
    모순된 학습 신호가 생긴다. 전부 지운다(count=0 = 무제한).
    """
    return re.sub(rf"{year}년\s*", "", question)


# ---------------------------------------------------------------------------
# (a) goldset_positive — goldset_layerA/B의 정형 질문을 소스로, 입력은 캐주얼
#     변주, 정답 슬롯은 결정론적으로 구성한다.
# ---------------------------------------------------------------------------
def load_jsonl(path):
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def gen_goldset_positive(candidates, year_range, metric_to_concept):
    sources = [
        ("goldA_restatement", GOLD_ROOT / "goldset_layerA" / "goldA_restatement.jsonl", "fiscal_year", 50),
        ("goldB_fact_numeric", GOLD_ROOT / "goldset_layerB" / "goldB_fact_numeric.jsonl", "base_year", 80),
        ("goldB_compute", GOLD_ROOT / "goldset_layerB" / "goldB_compute.jsonl", "base_year", 40),
        ("goldB_dual", GOLD_ROOT / "goldset_layerB" / "goldB_dual.jsonl", "base_year", 30),
    ]
    # goldB_facts.jsonl == fact_numeric+compute+dual의 합집합(동일 qid로 실측
    # 확인) 이라 따로 쓰지 않는다 — 겹쳐 쓰면 같은 레코드를 두 번 세게 된다.
    corp_set = set(candidates["corp"])
    records, i = [], 0
    for slice_name, path, year_key, want in sources:
        if not path.exists():
            continue
        rows = load_jsonl(path)
        stride = max(1, len(rows) // want)
        sampled = rows[::stride][:want]
        for rec in sampled:
            corp = rec.get("corp_name")
            if corp not in corp_set:
                continue
            mk = rec.get("metric_key")
            concept = metric_to_concept.get(mk)
            if not concept:
                continue
            year = rec.get(year_key)
            scope = rec.get("scope")           # dual은 없음 → None(둘 다 → 미지정)
            intent = rec.get("intent")
            q = rec["question"]

            multiturn = (i % 10) < 3            # 30%: 연도 생략 + prev_slots 승계
            # 비율 근거: 사용자 지시("일부는 인위적 멀티턴") — 정량적 요구가 없어
            # 임의로 30%를 택했다(단일턴:멀티턴 = 7:3, 실제 대화에서 후속질문이
            # 매 턴 발생하진 않는다는 상식적 가정).
            casual_q = casualize(q, i)
            if multiturn:
                casual_q = drop_year_token(casual_q, year)
                prev_slots = {"corp": corp, "corps": [corp], "concept": concept,
                              "year": year, "scope": scope}
                out = full_slots(corp=corp, concept=concept, scope=scope, intent=intent)
                note = f"{slice_name}·멀티턴(연도는 prev_slots 승계)"
                rec_out = make_record(casual_q, None, prev_slots, out, candidates,
                                      year_range, "goldset_positive", note)
            else:
                out = full_slots(corp=corp, concept=concept, year=year, scope=scope,
                                 intent=intent)
                note = f"{slice_name}·단일턴"
                rec_out = make_record(casual_q, None, None, out, candidates,
                                      year_range, "goldset_positive", note)
            records.append(rec_out)
            i += 1
    return records


# ---------------------------------------------------------------------------
# (b) event_template — details_gen_gold_events.json의 template1(when)/
#     template3(amount) 레코드. 질문 필드를 그대로 쓴다(이미 캐주얼한 문장).
# ---------------------------------------------------------------------------
def gen_event_template(candidates, year_range):
    if not EVENTS_JSON.exists():
        return []
    d = json.loads(EVENTS_JSON.read_text(encoding="utf-8"))
    ev_cand = set(candidates["event"])
    corp_cand = set(candidates["corp"])
    records = []
    for slice_name, wh, want in (("template1_when", "when", 50), ("template3_amount", "amount", 50)):
        rows = [x for x in d[slice_name] if x["type"] in ev_cand and x["corp"] in corp_cand]
        stride = max(1, len(rows) // want)
        for rec in rows[::stride][:want]:
            out = full_slots(corp=rec["corp"], event=rec["type"], wh=wh)
            note = f"{slice_name}·상태(참고,생성당시)={rec.get('state')}"
            records.append(make_record(rec["question"], None, None, out, candidates,
                                       year_range, "event_template", note))
    return records


# ---------------------------------------------------------------------------
# (c) convreplay_multiturn — tests/conv_replay/test_conv_replay.py의 실제
#     사고 재현. T2/T3/T5만 포함한다 — T1/T4/T6는 llmparse.extract_slots()가
#     프로덕션에서 아예 호출되지 않는 턴이다(T1: 규칙기반 단독으로 miss가 비어
#     호출 안 됨. T4: _is_definition_q가 llmparse 블록보다 먼저 glossary로
#     가로챔 — qa/pipeline.py:2511. T6: _CORRECTION_SIGNAL이 먼저 가로챔 —
#     qa/pipeline.py:2518). 이 3턴에 대한 목표 슬롯은 test_conv_replay.py의
#     FAKE_SLOTS를 그대로 가져온다 — 그 파일 자체가 실측 트레이스를 역산해
#     재구성한 "실제 LLM이 냈을 법한, 폐집합 검증을 통과한 결과"이기 때문이다.
#     T1/T4/T6까지 6건을 채우려면 그 턴들에 대해 "extract_slots가 냈을 값"을
#     새로 지어내야 하는데, 실제로 호출되지 않는 지점이라 그 값엔 대조할 실측
#     근거가 없다 — 지어내지 않는다(사용자 지시: "모른다고 지어내지 마라").
# ---------------------------------------------------------------------------
def gen_convreplay(candidates, year_range):
    turns = [
        ("T1", "한화에어로스페이스가 유상증자를 결정한 이후 부채비율이 어떻게 변했어?", None, None, None),
        ("T2", "유상증자를 언제 결정했는데?",
         "한화에어로스페이스가 유상증자를 결정한 이후 부채비율이 어떻게 변했어?",
         {"corp": "한화에어로스페이스", "corps": ["한화에어로스페이스"], "concept": None,
          "year": None, "scope": None},
         {"corp": None, "concept": None, "year": None, "scope": None,
          "event": "유상증자결정", "wh": "when", "intent": None}),
        ("T3", "모든 경우의수 다 고려해서 알려줘", "유상증자를 언제 결정했는데?",
         {"corp": "한화에어로스페이스", "corps": ["한화에어로스페이스"], "concept": None,
          "year": None, "scope": None},
         {"corp": None, "concept": None, "year": None, "scope": None,
          "event": "유상증자결정", "wh": "when", "intent": None}),
        ("T4", "연결/별도 기준이 다르므로 구분이 필요 무슨 말이야", None, None, None),
        ("T5", "투자할만한 회사야?", "모든 경우의수 다 고려해서 알려줘",
         {"corp": "한화에어로스페이스", "corps": ["한화에어로스페이스"], "concept": None,
          "year": None, "scope": None},
         {"corp": "LIG디펜스앤에어로스페이스", "concept": None, "year": None,
          "scope": None, "event": None, "wh": None, "intent": None}),
        ("T6", "내가 한화 에어로 스페이스라고 하지않았나?", None, None, None),
    ]
    records = []
    for label, q, prev_q, prev_slots, out in turns:
        if out is None:
            continue   # T1/T4/T6 — extract_slots가 프로덕션에서 호출 안 됨(위 docstring)
        note = (f"conv_replay {label} — 실측 사고 재현(test_conv_replay.py FAKE_SLOTS 그대로), "
                "실제로 extract_slots가 호출된 턴")
        records.append(make_record(q, prev_q, prev_slots, out, candidates, year_range,
                                   "convreplay_multiturn", note))
    return records


# ---------------------------------------------------------------------------
# (d) adversarial_negative — 업종/접두어/접미어로 헷갈리는 기업 쌍.
#     T4~T6 사고(한화에어로스페이스→LIG디펜스앤에어로스페이스 오염)를 직접 겨냥.
# ---------------------------------------------------------------------------
_IMPLICIT_FOLLOWUPS = [
    "그래서 어떻게 변했어?", "그럼 작년은?", "그거 맞아?", "최근엔 어때?",
    "그거 좀 더 자세히 알려줄래?", "그러면 늘어난거야 줄어든거야?", "왜 그런거야?", "정말이야?",
]
_EXPLICIT_FOLLOWUPS = [
    "그럼 {b}는?", "{b}는 어때?", "{b}랑 비교하면?", "그럼 {b}는 얼마야?", "{b} 기준으로도 알려줘",
]


def confusable_pairs(corp_names):
    """업종(sectors.py)·접두어·접미어 겹침으로 헷갈리는 기업 쌍(70개사 실제 스캔)."""
    from itertools import combinations
    from collections import defaultdict

    names = sorted(corp_names)
    sectors.restrict(set(names))
    idx = sectors.load()
    pairs = set()
    for k, v in idx["sector"].items():
        members = sorted(c for c in v if c in names)
        pairs.update(combinations(members, 2))
    sectors.restrict(None)

    pref = defaultdict(list)
    for n in names:
        pref[n[:2]].append(n)
    for v in pref.values():
        if len(v) >= 2:
            pairs.update(combinations(sorted(v), 2))

    suf = defaultdict(list)
    for n in names:
        for L in (4, 5, 6, 7):
            if len(n) > L:
                suf[n[-L:]].append(n)
    for v in suf.values():
        vv = sorted(set(v))
        if len(vv) >= 2:
            pairs.update(combinations(vv, 2))
    return sorted(pairs)


def gen_adversarial(candidates, year_range):
    pairs = confusable_pairs(candidates["corp"])
    concept = "매출액"
    year = 2024
    scope = "consolidated"
    assert concept in candidates["concept"] and year_range[0] <= year <= year_range[1]

    records = []
    for i, (a, b) in enumerate(pairs):
        prev_q = f"{a}의 {year}년 {_SCOPE_KO[scope]} 기준 {concept}은 얼마인가?"
        prev_slots = {"corp": a, "corps": [a], "concept": concept, "year": year, "scope": scope}

        # (i) 새 기업을 전혀 언급하지 않는 후속 질문 → corp는 null이어야 한다
        # (직전 corp로 승계는 코드(_merge_slots)의 몫 — 모델이 임의로 A를 복붙하거나
        # B로 바꿔치기하면 안 된다).
        followup_null = _IMPLICIT_FOLLOWUPS[i % len(_IMPLICIT_FOLLOWUPS)]
        out_null = full_slots()
        note_null = (f"hard_negative_null: prev corp={a}, 헷갈리는 이웃={b} "
                     "(업종/접두어/접미어 공유로 찾은 페어)")
        records.append(make_record(followup_null, prev_q, prev_slots, out_null,
                                   candidates, year_range, "adversarial_negative", note_null))

        # (ii) 명시적으로 새 기업 B를 언급하는 후속 질문 → corp는 B로 바뀌어야 한다
        followup_switch = _EXPLICIT_FOLLOWUPS[i % len(_EXPLICIT_FOLLOWUPS)].format(b=b)
        out_switch = full_slots(corp=b)
        note_switch = f"hard_negative_switch: prev corp={a} → 명시적 전환 corp={b}"
        records.append(make_record(followup_switch, prev_q, prev_slots, out_switch,
                                   candidates, year_range, "adversarial_negative", note_switch))
    return records


# ---------------------------------------------------------------------------
# (e) no_candidate_abstain — 후보 집합에 없는 값 → null.
#     (e-1) goldC.jsonl의 ratio 슬라이스: "부채비율"·"유동비율"은 파생 비율이라
#     concepts.py::ConceptIndex 표면형에 없다(실측 확인: llm_candidates에 없음)
#     → 실제 기업·실제 연도인데도 concept만 null이 정답인 자연스러운 예시.
#     (e-2) 완전히 가상의 회사명(코퍼스에 없음)을 언급하는 합성 질문 → corp null.
# ---------------------------------------------------------------------------
_GOLDC_RATIO_RE = re.compile(r"^(.+?)의 (\d{4})년 (.+?)은 얼마인가\?$")

_FAKE_CORPS = ["가상전자", "테스트홀딩스", "별빛에너지", "미래로보테크", "한별전자", "청록바이오"]


def gen_no_candidate_abstain(candidates, year_range):
    records = []
    corp_set = set(candidates["corp"])
    concept_set = set(candidates["concept"])

    # (e-1) goldC ratio
    path = GOLD_ROOT / "goldset_layerC" / "goldC.jsonl"
    if path.exists():
        rows = [r for r in load_jsonl(path) if r.get("slice") == "ratio"]
        matched = []
        for r in rows:
            m = _GOLDC_RATIO_RE.match(r["question"])
            if not m:
                continue
            corp, year, concept = m.group(1), int(m.group(2)), m.group(3)
            if corp in corp_set and concept not in concept_set:
                matched.append((r["question"], corp, year))
        stride = max(1, len(matched) // 20)
        for q, corp, year in matched[::stride][:20]:
            out = full_slots(corp=corp, year=year)   # concept: 후보 밖 → null
            note = "goldC ratio — 파생비율(부채비율 등)은 concept 후보 목록 밖 → concept:null"
            records.append(make_record(casualize(q, 0), None, None, out, candidates,
                                       year_range, "no_candidate_abstain", note))

    # (e-2) 완전 가상 회사
    for i, fake in enumerate(_FAKE_CORPS):
        q = f"{fake}의 2024년 매출액은 얼마야?"
        out = full_slots(concept="매출액", year=2024)   # corp: 후보 밖(코퍼스에 없음) → null
        note = f"corpus에 없는 가상 회사명({fake}) → corp:null"
        records.append(make_record(q, None, None, out, candidates, year_range,
                                   "no_candidate_abstain", note))
    return records


# ---------------------------------------------------------------------------
# (f) definition_corp_null — 정의형 질문. 방어적 학습 예시(실제 프로덕션
#     트리거 경로 아님 — qa/pipeline.py::_run()에서 _is_definition_q 검사가
#     llmparse 블록(2543행)보다 먼저(2511행) glossary로 가로채므로, 이 케이스는
#     실제로는 extract_slots()까지 오지 않는다. 그래도 견고성을 위해 넣는다).
# ---------------------------------------------------------------------------
_DEFINE_TEMPLATES = ["{c}가 뭐야?", "{c}란 무엇인가요?", "{c} 무슨 뜻이야?"]


def gen_definition_corp_null(candidates, year_range):
    sample_concepts = ["매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계"]
    sample_corps = candidates["corp"][:6]
    records = []
    for concept, corp in zip(sample_concepts, sample_corps):
        for tmpl in _DEFINE_TEMPLATES:
            q = tmpl.format(c=concept)
            prev_slots = {"corp": corp, "corps": [corp], "concept": concept,
                          "year": 2024, "scope": "consolidated"}
            # 정의형 질문은 데이터 조회 연속이 아니라 개념 설명 요청이다 — 직전
            # 슬롯을 전혀 잇지 않는 새 화제로 보고 전 슬롯 null을 정답으로 삼는다
            # (모델이 "매출액이 뭐야?"의 "매출액"을 데이터 조회 concept로 잘못
            # 채우는 것까지 막을 필요는 없다는 판단 — corp:null이 핵심 신호다).
            out = full_slots()
            note = ("정의형 질문 방어적 예시 — 실제로는 G3-1(qa/pipeline.py:2511)이 "
                    "llmparse보다 먼저 가로채 이 경로까지 오지 않음")
            records.append(make_record(q, None, prev_slots, out, candidates, year_range,
                                       "definition_corp_null", note))
    return records


# ---------------------------------------------------------------------------
# 조립·검증·분할·저장
# ---------------------------------------------------------------------------
def main():
    print("[1/5] 코퍼스 로딩(로컬 캐시만, API 호출 없음)...")
    store, labels, candidates, year_range, ci = load_context()
    metric_to_concept = build_metric_to_concept(ci, candidates)
    print(f"  candidates: corp={len(candidates['corp'])} concept={len(candidates['concept'])} "
          f"event={len(candidates['event'])} year_range={year_range}")

    print("[2/5] 카테고리별 생성...")
    gens = [
        ("goldset_positive", gen_goldset_positive(candidates, year_range, metric_to_concept)),
        ("event_template", gen_event_template(candidates, year_range)),
        ("convreplay_multiturn", gen_convreplay(candidates, year_range)),
        ("adversarial_negative", gen_adversarial(candidates, year_range)),
        ("no_candidate_abstain", gen_no_candidate_abstain(candidates, year_range)),
        ("definition_corp_null", gen_definition_corp_null(candidates, year_range)),
    ]
    for name, recs in gens:
        print(f"  {name}: {len(recs)}건 생성")

    print("[3/5] 자체 검증 (output이 _validate_slot()을 그대로 통과하는가)...")
    kept, dropped = [], []
    for cat, recs in gens:
        n_drop = 0
        for r in recs:
            slots = json.loads(r["output"])
            ok, bad_key = validate_output(slots, candidates, year_range)
            if ok:
                kept.append(r)
            else:
                n_drop += 1
                dropped.append((cat, bad_key, r["user"][:80]))
        print(f"  {cat}: {len(recs) - n_drop}/{len(recs)} 통과 (탈락 {n_drop}건)")
    if dropped:
        print("  탈락 사례(최대 5건):")
        for cat, k, u in dropped[:5]:
            print(f"    [{cat}] slot={k} user={u!r}")

    total = len(kept)
    n_adv = sum(1 for r in kept if r["category"] == "adversarial_negative")
    print(f"[4/5] 최종 {total}건 (adversarial_negative {n_adv}건, "
          f"비율 {n_adv/total:.1%} — 요구 기준 1/3 이상: {'충족' if n_adv/total >= 1/3 else '미충족'})")

    # 8:2 분할 — 카테고리별로 인덱스 5개마다 1개를 eval로 뺀다(결정론, 카테고리별
    # 분포를 train/eval 양쪽에 비례 유지).
    by_cat = {}
    for r in kept:
        by_cat.setdefault(r["category"], []).append(r)
    train, ev = [], []
    for cat, recs in by_cat.items():
        for i, r in enumerate(recs):
            (ev if i % 5 == 0 else train).append(r)

    print(f"[5/5] 저장: train={len(train)}건 eval={len(ev)}건 → {OUT_DIR}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, rows in (("train.jsonl", train), ("eval.jsonl", ev)):
        with (OUT_DIR / name).open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("\n카테고리별 최종 건수:")
    from collections import Counter
    c_all = Counter(r["category"] for r in kept)
    c_train = Counter(r["category"] for r in train)
    c_eval = Counter(r["category"] for r in ev)
    for cat in c_all:
        print(f"  {cat}: 전체 {c_all[cat]} = train {c_train[cat]} + eval {c_eval[cat]}")


if __name__ == "__main__":
    main()
