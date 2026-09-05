"""공시 단위 접근 — 한 접수번호가 담은 필드 전부, 그리고 정정 체인.

## 왜 필요한가

`structstore`는 `(group, field_key)`로 색인돼 있다. "이 필드를 가진 문서들"은 빠르지만
**"이 문서가 가진 필드들"**은 못 본다. 그런데 정답셋이 묻는 것은 후자 쪽이다.

    "자기주식취득결정 공시의 1일 매수 주문수량 한도는 정정 공시 전후로 몇 % 달라졌는가"

두 공시를 통째로 놓고 **달라진 칸을 찾는** 문제다.

## 문서 대조가 필드 매칭 문제를 우회한다

`보고자`가 9개 키에, `보통주식`이 58개 키에 붙어 있어 질문에서 필드를 집는 것이
어렵다. 그런데 정정 전후를 대조해 보면 **110개 필드 중 1~2개만 달라진다.**

    20250218000019 → 20250218001596
       BUY_OSTK_LMT   5,186,828 → 5,246,563      (달라진 유일한 칸)

후보가 1~2개로 줄면 어느 칸인지 고르는 일이 거의 사라진다. 값이 안 바뀐 108개 칸은
애초에 답이 될 수 없기 때문이다.

## 정정 체인

공시는 `[기재정정]` 접두어로 정정본을 표시한다. 같은 기업·같은 서식·가까운 시점을
묶으면 최초본과 정정본이 한 줄로 이어진다.

    20241115000375  주요사항보고서(자기주식취득결정)          ← 최초
    20241118000171  [기재정정]주요사항보고서(자기주식취득결정)
    20241118000328  [기재정정]주요사항보고서(자기주식취득결정)  ← 최종
"""

import json
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
SRC = _HERE.parent / "data" / "struct_facts.jsonl"
CACHE = _HERE.parent / "data" / "struct_by_rcept.jsonl"
IDX = _HERE.parent / "data" / "struct_by_rcept_idx.json"

# 서식 이름 앞에 붙는 상태 표기. `[기재정정]` 말고도 `[첨부추가]` 등이 있다.
# 정정만 지우면 "[첨부추가]분기보고서"가 별개 서식으로 남는다 — 접두 종류를
# 열거하지 말고 **대괄호 블록 전체**를 벗긴다.
_BRACKET = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")
_CORR = re.compile(r"^\s*(?:\[[^\]]*\]\s*)*\[[^\]]*정정[^\]]*\]")



def base_name(report_nm):
    return _BRACKET.sub("", report_nm or "").strip()


def is_correction(report_nm):
    return "정정" in (re.match(_BRACKET, report_nm or "") or _Empty()).group(0) \
        if re.match(_BRACKET, report_nm or "") else False


class _Empty:
    @staticmethod
    def group(_):
        return ""


def build():
    """접수번호별로 필드를 모아 한 줄씩 쓴다. 접수번호는 4천 건대라 가볍다."""
    by = {}
    with SRC.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rn = r.get("rcept_no")
            if not rn:
                continue
            d = by.setdefault(rn, {})
            # 같은 문서 안에서 같은 키가 여러 번 나오면 마지막이 실효값이다
            d[r["field_key"]] = [r.get("field_label"), r.get("value_raw"),
                                 r.get("value_decimal"), r.get("kind"),
                                 r.get("doc_group")]
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    idx = {}
    with CACHE.open("w", encoding="utf-8") as f:
        for rn, d in by.items():
            start = f.tell()
            f.write(json.dumps({"rcept_no": rn, "f": d}, ensure_ascii=False) + "\n")
            idx[rn] = [start, f.tell() - start]
    json.dump(idx, IDX.open("w", encoding="utf-8"))
    print(f"  공시 {len(by):,}건 → {CACHE}")
    return len(by)


