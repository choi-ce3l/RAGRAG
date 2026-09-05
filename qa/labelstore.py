"""label 기반 XBRL fact 인덱스 — 01 온톨로지 매핑의 지표 어휘를 넓히기 위한 것.

`numqa.FactStore`는 `metric_key`가 붙은 8개 지표(매출액·영업이익·순이익·자산총계·
부채총계·자본총계·부채비율·유동비율)만 싣는다. 그런데 실제 질문은 투자부동산·비유동부채·
재무활동현금흐름·자본잉여금·금융비용처럼 훨씬 넓은 어휘를 쓴다.

factstore.jsonl 전체(184만 줄, 1.1GB)에서 XBRL 재무제표 fact 21만 건만 골라
`label_norm` 기준으로 색인한다. 고유 label 약 4,900개. 원본 스캔이 10초쯤 걸리므로
필요한 필드만 추린 압축본을 캐시해 둔다.
"""

import json
import sqlite3
from pathlib import Path

from .concepts import canonical


def _better(new, old):
    """어느 fact를 근거로 삼을 것인가.

    ① 후속 공시로 대체된 것(is_superseded)은 피한다 — 폐기된 공시를 근거로 대면 안 된다.
    ② 둘 다 유효하면 최신 보고서를 쓴다. 재작성이 있었다면 최신 값이 회사의 최종 입장이고,
       없었다면 값이 같으므로 손해가 없다.

    이 선택을 안 하면(첫 번째 것을 그냥 쓰면) 조합의 43%에서 대체된 공시를 근거로 대게 된다 —
    파일에 오래된 것이 먼저 실려 있기 때문이다.
    """
    if bool(new.get("is_superseded")) != bool(old.get("is_superseded")):
        return not new.get("is_superseded")
    return str(new.get("rcept_no") or "") > str(old.get("rcept_no") or "")

_HERE = Path(__file__).resolve().parent
SOURCE = _HERE.parent / "code_chunkingandparsing" / "out" / "factstore.jsonl"
CACHE = _HERE.parent / "data" / "labelstore.jsonl"
# 분기·반기까지 담은 fact DB. 없으면 예전 JSONL 캐시로 돌아간다.
DB = _HERE.parent / "data" / "facts.db"

# DB는 모든 열이 TEXT다. 예전 JSONL과 타입이 어긋나면 조용히 틀린다 —
# 특히 is_superseded는 '0'이 문자열이라 bool('0') == True가 되어버린다.
_INT = ("base_year", "scale", "row_index", "col_index", "fiscal_term", "period_months")

# 연간 재무제표의 기간 표기. 손익·현금흐름은 '연간', 재무상태표는 시점 값이라 '시점'.
ANNUAL = ("연간", "시점")


def _cast(d):
    for k in _INT:
        v = d.get(k)
        d[k] = int(v) if v not in (None, "", "None") else None
    d["is_superseded"] = str(d.get("is_superseded") or "0") not in ("0", "", "False")
    return d

KEEP = ("fact_id", "corp_code", "corp_name", "rcept_no", "doc_id", "label_norm",
        "label_raw", "scope", "statement", "base_year", "value_decimal", "value_raw",
        "unit", "unit_kr", "scale", "is_superseded", "row_index", "col_index",
        "aclass_xbrl_code", "metric_key", "fiscal_term",
        "period_label", "period_months", "cumulative", "report_nm",
        "period_start", "period_end")

_STORE = None


def _iter_db():
    """사업보고서 fact만 읽는다.

    분기·반기까지 넣으면 .facts를 코퍼스 전역 분석에 쓰는 쪽(concepts·applicability·
    hierarchy·derived·kg)이 통째로 흔들린다. 그 분석들은 연간 기준으로 유도·검증한
    것이라 모집단을 바꾸면 안 된다. 분기 값은 lookup()에서 DB를 직접 친다.
    """
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    for r in con.execute("SELECT * FROM facts WHERE report_nm LIKE '%사업보고서%'"):
        yield _cast({k: r[k] for k in KEEP})
    con.close()


def _iter_source():
    with SOURCE.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            # XBRL 재무제표 fact만. statement가 없는 레코드는 비-XBRL 구조화 fact다.
            if d.get("store_kind") in (None, "xbrl_fact") and d.get("statement"):
                yield {k: d.get(k) for k in KEEP}


