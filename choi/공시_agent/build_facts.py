#!/usr/bin/env python3
"""분기·반기를 포함한 fact 빌더 — SQLite 저장.

기존 `code_chunkingandparsing/src/facts.py`는 건드리지 않는다. 파싱 함수는 그대로
재사용하고, **막고 있던 두 곳만** 확장한다.

  ① 대상 필터   `"사업보고서" in report_nm` → 분기·반기 포함
  ② 기간 정규식  `제 56 기 2023.01.01` 만 읽음 → `제 56 기 1분기 2023.01.01` 도

②가 핵심이다. 기수와 날짜 사이의 `1분기`·`반기` 때문에 기간 메타가 통째로 유실되고,
그 결과 열 배정이 어긋나 같은 값이 다른 기수에 붙었다.

## 3개월인가 누적인가
메타표의 `부터~까지` 길이가 답한다. 표본 40건에서 문서마다 기간이 한 종류뿐이었다.

    1분기 보고서 → 3개월      반기 보고서 → 6개월      3분기 보고서 → 9개월(누적)

길이를 못 읽으면 그 fact를 버린다. 3개월인지 누적인지 모르는 수치는
틀리게 답하는 것보다 없는 편이 낫다.

## 왜 SQLite인가
JSONL은 통째로 읽어 파이썬 객체로 만들어야 해서 기동 12초 · 메모리 798MB가 든다.
분기·반기를 넣으면 문서가 3.6배가 되어 2GB에 이른다. SQLite는 인덱스로 디스크에서
바로 조회하므로 기동이 사라지고 메모리가 수십 MB로 떨어진다. 표준 라이브러리라
의존성도 늘지 않는다.

    python build_facts.py --probe          # 표본으로 확인만 (DB 안 만듦)
    python build_facts.py --annual-only    # 회귀 검증: 기존 결과와 대조
    python build_facts.py --build          # 전체 빌드 → data/facts.db
"""

import argparse
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "code_chunkingandparsing" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import facts as F                                        # noqa: E402
import load
import supersede                                              # noqa: E402

DB = _HERE / "data" / "facts.db"

# 기수와 날짜 사이의 기간 표기(1분기·반기·중간)를 흡수한다. 이것이 확장의 핵심.
TERM_RE = re.compile(
    r"제\s*(\d+)\s*기\s*((?:[1-4]\s*분기|반기|중간)?)\s*(?:말)?\s*"
    r"(\d{4})\.(\d{2})\.(\d{2})(?:\s*부터\s*(\d{4})\.(\d{2})\.(\d{2})\s*까지)?")

MONTHS_LABEL = {3: "3개월", 6: "반기누적", 9: "3분기누적", 12: "연간"}


def _months(s, e):
    return round((e - s).days / 30.4)


def parse_group(code, inner_xml):
    """`facts._parse_group`의 확장판 — terms를 (기수, 기간표기)로 키잉한다.

    원본은 기수 하나로만 키잉해서, 같은 기수의 3개월/누적 열이 서로 덮어썼다.
    """
    from lxml import etree
    root = etree.fromstring(("<ROOT>" + inner_xml + "</ROOT>").encode("utf-8"),
                            etree.XMLParser(recover=True, huge_tree=True))
    tables = [t for t in root.iter() if isinstance(t.tag, str) and t.tag.lower() == "table"]
    matrices = [m for m in (F._matrix_of(t) for t in tables) if m]
    if not matrices:
        return None
    data = max(matrices, key=lambda m: (max(len(r) for r in m), len(m)))
    group_text = " ".join(c for m in matrices for r in m for c in r)

    terms = {}
    for mt in TERM_RE.finditer(group_text):
        term = int(mt.group(1))
        qual = (mt.group(2) or "").replace(" ", "")
        d1 = date(int(mt.group(3)), int(mt.group(4)), int(mt.group(5)))
        if mt.group(6):
            d2 = date(int(mt.group(6)), int(mt.group(7)), int(mt.group(8)))
            terms[(term, qual)] = (d1.isoformat(), d2.isoformat(), _months(d1, d2))
        else:
            terms.setdefault((term, qual), (None, d1.isoformat(), None))

    um = F._UNIT_RE.search(group_text)
    unit_kr = um.group(1) if um else "백만원"
    scale = F._UNIT_SCALE.get(unit_kr, 1_000_000)
    return terms, unit_kr, scale, data


