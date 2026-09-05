#!/usr/bin/env python3
"""단일턴 "축약·구어체 표면형 → 폐집합 후보 매핑" 튜닝 데이터 생성기.

## 스코프 (사용자 확정 — 이것만 다룬다)

발화 하나 안에서 corp 또는 concept의 축약·구어체 표면형이 폐집합 후보 중
무엇을 가리키는지만 학습한다. 멀티턴 승계(직전 슬롯)·다른 슬롯(event/wh/
scope/intent)은 다루지 않는다 — 그건 이미 규칙(직승계 게이트)이 처리하고
있고 실측(heldout)으로 "틀린 승계 0건"이 확인됐다.

## 레코드 형식 — 슬롯 단위, JSON 아닌 값 하나

    {"utterance": "삼전 작년에 얼마 벌었어?", "slot": "corp",
     "candidates": [...실제 70개 그대로...], "output": "삼성전자",
     "category": "...", "surface_form": "삼전", "source": "rule"|"hcx",
     "note": "..."}

`output`은 후보 리스트 안의 문자열 하나 또는 리터럴 null이다 — 지금
extract_slots()가 내는 7키 JSON과 다른, 더 좁은 태스크 전용 포맷이다.

## 절대 규칙
- `_COLLOQUIAL_ALIASES`(concepts.py)·`ALIASES`(ontology.py)에 이미 등록된
  표기는 학습 데이터에서 뺀다 — 사전이 이미 처리하는 몫을 중복 학습시키지
  않는다. 튜닝은 **미등록 표기의 일반화**만 담당한다.
- heldout.jsonl·conv_replay의 발화는 절대 넣지 않는다(진짜 held-out 유지).
- 규칙으로 부족한 변주만 HCX로 만든다(다른 외부 모델 금지) — 호출 수를
  최소화하도록 배치로 묻는다. 이 스크립트 실행 전 사용자에게 정확한 예상
  호출 수를 보고하고 진행한다.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qa import concepts, derived, labelstore, ontology, pipeline  # noqa: E402

def eun_neun(word):
    """마지막 글자에 받침이 있으면 '은', 없으면 '는'. 영문/숫자로 끝나면 '는'으로
    본다(실제 발화에서 흔히 그렇게 쓴다 — "KAI는")."""
    if not word:
        return "는"
    last = word[-1]
    if "가" <= last <= "힣":
        return "은" if (ord(last) - 0xAC00) % 28 != 0 else "는"
    return "는"


OUT_DIR = ROOT / "data" / "llmparse_tuning"
HELDOUT_PATH = OUT_DIR / "heldout.jsonl"


def load_context():
    store = pipeline.get_store()
    labels = labelstore.get()
    ci = concepts.get(labels.facts)
    corp_cands = sorted(store.corp_names)
    concept_cands = sorted(ci.llm_candidates, key=lambda c: (-ci.coverage(c), c))
    concept_cands += sorted(k for k in derived.RULES if k not in ci.llm_candidates)
    return store, labels, ci, corp_cands, concept_cands


def excluded_surface_forms():
    """이미 사전이 처리하는 표기 — 학습 데이터에서 뺀다."""
    corp_excl = set(ontology.ALIASES.keys())
    concept_excl = set()
    for aliases in concepts._COLLOQUIAL_ALIASES.values():
        for a in aliases:
            concept_excl.add(a)
    return corp_excl, concept_excl


def heldout_utterances():
    """heldout.jsonl(+conv_replay 6턴)의 발화 — 학습에 절대 안 넣을 집합."""
    out = set()
    if HELDOUT_PATH.exists():
        for line in HELDOUT_PATH.open(encoding="utf-8"):
            out.add(json.loads(line)["question"])
    conv_replay_turns = [
        "한화에어로스페이스가 유상증자를 결정한 이후 부채비율이 어떻게 변했어?",
        "유상증자를 언제 결정했는데?", "모든 경우의수 다 고려해서 알려줘",
        "연결/별도 기준이 다르므로 구분이 필요 무슨 말이야", "투자할만한 회사야?",
        "내가 한화 에어로 스페이스라고 하지않았나?",
    ]
    out.update(conv_replay_turns)
    return out


# ---------------------------------------------------------------------------
# (a) corp 규칙기반 축약 — 접미어(홀딩스/지주/금융지주) 제거, 다른 69개사와
#     겹치지 않을 때만 채택한다(그렇지 않으면 hard negative 후보로 돌린다).
# ---------------------------------------------------------------------------
SUFFIXES = ("금융지주", "지주", "홀딩스")


def rule_corp_abbrevs(corp_cands):
    cand_set = set(corp_cands)
    out = {}  # surface_form -> real name
    for name in corp_cands:
        for suf in SUFFIXES:
            if name.endswith(suf) and len(name) > len(suf):
                stripped = name[: -len(suf)]
                if len(stripped) < 2:
                    continue
                # 다른 69개사 중 이 축약형으로 시작하는 게 또 있으면(=진짜
                # 애매) 채택하지 않는다 — 그런 경우는 hard negative 몫이다.
                collides = any(o != name and o.startswith(stripped) for o in cand_set)
                if not collides and stripped not in out:
                    out[stripped] = name
    return out


# ---------------------------------------------------------------------------
# (b) concept 규칙기반 축약 — 흔한 한국어 줄임 몇 개만(사전 지식, corpus 무관).
#     후보 목록에 실제로 있는 정규형으로만 매핑한다.
# ---------------------------------------------------------------------------
_CONCEPT_RULE_ABBREVS = {
    "매출": "매출액", "영업익": "영업이익", "영업이윤": "영업이익",
    "당기순익": "당기순이익", "순익": "당기순이익", "순이익": "당기순이익",
    "부채율": "부채비율", "유동비": "유동비율", "자기자본비": "자기자본비율",
    "영업이익율": "영업이익률", "매출총익률": "매출총이익률",
}


def rule_concept_abbrevs(concept_cands):
    cand_set = set(concept_cands)
    return {k: v for k, v in _CONCEPT_RULE_ABBREVS.items() if v in cand_set}


# ---------------------------------------------------------------------------
# (b-2) corp 구어체 축약 — 규칙(접미어 제거)으로 못 잡는 51개사에 대해 HCX로
# 변주를 생성한 뒤(2회 배치 호출, narrative.call 사용) **전부 손으로 검증**한
# 결과다. HCX 원본 제안 중 실제로 위험하거나 틀린 것들을 실측으로 걸러냈다:
#   - "기아"→"기차": 명백한 오류(기차=train, 기아자동차와 무관)
#   - "카카오"→"카뱅": 카뱅은 카카오뱅크(이 corpus에 없는 별개 회사)를 가리킴
#   - "삼성전기"→"삼전": 이미 ontology.ALIASES에 "삼전"→"삼성전자"로 등록돼
#     있어 정면 충돌(실제로도 "삼전"은 삼성전자를 가리키는 게 맞다)
#   - "고려아연"→"아연"·"파마리서치"→"파마"·"하이브"→"하이"·"이마트"→"이마":
#     전부 일상 단어와 겹치는 흔한 말이라 오탐 위험이 너무 큼
#   - "두산로보틱스"→"로보": 같은 corpus의 "레인보우로보틱스"와 부분문자열
#     충돌(실측: 자동 충돌검사로 확인)
#   - "한미약품"→"한미": collision_groups()가 이미 잡은 "한미" 충돌 그룹
#     (한미반도체/한미약품) 그 자체라 hard negative 몫이지 긍정례가 아니다
#   - 그 외 확신 없는 것들("SK텔레콤→슼통", "크래프톤→크래프" 등)은 근거
#     불충분으로 전부 뺐다
# 살아남은 23건은 전부 자동 충돌검사(70개사 전원과 부분문자열 겹침 없음)까지
# 통과했다("대건"→대우건설은 "현대건설"과 겹쳐 이 단계에서 걸러졌다).
# ---------------------------------------------------------------------------
_CORP_HCX_VETTED = {
    "흠슬라": "HMM", "생건": "LG생활건강", "삼바": "삼성바이오로직스", "셀트": "셀트리온",
    "아모레": "아모레퍼시픽", "KAI": "한국항공우주", "글로비스": "현대글로비스",
    "로템": "현대로템", "모비스": "현대모비스", "오토에버": "현대오토에버", "SM": "에스엠",
    "한화솔": "한화솔루션", "한화오": "한화오션", "현건": "현대건설", "효중": "효성중공업",
    "한미반": "한미반도체", "삼엔": "삼성E&A", "삼스디": "삼성SDI", "에코비엠": "에코프로비엠",
    "삼화": "삼성화재해상보험", "LS일렉트릭": "엘에스일렉트릭", "YG엔터": "와이지엔터테인먼트",
    "제이와피": "JYP Ent",
}


def hcx_vetted_corp_abbrevs(corp_cands):
    cand_set = set(corp_cands)
    out = {}
    for abbr, real in _CORP_HCX_VETTED.items():
        if real not in cand_set:
            continue
        collides = any(o != real and (abbr in o or o.startswith(abbr)) for o in cand_set)
        if not collides:
            out[abbr] = real
    return out


# ---------------------------------------------------------------------------
# (c) hard negative — 접두어 공유로 애매한 그룹. 접두어만으론 못 좁힌다 →
#     정답은 null. 전부 규칙 기반(HCX 불필요) — corpus 실제 그룹만 쓴다.
# ---------------------------------------------------------------------------
def collision_groups(corp_cands):
    from collections import defaultdict
    groups = defaultdict(list)
    for n in corp_cands:
        groups[n[:2]].append(n)
    return {p: m for p, m in groups.items() if len(m) >= 2}


_AMBIG_TEMPLATES = [
    "{p} 영업이익 얼마야?", "{p} 매출 알려줘", "{p} 부채비율 어때?",
    "{p} 실적 어때?", "{p}{jn} 자산총계가 얼마야?",
    "{p} 당기순이익 얼마야?", "{p}{jn} 유동비율이 어때?",
]
_EXPLICIT_TEMPLATES = [
    "{full} 영업이익 얼마야?", "{full} 매출 알려줘", "{full}{jn} 부채비율이 어때?",
]


def gen_hard_negatives(corp_cands):
    groups = collision_groups(corp_cands)
    records = []
    for prefix, members in sorted(groups.items()):
        for i, tmpl in enumerate(_AMBIG_TEMPLATES):
            records.append({
                "utterance": tmpl.format(p=prefix, jn=eun_neun(prefix)), "slot": "corp",
                "candidates": corp_cands, "output": None,
                "category": "hard_negative_ambiguous_prefix",
                "surface_form": prefix, "source": "rule",
                "note": f"접두어 '{prefix}' 공유 {len(members)}개사({', '.join(members)}) — "
                        "접두어만으론 못 좁힌다, null이 정답",
            })
        # 대칭 긍정례 — 같은 접두어 그룹이라도 전체 이름을 명시하면 정확히 그 회사.
        # 템플릿 하나만 고르지 않고 전부 적용한다(회사당 3배, 새 판단 없이
        # 순수 문구 변주라 안전하다).
        for full in members:
            for tmpl in _EXPLICIT_TEMPLATES:
                records.append({
                    "utterance": tmpl.format(full=full, jn=eun_neun(full)), "slot": "corp",
                    "candidates": corp_cands, "output": full,
                    "category": "hard_negative_explicit_full_name",
                    "surface_form": full, "source": "rule",
                    "note": f"'{prefix}' 충돌 그룹이지만 전체 이름을 명시했으므로 정확히 이 회사",
                })
    return records


# ---------------------------------------------------------------------------
# (d) 기권(abstain) — corpus에 없는 회사. 실제로 존재하는(다른 코퍼스에선
#     흔한) 유명 회사인데 이 70개사엔 없는 것들을 우선 쓴다(허구 이름보다
#     현실적) — heldout 조사에서 이미 확인된 목록.
# ---------------------------------------------------------------------------
_ABSENT_REAL_CORPS = [
    "삼성물산", "SK이노베이션", "SK브로드밴드", "LG전자", "한화시스템",
    "LIG넥스원", "GS건설", "DL이앤씨", "CJ대한통운", "한국전력공사",
    "카카오뱅크", "네이버웹툰", "쿠팡", "배달의민족", "토스",
    # 400건 확보를 위해 추가 — 전부 실존하는 실제 회사이되 이 70개사 corpus엔
    # 없음(가상 이름보다 현실적인 기권 신호를 준다).
    "삼성카드", "신세계", "롯데케미칼", "포스코퓨처엠", "카카오페이",
    "당근마켓", "야놀자", "무신사", "GS리테일", "삼성증권",
    "롯데쇼핑", "농심", "오뚜기", "빙그레",
]
_ABSTAIN_TEMPLATES = [
    "{c} 영업이익 얼마야?", "{c} 매출 어때?", "{c}{jn} 부채비율이 얼마야?",
    "{c} 실적 알려줘",
]


def gen_abstain(corp_cands):
    cand_set = set(corp_cands)
    records = []
    for c in _ABSENT_REAL_CORPS:
        if c in cand_set:
            continue
        # 템플릿 하나만 고르지 않고 전부 적용한다(회사당 4배).
        for tmpl in _ABSTAIN_TEMPLATES:
            records.append({
                "utterance": tmpl.format(c=c, jn=eun_neun(c)), "slot": "corp",
                "candidates": corp_cands, "output": None,
                "category": "abstain_out_of_corpus", "surface_form": c, "source": "rule",
                "note": f"'{c}'는 실존하는 회사이나 이 corpus(70개사)엔 없음 — null이 정답",
            })
    return records


def main():
    store, labels, ci, corp_cands, concept_cands = load_context()
    corp_excl, concept_excl = excluded_surface_forms()
    heldout_q = heldout_utterances()

    print(f"[컨텍스트] corp 후보 {len(corp_cands)}개, concept 후보 {len(concept_cands)}개")
    print(f"[제외] 이미 사전 등록된 corp 별칭 {len(corp_excl)}개, concept 별칭 {len(concept_excl)}개")

    corp_abbrevs = rule_corp_abbrevs(corp_cands)
    corp_abbrevs.update(hcx_vetted_corp_abbrevs(corp_cands))   # 손으로 검증된 23건 추가
    corp_abbrevs = {k: v for k, v in corp_abbrevs.items() if k not in corp_excl}
    concept_abbrevs = rule_concept_abbrevs(concept_cands)
    concept_abbrevs = {k: v for k, v in concept_abbrevs.items() if k not in concept_excl}

    print(f"\n[corp 축약(규칙+HCX검증)] {len(corp_abbrevs)}건: {corp_abbrevs}")
    print(f"[규칙기반 concept 축약] {len(concept_abbrevs)}건: {concept_abbrevs} "
          f"(HCX concept 배치는 품질 미달로 전부 폐기 — 예: '유형자산'→'유틸리티' 같은 명백한 오류)")

    hard_neg = gen_hard_negatives(corp_cands)
    abstain = gen_abstain(corp_cands)
    print(f"\n[hard negative] {len(hard_neg)}건 "
          f"(ambiguous={sum(1 for r in hard_neg if r['category']=='hard_negative_ambiguous_prefix')}, "
          f"explicit={sum(1 for r in hard_neg if r['category']=='hard_negative_explicit_full_name')})")
    print(f"[abstain] {len(abstain)}건")

    # 축약을 실제 학습 레코드로 변환(긍정례) — 발화 템플릿에 얹는다. 템플릿
    # 하나만 순환 배정하지 않고 전부 적용한다(쌍당 4배/2배 — 이미 검증된
    # (표면형→정답) 매핑을 다른 말투로 반복하는 것뿐이라 새 판단이 필요
    #없다. 400건 확보를 위한 순수 기계적 확장).
    pos_templates = ["{s} 영업이익 얼마야?", "{s} 매출 알려줘", "{s} 실적 어때?", "{s}{jn} 부채비율이 얼마야?"]
    positives = []
    for s, real in corp_abbrevs.items():
        from_hcx = s in _CORP_HCX_VETTED
        for tmpl in pos_templates:
            positives.append({
                "utterance": tmpl.format(s=s, jn=eun_neun(s)), "slot": "corp", "candidates": corp_cands,
                "output": real, "category": "abbrev_positive_hcx_vetted" if from_hcx else "abbrev_positive_rule",
                "surface_form": s, "source": "hcx_vetted" if from_hcx else "rule",
                "note": (f"HCX 제안을 사람이 검증(실제 통용 표현+70개사 충돌 없음 확인)"
                         if from_hcx else f"접미어 제거({real}→{s}), 유일하게 매칭"),
            })
    concept_templates = ["{s} 얼마야?", "{s} 알려줘", "{s} 어때?", "{s} 얼마인지 궁금해"]
    for s, real in concept_abbrevs.items():
        for tmpl in concept_templates:
            positives.append({
                "utterance": tmpl.format(s=s), "slot": "concept", "candidates": concept_cands,
                "output": real, "category": "abbrev_positive_rule", "surface_form": s,
                "source": "rule", "note": f"흔한 한국어 줄임({real}→{s})",
            })

    all_records = positives + hard_neg + abstain

    # heldout/conv_replay 발화와 겹치는 건 제외(안전장치 — 애초에 안 겹치게
    # 만들었지만 우연 일치까지 확실히 막는다).
    before = len(all_records)
    all_records = [r for r in all_records if r["utterance"] not in heldout_q]
    if before != len(all_records):
        print(f"\n[안전장치] heldout/conv_replay와 겹쳐서 제외: {before - len(all_records)}건")

    # 중복 발화 제거(같은 문장이 서로 다른 카테고리에서 우연히 또 만들어졌을 경우).
    seen, dedup = set(), []
    for r in all_records:
        key = (r["utterance"], r["slot"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(r)
    if len(dedup) != len(all_records):
        print(f"[중복 제거] {len(all_records) - len(dedup)}건")
    all_records = dedup

    n_abstain = sum(1 for r in all_records if r["output"] is None)
    abstain_ratio = n_abstain / len(all_records)
    print(f"\n[최종 합계] {len(all_records)}건, abstain 비율 {abstain_ratio*100:.1f}% "
          f"({'기준(30%) 충족' if abstain_ratio >= 0.3 else '기준(30%) 미달!'})")

    from collections import Counter
    cat_counts = Counter(r["category"] for r in all_records)
    print("\n[카테고리별 건수]")
    for cat, n in cat_counts.most_common():
        print(f"  {cat}: {n}")

    # 8:2 분할 — 카테고리별로 비례 유지(기존 gen_llmparse_tuning_data.py와 같은 방식).
    by_cat = {}
    for r in all_records:
        by_cat.setdefault(r["category"], []).append(r)
    train, ev = [], []
    for cat, recs in by_cat.items():
        for i, r in enumerate(recs):
            # 9:1 분할 — heldout.jsonl이 이미 진짜 held-out 역할을 하므로,
            # 여기 eval은 학습 데이터 자체 품질을 훑어보는 용도로만 작게 둔다
            # (Clova Studio 업로드 최소건수 확보를 위해 train 비중을 높인다).
            (ev if i % 10 == 0 else train).append(r)

    out_dir = ROOT / "data" / "llmparse_tuning"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("slot_mapping_train.jsonl", train), ("slot_mapping_eval.jsonl", ev)):
        with (out_dir / name).open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n저장: train={len(train)}건, eval={len(ev)}건 → {out_dir}")

    # 표본 출력 — 실제로 뭐가 만들어졌는지 그대로 보여준다.
    print("\n[표본 — 카테고리별 1~2건]")
    shown = set()
    for r in all_records:
        if r["category"] in shown:
            continue
        shown.add(r["category"])
        print(f"  [{r['category']}] {r['utterance']!r} (slot={r['slot']}) -> {r['output']!r}  # {r['note']}")


if __name__ == "__main__":
    main()