def _build():
    recs = list(_iter_source())
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE.open("w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return recs


def _load_records(rebuild=False):
    if DB.exists():
        return list(_iter_db())
    if CACHE.exists() and not rebuild:
        try:
            return [json.loads(l) for l in CACHE.open(encoding="utf-8")]
        except json.JSONDecodeError:
            pass                        # 캐시가 깨졌으면 원본에서 다시 만든다
    if not SOURCE.exists():
        return []
    return _build()


class LabelStore:
    """(corp_code, label, scope, base_year) → fact 조회 + label 어휘."""

    def __init__(self, recs):
        self.facts = recs
        self._con = None
        self._canon_surfaces = {}       # 정규형 → 그 정규형을 갖는 원표기들
        self.idx = {}                   # statement 포함 (정확 조회)
        self.idx_any = {}               # statement 미지정 (질문에 힌트 없을 때)
        self.vocab_count = {}
        # (기업, 정규개념, 기준) → 실제로 값이 있는 연도들.
        # "가장 최근"을 전역 상수(2025)로 고정하면 안 되기 때문에 필요하다 —
        # 조합의 23.8%는 최신이 2025가 아니다.
        self.years_of = {}
        # (기업, 기수) → 연도. 제57기 같은 표현을 푸는 데 쓴다.
        self.term_year = {}
        for f in recs:
            label = f.get("label_norm") or f.get("label_raw")
            if not label:
                continue
            self.vocab_count[label] = self.vocab_count.get(label, 0) + 1
            # 정규형으로도 색인한다. 기업마다 "8.유형자산의 취득"처럼 번호를 붙여
            # 표기가 갈리는데, 원표기로만 색인하면 그 기업이 조회에서 통째로 빠진다.
            canon = canonical(label)
            self._canon_surfaces.setdefault(canon, set()).add(label)
            for lab in {label, canon}:
                key = (f["corp_code"], lab, f["scope"], f["base_year"])
                for table, k in ((self.idx, key + (f["statement"],)), (self.idx_any, key)):
                    old = table.get(k)
                    if old is None or _better(f, old):
                        table[k] = f
                self.years_of.setdefault((f["corp_code"], lab, f["scope"]), set()).add(f["base_year"])
            ft = f.get("fiscal_term")
            if ft is not None:
                self.term_year.setdefault((f["corp_code"], ft), f["base_year"])
        # 긴 것부터 매칭해야 '비유동부채'가 '유동부채'로 잘못 잡히지 않는다.
        self.vocab = sorted(self.vocab_count, key=len, reverse=True)

    def lookup(self, corp_code, label, scope, year, statement=None, period=None):
        """연간 값을 돌려준다. period를 주면 그 분기·반기 값을 DB에서 찾는다.

        period 없이 부르는 경로는 예전과 완전히 같은 인메모리 인덱스를 탄다.
        분기를 묻지 않은 질문의 답이 분기 값으로 바뀌는 일은 없다.
        """
        if period:
            return self._lookup_period(corp_code, label, scope, year, period, statement)

        if statement:
            hit = self.idx.get((corp_code, label, scope, year, statement))
            if hit:
                return hit
        return self.idx_any.get((corp_code, label, scope, year))

    def _labels_for(self, label):
        """요청 라벨 + 그 라벨을 정규형으로 갖는 원표기들."""
        return sorted(self._canon_surfaces.get(label, set()) | {label})

    def _query(self, corp_code, label, scope, year, spec, statement=None, metric=None):
        if metric:
            sql = ("SELECT * FROM facts WHERE corp_code=? AND scope=? AND base_year=?"
                   " AND metric_key=?")
            args = [corp_code, scope, str(year), metric]
        else:
            labs = self._labels_for(label)
            sql = ("SELECT * FROM facts WHERE corp_code=? AND scope=? AND base_year=?"
                   f" AND label_norm IN ({','.join('?' * len(labs))})")
            args = [corp_code, scope, str(year), *labs]
        sql += " AND period_label=?"
        args.append(spec["label"])
        # 1·2·3분기는 모두 period_label이 '3개월'이다. 종료월이 유일한 판별자다.
        if spec.get("end_month"):
            sql += " AND period_end LIKE ?"
            args.append(f"%-{spec['end_month']:02d}-%")
        if statement:
            sql += " AND statement=?"
            args.append(statement)
        # _better()와 같은 순서가 아니라 lookup_metric_annual()과 같은 순서를 쓴다 —
        # 정정되지 않은 것 우선, 그 다음 "당해 보고서"(그 base_year를 자기 문서로 갖는
        # report_nm) 우선, 그 다음 최신 접수번호. rcept_no DESC만 쓰면 나중에 나온
        # 보고서의 비교표시 열(같은 base_year·period_label이지만 다른 report_nm)이
        # 당해 원본 보고서보다 먼저 뽑혀 근거가 어긋난다(예: 2025.03 분기보고서를
        # 물었는데 2026.03 분기보고서의 비교열을 인용).
        sql += (" ORDER BY is_superseded ASC,"
                " CASE WHEN report_nm LIKE '%(' || base_year || '.%' THEN 0 ELSE 1 END,"
                " rcept_no DESC")
        return sql, args

    def _lookup_period(self, corp_code, label, scope, year, spec, statement=None,
                       metric=None):
        con = self._db()
        if con is None:
            return None
        if isinstance(spec, str):
            spec = {"label": spec}
        sql, args = self._query(corp_code, label, scope, year, spec, statement, metric)
        row = con.execute(sql + " LIMIT 1", args).fetchone()
        if row is None and statement:            # 표 종류 힌트가 틀렸을 수 있다
            sql, args = self._query(corp_code, label, scope, year, spec, None, metric)
            row = con.execute(sql + " LIMIT 1", args).fetchone()
        if row is None and spec.get("report"):
            row = self._lookup_stock_by_report(corp_code, label, scope, year, spec,
                                               statement, metric)
        return _cast({k: row[k] for k in KEEP}) if row else None

    def _lookup_stock_by_report(self, corp_code, label, scope, year, spec,
                                statement, metric):
        """재무상태표 항목(자산총계 등)의 분기·반기 값 — period_label이 '시점'
        하나뿐이라 '3개월'·'반기누적' 같은 흐름 구분 필터로는 못 찾는다(부채비율
        같은 파생비율을 분기·반기 기준으로 계산하려 해도 계속 실패했다 — GOLD-
        W1-DGN-02). 대신 spec['report'](예: '반기보고서')로 그 시점의 보고서
        자체를 골라, period_label='시점'인 값만 받는다 — 매출액처럼 실제로
        '3개월'/'누적'이 갈리는 흐름 항목은 이 함수에 안 들어온다(정상 경로에서
        이미 값을 찾아 여기까지 안 옴).
        """
        con = self._db()
        report, end_month = spec.get("report"), spec.get("end_month")
        if not report or not end_month:
            return None
        if metric:
            sql = ("SELECT * FROM facts WHERE corp_code=? AND scope=? AND base_year=?"
                   " AND metric_key=?")
            args = [corp_code, scope, str(year), metric]
        else:
            labs = self._labels_for(label)
            sql = ("SELECT * FROM facts WHERE corp_code=? AND scope=? AND base_year=?"
                   f" AND label_norm IN ({','.join('?' * len(labs))})")
            args = [corp_code, scope, str(year), *labs]
        sql += " AND report_nm LIKE ? AND report_nm LIKE ? AND period_label='시점'"
        args += [f"%{report}%", f"%({year}.{end_month:02d})%"]
        if statement:
            sql += " AND statement=?"
            args.append(statement)
        sql += " ORDER BY is_superseded ASC, rcept_no DESC LIMIT 1"
        return con.execute(sql, args).fetchone()

    def lookup_metric(self, corp_code, metric, scope, year, period, statement=None):
        """metric_key로 바로 친다. numqa FactStore는 연간만 알기 때문에 필요하다."""
        return self._lookup_period(corp_code, None, scope, year, period, statement,
                                   metric=metric)

    def periods(self, corp_code, label, scope, year):
        """이 조합이 실제로 가진 기간 표기들. 되물음·안내에 쓴다."""
        con = self._db()
        if con is None:
            return []
        labs = self._labels_for(label)
        rows = con.execute(
            "SELECT DISTINCT period_label FROM facts WHERE corp_code=? AND scope=?"
            f" AND base_year=? AND label_norm IN ({','.join('?' * len(labs))})"
            " AND is_superseded='0'",
            [corp_code, scope, str(year), *labs]).fetchall()
        return sorted({r["period_label"] for r in rows if r["period_label"]})

    def lookup_doc(self, rcept_no, label, scope, year=None, term=None,
                   statement=None, metric=None, period=None):
        """지정한 보고서 **안에서** 읽는다.

        한 문서에는 여러 기수 열이 함께 실린다(제19기 사업보고서 → 제17·18·19기).
        "그 보고서에 비교표시된 제17기 값"은 최신 보고서의 2023년 값과 다를 수 있다
        — 재작성·정정이 있으면 그렇다. 문서를 고정하면 인쇄된 그 열을 집는다.
        """
        con = self._db()
        if con is None:
            return None
        if metric:
            sql = "SELECT * FROM facts WHERE rcept_no=? AND scope=? AND metric_key=?"
            args = [rcept_no, scope, metric]
        else:
            labs = self._labels_for(label)
            sql = ("SELECT * FROM facts WHERE rcept_no=? AND scope=?"
                   f" AND label_norm IN ({','.join('?' * len(labs))})")
            args = [rcept_no, scope, *labs]
        if term is not None:
            sql += " AND CAST(fiscal_term AS INT)=?"
            args.append(int(term))
        elif year is not None:
            sql += " AND base_year=?"
            args.append(str(year))
        base_sql, base_args = sql, list(args)
        if period:
            sql += " AND period_label=?"
            args.append(period["label"] if isinstance(period, dict) else period)
            if isinstance(period, dict) and period.get("end_month"):
                sql += " AND period_end LIKE ?"
                args.append(f"%-{period['end_month']:02d}-%")
        if statement:
            sql += " AND statement=?"
            args.append(statement)
        row = con.execute(sql + " ORDER BY row_index LIMIT 1", args).fetchone()
        if row is None and period:
            # 재무상태표 항목(자산총계 등)은 "시점" 값 하나뿐이라 period_label이
            # '반기누적'·'3개월' 같은 흐름 구분과 안 맞는다 — 분기·반기 재무비율을
            # 부채총계÷자산총계처럼 계산하려 해도 문서를 지정해도 못 찾았다(GOLD-
            # W1-DGN-02). 문서 하나로 이미 시점이 고정됐으니, period_label이 '시점'
            # 하나뿐인 경우에만(=흐름이 아닌 재무상태표 항목일 때만) 필터 없이 재시도한다
            # — 매출액처럼 '3개월'/'누적'이 실제로 갈리는 흐름 항목은 잘못 집을
            # 위험이 있어 그대로 실패시킨다.
            probe_sql = base_sql + (" AND statement=?" if statement else "")
            probe_args = base_args + ([statement] if statement else [])
            variants = con.execute(
                f"SELECT DISTINCT period_label FROM ({probe_sql})", probe_args).fetchall()
            labels_seen = {v["period_label"] for v in variants}
            if labels_seen == {"시점"}:
                row = con.execute(probe_sql + " ORDER BY row_index LIMIT 1",
                                  probe_args).fetchone()
        if row is None and statement:
            return self.lookup_doc(rcept_no, label, scope, year, term, None,
                                   metric, period)
        return _cast({k: row[k] for k in KEEP}) if row else None

    def doc_terms(self, rcept_no):
        """그 문서가 담고 있는 기수들. 열을 지정하지 않았을 때 자기 기수를 쓴다."""
        con = self._db()
        if con is None:
            return []
        rows = con.execute("SELECT DISTINCT fiscal_term FROM facts WHERE rcept_no=?",
                           (rcept_no,)).fetchall()
        return sorted({int(r["fiscal_term"]) for r in rows if r["fiscal_term"]})

    def lookup_metric_annual(self, corp_code, metric, scope, year, statement=None,
                             prefer_latest=False):
        """연간 metric 조회 — 근거 선택 정책을 label 경로와 똑같이 적용한다.

        numqa의 FactStore도 같은 fact를 갖고 있지만 고르는 규칙이 다르다. 그래서
        한화솔루션 2023년 자산총계를 물으면 두 경로가 다른 답을 냈다.

            label 경로  24,790,424 백만원  ([기재정정]사업보고서 2025.12 · 재작성치)
            metric 경로 24,492,909,473,815 원 (사업보고서 2023.12 · 최초치)

        재작성이 있었으면 **최신 유효본의 값**이 맞다. 한 시스템 안에서 같은 질문에
        두 답이 나오면 안 되므로, 정책을 한쪽으로 모은다.
        """
        con = self._db()
        if con is None:
            return None
        sql = ("SELECT * FROM facts WHERE corp_code=? AND scope=? AND base_year=?"
               " AND metric_key=?"
               f" AND period_label IN ({','.join('?' * len(ANNUAL))})")
        args = [corp_code, scope, str(year), metric, *ANNUAL]
        if statement:
            sql += " AND statement=?"
            args.append(statement)
        sql += " AND report_nm LIKE '%사업보고서%'"
        # 그 해의 값은 **그 해 사업보고서**가 1차 출처다.
        #
        # 이후 보고서의 비교표시 열은 재작성될 수 있고, 그 값은 "재작성치"이지
        # 그 해에 확정 보고된 값이 아니다. 둘 다 옳은 답이라 질문이 어느 쪽을
        # 원하는지가 정한다 — 기본은 당해 보고서, 최신을 원하면 prefer_latest.
        #
        #     CJ제일제당 2024 영업이익  당해 1,553,017,638천원 / 재작성 1,452,067,669천원
        #     KB금융  2023 당기순이익   당해 4,563,431백만원   / 재작성 4,526,334백만원
        order = ("is_superseded ASC, rcept_no DESC" if prefer_latest else
                 "is_superseded ASC,"
                 " CASE WHEN report_nm LIKE '%(' || base_year || '.%' THEN 0 ELSE 1 END,"
                 " rcept_no DESC")
        sql += f" ORDER BY {order} LIMIT 1"
        row = con.execute(sql, args).fetchone()
        if row is None and statement:
            return self.lookup_metric_annual(corp_code, metric, scope, year,
                                             prefer_latest=prefer_latest)
        return _cast({k: row[k] for k in KEEP}) if row else None

    def corp_years(self, corp_code):
        """이 기업의 사업보고서 보유 연도. 없는 해를 물었을 때 무엇이 있는지 밝히려는 것이다.

        "2020년 자료가 없습니다"보다 "2023~2025년만 있습니다"가 낫다. 앞의 말로는
        다시 물어야 할지 포기해야 할지 알 수 없다.
        """
        con = self._db()
        if con is None:
            return []
        rows = con.execute(
            "SELECT DISTINCT base_year FROM facts WHERE corp_code=?"
            " AND report_nm LIKE '%사업보고서%'", (corp_code,)).fetchall()
        return sorted({int(r["base_year"]) for r in rows if r["base_year"]})

    def _db(self):
        if self._con is None and DB.exists():
            self._con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True,
                                        check_same_thread=False)
            self._con.row_factory = sqlite3.Row
        return self._con

    def years(self, corp_code, label, scope):
        """이 조합이 실제로 가진 연도들. 없으면 빈 집합."""
        return self.years_of.get((corp_code, label, scope), set())

    def year_of_term(self, corp_code, term):
        """제N기 → 회계연도. corpus에서 유도한 것이라 기업마다 다르다."""
        return self.term_year.get((corp_code, term))

    def term_of_year(self, corp_code, year):
        """회계연도 → 제N기. year_of_term의 역방향 — 순위·시리즈 답에 기수를 붙일 때 쓴다."""
        for (cc, term), y in self.term_year.items():
            if cc == corp_code and y == year:
                return term
        return None

    def match_label(self, question, min_len=3):
        """질문에서 가장 긴 label을 찾는다. 없으면 None."""
        for v in self.vocab:
            if len(v) >= min_len and v in question:
                return v
        return None


def get(rebuild=False):
    global _STORE
    if _STORE is None or rebuild:
        _STORE = LabelStore(_load_records(rebuild))
    return _STORE
