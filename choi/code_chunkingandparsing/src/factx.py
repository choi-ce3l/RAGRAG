"""factx.py — 비-XBRL 표에서 exact 숫자/텍스트 추출 (Phase 1).

설계: choi/md 계획 + jin/PARSER_DESIGN.md §4–§6 · jin/NORMALIZED_SCHEMAS.md §4–§7,§10.
facts.py(XBRL 핵심재무제표)를 보완해 major/holding/exchange의 라벨-값 표와 periodic 요약/부문
matrix 표에서 숫자·텍스트를 원문 그대로 뽑는다.

추출 방식(계열별, jin 규칙):
- major/holding: raw XML의 `<TE ACODE="X">값</TE>` / `<TU AUNIT AUNITVALUE>값</TU>` 스캔(ACODE 정본).
- exchange: HTML 표를 matrix로 파싱(parse.extract_html_tables) 후 라벨→값(ACODE 없음).
- summary_extraction: `<SUMMARY><EXTRACTION ACODE=...>값` 사전추출 스칼라(periodic/holding).

출력 = jin structured_fact 통합 스키마(+ kind/source_type/provenance). 숫자는 Decimal 파싱만,
값 문자열(value_raw)은 원문 그대로 보존(가드레일).

CLI: python factx.py <rcept>   |   python factx.py build
"""
import os
import re
import json
import unicodedata

import load
import parse
import facts as FA

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_HERE, "..", "out")

# raw XML 값 셀: <TE ACODE="X" ...>값</TE>, <TU AUNIT="U" AUNITVALUE="V" ...>값</TU>
_TE_RE = re.compile(r'<TE\b([^>]*)>(.*?)</TE>', re.S)
_TU_RE = re.compile(r'<TU\b([^>]*)>(.*?)</TU>', re.S)
_EXTRACT_RE = re.compile(r'<EXTRACTION\b([^>]*)>(.*?)</EXTRACTION>', re.S)
_ATTR = lambda s, k: (re.search(rf'{k}="([^"]*)"', s) or [None, None])[1]
_DOCACODE_RE = re.compile(r'<DOCUMENT-NAME\b[^>]*ACODE="([^"]*)"', re.S)


def _clean(s):
    return unicodedata.normalize("NFC", " ".join(re.sub("<[^>]+>", "", s).split()))


def _value_type(value_raw, value_num, value_unit_value):
    """jin §4 value_type 판정(percent 자동판정은 신뢰낮음 → number로, 라벨사전은 상위계층)."""
    s = value_raw.strip()
    if s in ("-", "", "–", "—"):
        return "null_marker"
    if value_num is not None:
        return "number"
    if re.fullmatch(r"\d{4}[-.]?\d{2}[-.]?\d{2}", s) or re.search(r"\d{4}년\s*\d{1,2}월", s):
        return "date"
    return "text"


def _rec(entry, doc_group, source_type, field_key, field_label, value_raw,
         unit_code=None, unit_value=None, table_id=None, section_path=None, extra=None):
    vr = _clean(value_raw)
    num, _sign = FA._parse_number(vr)
    is_null = vr in ("-", "", "–", "—")
    rec = {
        "fact_id": f"{entry['doc_group']}_{entry['rcept_no']}:{source_type}:{field_key}",
        "corp_code": entry["corp_code"],
        "corp_name": unicodedata.normalize("NFC", entry["corp_name"]),
        "rcept_no": entry["rcept_no"], "doc_id": f"{entry['doc_group']}_{entry['rcept_no']}",
        "doc_group": doc_group, "source_type": source_type,
        "kind": "numeric" if num is not None else "text",
        "field_key": field_key, "field_label": field_label,
        "value_raw": vr, "value_decimal": str(num) if num is not None else None,
        "value_unit_code": unit_code, "value_unit_value": unit_value,
        "is_null": is_null, "value_type": _value_type(vr, num, unit_value),
        "table_id": table_id, "section_path": section_path,
        "base_year": entry.get("base_year"),
        "is_superseded": bool((entry.get("_sup") or {}).get("is_superseded", False)),
        # 정정 매칭 신뢰도 — is_superseded는 "확실할 때만" True로 두는 안전한 값이라
        # (corrlink.py 참고) 매칭이 애매했던 문서(ambiguous/ref_absent)와 애초에
        # 정정이 없는 문서를 구분 못 했다. 그 진단 정보를 그대로 흘려보낸다.
        "supersede_method": (entry.get("_sup") or {}).get("match_method"),
    }
    if extra:
        rec.update(extra)
    return rec


