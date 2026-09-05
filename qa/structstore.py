"""비-XBRL 구조화 fact 저장소 — L2 필드 온톨로지가 가리키는 실제 값.

`factx.jsonl`은 184만 줄(929MB)이고 온톨로지에 든 387개 필드만 해도 50만 건이다.
전부 메모리에 올리지 않는다. 필드별로 묶어 정렬한 캐시를 만들고 바이트 오프셋 색인을
따로 두어, 질문이 지목한 필드의 레코드만 읽는다.

회차 선택 규칙은 기존 `numqa.answer_struct`의 것을 따른다 — 최신을 임의로 고르지 않고,
질문의 날짜·연도 단서로 좁히며, 못 좁히면 회차를 나열해 되묻는다.
"""

import collections
import json
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
FACTX = _HERE.parent / "code_chunkingandparsing" / "out" / "factx.jsonl"
DATA = _HERE.parent / "data"
CACHE = DATA / "struct_facts.jsonl"
OFFSETS = DATA / "struct_offsets.json"

KEEP = ("fact_id", "corp_code", "corp_name", "rcept_no", "doc_group", "field_key",
        "field_label", "value_raw", "value_decimal", "kind", "is_superseded",
        "supersede_method", "base_year")

_IDX_SUFFIX = re.compile(r"#\d+$")


def _wanted(ontology):
    return {tuple(k.split("\t")) for k in ontology}


def build(ontology):
    """온톨로지에 든 필드의 레코드만 골라 필드별로 묶어 저장하고 오프셋을 남긴다."""
    want = _wanted(ontology)
    buckets = collections.defaultdict(list)
    with FACTX.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            g, k = d.get("doc_group"), d.get("field_key")
            if not g or not k:
                continue
            fk = _IDX_SUFFIX.sub("", str(k))
            if (g, fk) not in want:
                continue
            rec = {x: d.get(x) for x in KEEP}
            rec["field_key"] = fk
            buckets[(g, fk)].append(rec)

    DATA.mkdir(parents=True, exist_ok=True)
    offsets = {}
    with CACHE.open("wb") as out:
        pos = 0
        for (g, fk), recs in buckets.items():
            blob = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs).encode("utf-8")
            out.write(blob)
            offsets[f"{g}\t{fk}"] = [pos, len(blob), len(recs)]
            pos += len(blob)
    OFFSETS.write_text(json.dumps(offsets, ensure_ascii=False), encoding="utf-8")
    return offsets


class StructFacts:
    def __init__(self, offsets, manifest):
        self.offsets = offsets
        self.report_nm = {e["rcept_no"]: e.get("report_nm", "") for e in manifest}
        self.rcept_dt = {e["rcept_no"]: e.get("rcept_dt", "") for e in manifest}
        self.corp_code = {}
        for e in manifest:
            self.corp_code.setdefault(e.get("corp_name"), e.get("corp_code"))
        self._cache = {}

    def records(self, group, field_key):
        key = f"{group}\t{field_key}"
        if key in self._cache:
            return self._cache[key]
        spec = self.offsets.get(key)
        if not spec:
            return []
        start, length, _ = spec
        with CACHE.open("rb") as f:
            f.seek(start)
            blob = f.read(length)
        recs = [json.loads(l) for l in blob.decode("utf-8").splitlines() if l]
        self._cache[key] = recs
        return recs

    def lookup(self, corp_code, group, field_key):
        return [r for r in self.records(group, field_key)
                if r["corp_code"] == corp_code and not r.get("is_superseded")]


_STORE = None


def get(ontology=None, rebuild=False):
    global _STORE
    if _STORE is not None and not rebuild:
        return _STORE
    import sys
    src = _HERE.parent / "code_chunkingandparsing" / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    import load                                          # noqa: PLC0415

    if OFFSETS.exists() and not rebuild:
        offsets = json.loads(OFFSETS.read_text(encoding="utf-8"))
    else:
        from . import fields                             # noqa: PLC0415
        offsets = build(ontology or fields.load_ontology())
    _STORE = StructFacts(offsets, load.load_manifest())
    return _STORE


# ---------------------------------------------------------------------------
# 회차 선택 + 문장화
# ---------------------------------------------------------------------------
_QDATE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")


def _question_date(q):
    m = _QDATE.search(q)
    return f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}" if m else None


def select(question, cands, store, year=None, rcept_no=None):
    """질문 단서로 회차를 좁힌다. 최신을 임의로 고르지 않는다.

    단서의 우선순위는 접수번호 > 날짜 > 연도다. 질문이 접수번호를 직접 대면 그
    한 건이 답이고 되물을 이유가 없다. 예전에는 이 단서를 안 봐서,
    "대량보유상황보고서(rcept_no 20250709000399)의 보고자는?"에
    **번호를 손에 쥐고도** "회차가 여러 건이라 특정이 필요합니다"라고 답했다.
    """
    by_rcept = {}
    for f in cands:                       # 같은 필링 안 중복은 마지막(실효값)
        by_rcept[f["rcept_no"]] = f
    cands = list(by_rcept.values())

    if rcept_no:
        hit = [f for f in cands if f["rcept_no"] == rcept_no]
        if hit:
            return hit                    # 번호가 맞으면 그것이 답이다

    qd = _question_date(question)
    sel = []
    if qd:
        sel = ([f for f in cands if store.rcept_dt.get(f["rcept_no"], "") == qd]
               or [f for f in cands if store.rcept_dt.get(f["rcept_no"], "")[:6] == qd[:6]])
    if not sel and year:
        sel = [f for f in cands if store.rcept_dt.get(f["rcept_no"], "")[:4] == str(year)]
    return sel or cands
