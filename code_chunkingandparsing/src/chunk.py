"""청크 후처리 — Phase 3.

parse.py가 낸 섹션 청크를 최종 검색/임베딩 단위 레코드로 **보강**한다.
설계 기준: choi/md/04_청킹_구현스펙.md.

이번 증분에서 구현하는 범위 (모델-독립적이라 지금 확정 가능한 것):
- ③ 문맥 프리픽스(context_prefix)  — 결정론적 v1
- ④ parent-child ID (chunk_id / parent_id / sub_index)
- ⑤ 메타 필드 (section_key, section_path, industry, sector, is_superseded)

이번 증분에서 **보류**하는 것:
- ② 크기 정규화(토큰 밴드 분할/병합) — 임베딩 토크나이저 확정 후(04 열린항목 #1).
  지금은 섹션 1개 = child 1개(sub_index=0) 그대로 둔다.
- 정정공시 supersede 판정 — 별도 파이프라인 단계. 여기선 is_superseded=False 고정.
"""
import collections
import re
import unicodedata

import load
import normalize
import parse

# 최상위 섹션 heading 판별(로마숫자 접두: "I.", "II." …). breadcrumb 상위 항목 추적용.
_ROMAN = re.compile(r"^[IVXLCDM]+\.")


def _block_type(text):
    """청크 블록 유형(evidence pack용): table > heading > narrative."""
    if "[표 " in text or "\n|" in text:
        return "table"
    if text.lstrip().startswith("##"):
        return "heading"
    return "narrative"


def _context_prefix(corp_name, report_nm, base_year, base_month, section_path):
    """③ 임베딩 입력용 프리픽스. 04 스펙: {corp} | {report} | {연.월} | {section_path}."""
    parts = [corp_name]
    if report_nm:
        parts.append(report_nm)
    if base_year:
        parts.append(f"{base_year}.{int(base_month):02d}" if base_month else str(base_year))
    if section_path:
        parts.append(section_path)
    return " | ".join(parts)


def _section_key(chunk):
    """⑤ 기업·연도 불변 섹션 식별자.

    우선순위: AASSOCNOTE(기업·연도 안정) > ATOCID(문서 내 안정) > section_index(최후 폴백).
    셋 다 없어 'SEC_None'이 되던 문제를 폴백으로 방지한다.
    """
    if chunk.get("section_assocnote"):
        return chunk["section_assocnote"]
    if chunk.get("section_atocid"):
        return f"SEC_{chunk['section_atocid']}"
    return f"SEC_IDX_{chunk['section_index']}"


def _base_meta(entry, universe):
    """entry + universe 조인으로 청크에 얹을 공통 메타(base_month/industry/sector)."""
    umeta = universe.get(unicodedata.normalize("NFC", entry["corp_name"]), {})
    return {
        "base_month": entry.get("base_month"),
        "industry": umeta.get("industry"),
        "sector": umeta.get("sector"),
    }


def _supersede_fields(entry, supersede):
    """supersede 맵에서 이 문서의 is_superseded/canonical_doc_id를 뽑는다(없으면 기본값)."""
    info = (supersede or {}).get(entry["doc_id"])
    if info:
        return {"is_superseded": info["is_superseded"],
                "canonical_doc_id": info["canonical_doc_id"],
                "supersede_method": info.get("match_method")}
    return {"is_superseded": False, "canonical_doc_id": entry["doc_id"],
            "supersede_method": None}


