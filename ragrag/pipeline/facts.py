"""facts.py — 재무제표 표 → 구조화 fact 레코드 (Phase B).

설계: choi/md/19_fact_추출기_설계.md

핵심: {XBRL} 핵심재무제표(BS/IS/CF/EF, 연결 _C / 별도 _S)는 TABLE-GROUP 안에
메타표(기수→기간·단위)+데이터표(행라벨×기간값) 2개로 들어있다. 메타표는 _is_data_table
필터에 걸려 tables.jsonl에서 빠지므로, 여기서는 **raw XML의 TABLE-GROUP 스팬을 직접
잘라** 메타표+데이터표를 함께 읽어 (행×기간열) 단위 fact를 만든다.

숫자는 부동소수가 아닌 Decimal로 보존한다. LLM은 이 fact를 읽어 문장화만 하고, 값 선택·
계산은 lookup_fact/compute 함수가 담당한다(16번 원칙).

CLI:
  python facts.py <rcept_no>   # 한 문서 fact 미리보기
  python facts.py build        # 전체(periodic 사업보고서) -> out/facts.jsonl
"""
import os
import re
import json
import unicodedata
from decimal import Decimal, InvalidOperation

from lxml import etree

from . import load
from . import supersede

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_HERE, "..", "out")

_CELL_TAGS = {"td", "th", "tu", "te"}
_CORE_PREFIXES = ("BS", "IS", "CF", "EF")   # 재무상태/손익·포괄손익/현금흐름/자본변동

# TABLE-GROUP 스팬. 핵심FS 그룹은 내부에 다른 TABLE-GROUP을 품지 않으므로 non-greedy로 충분.
_TG_RE = re.compile(r'<TABLE-GROUP\b[^>]*ACLASS="\{XBRL\}([A-Za-z0-9_]+)"[^>]*>(.*?)</TABLE-GROUP>', re.S)
# 메타표의 기수→기간: "제 57 기 2025.01.01 부터 2025.12.31 까지" / "제 57 기말 2025.12.31 현재"
_TERM_RE = re.compile(
    r'제\s*(\d+)\s*기말?\s*(\d{4})\.(\d{2})\.(\d{2})(?:\s*부터\s*(\d{4})\.(\d{2})\.(\d{2})\s*까지)?')
_HEADER_TERM_RE = re.compile(r'제\s*(\d+)\s*기')          # 데이터표 헤더 셀의 기수
_NOTE_REF_RE = re.compile(r'\s*\(주[\d,\s]*\)\s*$')       # 라벨 꼬리의 주석 참조 (주30)
_UNIT_RE = re.compile(r'단위\s*[:：]\s*([가-힣]+)')
_UNIT_SCALE = {"원": 1, "천원": 1000, "백만원": 1_000_000,
               "십억원": 1_000_000_000, "억원": 100_000_000, "조원": 1_000_000_000_000}

# ---------------------------------------------------------------------------
# 경량 온톨로지 (19번 §온톨로지, 최소셋) — label_norm -> metric_key
# ---------------------------------------------------------------------------
_ONTOLOGY = {
    "revenue": ["매출액", "매출", "영업수익", "수익(매출액)", "수익"],
    "cogs": ["매출원가"],
    "gross_profit": ["매출총이익", "매출총이익(손실)"],
    "operating_income": ["영업이익", "영업이익(손실)", "영업손익"],
    "net_income": ["당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익", "당기순손익"],
    "total_comprehensive_income": ["총포괄손익", "당기총포괄손익", "총포괄이익"],
    "nci": ["비지배지분"],   # 표별 충돌(IS_C2 당기순이익귀속 vs IS_C3 포괄손익귀속) — statement로 구분
    "total_assets": ["자산총계"],
    "total_liabilities": ["부채총계"],
    "total_equity": ["자본총계"],
}
# 라벨 공백 변형("자산총계" vs "자산 총계") 흡수 — 키는 공백 제거해 매칭.
_LABEL2METRIC = {lab.replace(" ", ""): mk for mk, labs in _ONTOLOGY.items() for lab in labs}

_STATEMENT = {"BS": "balance_sheet", "IS": "income_statement",
              "CF": "cashflow", "EF": "equity"}


def _cell_text(c):
    return unicodedata.normalize("NFC", " ".join("".join(c.itertext()).split()))


def _norm_label(raw):
    return unicodedata.normalize("NFC", " ".join(_NOTE_REF_RE.sub("", raw).split()))


def _parse_number(cell):
    """표 셀 -> (Decimal, sign) 또는 (None, None). 괄호=음수, 쉼표 제거, 비수치 skip."""
    s = cell.strip()
    if not s or s in ("-", "–", "—", "―"):
        return None, None
    neg = s.startswith("(") and s.endswith(")")
    body = s.strip("()").replace(",", "").replace(" ", "")
    if not re.fullmatch(r"-?\d+(\.\d+)?", body):
        return None, None
    try:
        d = Decimal(body)
    except InvalidOperation:
        return None, None
    if neg:
        d = -d
    return (d, -1 if d < 0 else 1)


