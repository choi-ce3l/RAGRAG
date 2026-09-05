#!/usr/bin/env python3
"""정기보고서 본문 표 추출 — XBRL 재무제표 밖의 표들.

## 왜 필요한가

정답셋 실패의 15%가 본문 표를 요구한다.

    "최대주주 및 특수관계인 중 현대자동차·기아·현대모비스의 지분율을 높은 순서대로"
    "직원 등의 연간급여총액 합계는?"
    "소송현황 표에 열거된 3건의 소송가액 합계는?"
    "배당에 관한 사항 표의 최근 3개년 현금배당성향"

이 값들은 XBRL 재무제표에도, 공시 구조화 fact에도 없다. 사업보고서 본문의 표에 있다.

## 구조가 다르다

XBRL 재무제표는 `<TE ACODE="...">`를 쓰지만 본문 표는 `<TD>`/`<TH>`다.
그래서 기존 파서가 보지 못했다.

    <THEAD> <TH>보통주</TH> <TH>우선주</TH> ...
    <TBODY> <TR> <TD>현대자동차(주)</TD> <TD>23,327,400</TD> <TD>-</TD> <TD>20.95</TD>

## 좌표를 끝까지 들고 간다

구조화 공시 파서는 표 머리글을 모든 칸에 찍고 위치를 버렸다. 그래서 `보고자`가
9개 키에, `보통주식`이 58개 키에 붙어 어느 칸인지 알 수 없게 됐고, 나중에 LLM으로
복원하려다 실패했다(96,613 토큰).

여기서는 **문서·절·표·행·열**을 모두 남긴다. 앞에서 버린 것은 뒤에서 못 살린다.

    python build_tables.py --probe     # 표본 확인
    python build_tables.py --build     # 전체 빌드
"""

import argparse
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "code_chunkingandparsing" / "src"))
import load                                                    # noqa: E402

DB = _HERE / "data" / "tables.db"

TABLE = re.compile(r"<TABLE\b[^>]*>(.*?)</TABLE>", re.S | re.I)
ROW = re.compile(r"<TR\b[^>]*>(.*?)</TR>", re.S | re.I)
CELL = re.compile(r"<(TD|TH)\b[^>]*>(.*?)</\1>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")
# 절 제목 — 표 앞에 나오는 굵은 글씨나 문단
HEAD = re.compile(r"<(?:P|SPAN|TITLE)\b[^>]*>([^<]{2,60})</(?:P|SPAN|TITLE)>", re.I)

COLUMNS = ["row_key", "doc_id", "rcept_no", "corp_code", "corp_name", "report_nm",
           "section", "table_idx", "row_idx", "col_idx", "header", "row_label",
           "value_raw", "value_num", "is_superseded"]


def _txt(s):
    return re.sub(r"\s+", " ", TAG.sub("", s or "")).replace("&nbsp;", " ").strip()


def _num(s):
    t = re.sub(r"[,\s]", "", str(s or ""))
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").rstrip("%")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", t or ""):
        return None
    v = float(t)
    return -v if neg else v


def _header_of(rows):
    """(열별 머리글, 머리글 행 수).

    본문 표도 머리글이 두 줄인 경우가 많다.

        행0: ['주주명', '소유주식수(주)', '', '지분율(%)', '']
        행1: ['', '보통주', '우선주', '보통주', '우선주']

    첫 줄만 읽으면 지분율 열의 머리글이 비어 조회가 안 된다. XBRL 분기표에서
    똑같은 문제를 겪었다 — 머리글 줄을 하나로 보면 열이 어긋난다.

    숫자가 하나도 없는 **앞쪽 연속 행**을 머리글로 본다. 열 수가 다르면(병합)
    긴 쪽에 맞춰 이어 붙인다.
    """
    hn = 0
    for r in rows[:3]:                     # 머리글이 세 줄을 넘는 표는 드물다
        if any(_num(c) is not None for c in r):
            break
        hn += 1
    if hn == 0:
        return rows[0], 1
    width = max(len(r) for r in rows)
    head = [""] * width
    for r in rows[:hn]:
        for j, c in _spread(r, width):
            if c:
                head[j] = (head[j] + " " + c).strip()
    return head, hn


def _spread(row, width):
    """병합된 머리글 칸을 데이터 열에 배분한다 — (열번호, 값) 쌍으로.

    병합이 풀려 있지 않아 머리글 줄마다 칸 수가 다르다.

        행0: ['주주명', '소유주식수(주)', '지분율(%)']   ← 3칸 (각각 2열씩 병합)
        행1: ['보통주', '우선주', '보통주', '우선주']      ← 4칸 (라벨 열 없음)
        행2: ['현대자동차(주)', v, v, v, v]              ← 5칸

    칸 수로 배분한다. XBRL 분기표에서 쓴 것과 같은 논리다 — 머리글 n칸이
    데이터 m열을 균등하게 나눈다.
    """
    n = len(row)
    if n == width:
        return list(enumerate(row))
    if n == width - 1:                       # 라벨 열이 없는 줄
        return [(i + 1, c) for i, c in enumerate(row)]
    if n >= 2 and (width - 1) % (n - 1) == 0:   # 라벨 + 균등 병합
        per = (width - 1) // (n - 1)
        out = [(0, row[0])]
        for i, c in enumerate(row[1:]):
            out += [(1 + i * per + k, c) for k in range(per)]
        return out
    return [(i, c) for i, c in enumerate(row) if i < width]
    """표 바로 앞의 제목. 어느 표인지 알아야 조회할 수 있다."""
    head = raw[max(0, pos - window):pos]
    cands = [_txt(h) for h in HEAD.findall(head)]
    cands = [c for c in cands if 2 <= len(c) <= 60 and not c.startswith("(")]
    return cands[-1] if cands else ""