HEADER_RE = re.compile(r"제\s*(\d+)\s*기\s*((?:[1-4]\s*분기|반기|중간)?)")
# 2단 헤더의 아랫줄 — "3개월 | 누적 | 3개월 | 누적"
SUB_RE = re.compile(r"^\s*(3\s*개월|누적|당[분기]*기|전[분기]*기|\d+\s*개월)\s*$")


def _column_map(data):
    """데이터 열 → (기수, 기간표기, 누적여부, 헤더끝행).

    분기·반기 보고서의 손익계산서는 헤더가 두 줄이다.

        행0: ['', '제 50 기 반기', '제 49 기 반기']      ← 병합돼 3칸
        행1: ['3개월', '누적', '3개월', '누적']           ← 라벨 열이 없어 4칸
        행2: ['매출', v1, v2, v3, v4]                   ← 5칸

    행0만 헤더로 보면 열이 어긋나 1분기 값이 반기누적으로 붙는다. 실제로 그렇게
    라벨링돼 있었고, 누적 단조성 검사에서 168건이 걸려 드러났다.

    병합이 풀려 있지 않으므로 칸 수로 배분한다 — 기수 n개가 아랫줄 m칸을 균등하게 나눈다.
    """
    h0 = data[0] if data else []
    terms = [(i, HEADER_RE.search(c)) for i, c in enumerate(h0)]
    terms = [(i, int(m.group(1)), (m.group(2) or "").replace(" ", ""))
             for i, m in terms if m]
    if not terms:
        return {}, 1

    h1 = data[1] if len(data) > 1 else []
    subs = [c.strip() for c in h1]
    if subs and all(SUB_RE.match(c) for c in subs if c) and len(subs) >= len(terms) * 2:
        per = max(1, len(subs) // len(terms))
        out = {}
        for i, marker in enumerate(subs):
            if not marker:
                continue
            t = terms[min(i // per, len(terms) - 1)]
            out[i + 1] = (t[1], t[2], "누적" in marker)   # 데이터 열은 라벨 열 다음부터
        return out, 2

    # 단일 헤더 — 기존 방식. 누적 여부는 메타 기간이 말해준다.
    return {i: (t, q, None) for i, t, q in terms}, 1


def extract(entry, sup_info=None):
    """문서 1건 → fact 목록. 기간 정보를 못 읽은 fact는 버린다."""
    path = load.main_xml_path(entry)
    if not path or entry.get("file_format") != "xml":
        return [], 0
    raw = load.read_text(path)
    doc_id = f"{entry['doc_group']}_{entry['rcept_no']}"
    corp_name = unicodedata.normalize("NFC", entry["corp_name"])
    is_sup = bool((sup_info or {}).get("is_superseded", False))
    report_nm = entry.get("report_nm") or ""

    out, dropped = [], 0
    for m in F._TG_RE.finditer(raw):
        code, inner = m.group(1), m.group(2)
        if not code.startswith(F._CORE_PREFIXES):
            continue
        parsed = parse_group(code, inner)
        if not parsed:
            continue
        terms, unit_kr, scale, data = parsed
        scope, statement = F._scope_of(code), F._statement_of(code)
        header = data[0]

        col2key, hdr_rows = _column_map(data)
        if not col2key:
            continue

        for r, row in enumerate(data[hdr_rows:], start=hdr_rows):
            label_raw = row[0] if row else ""
            label_norm = F._norm_label(label_raw)
            if not label_norm:
                continue
            metric_key = F._LABEL2METRIC.get(label_norm.replace(" ", ""))
            for c, key in col2key.items():
                if c >= len(row):
                    continue
                val, sign = F._parse_number(row[c])
                if val is None:
                    continue
                term, qual, cumulative = key
                span = terms.get((term, qual)) or terms.get((term, ""))
                if span is None:
                    dropped += 1                 # 기간을 모르는 값은 넣지 않는다
                    continue
                start, end, months = span
                if statement in ("income_statement", "cashflow"):
                    if months is None:
                        dropped += 1             # 기간 항목인데 길이를 모르면 버린다
                        continue
                    if cumulative is False:      # "3개월" 열 — 메타의 누적 기간이 아니다
                        months = 3
                        if end:
                            y, mth, d = (int(v) for v in end.split("-"))
                            sm = mth - 2
                            sy = y if sm > 0 else y - 1
                            start = f"{sy:04d}-{(sm - 1) % 12 + 1:02d}-01"
                    elif cumulative is True and months == 3:
                        pass                     # 1분기는 3개월 = 누적이 같다
                plabel = MONTHS_LABEL.get(months, "시점" if months is None else f"{months}개월")
                out.append({
                    # 저장 키와 표시 좌표는 다르다. 좌표(fact_id)는 기존 파서·정답셋과
                    # 같은 c{기수} 규칙이라 한 표에 같은 기수가 두 열이면 겹친다.
                    # 겹친 채로 PK를 걸면 30,511건이 조용히 덮어써진다.
                    "row_key": f"{doc_id}:{code}:r{r}:c{c}",
                    # 좌표는 기존 파서와 같은 규칙을 쓴다 — c 뒤는 열이 아니라 기수다.
                    # 분기·반기는 같은 기수에 3개월·누적 두 열이 있어 그대로 두면
                    # 좌표가 충돌한다. 연간이 아닐 때만 기간을 덧붙여 구분한다.
                    "fact_id": (f"{doc_id}:{code}:r{r}:c{term}"
                                + ("" if plabel in ("연간", "시점") else f":{plabel}")),
                    "cumulative": "" if cumulative is None else int(bool(cumulative)),
                    "corp_code": entry["corp_code"], "corp_name": corp_name,
                    "rcept_no": entry["rcept_no"], "doc_id": doc_id,
                    "report_nm": report_nm,
                    "row_index": r, "col_index": c,
                    "statement": statement, "aclass_xbrl_code": "{XBRL}" + code,
                    "scope": scope,
                    "label_raw": label_raw, "label_norm": label_norm,
                    "metric_key": metric_key,
                    "value_raw": row[c].strip(), "value_decimal": str(val), "sign": sign,
                    "unit": "KRW", "unit_kr": unit_kr, "scale": scale,
                    "fiscal_term": term, "base_year": int(end[:4]) if end else entry.get("base_year"),
                    "period_start": start, "period_end": end,
                    "period_months": months,
                    "period_label": plabel,
                    "is_superseded": int(is_sup), "parser_confidence": 1.0,
                })
    return out, dropped


COLUMNS = ["row_key", "fact_id", "corp_code", "corp_name", "rcept_no", "doc_id", "report_nm",
           "row_index", "col_index", "statement", "aclass_xbrl_code", "scope",
           "label_raw", "label_norm", "metric_key", "value_raw", "value_decimal",
           "sign", "unit", "unit_kr", "scale", "fiscal_term", "base_year",
           "period_start", "period_end", "period_months", "period_label", "cumulative",
           "is_superseded", "parser_confidence"]


def open_db(path=DB, fresh=False):
    path = Path(path)
    if fresh and path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(f"CREATE TABLE IF NOT EXISTS facts ({', '.join(c + ' TEXT' for c in COLUMNS)}, "
                "PRIMARY KEY (row_key))")
    return con


def index_db(con):
    for sql in (
        "CREATE INDEX IF NOT EXISTS ix_lookup ON facts"
        " (corp_code, label_norm, scope, base_year, period_label)",
        "CREATE INDEX IF NOT EXISTS ix_metric ON facts"
        " (corp_code, metric_key, scope, base_year, period_label)",
        "CREATE INDEX IF NOT EXISTS ix_corp ON facts (corp_name, base_year)",
    ):
        con.execute(sql)
    con.commit()


# ---------------------------------------------------------------------------
# 검증 — 새 데이터를 넣기 전에 검사를 먼저 켠다
# ---------------------------------------------------------------------------
def regression_annual(sample=12):
    """사업보고서 결과가 기존 파서와 완전히 같은지 대조한다.

    확장이 기존 파싱을 건드리지 않았음을 보이는 것이 목적이다.
    fact_id는 키잉 방식이 바뀌었으므로(기수 → 열 인덱스) 값 집합으로 비교한다.
    """
    m = load.load_manifest()
    ann = [e for e in m if e["doc_group"] == "periodic"
           and "사업보고서" in (e.get("report_nm") or "")][:sample]
    same = diff = 0
    for e in ann:
        old = F.extract_facts(e, None)
        new, _ = extract(e, None)

        def key(f):
            return (f["statement"], f["scope"], f["label_norm"],
                    f["fiscal_term"], f["value_decimal"])
        a, b = {key(f) for f in old}, {key(f) for f in new}
        if a == b:
            same += 1
        else:
            diff += 1
            print(f"  차이: {e['corp_name']} {e['report_nm']} "
                  f"기존 {len(a)} / 신규 {len(b)} · 누락 {len(a - b)} · 추가 {len(b - a)}")
    print(f"  사업보고서 {sample}건 — 동일 {same} · 차이 {diff}")
    return diff == 0


def probe(n=8):
    """분기·반기 표본으로 기간 인식과 값 구분을 확인한다."""
    m = load.load_manifest()
    qs = [e for e in m if e["doc_group"] == "periodic"
          and any(k in (e.get("report_nm") or "") for k in ("분기보고서", "반기보고서"))][:n]
    import collections
    lab = collections.Counter()
    tot = drop = 0
    for e in qs:
        fs, d = extract(e, None)
        tot += len(fs)
        drop += d
        lab.update(f["period_label"] for f in fs)
        iss = [f for f in fs if f["statement"] == "income_statement"]
        vals = {(f["fiscal_term"], f["label_norm"]): f["value_decimal"] for f in iss}
        dup = len(iss) - len(vals)
        print(f"  {e['corp_name'][:10]:<12} {e['report_nm'][:16]:<18} "
              f"fact {len(fs):>4} · 버림 {d:>3} · IS 중복키 {dup}")
    print(f"\n  기간 분포: {dict(lab)}")
    print(f"  총 {tot:,}건 · 버림 {drop}건")


def build(fresh=True, limit=None):
    m = load.load_manifest()
    # 정정 이력은 반드시 supersede 맵에서 받아온다. 문서를 자기 자신에 매핑하면
    # is_superseded가 전부 0이 되어, 정정된 옛 수치를 최신인 양 근거로 내세우게 된다.
    sup = supersede.build_supersede_map(m)
    targets = [e for e in m if e["doc_group"] == "periodic"
               and any(k in (e.get("report_nm") or "")
                       for k in ("사업보고서", "반기보고서", "분기보고서"))]
    if limit:
        targets = targets[:limit]
    con = open_db(fresh=fresh)
    n = drop = 0
    ins = f"INSERT OR REPLACE INTO facts ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})"
    for i, e in enumerate(targets, 1):
        try:
            fs, d = extract(e, sup.get(f"periodic_{e['rcept_no']}"))
        except Exception as ex:                            # noqa: BLE001
            print(f"  [{i}] 실패 {e['corp_name']} {e['rcept_no']}: {type(ex).__name__}")
            continue
        drop += d
        if fs:
            con.executemany(ins, [[str(f[c]) if f[c] is not None else None for c in COLUMNS]
                                  for f in fs])
            n += len(fs)
        if i % 100 == 0:
            con.commit()
            print(f"  [{i}/{len(targets)}] 누적 fact {n:,}")
    con.commit()
    index_db(con)
    stored = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    print(f"\n  문서 {len(targets)}건 → fact {n:,}건 (버림 {drop:,}) → {DB}")
    # fact_id가 PRIMARY KEY라 좌표가 겹치면 INSERT OR REPLACE가 조용히 덮어쓴다.
    # 넣은 수와 남은 수를 대조해야 그 손실이 보인다.
    if stored != n:
        print(f"  ⚠ 좌표 충돌 {n - stored:,}건 — 저장 {stored:,}건")
    else:
        print(f"  좌표 충돌 없음 (저장 {stored:,}건)")
    con.close()
    return n


def validate(con=None):
    """4단계 — 누적 단조성 검사.

    같은 기업·같은 연결범위·같은 사업연도의 매출은 3→6→9→12개월로 갈수록 커져야 한다.
    작아진다면 3개월 열을 누적으로 잘못 붙였다는 뜻이다. 실제로 2단 헤더를 놓쳤을 때
    이 검사가 168건을 잡아냈다.

    value_decimal은 표에 찍힌 그대로라 scale을 곱해야 원 단위가 된다. 사업보고서는
    백만원, 분기보고서는 원으로 적는 경우가 많아 곱하지 않으면 연간이 9개월보다
    작아 보인다.
    """
    con = con or open_db()
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT corp_name, scope, base_year, period_months, value_decimal, scale "
        "FROM facts WHERE metric_key='revenue' AND statement='income_statement' "
        "  AND is_superseded='0' AND cumulative IN ('1','') "
        "  AND period_months NOT IN ('None','')").fetchall()
    by = {}
    for r in rows:
        k = (r["corp_name"], r["scope"], r["base_year"])
        v = float(r["value_decimal"]) * float(r["scale"] or 1)
        by.setdefault(k, {})[int(r["period_months"])] = v

    ok = bad = few = 0
    worst = []
    for k, d in by.items():
        ms = sorted(d)
        if len(ms) < 2:
            few += 1
            continue
        seq = [d[m] for m in ms]
        if all(seq[i] <= seq[i + 1] * 1.005 for i in range(len(seq) - 1)):
            ok += 1
        else:
            bad += 1
            if len(worst) < 5:
                worst.append((k, {m: f"{d[m]:,.0f}" for m in ms}))

    print("=== 4단계 검증 — 누적 단조성 ===")
    total = ok + bad
    print(f"  대상 {total}조합 · 단조 {ok} · 위반 {bad} · 기간 1종뿐 {few}")
    print(f"  위반율 {100 * bad / max(1, total):.1f}%")
    for k, d in worst:
        print(f"    {k} {d}")
    print("  → 통과" if bad == 0 else "  → 실패")
    return bad == 0


def main():
    ap = argparse.ArgumentParser(description="분기·반기 포함 fact 빌더")
    ap.add_argument("--probe", action="store_true", help="표본 확인만")
    ap.add_argument("--annual-only", action="store_true", help="사업보고서 회귀 검증")
    ap.add_argument("--build", action="store_true", help="전체 빌드")
    ap.add_argument("--validate", action="store_true", help="누적 단조성 검증")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    if a.annual_only:
        print("=== 1단계 회귀 검증 — 기존 파서와 대조 ===")
        ok = regression_annual()
        print("  →", "통과" if ok else "차이 있음 — 확장이 기존을 건드렸다")
        return 0 if ok else 1
    if a.probe:
        print("=== 분기·반기 표본 ===")
        probe()
        return 0
    if a.validate:
        raise SystemExit(0 if validate() else 1)
    if a.build:
        build(limit=a.limit)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
