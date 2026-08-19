"""정정공시 supersede 판정 — Phase 3.

RAG 색인에서 정정([기재정정])으로 대체된 옛 버전을 기본검색에서 빼기 위해,
같은 '논리적 공시'를 묶고 최신본만 canonical로 남긴다.

## 이번 증분 범위: periodic 전용 (신뢰 가능한 period-key)
periodic 정정은 같은 보고서를 재제출한 것이라 (corp_code, doc_subtype, base_year,
base_month)로 원본+정정이 정확히 묶인다(135그룹 전수, 정정 포함 확인). XML 파싱 불필요.

## major / exchange / holding: corrlink.py의 date/chain 링커로 통합
정정문서의 "최초제출일"(major/holding)·"공시서류제출일"(exchange)을 태그 관통 텍스트에서
추출해 원본/직전본에 연결하고 union-find로 체인을 묶는다. build_supersede_map이 두 방식을
합쳐 하나의 맵으로 반환한다.
"""
import collections

from . import corrlink


def _pkey(e):
    return (e["corp_code"], e["doc_subtype"], e.get("base_year"), e.get("base_month"))


def _periodic_map(manifest):
    """periodic period-key 기반 supersede 맵.

    정정을 포함하는 period-key 그룹의 모든 문서(원본+정정)를 매핑한다:
    - canonical = 접수일(rcept_dt) 최신, 동일 시 rcept_no 최대
    - canonical 은 is_superseded=False, 나머지는 True. 전부 canonical_doc_id를 가리킴.
    """
    groups = collections.defaultdict(list)
    for e in manifest:
        if e["doc_group"] == "periodic":
            groups[_pkey(e)].append(e)

    out = {}
    for key, docs in groups.items():
        if len(docs) < 2 or not any(d["is_correction"] for d in docs):
            continue
        canonical = max(docs, key=lambda d: (d["rcept_dt"], d["rcept_no"]))
        skey = "|".join(str(k) for k in key)
        for d in docs:
            out[d["doc_id"]] = {
                "is_superseded": d["doc_id"] != canonical["doc_id"],
                "canonical_doc_id": canonical["doc_id"],
                "supersede_key": skey,
                "match_method": "period_key",
            }
    return out


def build_supersede_map(manifest):
    """전체 supersede 맵 -> {doc_id: {is_superseded, canonical_doc_id, supersede_key, match_method}}.

    periodic(period-key) + major/exchange/holding(corrlink date/chain 링커)를 합친다.
    정정이 없는 단일 문서는 매핑에 넣지 않는다(chunk.py 기본값 False로 처리).
    """
    out = _periodic_map(manifest)
    out.update(corrlink.build_nonperiodic_links(manifest))
    return out


if __name__ == "__main__":
    from . import load
    m = load.load_manifest()
    smap = build_supersede_map(m)
    sup = sum(1 for v in smap.values() if v["is_superseded"])
    can = sum(1 for v in smap.values() if not v["is_superseded"])
    print(f"period-key 정정 그룹 문서 {len(smap)}건 | canonical {can} | superseded {sup}")
    # 예시 하나
    for did, v in list(smap.items())[:6]:
        print(f"  {did}  superseded={v['is_superseded']}  -> {v['canonical_doc_id']}")