# ---------------------------------------------------------------------------
# major / holding: raw XML ACODE 스캔 (jin §4, §6)
# ---------------------------------------------------------------------------
def _scan_acode(entry, doc_group, source_type):
    raw = load.read_text(load.main_xml_path(entry))
    doc_acode = (_DOCACODE_RE.search(raw) or [None, None])[1]
    # TE 앞에 오는 라벨 TD를 근사 매핑(직전 <TD>텍스트</TD>). 없으면 None.
    out = []
    seen = {}
    for m in _TE_RE.finditer(raw):
        attrs, inner = m.group(1), m.group(2)
        acode = _ATTR(attrs, "ACODE")
        if not acode:
            continue
        # 직전 300자에서 마지막 TD 라벨 추출(근사)
        pre = raw[max(0, m.start() - 400):m.start()]
        lm = re.findall(r"<TD\b[^>]*>(.*?)</TD>", pre, re.S)
        label = _clean(lm[-1]) if lm else None
        key = acode if acode not in seen else f"{acode}#{seen.get(acode,0)}"
        seen[acode] = seen.get(acode, 0) + 1
        out.append(_rec(entry, doc_group, source_type, key, label, inner,
                        extra={"document_acode": doc_acode, "acode": acode}))
    for m in _TU_RE.finditer(raw):
        attrs, inner = m.group(1), m.group(2)
        aunit, aunitval = _ATTR(attrs, "AUNIT"), _ATTR(attrs, "AUNITVALUE")
        if not aunit:
            continue
        key = f"TU:{aunit}" if f"TU:{aunit}" not in seen else f"TU:{aunit}#{seen.get('TU:'+aunit,0)}"
        seen["TU:" + aunit] = seen.get("TU:" + aunit, 0) + 1
        out.append(_rec(entry, doc_group, source_type, key, None, inner,
                        unit_code=aunit, unit_value=aunitval,
                        extra={"document_acode": doc_acode, "acode": aunit}))
    return out


def _scan_summary(entry, doc_group):
    raw = load.read_text(load.main_xml_path(entry))
    out = []
    for m in _EXTRACT_RE.finditer(raw):
        acode = _ATTR(m.group(1), "ACODE")
        if not acode:
            continue
        out.append(_rec(entry, doc_group, "summary", f"EXT:{acode}", None, m.group(2),
                        extra={"acode": acode}))
    return out


# ---------------------------------------------------------------------------
# exchange: HTML matrix 라벨→값 (jin §5, ACODE 없음)
# ---------------------------------------------------------------------------
# jin §5.3 표준 라벨(값이 라벨의 다음 셀). 부분일치로 허용.
_EX_LABELS = ["체결계약명", "계약상대", "계약금액(원)", "최근매출액(원)", "매출액대비(%)",
              "시작일", "종료일", "계약(수주)일자", "판매ㆍ공급계약 구분", "판매·공급계약 구분",
              "투자구분", "투자금액(원)", "자기자본대비(%)", "영업정지금액(원)", "영업정지사유",
              "판매ㆍ공급지역", "확정계약여부"]


def extract_exchange(entry):
    try:
        _chunk, tables = parse.parse_exchange(entry)
    except Exception:
        return []
    out = []
    for t in tables:
        mx = t["matrix"]
        tid = t["table_id"]
        for row in mx:
            cells = [_clean(c) for c in row]
            for i, c in enumerate(cells):
                lab = next((L for L in _EX_LABELS if c and (c == L or c.rstrip("0123456789. ") == L or L in c)), None)
                if not lab:
                    continue
                val = next((cells[j] for j in range(i + 1, len(cells)) if cells[j]), None)
                if val is None:
                    continue
                out.append(_rec(entry, "exchange", "exchange", lab, c, val, table_id=tid))
    return out


# ---------------------------------------------------------------------------
# periodic 비-XBRL matrix: 비율(부채비율·유동비율) — XBRL엔 없는 지표. facts 스키마로 방출.
# ---------------------------------------------------------------------------
_RATIO_ONT = {"부채비율": "debt_ratio", "유동비율": "current_ratio"}
_PERIODIC_SEC = ("요약재무정보", "재무상태 및 영업실적", "유동성 및 자금조달", "위험관리")


def _num_pct(cell):
    s = cell.strip()
    if s.endswith("%") and re.match(r"^-?[\d,]+\.?\d*%$", s):
        return s
    return None


