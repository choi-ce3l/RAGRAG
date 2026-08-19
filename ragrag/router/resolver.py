"""resolver.py — version resolver (검색/조회 전 유효 문서 버전 확정).

입력: Frame(+ manifest, choi.supersede/corrlink가 만드는 supersede map). 출력: 그 이후
단계(execute.py)가 fact를 조회할 때 후보로 삼을 doc_id 집합 + supersede_method(신뢰도
표기). facts.jsonl 자체가 이미 각 fact에 is_superseded를 갖고 있지만(§6.2-(1) 참고),
그건 latest_effective 하나만 표현할 수 있다 — as_of/pair/original/corrected처럼 "그
시점의 최신본"이나 "정정 전/후 쌍"을 표현하려면 manifest+supersede 그래프가 별도로
필요해서 이 모듈이 존재한다.

주의(CHOI_상태파악.md §6.1): corrlink의 supersede_method 중 ambiguous/ref_absent는
저신뢰다(원본을 못 찾았거나 여러 후보 중 하나를 임의로 못 고른 상태). 이 모듈은 그
method를 항상 함께 반환하므로, 호출측이 신뢰도를 보고 정책을 정할 수 있다 — 이 모듈
자체는 저신뢰 링크를 폐기하지 않는다(그 판단은 execute.py/compose.py 몫).
"""
import collections

from ragrag.pipeline import load as choi_load            # noqa: E402
from ragrag.pipeline import supersede as choi_supersede  # noqa: E402

LOW_CONFIDENCE_METHODS = {"ambiguous", "ref_absent"}

_STATE = None  # (manifest, smap, periodic_groups, by_doc_id) — 1회 구축 후 캐시


def _load_state():
    """manifest + supersede map을 1회 구축해 캐시.

    corrlink가 major/exchange/holding 정정문서 raw XML을 직접 스캔하므로 비용이 있다
    (수 초). 프로세스당 한 번만 하도록 모듈 전역에 캐시한다 — diagnose.py가 797문항을
    반복 호출해도 재구축하지 않는다.
    """
    global _STATE
    if _STATE is not None:
        return _STATE
    manifest = choi_load.load_manifest()
    smap = choi_supersede.build_supersede_map(manifest)
    by_doc_id = {e["doc_id"]: e for e in manifest}
    periodic_groups = collections.defaultdict(list)
    for e in manifest:
        if e["doc_group"] == "periodic":
            key = (e["corp_code"], e["doc_subtype"], e.get("base_year"), e.get("base_month"))
            periodic_groups[key].append(e)
    _STATE = (manifest, smap, periodic_groups, by_doc_id)
    return _STATE


def _is_superseded(doc_id, smap):
    """smap에 없는 문서는 정정이 없었다는 뜻 — chunk.py와 동일하게 기본값 False."""
    return bool(smap.get(doc_id, {}).get("is_superseded", False))


def _candidate_periodic_docs(frame):
    """frame.corp_code(+period.year)에 맞는 periodic 문서들(모든 doc_subtype)."""
    manifest, smap, periodic_groups, by_doc_id = _load_state()
    if not frame.corp_code:
        return []
    year = frame.period.year
    docs = [e for e in manifest
            if e["doc_group"] == "periodic" and e["corp_code"] == frame.corp_code
            and (year is None or e.get("base_year") == year)]
    return docs


class ResolverResult:
    def __init__(self, doc_ids, method=None, low_confidence=False, detail=None,
                 original_doc_id=None, corrected_doc_id=None):
        self.doc_ids = set(doc_ids)
        self.method = method
        self.low_confidence = low_confidence
        self.detail = detail or {}
        self.original_doc_id = original_doc_id
        self.corrected_doc_id = corrected_doc_id

    def to_dict(self):
        return {"doc_ids": sorted(self.doc_ids), "method": self.method,
                "low_confidence": self.low_confidence, "detail": self.detail,
                "original_doc_id": self.original_doc_id,
                "corrected_doc_id": self.corrected_doc_id}


def periodic_docs_exist(corp_code, year=None):
    """existence 경로용 경량 확인: 이 기업의 periodic 문서가(연도 지정 시 그 연도) 코퍼스에
    존재하는가. chunks.jsonl(1GB+)을 열지 않고 manifest만으로 답한다 — "확인 범위"를
    명시하는 목적에는 manifest 레벨이면 충분하고 훨씬 싸다.
    """
    manifest, _, _, _ = _load_state()
    docs = [e for e in manifest if e["doc_group"] == "periodic" and e["corp_code"] == corp_code
            and (year is None or e.get("base_year") == year)]
    return docs