class Filings:
    def __init__(self, idx, meta):
        self.idx = idx
        self.meta = meta                      # rcept_no → {corp_name, report_nm, rcept_dt}
        self._cache = {}

    def fields(self, rcept_no):
        """그 공시가 담은 항목 전부. {키: {label, raw, dec, kind}}

        구조화 공시(주요사항·대량보유·주식교환)는 field_key로, 정기보고서는 XBRL
        재무제표 행으로 읽는다. `struct_facts`에는 정기보고서가 없어서, 이 갈래가
        없으면 사업보고서 정정 비교가 전부 "달라진 항목 없음"으로 나온다.
        """
        if rcept_no in self._cache:
            return self._cache[rcept_no]
        spec = self.idx.get(rcept_no)
        if not spec:
            out = self._xbrl_fields(rcept_no)
            self._cache[rcept_no] = out
            return out
        start, length = spec
        with CACHE.open("rb") as f:
            f.seek(start)
            row = json.loads(f.read(length).decode("utf-8"))
        out = {k: {"label": v[0], "raw": v[1], "dec": v[2], "kind": v[3], "group": v[4]}
               for k, v in row["f"].items()}
        self._cache[rcept_no] = out
        return out

    def _xbrl_fields(self, rcept_no):
        """정기보고서 — 재무제표 행을 항목으로 삼는다.

        키는 (계정코드, 행 라벨, 기수, 연결범위)다. 정정본에서 같은 행이 재작성되면
        같은 키에서 값이 달라진다.
        """
        import sqlite3
        db = _HERE.parent / "data" / "facts.db"
        if not db.exists():
            return {}
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        out = {}
        for r in con.execute(
                "SELECT aclass_xbrl_code, label_norm, fiscal_term, scope, value_raw,"
                " value_decimal, unit_kr FROM facts WHERE rcept_no=?", (rcept_no,)):
            k = f"{r['aclass_xbrl_code']}|{r['label_norm']}|{r['fiscal_term']}|{r['scope']}"
            out[k] = {"label": f"{r['label_norm']} (제{r['fiscal_term']}기 "
                               f"{'연결' if r['scope'] == 'consolidated' else '별도'})",
                      "raw": r["value_raw"], "dec": r["value_decimal"],
                      "kind": "numeric", "group": "periodic"}
        con.close()
        return out

    def chain(self, rcept_no):
        """같은 사건의 공시들을 시간순으로. 최초본이 앞, 최종 정정본이 뒤.

        ## 시간 창을 쓰지 않는다

        처음엔 "45일 이내"로 묶었다. 임의의 숫자였고 실제로 틀렸다 — OCI홀딩스
        유상증자결정의 3번째 정정이 56일 뒤라 잘려 나가, 2차례 정정을 1차례로 셌다.

        공시의 구조가 답을 준다. **원본이 새 사건을 열고, 뒤따르는 [기재정정]들은
        다음 원본이 나올 때까지 그 사건에 속한다.**

            20230727 원본        ← 사건 A 시작
            20230829 [기재정정]  ← 사건 A
            20230921 [기재정정]  ← 사건 A   (56일 뒤여도 사이에 원본이 없다)
            20240112 원본        ← 사건 B 시작
            20240408 [기재정정]  ← 사건 B

        날짜 간격이 아니라 **원본의 등장**이 경계다. 마법의 숫자가 사라진다.
        """
        m = self.meta.get(rcept_no)
        if not m:
            return [rcept_no]
        corp, base = m.get("corp_name"), base_name(m.get("report_nm"))
        same = sorted((rn for rn, v in self.meta.items()
                       if v.get("corp_name") == corp
                       and base_name(v.get("report_nm")) == base),
                      key=lambda r: ((self.meta[r].get("rcept_dt") or ""), r))
        groups, cur = [], []
        for rn in same:
            if not is_correction(self.meta[rn].get("report_nm")):
                if cur:
                    groups.append(cur)
                cur = [rn]                      # 원본이 새 사건을 연다
            else:
                cur.append(rn) if cur else groups.append([rn])
        if cur:
            groups.append(cur)
        for gset in groups:
            if rcept_no in gset:
                return gset
        return [rcept_no]

    def find_events(self, corp, event_type, year=None):
        """이 기업의 이 이벤트 유형(정규화된 report_nm 기준) 발생 목록.

        각 발생("사건")은 원본+뒤따르는 정정본을 chain()과 같은 규칙(원본의
        등장이 사건 경계)으로 하나로 묶는다 — 같은 유형이라도 서로 다른 날짜에
        여러 번 결정될 수 있어(예: 유상증자결정을 두 차례 별도로 결정), 각각을
        독립된 앵커로 남긴다.

        반환: [{"corp","rcept_no"(최종 정정본),"rcept_dt"(최초 접수일=결정일로
        본다),"report_nm"(최종본 표시명),"is_correction"(정정 이력 있음),
        "chain":[rcept_no,...]}], 최초 접수일 내림차순(최신 사건 먼저).

        event_type은 qa/events_vocab.py::normalize()가 만든 정규화 이름이다 —
        이 메서드는 어휘 자체를 모르고, corp+event_type 조합으로 문서만 고른다.
        """
        from . import events_vocab
        docs = sorted(
            (rn for rn, m in self.meta.items()
             if m.get("corp_name") == corp and events_vocab.normalize(m.get("report_nm")) == event_type),
            key=lambda r: (self.meta[r].get("rcept_dt") or "", r))
        groups, cur = [], []
        for rn in docs:
            if not is_correction(self.meta[rn].get("report_nm")):
                if cur:
                    groups.append(cur)
                cur = [rn]
            else:
                cur.append(rn) if cur else groups.append([rn])
        if cur:
            groups.append(cur)
        out = []
        for g in groups:
            first_dt = self.meta[g[0]].get("rcept_dt") or ""
            if year and not first_dt.startswith(str(year)):
                continue
            final = g[-1]
            out.append({
                "corp": corp, "rcept_no": final, "rcept_dt": first_dt,
                "report_nm": self.meta[final].get("report_nm"),
                "is_correction": len(g) > 1, "chain": g,
            })
        out.sort(key=lambda a: a["rcept_dt"], reverse=True)
        return out

    def diff(self, a, b):
        """두 공시에서 값이 달라진 칸만. 후보를 좁히는 것이 목적이다."""
        A, B = self.fields(a), self.fields(b)
        out = []
        for k in sorted(set(A) | set(B)):
            x, y = A.get(k), B.get(k)
            if str((x or {}).get("raw")) != str((y or {}).get("raw")):
                out.append({"key": k, "label": (x or y or {}).get("label"),
                            "before": (x or {}).get("raw"), "after": (y or {}).get("raw"),
                            "before_dec": (x or {}).get("dec"), "after_dec": (y or {}).get("dec"),
                            "kind": (x or y or {}).get("kind")})
        return out


def _daynum(s):
    try:
        return int(s[:4]) * 372 + int(s[4:6]) * 31 + int(s[6:8])
    except (ValueError, TypeError):
        return 0


_STORE = None


def get(rebuild=False):
    global _STORE
    if _STORE is None or rebuild:
        if rebuild or not IDX.exists():
            build()
        from . import rcept
        _STORE = Filings(json.load(IDX.open(encoding="utf-8")), rcept.load())
    return _STORE