def extract_periodic_ratios(entry, sup=None):
    """MD&A 등 비-XBRL matrix에서 부채비율·유동비율을 facts 스키마(metric_key/scope/year)로 추출.

    scope는 MD&A 관행상 연결 기본(별도 표기가 명시된 표만 별도). 값은 '284.5%' 그대로 보존.
    """
    if entry["doc_group"] != "periodic" or "사업보고서" not in (entry.get("report_nm") or ""):
        return []
    if entry.get("file_format") != "xml":
        return []
    if sup is not None:
        entry = {**entry, "_sup": sup}
    base = entry.get("base_year")
    try:
        _chunks, tables = parse.parse_document(entry)
    except Exception:
        return []
    out = []
    seen = set()
    for t in tables:
        sec = t.get("section_heading") or ""
        if t.get("aclass_xbrl_code") or not any(k in sec for k in _PERIODIC_SEC):
            continue
        scope = "separate" if "별도" in sec else "consolidated"
        for row in t["matrix"]:
            cells = [_clean(c) for c in row]
            for i, c in enumerate(cells):
                mk = _RATIO_ONT.get(c)
                if not mk:
                    continue
                vals = [v for v in (_num_pct(x) for x in cells[i + 1:]) if v]
                for j, v in enumerate(vals[:3]):
                    yr = (base - j) if base else None
                    key = (entry["corp_code"], mk, scope, yr)
                    if key in seen:
                        continue
                    seen.add(key)
                    num, _s = FA._parse_number(v.rstrip("%"))
                    out.append({
                        "fact_id": f"periodic_{entry['rcept_no']}:ratio:{mk}:{scope}:{yr}",
                        "corp_code": entry["corp_code"],
                        "corp_name": unicodedata.normalize("NFC", entry["corp_name"]),
                        "rcept_no": entry["rcept_no"], "doc_id": f"periodic_{entry['rcept_no']}",
                        "metric_key": mk, "scope": scope, "statement": "ratio",
                        "label_raw": c, "value_raw": v,
                        "value_decimal": str(num) if num is not None else None,
                        "unit_kr": "%", "scale": 1, "aclass_xbrl_code": None,
                        "base_year": yr, "source_type": "mda", "kind": "numeric",
                        "is_superseded": bool((entry.get("_sup") or {}).get("is_superseded", False)),
                    })
    return out


def extract(entry, sup=None):
    g = entry["doc_group"]
    if sup is not None:
        entry = {**entry, "_sup": sup}
    if g == "major":
        return _scan_acode(entry, "major", "major") + _scan_summary(entry, "major")
    if g == "holding":
        return _scan_acode(entry, "holding", "holding") + _scan_summary(entry, "holding")
    if g == "exchange":
        return extract_exchange(entry)
    return []


def build(manifest=None):
    manifest = manifest or load.load_manifest()
    import supersede
    smap = supersede.build_supersede_map(manifest)
    targets = [e for e in manifest if e["doc_group"] in ("major", "holding", "exchange")
               and e.get("file_format") == "xml"]
    os.makedirs(OUT_DIR, exist_ok=True)
    n = nd = ne = 0
    byg = {}
    with open(os.path.join(OUT_DIR, "factx.jsonl"), "w", encoding="utf-8") as fo:
        for e in targets:
            try:
                recs = extract(e, smap.get(f"{e['doc_group']}_{e['rcept_no']}"))
            except Exception:  # noqa: BLE001
                ne += 1
                continue
            for r in recs:
                fo.write(json.dumps(r, ensure_ascii=False) + "\n")
            if recs:
                nd += 1
                byg[e["doc_group"]] = byg.get(e["doc_group"], 0) + 1
            n += len(recs)
    return {"n_target": len(targets), "n_docs_with_facts": nd, "n_facts": n,
            "n_err": ne, "by_group": byg}


def build_periodic(manifest=None):
    """annual periodic 비-XBRL 비율(부채비율·유동비율) → out/factx_periodic.jsonl."""
    manifest = manifest or load.load_manifest()
    import supersede
    smap = supersede.build_supersede_map(manifest)
    targets = [e for e in manifest if e["doc_group"] == "periodic"
               and "사업보고서" in (e.get("report_nm") or "") and e.get("file_format") == "xml"]
    os.makedirs(OUT_DIR, exist_ok=True)
    n = nd = 0
    with open(os.path.join(OUT_DIR, "factx_periodic.jsonl"), "w", encoding="utf-8") as fo:
        for e in targets:
            try:
                recs = extract_periodic_ratios(e, smap.get(f"periodic_{e['rcept_no']}"))
            except Exception:  # noqa: BLE001
                continue
            for r in recs:
                fo.write(json.dumps(r, ensure_ascii=False) + "\n")
            if recs:
                nd += 1
            n += len(recs)
    return {"n_docs": len(targets), "n_docs_with_ratios": nd, "n_facts": n}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        print(json.dumps(build(), ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "build_periodic":
        print(json.dumps(build_periodic(), ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1:
        man = load.load_manifest()
        for e in [x for x in man if x["rcept_no"] in sys.argv[1:]]:
            recs = extract(e)
            print(f"=== {e['corp_name']} {e['rcept_no']} {e['doc_group']} : {len(recs)}건 ===")
            for r in recs:
                if r["value_raw"] and not r["is_null"]:
                    print(f"  [{r['kind']:>7}] {r['field_key']:>18} = {r['value_raw'][:45]}"
                          f"  (label={r['field_label']})")
    else:
        print(__doc__)
