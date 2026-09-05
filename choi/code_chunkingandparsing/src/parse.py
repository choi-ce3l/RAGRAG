"""
문서 파서 — Phase 2.

지금은 exchange(거래소공시)만 구현한다. 관찰노트에서 확인한 대로 exchange는 확장자만
.xml이고 실제로는 <html> 루트의 고정서식 HTML(label-value 표)이라 가장 단순하다.
periodic/major/holding(DART DOCUMENT XML)은 다음 단계에서 추가한다.

출력 스키마 (팀 합의 대상):
- chunk:  임베딩/검색 대상 텍스트 조각 (+ 메타데이터)
- tables: 숫자 검증용 표 매트릭스 (마크다운으로 뭉개기 전 원본 셀 값)
"""
import os
import re
import unicodedata

from lxml import etree

import load

# DART DOCUMENT XML에서 값이 들어있는 셀 태그. 관찰노트 반영: TD 말고도 TU(단위셀)/TE(추출셀)/TH.
_CELL_TAGS = {"td", "th", "tu", "te"}
# TITLE ATOC="Y" 를 원문에서 물리적 위치로 잡는다(트리 왜곡 회피 — 관찰노트 참고).
_TITLE_RE = re.compile(r'<TITLE\b[^>]*\bATOC="Y"[^>]*>(.*?)</TITLE>', re.S)
_ATTR = lambda tag, name: (re.search(rf'{name}="([^"]*)"', tag) or [None, None])[1]


def _cell_text(td):
    """<td> 안의 모든 텍스트를 공백 정리해서 한 문자열로."""
    txt = "".join(td.itertext())
    return unicodedata.normalize("NFC", " ".join(txt.split()))


def extract_html_tables(html_text):
    """HTML 문자열 -> 표 리스트. 각 표는 행 리스트, 각 행은 셀 텍스트 리스트.

    관찰노트 반영: charset=euc-kr 선언이 거짓이므로 인코딩을 utf-8로 명시해 파싱한다.
    rowspan/colspan 병합은 지금 펼치지 않는다(v0) — label-value 표라 셀 텍스트만으로도
    사람이 읽고 검증할 수 있고, 필요해지면 그때 확장한다.
    """
    parser = etree.HTMLParser(encoding="utf-8")
    root = etree.fromstring(html_text.encode("utf-8"), parser)
    if root is None:
        return []
    tables = []
    for tbl in root.iter("table"):
        rows = []
        for tr in tbl.iter("tr"):
            cells = [_cell_text(td) for td in tr if isinstance(td.tag, str)
                     and td.tag.lower() in ("td", "th")]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _matrix_to_markdown(matrix, idx):
    """표 매트릭스 -> 마크다운(청크 텍스트용). 열 수가 들쭉날쭉해도 그냥 파이프로 잇는다."""
    lines = [f"[표 {idx}]"]
    for row in matrix:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _base_year(entry):
    """base_year가 메타에 있으면 쓰고, 없으면 접수일 연도로 폴백."""
    return entry.get("base_year") or int(entry["rcept_dt"][:4])


def parse_exchange(entry):
    """exchange 문서 1건 -> (chunk dict, list[table dict]).

    exchange는 목차(TITLE)가 없는 단일 표 문서라 문서 전체가 청크 1개다.
    """
    path = load.main_xml_path(entry)
    html = load.read_text(path)
    matrices = extract_html_tables(html)

    doc_id = f"{entry['doc_group']}_{entry['rcept_no']}"
    text = "\n\n".join(_matrix_to_markdown(m, i + 1) for i, m in enumerate(matrices))

    chunk = {
        "doc_id": doc_id,
        "corp_code": entry["corp_code"],
        "corp_name": unicodedata.normalize("NFC", entry["corp_name"]),
        "doc_group": entry["doc_group"],
        "doc_subtype": entry.get("doc_subtype"),
        "rcept_no": entry["rcept_no"],
        "rcept_dt": entry["rcept_dt"],
        "base_year": _base_year(entry),
        "is_correction": entry.get("is_correction", False),
        "section_heading": None,   # exchange는 목차 없음
        "report_nm": entry.get("report_nm"),
        "n_chars": len(text),
        "text": text,
    }
    tables = [{
        "doc_id": doc_id,
        "doc_group": entry["doc_group"],
        "rcept_no": entry["rcept_no"],
        "section_index": 0,
        "section_heading": None,
        "table_index": i + 1,
        "table_id": f"{doc_id}#0:t{i + 1}",   # 안정 표 ID(섹션#0)
        "matrix": m,
        "aclass_xbrl_code": None,
    } for i, m in enumerate(matrices)]
    return chunk, tables


# ---------------------------------------------------------------------------
# DART DOCUMENT XML (periodic / major / holding) — TITLE 목차 기반 섹션 파싱
# ---------------------------------------------------------------------------

# 표 폭발 대응(관찰: periodic 표의 54%가 1행/1열, 41%가 실질셀<=1인 레이아웃/서식용).
# DART XML은 서식용으로 <TABLE>을 남발하므로 "데이터 표"만 남긴다.
# 임계값은 30개 periodic 표본 측정 기반: 이 규칙이 표를 ~66% 제거하고 재무제표는 보존.
_MIN_ROWS, _MIN_COLS, _MIN_NONEMPTY = 2, 2, 4