def resolve(frame):
    """Frame.version_selector에 따라 유효 doc_id 후보 풀을 계산."""
    manifest, smap, periodic_groups, by_doc_id = _load_state()
    vsel = frame.version_selector
    vsel = vsel.value if hasattr(vsel, "value") else vsel

    docs = _candidate_periodic_docs(frame)
    if not docs:
        return ResolverResult(doc_ids=set(), method=None, detail={"reason": "no_candidate_docs"})

    if vsel == "latest_effective":
        keep = [d for d in docs if not _is_superseded(d["doc_id"], smap)]
        return ResolverResult(doc_ids={d["doc_id"] for d in keep}, method="latest_effective")

    if vsel == "as_of":
        as_of = frame.period.as_of_date
        if not as_of:
            # 명시적 as_of_date가 없으면 latest_effective로 폴백(신호는 있었지만 날짜가 없음).
            keep = [d for d in docs if not _is_superseded(d["doc_id"], smap)]
            return ResolverResult(doc_ids={d["doc_id"] for d in keep}, method="latest_effective",
                                   detail={"reason": "as_of_requested_without_date"})
        eligible = [d for d in docs if d["rcept_dt"] <= as_of]
        if not eligible:
            return ResolverResult(doc_ids=set(), method="as_of",
                                   detail={"reason": "no_filing_before_as_of", "as_of": as_of})
        latest = max(eligible, key=lambda d: (d["rcept_dt"], d["rcept_no"]))
        return ResolverResult(doc_ids={latest["doc_id"]}, method="as_of",
                               detail={"as_of": as_of, "picked_rcept_dt": latest["rcept_dt"]})

    if vsel in ("pair", "original", "corrected"):
        # supersede_key(=period-key 또는 link:root)로 문서들을 클러스터링해 원공시/정정본을 찾는다.
        clusters = collections.defaultdict(list)
        loners = []
        for d in docs:
            info = smap.get(d["doc_id"])
            if info:
                clusters[info["supersede_key"]].append((d, info))
            else:
                loners.append(d)
        if not clusters:
            # 정정 이력이 없다 — pair/original/corrected를 요청해도 버전이 하나뿐.
            return ResolverResult(doc_ids={d["doc_id"] for d in loners}, method=None,
                                   detail={"reason": "no_correction_history"})
        # 가장 멤버가 많은 클러스터를 채택(같은 corp+연도에 클러스터가 여럿이면 모호 — 기록만).
        key = max(clusters, key=lambda k: len(clusters[k]))
        members = clusters[key]
        ambiguous_clusters = len(clusters) > 1
        member_docs = [d for d, _ in members]
        original = min(member_docs, key=lambda d: (d["rcept_dt"], d["rcept_no"]))
        corrected = max(member_docs, key=lambda d: (d["rcept_dt"], d["rcept_no"]))
        method = members[0][1]["match_method"]
        low_conf = method in LOW_CONFIDENCE_METHODS
        if vsel == "original":
            return ResolverResult(doc_ids={original["doc_id"]}, method=method,
                                   low_confidence=low_conf,
                                   detail={"ambiguous_clusters": ambiguous_clusters})
        if vsel == "corrected":
            return ResolverResult(doc_ids={corrected["doc_id"]}, method=method,
                                   low_confidence=low_conf,
                                   detail={"ambiguous_clusters": ambiguous_clusters})
        # pair
        return ResolverResult(
            doc_ids={original["doc_id"], corrected["doc_id"]}, method=method,
            low_confidence=low_conf,
            detail={"ambiguous_clusters": ambiguous_clusters, "n_members": len(member_docs)},
            original_doc_id=original["doc_id"], corrected_doc_id=corrected["doc_id"])

    # 알 수 없는 selector — 안전한 기본값으로 폴백.
    keep = [d for d in docs if not _is_superseded(d["doc_id"], smap)]
    return ResolverResult(doc_ids={d["doc_id"] for d in keep}, method="latest_effective",
                           detail={"reason": f"unknown_version_selector:{vsel}"})


if __name__ == "__main__":
    import json
    import time
    t0 = time.time()
    manifest, smap, _, _ = _load_state()
    sup = sum(1 for v in smap.values() if v["is_superseded"])
    print(f"manifest {len(manifest)}건, supersede map {len(smap)}건(superseded {sup}), "
          f"{time.time()-t0:.1f}s")
