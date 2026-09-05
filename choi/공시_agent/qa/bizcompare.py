""""두 회사 중 어느 쪽이 이 제품을 직접 생산하는가"류 사업설명 대조.

## 왜 필요한가

"OCI홀딩스와 한화솔루션 중, 태양광용 폴리실리콘을 직접 생산하는 업스트림 소재
사업을 영위하는 곳은 어디인가?"(GOLD-W1-OCI-02) — 정답이 재무 수치가 아니라
사업보고서 "II. 사업의 내용" 산문에 적힌 사실이라 XBRL fact store로는 못 푼다.
반면 완전한 서술형 질문도 아니다 — 정답이 후보 둘 중 하나로 확정되고, "그
회사의 사업내용 산문에 그 제품을 직접 생산한다는 문장이 있는가"라는 기계적
존재확인 문제다.

## 판정 방법 — LLM 없이 청크 빈도로 가른다

`qa/chunkstore.py`(narrative 청크 BM25 저장소)에서 "II. 사업의 내용" 절만 골라,
질문이 지목한 제품명이 "직접 생산/제조" 동사와 한 청크 안에 함께 나오는 횟수를
회사별로 센다. 한쪽만 여러 번 나오고 다른 쪽은 0건(또는 그 산업 전반을 설명하는
문맥 1건 미만)이면 그 한쪽을 답으로 낸다. 애매하면(둘 다 여러 번, 또는 둘 다
0건) 손대지 않는다 — LLM 해석 없이 신뢰할 수 있는 만큼만 답한다.
"""

import re

from . import chunkstore

_SECTION_PREFIX = "II. 사업의 내용"
_PRODUCE_VERB = re.compile(r"직접\s*(?:생산|제조)")
_PRODUCT = re.compile(r"([가-힣A-Za-z0-9]+)\s*(?:을|를)\s*직접\s*(?:생산|제조)")


def wanted(question, corps):
    """이 경로를 시도할 질문인가 — 후보가 정확히 둘이고 "직접 생산/제조" 표현이 있다."""
    return len(corps or []) == 2 and bool(_PRODUCE_VERB.search(question or ""))


def _extract_product(question):
    m = _PRODUCT.search(question)
    return m.group(1) if m else None


def _hit_count(product, corp_name):
    cs = chunkstore.get()
    idxs = cs.by_corp.get(corp_name, [])
    cnt = 0
    for i in idxs:
        rec = cs.recs[i]
        if rec.get("is_superseded"):
            continue
        if not (rec.get("section_path") or "").startswith(_SECTION_PREFIX):
            continue
        text = rec.get("text") or ""
        if product in text and re.search(r"생산|제조", text):
            cnt += 1
    return cnt


def producer_of(question, corps):
    """(회사명, 근거 rcept_no 목록, 히트 수 dict) 또는 (None, 사유, {})."""
    product = _extract_product(question)
    if not product:
        return None, "질문에서 제품명을 뽑지 못했습니다.", {}
    if chunkstore.get() is None:
        return None, "서술형 텍스트 저장소가 없습니다.", {}

    counts = {corp: _hit_count(product, corp) for corp in corps}
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    (top_corp, top_n), (other_corp, other_n) = ranked[0], ranked[1]
    # 확신 기준 — 한쪽은 여러 번 등장하고 다른 쪽은 사실상 없어야(0~1건) 한다.
    # 둘 다 나오거나(업종 전반 설명이 양쪽에 다 걸릴 수 있다) 둘 다 없으면 손대지 않는다.
    if top_n >= 3 and other_n <= 1:
        cs = chunkstore.get()
        rns = sorted({cs.recs[i]["rcept_no"] for i in cs.by_corp.get(top_corp, [])
                     if product in (cs.recs[i].get("text") or "")
                     and not cs.recs[i].get("is_superseded")
                     and (cs.recs[i].get("section_path") or "").startswith(_SECTION_PREFIX)},
                    reverse=True)[:1]
        return top_corp, rns, counts
    return None, f"'{product}' 언급 빈도로 한쪽을 특정하지 못함: {counts}", {}
