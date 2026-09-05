"""비-periodic 정정 링커 — Phase 3.

major/exchange/holding 정정공시를 원본/체인에 연결해 supersede 그룹을 만든다.
periodic은 period-key로 이미 처리(supersede.py) — 여기선 다루지 않는다.

## 연결 신호 (실측 확인)
- major/holding: 정정문서 텍스트의 **"최초제출일"** = 원본 접수일 직접 지시(추출률 99~100%).
- exchange: **"정정관련 공시서류제출일"** = 직전 제출본 접수일(추출률 100%). 재정정 체인 존재.

## 방법
1. 각 정정문서에서 참조 제출일(YYYYMMDD)을 태그 관통 텍스트에서 추출.
2. 같은 (corp_code, doc_group)에서 rcept_dt==참조일 & 시점 이전인 후보를 찾음.
   후보 다수면 정규화 report_nm으로 좁힘 → 유일하면 edge, 아니면 ambiguous.
3. 참조일이 같은(부재 원본 공유) 정정끼리도 refkey로 union → 원본이 수집창 밖이어도 체인 묶음.
4. union-find로 클러스터 → 정정 포함 클러스터의 최신본을 canonical, 나머지 superseded.

## 한계
- exchange 후보 다수/부재는 supersede 대상(is_superseded)에서 제외(안전). jin FINDINGS
  Task 12와 정합 — 이 기본값은 그대로 유지한다.
- 다만 그 "제외" 판단 자체(method="ambiguous"/"ref_absent"/"no_ref_date")는 이제 `out`에
  같이 남긴다. 예전엔 union-find 클러스터가 2명 미만이면(=매칭 실패) 통째로 버려져서,
  "정정 여부를 확신 못 함"과 "애초에 정정이 없음"이 구분 안 됐다(공시_agent D-TRACE
  계획서의 "정정 가능성" 표시가 이래서 못 구현되고 있었다 — 실측 확인). is_superseded는
  여전히 False로 안전하게 두고, match_method만 진단용으로 노출한다.
"""
import collections
import re
import unicodedata

from lxml import etree

import load

_D = r"(\d{4})[.\-년/\s]+(\d{1,2})[.\-월/\s]+(\d{1,2})"
_PAT_ORIG = re.compile(r"최초\s*제출일[^0-9]{0,10}" + _D)      # major/holding
_PAT_PRIOR = re.compile(r"공시서류\s*제출일[^0-9]{0,10}" + _D)  # exchange
_PREF = re.compile(r"^\[[^\]]*\]")  # [기재정정]/[첨부정정] 등 접두어


def _norm_report(nm):
    return _PREF.sub("", nm or "").strip()


def _doc_text(entry):
    """정정문서 전체 텍스트(태그 관통, NFC). 값이 태그 사이에 흩어진 문제를 회피."""
    raw = load.read_text(load.main_xml_path(entry))
    if entry["doc_group"] == "exchange":
        root = etree.fromstring(raw.encode("utf-8"), etree.HTMLParser(encoding="utf-8"))
    else:
        root = etree.fromstring(b"<ROOT>" + raw.encode("utf-8") + b"</ROOT>",
                                etree.XMLParser(recover=True, huge_tree=True))
    if root is None:
        return ""
    return unicodedata.normalize("NFC", " ".join(" ".join(root.itertext()).split()))


def _ref_date(entry):
    """정정문서가 가리키는 원본/직전 제출일 -> 'YYYYMMDD' 또는 None."""
    try:
        t = _doc_text(entry)
    except Exception:
        return None
    mm = _PAT_ORIG.search(t) or _PAT_PRIOR.search(t)
    if not mm:
        return None
    return f"{mm.group(1)}{int(mm.group(2)):02d}{int(mm.group(3)):02d}"


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def build_nonperiodic_links(manifest):
    """{doc_id: {is_superseded, canonical_doc_id, supersede_key, match_method}} (major/exchange/holding)."""
    groups = ("major", "exchange", "holding")
    docs = [e for e in manifest if e["doc_group"] in groups]
    idx = collections.defaultdict(list)  # (corp, group, rcept_dt) -> docs
    for e in docs:
        idx[(e["corp_code"], e["doc_group"], e["rcept_dt"])].append(e)

    uf = _UF()
    method = {}
    refkey = collections.defaultdict(list)  # (corp, group, ref_date, norm_report) -> corrections

    for e in docs:
        if not e["is_correction"]:
            continue
        d = _ref_date(e)
        if not d:
            method[e["doc_id"]] = "no_ref_date"
            continue
        refkey[(e["corp_code"], e["doc_group"], d, _norm_report(e["report_nm"]))].append(e)
        cand = [x for x in idx.get((e["corp_code"], e["doc_group"], d), [])
                if x["doc_id"] != e["doc_id"] and x["rcept_dt"] <= e["rcept_dt"]]
        if len(cand) > 1:  # 같은 날 여러 건 -> report_nm으로 좁힘
            narrowed = [x for x in cand
                        if _norm_report(x["report_nm"]) == _norm_report(e["report_nm"])]
            cand = narrowed or cand
        if len(cand) == 1:
            uf.union(e["doc_id"], cand[0]["doc_id"])
            method[e["doc_id"]] = "date_unique"
        elif len(cand) > 1:
            method[e["doc_id"]] = "ambiguous"
        else:
            method[e["doc_id"]] = "ref_absent"

    # 참조일 공유 정정끼리 묶기(원본이 수집창 밖이어도 체인 유지)
    for members in refkey.values():
        for e in members[1:]:
            uf.union(members[0]["doc_id"], e["doc_id"])

    clusters = collections.defaultdict(list)
    for e in docs:
        if e["doc_id"] in uf.p:
            clusters[uf.find(e["doc_id"])].append(e)

    out = {}
    for root_id, members in clusters.items():
        if len(members) < 2 or not any(x["is_correction"] for x in members):
            continue
        canonical = max(members, key=lambda d: (d["rcept_dt"], d["rcept_no"]))
        for d in members:
            out[d["doc_id"]] = {
                "is_superseded": d["doc_id"] != canonical["doc_id"],
                "canonical_doc_id": canonical["doc_id"],
                "supersede_key": f"link:{root_id}",
                "match_method": method.get(d["doc_id"], "linked_original"),
            }

    # 매칭에 실패한 정정문서(ambiguous/ref_absent/no_ref_date)도 진단 정보를 남긴다.
    # is_superseded=False(안전한 기존 기본값)는 그대로 두되, "정정공시인데 원본
    # 연결을 확정 못 했다"는 사실 자체는 이제 살아남는다 — 예전엔 위 클러스터
    # 필터(len(members)<2)에 걸려 통째로 사라졌다.
    for doc_id, m in method.items():
        if doc_id in out:
            continue
        if m in ("ambiguous", "ref_absent", "no_ref_date"):
            out[doc_id] = {
                "is_superseded": False,
                "canonical_doc_id": None,
                "supersede_key": None,
                "match_method": m,
            }
    return out


if __name__ == "__main__":
    import collections as _c
    m = load.load_manifest()
    links = build_nonperiodic_links(m)
    sup = sum(1 for v in links.values() if v["is_superseded"])
    can = sum(1 for v in links.values() if not v["is_superseded"])
    byg = _c.Counter(did.split("_")[0] for did in links)
    bym = _c.Counter(v["match_method"] for v in links.values())
    print(f"비-periodic 링크 문서 {len(links)} | canonical {can} | superseded {sup}")
    print("그룹별 링크 문서:", dict(byg))
    print("match_method 분포:", dict(bym))