def _section_of(raw, pos, window=3000):
    """표 바로 앞의 제목. 어느 표인지 알아야 조회할 수 있다."""
    head = raw[max(0, pos - window):pos]
    cands = [_txt(h) for h in HEAD.findall(head)]
    cands = [c for c in cands if 2 <= len(c) <= 60 and not c.startswith("(")]
    return cands[-1] if cands else ""


def extract(entry, sup=None):
    path = load.main_xml_path(entry)
    if not path or entry.get("file_format") != "xml":
        return []
    raw = load.read_text(path)
    doc_id = f"{entry['doc_group']}_{entry['rcept_no']}"
    corp = unicodedata.normalize("NFC", entry["corp_name"])
    is_sup = int(bool((sup or {}).get("is_superseded", False)))
    out = []
    for ti, m in enumerate(TABLE.finditer(raw)):
        body = m.group(1)
        if "<TE" in body:                 # XBRL 재무제표는 기존 파서 몫이다
            continue
        rows = [[_txt(c[1]) for c in CELL.findall(r)] for r in ROW.findall(body)]
        rows = [r for r in rows if r]
        if len(rows) < 2:
            continue
        section = _section_of(raw, m.start())
        header, hn = _header_of(rows)
        for ri, row in enumerate(rows[hn:], start=hn):
            label = row[0] if row else ""
            for ci, cell in enumerate(row):
                if ci == 0 or not cell or cell == "-":
                    continue
                out.append({
                    "row_key": f"{doc_id}:t{ti}:r{ri}:c{ci}",
                    "doc_id": doc_id, "rcept_no": entry["rcept_no"],
                    "corp_code": entry["corp_code"], "corp_name": corp,
                    "report_nm": entry.get("report_nm", ""),
                    "section": section, "table_idx": ti, "row_idx": ri, "col_idx": ci,
                    "header": header[ci] if ci < len(header) else "",
                    "row_label": label, "value_raw": cell, "value_num": _num(cell),
                    "is_superseded": is_sup,
                })
    return out


def open_db(fresh=False):
    if fresh and DB.exists():
        DB.unlink()
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(f"CREATE TABLE IF NOT EXISTS cells ({', '.join(c + ' TEXT' for c in COLUMNS)}, "
                "PRIMARY KEY (row_key))")
    return con


def index_db(con):
    for sql in ("CREATE INDEX IF NOT EXISTS ix_corp ON cells (corp_code, section)",
                "CREATE INDEX IF NOT EXISTS ix_label ON cells (corp_code, row_label)",
                "CREATE INDEX IF NOT EXISTS ix_doc ON cells (rcept_no, table_idx)"):
        con.execute(sql)
    con.commit()


def build(limit=None):
    m = load.load_manifest()
    import supersede
    sup = supersede.build_supersede_map(m)
    targets = [e for e in m if e["doc_group"] == "periodic"
               and any(k in (e.get("report_nm") or "")
                       for k in ("사업보고서", "반기보고서", "분기보고서"))]
    if limit:
        targets = targets[:limit]
    con = open_db(fresh=True)
    ins = (f"INSERT OR REPLACE INTO cells ({', '.join(COLUMNS)}) "
           f"VALUES ({', '.join('?' * len(COLUMNS))})")
    n = 0
    for i, e in enumerate(targets, 1):
        try:
            rows = extract(e, sup.get(f"periodic_{e['rcept_no']}"))
        except Exception as ex:                                # noqa: BLE001
            print(f"  [{i}] 실패 {e['corp_name']}: {type(ex).__name__}")
            continue
        if rows:
            con.executemany(ins, [[str(r[c]) if r[c] is not None else None
                                   for c in COLUMNS] for r in rows])
            n += len(rows)
        if i % 100 == 0:
            con.commit()
            print(f"  [{i}/{len(targets)}] 누적 셀 {n:,}")
    con.commit()
    index_db(con)
    print(f"\n  문서 {len(targets)}건 → 셀 {n:,}개 → {DB}")
    con.close()


def probe(n=3):
    m = load.load_manifest()
    e = [x for x in m if x["corp_name"] == "현대건설"
         and "사업보고서" in (x.get("report_nm") or "")
         and "2024.12" in (x.get("report_nm") or "")][0]
    rows = extract(e)
    print(f"현대건설 사업보고서 (2024.12) → 셀 {len(rows):,}개")
    want = [r for r in rows if r["row_label"] in ("현대자동차(주)", "기아(주)", "현대모비스(주)")]
    print(f"\n최대주주 관련 셀 {len(want)}개:")
    for r in want[:12]:
        print(f"   [{r['section'][:24]:<26}] t{r['table_idx']} r{r['row_idx']} c{r['col_idx']}"
              f"  {r['row_label']:<14} {r['header']:<8} {r['value_raw']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    if a.probe:
        probe()
    elif a.build:
        build(a.limit)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