def enrich_document(entry, universe, supersede=None):
    """periodic/major/holding 문서 1건 -> (list[보강 chunk], list[table])."""
    chunks, tables = parse.parse_document(entry)
    meta = _base_meta(entry, universe)
    sup = _supersede_fields(entry, supersede)
    # 섹션(section_index) -> 그 섹션 표들의 table_id (evidence pack에서 청크↔표 연결)
    tbl_by_sec = collections.defaultdict(list)
    for t in tables:
        tbl_by_sec[t.get("section_index")].append(t["table_id"])

    out = []
    breadcrumb = None  # 직전 최상위(로마숫자) 섹션 heading
    for c in chunks:
        heading = c["section_heading"]
        if heading and _ROMAN.match(heading):
            breadcrumb = heading
        # section_path = 상위 > 현재 (상위가 자기 자신이거나 없으면 heading만)
        if breadcrumb and heading and breadcrumb != heading:
            section_path = f"{breadcrumb} > {heading}"
        else:
            section_path = heading or ""

        si = c["section_index"]
        skey = _section_key(c)
        # ② 크기 정규화: 섹션이 상한 초과면 자연 블록 경계로 하위 청크 분할.
        # parent(=섹션 전체)는 별도 저장 없이 같은 parent_id 하위 청크를 sub_index 순으로 이으면 복원.
        pieces = normalize.split_text(c["text"])
        prefix = _context_prefix(c["corp_name"], c.get("report_nm"),
                                 c.get("base_year"), meta.get("base_month"), section_path)
        pid = f"{c['doc_id']}#{si}"
        for j, piece in enumerate(pieces):
            out.append({
                **c,
                **meta,
                "chunk_id": f"{pid}.{j}",
                "parent_id": pid,
                "sub_index": j,
                "n_sub": len(pieces),
                "prev_chunk_id": f"{pid}.{j - 1}" if j > 0 else None,
                "next_chunk_id": f"{pid}.{j + 1}" if j < len(pieces) - 1 else None,
                "block_type": _block_type(piece),
                "table_ids": tbl_by_sec.get(si, []),
                "section_key": skey,
                "section_path": section_path,
                "n_chars": len(piece),
                "text": piece,
                "context_prefix": prefix,
                **sup,
            })
    return out, tables


def enrich_exchange(entry, universe, supersede=None):
    """exchange 문서 1건 -> (list[보강 chunk](1개), list[table]). 목차 없어 문서=청크1개."""
    chunk, tables = parse.parse_exchange(entry)
    meta = _base_meta(entry, universe)
    sup = _supersede_fields(entry, supersede)
    prefix = _context_prefix(chunk["corp_name"], chunk.get("report_nm"),
                             chunk.get("base_year"), meta.get("base_month"), "")
    table_ids = [t["table_id"] for t in tables]
    pid = f"{chunk['doc_id']}#0"
    pieces = normalize.split_text(chunk["text"])
    recs = []
    for j, piece in enumerate(pieces):
        recs.append({
            **chunk,
            **meta,
            "chunk_id": f"{pid}.{j}",
            "parent_id": pid,
            "section_index": 0,
            "sub_index": j,
            "n_sub": len(pieces),
            "prev_chunk_id": f"{pid}.{j - 1}" if j > 0 else None,
            "next_chunk_id": f"{pid}.{j + 1}" if j < len(pieces) - 1 else None,
            "block_type": _block_type(piece),
            "table_ids": table_ids,
            "section_key": "SEC_0",
            "section_path": "",
            "n_chars": len(piece),
            "text": piece,
            "context_prefix": prefix,
            **sup,
        })
    return recs, tables


def enrich(entry, universe, supersede=None):
    """doc_group에 따라 알맞은 보강 함수로 라우팅."""
    if entry["doc_group"] == "exchange":
        return enrich_exchange(entry, universe, supersede)
    return enrich_document(entry, universe, supersede)


if __name__ == "__main__":
    manifest = load.load_manifest()
    universe = load.load_universe()
    # 그룹별 1건씩 스모크 테스트.
    seen = set()
    for e in manifest:
        g = e["doc_group"]
        if g in seen:
            continue
        seen.add(g)
        recs, tables = enrich(e, universe)
        print("=" * 78)
        print(f"[{g}] {recs[0]['corp_name']} | {e['report_nm']} | "
              f"청크 {len(recs)}개 | 표 {len(tables)}개")
        r = recs[0]
        print(f"  chunk_id     : {r['chunk_id']}   parent_id: {r['parent_id']}")
        print(f"  section_key  : {r['section_key']}   path: {r['section_path'][:50]}")
        print(f"  industry/sec : {r.get('industry')} / {r.get('sector')}")
        print(f"  prefix       : {r['context_prefix'][:90]}")
        if len(seen) == 4:
            break