def _scope_of(code):
    """{XBRL}BS_C/IS_C2 -> consolidated, BS_S/IS_S1 -> separate. 접미 없는 코드(분기)는 연결."""
    base = code.rstrip("0123456789")
    return "separate" if base.endswith("_S") else "consolidated"


def _statement_of(code):
    return _STATEMENT.get(code[:2])


def _matrix_of(tbl):
    rows = []
    for tr in tbl.iter():
        if isinstance(tr.tag, str) and tr.tag.lower() == "tr":
            cells = [_cell_text(c) for c in tr
                     if isinstance(c.tag, str) and c.tag.lower() in _CELL_TAGS]
            if cells:
                rows.append(cells)
    return rows


def _parse_group(code, inner_xml):
    """TABLE-GROUP 내부 -> (terms{기수:(start,end)}, unit, scale, data_matrix). 실패 시 None."""
    root = etree.fromstring(("<ROOT>" + inner_xml + "</ROOT>").encode("utf-8"),
                            etree.XMLParser(recover=True, huge_tree=True))
    tables = [t for t in root.iter() if isinstance(t.tag, str) and t.tag.lower() == "table"]
    if not tables:
        return None
    matrices = [_matrix_of(t) for t in tables]
    matrices = [m for m in matrices if m]
    if not matrices:
        return None
    # 데이터표 = 열 폭이 가장 넓은 표. 메타는 나머지(1열) 텍스트에서 스캔.
    data = max(matrices, key=lambda m: (max(len(r) for r in m), len(m)))
    group_text = " ".join(c for m in matrices for r in m for c in r)

    terms = {}
    for mt in _TERM_RE.finditer(group_text):
        term = int(mt.group(1))
        d1 = f"{mt.group(2)}-{mt.group(3)}-{mt.group(4)}"
        if mt.group(5):                       # 부터~까지 (IS/CF 기간)
            terms[term] = (d1, f"{mt.group(5)}-{mt.group(6)}-{mt.group(7)}")
        else:                                 # 현재 (BS/EF 시점) — 시작 없음
            terms.setdefault(term, (None, d1))
    um = _UNIT_RE.search(group_text)
    unit_kr = um.group(1) if um else "백만원"   # 재무제표 기본 단위
    scale = _UNIT_SCALE.get(unit_kr, 1_000_000)
    return terms, unit_kr, scale, data


def extract_facts(entry, sup_info=None):
    """periodic 문서 1건 -> list[fact dict]. 핵심FS(BS/IS/CF/EF) 표만."""
    path = load.main_xml_path(entry)
    if not path or entry.get("file_format") != "xml":
        return []
    raw = load.read_text(path)
    doc_id = f"{entry['doc_group']}_{entry['rcept_no']}"
    corp_name = unicodedata.normalize("NFC", entry["corp_name"])
    is_superseded = bool((sup_info or {}).get("is_superseded", False))

    facts = []
    for m in _TG_RE.finditer(raw):
        code, inner = m.group(1), m.group(2)
        if not code.startswith(_CORE_PREFIXES):
            continue
        parsed = _parse_group(code, inner)
        if not parsed:
            continue
        terms, unit_kr, scale, data = parsed
        scope = _scope_of(code)
        statement = _statement_of(code)
        header = data[0]
        col2term = {c: int(_HEADER_TERM_RE.search(cell).group(1))
                    for c, cell in enumerate(header)
                    if _HEADER_TERM_RE.search(cell)}
        if not col2term:
            continue
        for r, row in enumerate(data[1:], start=1):
            label_raw = row[0] if row else ""
            label_norm = _norm_label(label_raw)
            if not label_norm:
                continue
            metric_key = _LABEL2METRIC.get(label_norm.replace(" ", ""))
            for c, term in col2term.items():
                if c >= len(row):
                    continue
                val, sign = _parse_number(row[c])
                if val is None:
                    continue
                start, end = terms.get(term, (None, None))
                base_year = int(end[:4]) if end else entry.get("base_year")
                facts.append({
                    "fact_id": f"{doc_id}:{code}:r{r}:c{term}",
                    "corp_code": entry["corp_code"], "corp_name": corp_name,
                    "rcept_no": entry["rcept_no"], "doc_id": doc_id,
                    "row_index": r, "col_index": c,
                    "statement": statement, "aclass_xbrl_code": "{XBRL}" + code,
                    "scope": scope,
                    "label_raw": label_raw, "label_norm": label_norm, "metric_key": metric_key,
                    "value_raw": row[c].strip(), "value_decimal": str(val), "sign": sign,
                    "unit": "KRW", "unit_kr": unit_kr, "scale": scale,
                    "fiscal_term": term, "base_year": base_year,
                    "period_start": start, "period_end": end,
                    "is_superseded": is_superseded, "parser_confidence": 1.0,
                })
    return facts