def _xbrl_code(tbl):
    """표를 감싸는 TABLE-GROUP의 ACLASS가 {XBRL}*이면 그 코드(BS_C/IS_S1/NT_* 등) 반환.

    조각(fragment) 내부에 여는 <TABLE-GROUP>이 남아 있을 때만 유효(분기/반기 자기완결형 표).
    사업보고서는 <TABLE-GROUP {XBRL}><TITLE ATOC=Y>…</TITLE><TABLE>…</TABLE></TABLE-GROUP> 구조라
    TITLE 물리절단 시 여는 태그가 조각 밖으로 빠져 조상 탐색이 실패한다 → 아래 섹션단위 폴백 사용.
    """
    for a in tbl.iterancestors():
        if isinstance(a.tag, str) and a.tag.lower() == "table-group":
            ac = a.get("ACLASS") or ""
            return ac if ac.startswith("{XBRL}") else None
    return None


# TITLE 물리절단으로 여는 <TABLE-GROUP {XBRL}>이 조각 밖으로 빠지는 문제(사업보고서 재무제표)를
# 보완하기 위해, 원문 raw에서 각 TITLE 위치를 실제로 감싸는 TABLE-GROUP 코드를 스택으로 조회한다.
_TG_OPEN_RE = re.compile(r'<TABLE-GROUP\b[^>]*>', re.S)
_TG_CLOSE_RE = re.compile(r'</TABLE-GROUP\s*>', re.S)


def _title_enclosing_xbrl(raw, title_starts):
    """각 TITLE 시작위치를 감싸는 최내곽 TABLE-GROUP의 {XBRL} 코드를 조회한다.

    반환: {title_index: "{XBRL}IS_C2" | None}. open/close/TITLE 이벤트를 위치순으로 병합해
    스택 top으로 감싸는 그룹을 판정하므로 TABLE-GROUP 중첩에도 안전하다.
    """
    events = []
    for m in _TG_OPEN_RE.finditer(raw):
        events.append((m.start(), 0, _ATTR(m.group(0), "ACLASS") or ""))
    for m in _TG_CLOSE_RE.finditer(raw):
        events.append((m.start(), 1, None))
    for i, p in enumerate(title_starts):
        events.append((p, 2, i))          # 같은 위치면 open(0)→close(1)→title(2) 순으로 처리
    events.sort(key=lambda e: (e[0], e[1]))
    stack, result = [], {}
    for _pos, kind, val in events:
        if kind == 0:
            stack.append(val)
        elif kind == 1:
            if stack:
                stack.pop()
        else:
            code = stack[-1] if stack else ""
            result[val] = code if code.startswith("{XBRL}") else None
    return result


def _is_data_table(matrix):
    """레이아웃/서식용 표를 걸러낸다. 실질(비어있지 않은) 셀 기준으로 데이터 표만 True.

    주의: {XBRL} 그룹 안에도 소형 레이아웃 표가 섞여 있으므로, XBRL 여부로 무조건
    남기지 않고 모양(shape)으로 판정한다(표본 측정: XBRL 24k 중 18k가 레이아웃 표).
    """
    nr = len(matrix)
    nc = max((len(r) for r in matrix), default=0)
    ne = sum(1 for r in matrix for c in r if c and c.strip())
    return nr >= _MIN_ROWS and nc >= _MIN_COLS and ne >= _MIN_NONEMPTY


def _tables_in(elem, section_xbrl=None):
    """lxml 요소 하위의 데이터 TABLE -> [{matrix, aclass_xbrl_code}]. 값 셀 4종(TD/TH/TU/TE).

    레이아웃/서식용 표는 _is_data_table로 제외. XBRL 재무제표 표는 aclass_xbrl_code로 태깅.
    section_xbrl: 조각 조상에서 코드를 못 찾을 때 쓰는 섹션단위 폴백(사업보고서 재무제표용).
    """
    out = []
    for tbl in elem.iter():
        if not (isinstance(tbl.tag, str) and tbl.tag.lower() == "table"):
            continue
        rows = []
        for tr in tbl.iter():
            if not (isinstance(tr.tag, str) and tr.tag.lower() == "tr"):
                continue
            cells = [_cell_text(c) for c in tr
                     if isinstance(c.tag, str) and c.tag.lower() in _CELL_TAGS]
            if cells:
                rows.append(cells)
        if rows and _is_data_table(rows):
            out.append({"matrix": rows, "aclass_xbrl_code": _xbrl_code(tbl) or section_xbrl})
    return out