# ---------------------------------------------------------------------------
# 조회/계산 인터페이스 (16번 함수 계약)
# ---------------------------------------------------------------------------
def load_facts(path=None):
    path = path or os.path.join(OUT_DIR, "facts.jsonl")
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def lookup_fact(facts, metric_key, scope=None, base_year=None, corp_code=None,
                include_superseded=False, statement=None):
    """facts에서 metric_key(+차원)로 fact[] 조회. 보통 1건; 연결/별도·표충돌이면 복수 반환.

    복수면 임의 선택하지 않고 그대로 돌려준다 — 호출측이 scope/statement로 구분 제시(Q7/Q9).
    """
    out = []
    for f in facts:
        if f.get("metric_key") != metric_key:
            continue
        if corp_code and f["corp_code"] != corp_code:
            continue
        if scope and f["scope"] != scope:
            continue
        if statement and f["statement"] != statement:
            continue
        if base_year and f["base_year"] != base_year:
            continue
        if not include_superseded and f["is_superseded"]:
            continue
        out.append(f)
    return out


def get_fact(facts_by_id, fact_id):
    return facts_by_id.get(fact_id)


# 공식 registry — Decimal 계산. 입력은 원천 fact(값+원천ID). LLM 불개입(가드레일).
def compute(formula, facts):
    """formula in {growth, ratio, sum, diff}. facts=[fact,...] value_decimal 사용.

    반환: {value(Decimal str), formula, inputs(fact_id[]), 식}.
    """
    vals = [Decimal(f["value_decimal"]) for f in facts]
    ids = [f["fact_id"] for f in facts]
    if formula == "growth":                 # (당기-전기)/전기 — facts=[당기, 전기]
        cur, prev = vals[0], vals[1]
        v = (cur - prev) / prev * Decimal(100)
        expr = f"({cur}-{prev})/{prev}*100"
    elif formula == "ratio":                # 분자/분모*100 — facts=[분자, 분모]
        v = vals[0] / vals[1] * Decimal(100)
        expr = f"{vals[0]}/{vals[1]}*100"
    elif formula == "sum":
        v = sum(vals)
        expr = "+".join(str(x) for x in vals)
    elif formula == "diff":
        v = vals[0] - vals[1]
        expr = f"{vals[0]}-{vals[1]}"
    else:
        raise ValueError(f"unknown formula: {formula}")
    return {"value": str(v), "formula": formula, "inputs": ids, "expr": expr}


# ---------------------------------------------------------------------------
def build(manifest=None):
    """periodic 사업보고서 전체 -> out/facts.jsonl. 반환 요약 dict."""
    manifest = manifest or load.load_manifest()
    smap = supersede.build_supersede_map(manifest)
    targets = [e for e in manifest
               if e["doc_group"] == "periodic"
               and "사업보고서" in (e.get("report_nm") or "")
               and e.get("file_format") == "xml"]
    os.makedirs(OUT_DIR, exist_ok=True)
    n_fact = n_doc = n_err = 0
    corps = set()
    with open(os.path.join(OUT_DIR, "facts.jsonl"), "w", encoding="utf-8") as fo:
        for e in targets:
            try:
                fs = extract_facts(e, smap.get(f"periodic_{e['rcept_no']}"))
            except Exception as ex:  # noqa: BLE001
                n_err += 1
                continue
            for f in fs:
                fo.write(json.dumps(f, ensure_ascii=False) + "\n")
            if fs:
                n_doc += 1
                corps.add(e["corp_name"])
            n_fact += len(fs)
    return {"n_target_docs": len(targets), "n_docs_with_facts": n_doc,
            "n_corps": len(corps), "n_facts": n_fact, "n_err": n_err}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        summary = build()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1:
        man = load.load_manifest()
        smap = supersede.build_supersede_map(man)
        for e in [x for x in man if x["rcept_no"] in sys.argv[1:]]:
            fs = extract_facts(e, smap.get(f"{e['doc_group']}_{e['rcept_no']}"))
            print(f"=== {e['corp_name']} {e['rcept_no']} {e.get('report_nm')} : fact {len(fs)}개 ===")
            for f in fs:
                if f["metric_key"]:
                    print(f"  [{f['metric_key']:>18} {f['scope']:>12} {f['base_year']}] "
                          f"{f['label_raw']} = {f['value_raw']} ({f['unit_kr']}) "
                          f"<{f['aclass_xbrl_code']}> {f['fact_id']}")
    else:
        print(__doc__)