def _narrative_in(elem):
    """서술형 텍스트만 추출. 표 안 텍스트는 제외하고, 최상위 P만 채택한다.

    관찰노트 이슈 반영: <P> 안에 <P>가 수백 단계 중첩된 문서가 있어, 모든 P를 순회하면
    같은 텍스트가 중첩 수만큼 반복되어 폭발한다. 그래서 TABLE/P 조상이 없는 P만 취한다.
    """
    out = []
    for p in elem.iter():
        if not (isinstance(p.tag, str) and p.tag.lower() == "p"):
            continue
        anc = {a.tag.lower() for a in p.iterancestors() if isinstance(a.tag, str)}
        if "table" in anc or "p" in anc:
            continue
        txt = " ".join("".join(p.itertext()).split())
        if txt:
            out.append(unicodedata.normalize("NFC", txt))
    return "\n".join(out)


def _parse_fragment(fragment_bytes):
    """섹션 원문 조각을 recover 파서로 감싸 파싱. 실패해도 트리를 돌려준다."""
    wrapped = b"<ROOT>" + fragment_bytes + b"</ROOT>"
    parser = etree.XMLParser(recover=True, huge_tree=True)
    return etree.fromstring(wrapped, parser)


def parse_document(entry):
    """periodic/major/holding 문서 1건 -> (list[chunk], list[table]).

    섹션 경계 = 원문에서 <TITLE ATOC="Y"> 태그의 물리적 위치. 트리(부모-자식)를 믿지 않고
    원본 바이트 순서로 자르므로 recover 복구 트리의 왜곡과 무관하게 안정적이다.
    """
    raw = load.read_text(entry["main_path"] if "main_path" in entry else load.main_xml_path(entry))
    matches = list(_TITLE_RE.finditer(raw))
    # 각 TITLE을 감싸는 {XBRL} TABLE-GROUP 코드(사업보고서 재무제표: 여는 태그가 TITLE 앞이라 조각 밖).
    section_xbrl = _title_enclosing_xbrl(raw, [m.start() for m in matches])
    doc_id = f"{entry['doc_group']}_{entry['rcept_no']}"
    base = {
        "doc_id": doc_id,
        "corp_code": entry["corp_code"],
        "corp_name": unicodedata.normalize("NFC", entry["corp_name"]),
        "doc_group": entry["doc_group"],
        "doc_subtype": entry.get("doc_subtype"),
        "rcept_no": entry["rcept_no"],
        "rcept_dt": entry["rcept_dt"],
        "base_year": _base_year(entry),
        "is_correction": entry.get("is_correction", False),
        "report_nm": entry.get("report_nm"),
    }

    chunks, tables = [], []
    for i, m in enumerate(matches):
        tag = m.group(0)[:m.group(0).index(">") + 1]
        heading = unicodedata.normalize("NFC", " ".join(m.group(1).split()))
        atocid = _ATTR(tag, "ATOCID")
        assoc = _ATTR(tag, "AASSOCNOTE")
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        frag = raw[m.end():end]                      # TITLE 태그 다음부터 다음 TITLE 전까지
        root = _parse_fragment(frag.encode("utf-8"))

        tbls = _tables_in(root, section_xbrl.get(i)) if root is not None else []
        narrative = _narrative_in(root) if root is not None else ""

        # 청크 text에는 표를 예산(약 2만자) 안에서만 인라인한다. 표 전체 매트릭스는
        # 아래 tables 리스트에 온전히 보존되므로, 임베딩용 text가 재무제표 주석처럼 표
        # 수백 개짜리 섹션에서 수 MB로 폭발하는 것을 막는다.
        parts = [f"## {heading}"]
        if narrative:
            parts.append(narrative)
        budget, inlined = 20000, 0
        for k, tbl in enumerate(tbls):
            md = _matrix_to_markdown(tbl["matrix"], k + 1)
            if inlined + len(md) > budget:
                parts.append(f"(표 {len(tbls) - k}개 더 있음 — tables.jsonl 참조)")
                break
            parts.append(md)
            inlined += len(md)
        text = "\n\n".join(parts)

        chunks.append({**base,
                       "section_index": i,
                       "section_atocid": atocid,
                       "section_assocnote": assoc,
                       "section_heading": heading,
                       "n_chars": len(text),
                       "text": text})
        for k, tbl in enumerate(tbls):
            tables.append({"doc_id": doc_id, "doc_group": entry["doc_group"],
                           "rcept_no": entry["rcept_no"],
                           "section_index": i, "section_atocid": atocid,
                           "section_heading": heading,
                           "table_index": k + 1,
                           "table_id": f"{doc_id}#{i}:t{k + 1}",   # 안정 표 ID(섹션#i)
                           "matrix": tbl["matrix"],
                           "aclass_xbrl_code": tbl["aclass_xbrl_code"]})
    return chunks, tables


if __name__ == "__main__":
    import sys
    manifest = load.load_manifest()
    ex = [e for e in manifest if e["doc_group"] == "exchange"]
    # 인자로 rcept_no를 주면 그 문서, 아니면 처음 몇 건을 보여준다.
    targets = [e for e in ex if e["rcept_no"] in sys.argv[1:]] or ex[:2]
    for e in targets:
        chunk, tables = parse_exchange(e)
        print("=" * 70)
        print(f"{chunk['corp_name']} | {chunk['doc_subtype']} | {chunk['rcept_no']} "
              f"| 표 {len(tables)}개 | {chunk['n_chars']}자")
        print("-" * 70)
        print(chunk["text"][:1500])
